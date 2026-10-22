"""R2 静态字典（Python 侧）：唯一 PC 的静态字段 + C++ builder 可读的紧凑格式。

背景与依据
----------
R2 原生 builder（`etm_storebuild`）不做反汇编/ELF 解析；它按 `prescan`/`readNative` 得到的
唯一范围查询本模块产出的静态字典，直接把 `id` 填进 RUN 记录、把文本/机器码写进 store 的
static/strings 块。语义目标（R2 设计 §3.1）：

* 每个唯一 PC 一份，字段与 `trace_store_write._Assembler._static_entry` **逐项一致**：
  `pc/size/bytes/mnemonic/operands/flow/conditional/function/file/line/predicated/id`；
* `id` = 首次出现顺序（= 参考 store static 块的顺序索引）；
* `function/file/line` 来自 ELF（符号 + DWARF 行表），`mnemonic/operands` 来自首次出现的
  范围上下文反汇编（IT 块内可能带条件后缀），`flow/conditional` 来自该指令字节的单独反汇编，
  `predicated` 由 `predicate_addresses` 判定（镜像外 PC 走 `_InstructionTable` 的 IT 回溯回退）；
* 与参考一样做机器码/源码位置交叉核对（`Image.read` / `Image.location`）。

文件格式（`static-dict.bin` v1）
--------------------------------
R2 设计 §3.2 的 61 B header + 字符串池 + **按 PC 升序的 16 B 条目表**：

    0   9   magic "R2STATIC1"
    9   4   version u32 = 1
    13 32   elfSha256（原样 32 字节）
    45  4   entryCount u32
    49  4   stringCount u32
    53  8   tableOffset u64（= 61 + 字符串池字节数）
    string pool：每项 u32 长度 + UTF-8；0 号是空串
    entry table：16 B/项：pc u32 | size u8 | id u32 | predicated u8 | flow u8 | pad 3
    文本槽：PC 升序第 e 条的 function/file/mnemonic/operands = 池索引 1+4e … 4+4e

设计 §3.2 未写明、但参考 store 的 static 块与 LINE_RUNS/热点需要的 `line/conditional/机器码`，
放在**条目表之后的可选扩展段**（旧读取器读到条目表末尾即可停止，不受影响）：

    ext header 16 B：magic b"R2EXT01\\0" | extVersion u32=1 | count u32 (=entryCount)
    ext 每项 24 B：line i32（-1=None；同时用 flags.bit3 显式标出）| conditional u8 |
                  flags u8（bit0=有机器码；bit1=function 存在；bit2=file 存在；bit3=line 存在）|
                  pad2 | bytes[16]（不足补 0）
    `function/file` 的 None 与空串 '' 在 store 的 static 块里由“存在位”区分（不是由字符串 ID）；
    扩展段的 flags 位把这一区别带给 C++，避免把 '' 误当成 None。

本模块只做 Python 侧准备与格式读写/对账；不修改 store、不写块 12。
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import struct
import sys
import threading
import time
from pathlib import Path

import native_binary
import trace_store
from decode_trace import Image
from flow_analysis import predicate_addresses

STATIC_MAGIC = b'R2STATIC1'
STATIC_VERSION = 1
HEADER_BYTES = 61
ENTRY_BYTES = 16
EXT_MAGIC = b'R2EXT01\x00'
EXT_VERSION = 1
EXT_HEADER_BYTES = 16
EXT_ENTRY_BYTES = 24
MACHINE_CODE_BYTES = 16
RANGE_ELEMENT_TYPE = 5                 # OCSD_GEN_TRC_ELEM_INSTR_RANGE

FIELD_NAMES = ('pc', 'size', 'flow', 'conditional', 'predicated', 'function', 'file', 'line',
               'mnemonic', 'operands', 'bytes', 'id')

_ADDRESS_CACHE = collections.OrderedDict()
_ADDRESS_CACHE_LOCK = threading.RLock()


def _address_preparation(image):
    from trace_store_write import _known_addresses
    started = time.perf_counter()
    enabled = (type(image) is Image and 'read' not in vars(image)
               and os.environ.get('ETM_PREPARE_CACHE', '1').strip().lower() not in ('0', 'false', 'off'))
    # Include the actual segments/function spans: callers may deliberately edit
    # an Image for diagnostics. Never reuse context-sensitive range disassembly.
    key = (hashlib.sha256(image.bytes).digest(),
           tuple((base, hashlib.sha256(data).digest()) for base,data in image.segments),
           tuple(image.functions)) if enabled else None
    with _ADDRESS_CACHE_LOCK:
        cached = _ADDRESS_CACHE.get(key) if enabled else None
        if cached is not None:
            _ADDRESS_CACHE.move_to_end(key)
            return cached[0], cached[1], dict(addressPreparationCacheHit=True,
                addressCacheSeconds=time.perf_counter()-started,
                predicateAddressesSeconds=0.0, knownAddressesSeconds=0.0)
        phase = time.perf_counter()
        predicates = frozenset(predicate_addresses(image))
        predicate_seconds = time.perf_counter()-phase
        phase = time.perf_counter()
        known = frozenset(_known_addresses(image))
        known_seconds = time.perf_counter()-phase
        weight = ((len(predicates)+len(known))*96 + len(image.segments)*128 + 128
                  + sum(192+len(name)*4 for _,_,name in image.functions))
        if enabled and weight <= 16*1024*1024:
            _ADDRESS_CACHE[key] = (predicates, known, weight)
            while len(_ADDRESS_CACHE)>4 or sum(value[2] for value in _ADDRESS_CACHE.values())>16*1024*1024:
                _ADDRESS_CACHE.popitem(last=False)
        return predicates, known, dict(addressPreparationCacheHit=False,
            addressCacheSeconds=time.perf_counter()-started,
            predicateAddressesSeconds=predicate_seconds, knownAddressesSeconds=known_seconds)


class StaticDictError(ValueError):
    """静态字典格式/内容错误（明确拒绝，不返回拼凑结果）。"""


# ---------------------------------------------------------------------------------------------
# 唯一范围（首次出现顺序）
# ---------------------------------------------------------------------------------------------

def collect_ranges(native_path, progress=None):
    """读 native.bin，返回 ([(start,end,payload)], 统计)。保留首次出现顺序、校验 payload 一致。"""
    native = native_binary.NativeFile(native_path)
    order = []
    payloads = {}
    element_count = 0
    range_records = 0
    for record in native.records():
        if record[0] != native_binary.RECORD_ELEMENT:
            continue
        element_count += 1
        if int(record[1]) != RANGE_ELEMENT_TYPE:
            continue
        range_records += 1
        start, end, payload = int(record[4]), int(record[5]), int(record[16])
        key = (start, end)
        previous = payloads.get(key)
        if previous is None:
            payloads[key] = payload
            order.append(key)
        elif previous != payload:
            raise StaticDictError('同一范围出现不同 payload：0x%08x..0x%08x %d≠%d'
                                  % (start, end, previous, payload))
        if progress is not None and range_records % 2000000 == 0:
            progress('  已读 %d 条范围记录（唯一 %d）' % (range_records, len(order)))
    ranges = [(start, end, payloads[(start, end)]) for start, end in order]
    stats = dict(nativeFile=str(native_path), protocol=native.protocol,
                 nativeBytes=Path(native_path).stat().st_size,
                 elementRecords=element_count, rangeRecords=range_records,
                 uniqueRanges=len(ranges))
    return ranges, stats


# ---------------------------------------------------------------------------------------------
# 静态条目（与 _Assembler._static_entry 逐项一致）
# ---------------------------------------------------------------------------------------------

def build_static_entries(ranges, elf_path, *, verify_image=True, progress=None, image=None):
    """按首次出现顺序生成静态条目（与参考 `_Assembler._static_entry` 同口径）。"""
    import decode_stream as expander

    started = time.perf_counter()
    timings = {}
    phase = time.perf_counter()
    image = image or Image(str(elf_path))
    timings['loadImageSeconds'] = round(time.perf_counter() - phase, 4)
    predicates, known, address_costs = _address_preparation(image)
    timings.update(address_costs)
    phase = time.perf_counter()
    table = expander._InstructionTable(image, predicates, known)
    timings['instructionTableSeconds'] = round(time.perf_counter() - phase, 4)

    entries = []
    static_by_pc = {}
    span_cache = {}
    predicate_fallback = 0
    disasm_seconds = 0.0
    info_seconds = 0.0
    for start, end, payload in ranges:
        phase = time.perf_counter()
        selected = expander._range_instructions(start, end, table.decoder, image, span_cache)
        disasm_seconds += time.perf_counter() - phase
        if len(selected) != payload or not selected \
                or selected[-1].address + selected[-1].size != end:
            raise StaticDictError(
                '机器码范围核对失败：0x%08x..0x%08x；OpenCSD=%d，反汇编=%d。'
                % (start, end, payload, len(selected)))
        for instruction in selected:
            pc = int(instruction.address)
            if pc in static_by_pc:
                continue
            phase = time.perf_counter()
            info = table.info(instruction)
            (size, hexed, mnemonic, operands, flow, conditional, function, source, line,
             forced) = info[:10]
            raw = bytes.fromhex(hexed)
            if len(raw) != size or len(raw) != instruction.size:
                raise StaticDictError('指令长度与机器码不符：pc=0x%08x size=%d len=%d'
                                      % (pc, size, len(raw)))
            if verify_image:
                expected = image.read(pc, len(raw)).hex()
                if expected != hexed:
                    raise StaticDictError('机器码与 ELF 不一致：pc=0x%08x 反汇编=%s 镜像=%s'
                                          % (pc, hexed, expected))
                location = image.location(pc)
                if location != (function, source, line):
                    raise StaticDictError('源码位置与 ELF 不一致：pc=0x%08x 反汇编=%r 镜像=%r'
                                          % (pc, (function, source, line), location))
            if flow not in trace_store.FLOW_CODES:
                raise StaticDictError('未知 flow：%r（pc=0x%08x）' % (flow, pc))
            predicated = bool(forced)
            if predicated and pc not in predicates:
                predicate_fallback += 1
            entry = dict(id=len(entries), pc=pc, size=int(size), bytes=raw.hex(),
                         mnemonic=mnemonic, operands=operands, flow=flow,
                         conditional=bool(conditional), function=function, file=source,
                         line=line, predicated=predicated)
            static_by_pc[pc] = entry
            entries.append(entry)
            info_seconds += time.perf_counter() - phase
        if progress is not None and len(entries) and len(entries) % 500 == 0:
            progress('  静态条目：%d（唯一范围已处理）' % len(entries))
    timings['disasmUniqueRangesSeconds'] = round(disasm_seconds, 4)
    timings['staticInfoSeconds'] = round(info_seconds, 4)
    stats = dict(uniquePCs=len(entries), predicateFallback=predicate_fallback,
                 sourceMapped=sum(1 for item in entries if item['line']),
                 functions=len(image.functions),
                 functionBytes=sum(size for _, size, _ in image.functions),
                 predicateCount=len(predicates), knownCount=len(known),
                 timings=timings, buildSeconds=round(time.perf_counter() - started, 3))
    return entries, stats


# ---------------------------------------------------------------------------------------------
# 文件格式：写 / 读
# ---------------------------------------------------------------------------------------------

def write_static_dict(path, entries, elf_sha256):
    """写 static-dict.bin v1（16 B 条目表 + 可选 line/conditional/机器码扩展段）。"""
    if isinstance(elf_sha256, str):
        elf_sha256 = bytes.fromhex(elf_sha256)
    if len(elf_sha256) != 32:
        raise StaticDictError('elfSha256 必须是 32 字节')
    ordered = sorted(entries, key=lambda item: item['pc'])
    # 固定槽位：PC 升序第 e 条的 function/file/mnemonic/operands = 1+4e … 4+4e。
    # 因此池不做去重：每个条目固定追加 4 个字符串（缺失写空串）。
    strings = ['']
    for item in ordered:
        strings.extend([item['function'] or '', item['file'] or '',
                        item['mnemonic'] or '', item.get('operands') or ''])
    pool = bytearray()
    for text in strings:
        raw = text.encode('utf-8')
        pool += struct.pack('<I', len(raw))
        pool += raw
    table_offset = HEADER_BYTES + len(pool)
    blob = bytearray(HEADER_BYTES)
    blob[0:9] = STATIC_MAGIC
    struct.pack_into('<I', blob, 9, STATIC_VERSION)
    blob[13:45] = elf_sha256
    struct.pack_into('<II', blob, 45, len(ordered), len(strings))
    struct.pack_into('<Q', blob, 53, table_offset)
    with open(path, 'wb') as handle:
        handle.write(blob)
        handle.write(pool)
        table = bytearray(len(ordered) * ENTRY_BYTES)
        for index, item in enumerate(ordered):
            base = index * ENTRY_BYTES
            pc, size, identifier, predicated = item['pc'], item['size'], item['id'], item['predicated']
            struct.pack_into('<IBI', table, base, int(pc), int(size), int(identifier))
            table[base + 9] = 1 if predicated else 0
            table[base + 10] = trace_store.FLOW_CODES[item['flow']]
            # 文本槽由固定规则给出：1+4e … 4+4e（读取端按同一规则解析，与 p2/t13 约定一致）。
        handle.write(table)
        # 扩展段：line / conditional / 机器码（旧读取器在条目表末尾停止即可）
        ext = bytearray(EXT_HEADER_BYTES)
        ext[0:8] = EXT_MAGIC
        struct.pack_into('<II', ext, 8, EXT_VERSION, len(ordered))
        handle.write(ext)
        ext_blob = bytearray(len(ordered) * EXT_ENTRY_BYTES)
        for index, item in enumerate(ordered):
            base = index * EXT_ENTRY_BYTES
            line = -1 if item.get('line') is None else int(item['line'])
            struct.pack_into('<i', ext_blob, base, line)
            ext_blob[base + 4] = 1 if item.get('conditional') else 0
            raw = bytes.fromhex(item.get('bytes') or '')
            flags = 0
            if raw:
                flags |= 1
                ext_blob[base + 6:base + 6 + min(len(raw), MACHINE_CODE_BYTES)] = \
                    raw[:MACHINE_CODE_BYTES]
            if item.get('function') is not None:
                flags |= 2
            if item.get('file') is not None:
                flags |= 4
            if item.get('line') is not None:
                flags |= 8
            ext_blob[base + 5] = flags
        handle.write(ext_blob)
    return dict(path=str(path), bytes=Path(path).stat().st_size,
                entryCount=len(ordered), stringCount=len(strings),
                tableOffset=table_offset, extBytes=EXT_HEADER_BYTES
                + len(ordered) * EXT_ENTRY_BYTES)


def read_static_dict(path, with_extension=True):
    """读 static-dict.bin；返回 header + entries（文本按固定槽位解析，扩展段可选）。"""
    raw = Path(path).read_bytes()
    if len(raw) < HEADER_BYTES or raw[0:9] != STATIC_MAGIC:
        raise StaticDictError('静态字典 magic 不符（不是 static-dict.bin v1）')
    version = struct.unpack_from('<I', raw, 9)[0]
    if version != STATIC_VERSION:
        raise StaticDictError('静态字典版本不支持：%d' % version)
    elf_sha256 = raw[13:45]
    entry_count, string_count = struct.unpack_from('<II', raw, 45)
    table_offset = struct.unpack_from('<Q', raw, 53)[0]
    entries_end = table_offset + entry_count * ENTRY_BYTES
    if entries_end > len(raw):
        raise StaticDictError('静态字典条目表被截断')
    strings = []
    position = HEADER_BYTES
    while position < table_offset:
        if position + 4 > table_offset:
            raise StaticDictError('字符串池被截断')
        length = struct.unpack_from('<I', raw, position)[0]
        position += 4
        if position + length > table_offset:
            raise StaticDictError('字符串池条目越界')
        try:
            strings.append(raw[position:position + length].decode('utf-8'))
        except UnicodeDecodeError:
            raise StaticDictError('字符串池不是合法 UTF-8')
        position += length
    if len(strings) != string_count:
        raise StaticDictError('字符串数量与 header 不符：%d≠%d'
                              % (len(strings), string_count))
    extension = None
    if with_extension and entries_end + EXT_HEADER_BYTES <= len(raw):
        magic = raw[entries_end:entries_end + 8]
        if magic == EXT_MAGIC:
            ext_version, ext_count = struct.unpack_from('<II', raw, entries_end + 8)
            if ext_version != EXT_VERSION or ext_count != entry_count:
                raise StaticDictError('扩展段版本/条目数不符')
            ext_start = entries_end + EXT_HEADER_BYTES
            if ext_start + ext_count * EXT_ENTRY_BYTES > len(raw):
                raise StaticDictError('扩展段被截断')
            extension = {}
    entries = []
    for index in range(entry_count):
        base = table_offset + index * ENTRY_BYTES
        pc, size, identifier = struct.unpack_from('<IBI', raw, base)
        predicated = bool(raw[base + 9])
        flow_code = raw[base + 10]
        flow = trace_store.FLOW_NAMES.get(flow_code)
        if flow is None:
            raise StaticDictError('条目 flow 编码未知：%d（pc=0x%08x）' % (flow_code, pc))
        slot = 1 + index * 4
        texts = []
        for offset in range(4):
            position_in_pool = slot + offset
            if string_count and position_in_pool < string_count:
                texts.append(strings[position_in_pool])
            else:
                texts.append('')          # 旧/空池样本：文本缺失，由调用方决定是否容忍
        function = texts[0]
        file_value = texts[1]
        mnemonic = texts[2]
        operands = texts[3]
        entry = dict(pc=int(pc), size=int(size), id=int(identifier), flow=flow,
                     predicated=predicated, function=function, file=file_value,
                     mnemonic=mnemonic, operands=operands,
                     conditional=None, line=None, bytes=None)
        if extension is not None:
            ext_base = ext_start + index * EXT_ENTRY_BYTES
            line = struct.unpack_from('<i', raw, ext_base)[0]
            flags = raw[ext_base + 5]
            entry['line'] = None if (line < 0 or not (flags & 8)) else line
            entry['conditional'] = bool(raw[ext_base + 4])
            if not (flags & 2):
                entry['function'] = None
            if not (flags & 4):
                entry['file'] = None
            raw_code = raw[ext_base + 6:ext_base + 6 + MACHINE_CODE_BYTES]
            if flags & 1:
                entry['bytes'] = bytes(raw_code[:size]).hex()
        entries.append(entry)
    return dict(version=version, elfSha256=elf_sha256.hex(), entryCount=entry_count,
                stringCount=string_count, tableOffset=table_offset,
                hasExtension=extension is not None, entries=entries,
                sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))


# ---------------------------------------------------------------------------------------------
# 与参考 store 的 static 块逐项对照
# ---------------------------------------------------------------------------------------------

def compare_entries(entries, store):
    """与参考 store 的 static 块逐项对照；返回 mismatch 列表与统计。"""
    reference = store.static
    mismatches = []
    by_id = {item['id']: item for item in entries}
    if len(reference) != len(entries):
        mismatches.append(dict(kind='length', reference=len(reference),
                               candidate=len(entries)))
    for identifier, reference_entry in enumerate(reference):
        # static 块顺序 = 首次出现顺序 = 字典 id（读取端 _Static 不带 identifier 字段）
        candidate = by_id.get(identifier)
        if candidate is None:
            mismatches.append(dict(kind='missing', id=identifier, pc=reference_entry.pc))
            continue
        fields = dict(pc=reference_entry.pc, size=reference_entry.size,
                      bytes=reference_entry.bytes.hex(), mnemonic=reference_entry.mnemonic,
                      operands=reference_entry.operands, function=reference_entry.function,
                      file=reference_entry.file, line=reference_entry.line,
                      flow=reference_entry.flow, conditional=reference_entry.conditional,
                      predicated=reference_entry.predicated, id=identifier)
        for key, expected in fields.items():
            actual = candidate.get(key)
            if actual != expected:
                mismatches.append(dict(id=identifier, pc=reference_entry.pc,
                                       field=key, reference=expected, candidate=actual))
                if len(mismatches) > 20:
                    break
        if len(mismatches) > 20:
            break
    return dict(referenceCount=len(reference), candidateCount=len(entries),
                mismatches=mismatches[:20], equal=not mismatches)


# ---------------------------------------------------------------------------------------------
# 编排：build
# ---------------------------------------------------------------------------------------------

def default_paths(target):
    """把命令行参数（记录目录或 native.bin）解析成 (native, elf, store)。"""
    path = Path(target)
    if path.is_dir():
        record = path
        native = record / 'native.bin'
        elf = record / 'firmware.elf'
        store_dir = record / 'store'
    else:
        native = path
        record = path.parent
        elf = record / 'firmware.elf'
        store_dir = record / 'store'
    return native, elf, store_dir


def build(target, *, native=None, elf=None, out=None, reference_store=None,
          entries_json=None, stats_path=None, verify_image=True, progress=None):
    started = time.perf_counter()
    native_path, elf_path, store_dir = default_paths(target)
    if native is not None:
        native_path = Path(native)
    if elf is not None:
        elf_path = Path(elf)
    if not native_path.is_file():
        raise StaticDictError('缺少 native.bin：%s（原生 builder 需要它做唯一范围预扫描）'
                              % native_path)
    if not elf_path.is_file():
        raise StaticDictError('缺少 firmware.elf：%s' % elf_path)
    out = Path(out) if out else native_path.parent / 'static-dict.bin'
    stats = dict(target=str(target), nativeFile=str(native_path), elfFile=str(elf_path),
                 output=str(out), nativeBytes=native_path.stat().st_size,
                 elfBytes=elf_path.stat().st_size)
    ranges, range_stats = collect_ranges(native_path, progress)
    stats.update(range_stats)
    started_build = time.perf_counter()
    entries, build_stats = build_static_entries(ranges, elf_path, verify_image=verify_image,
                                                progress=progress)
    stats.update(build_stats)
    stats['nativeBuildSeconds'] = round(time.perf_counter() - started_build, 3)
    if progress is not None:
        progress('  写静态字典：%s（%d 个唯一 PC）' % (out, len(entries)))
    write_started = time.perf_counter()
    write_info = write_static_dict(out, entries, hashlib.sha256(elf_path.read_bytes())
                                   .hexdigest())
    stats['writeSeconds'] = round(time.perf_counter() - write_started, 4)
    stats['staticDict'] = dict(write_info)
    stats['staticDict']['sha256'] = hashlib.sha256(out.read_bytes()).hexdigest()
    if entries_json:
        Path(entries_json).write_text(json.dumps(entries, ensure_ascii=False),
                                      encoding='utf-8')
        stats['entriesJson'] = str(entries_json)
    comparison = None
    reference_dir = Path(reference_store) if reference_store is not None else store_dir
    if reference_dir.is_dir() and (reference_dir / trace_store.MANIFEST_NAME).is_file():
        store = trace_store.TraceStore(reference_dir, verify='sizes')
        try:
            comparison = compare_entries(entries, store)
            comparison['referenceStore'] = str(reference_dir)
        finally:
            store.close()
    stats['comparison'] = comparison
    stats['peakRssBytes'] = trace_store.peak_rss_bytes()
    stats['totalSeconds'] = round(time.perf_counter() - started, 3)
    if stats_path:
        Path(stats_path).write_text(json.dumps(stats, ensure_ascii=False, indent=1),
                                    encoding='utf-8')
    return stats


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------

def _main(argv=None):
    parser = argparse.ArgumentParser(
        prog='static_dict.py',
        description='R2 静态字典：唯一 PC 的静态字段 → static-dict.bin（供原生 builder）')
    sub = parser.add_subparsers(dest='command', required=True)
    build_parser = sub.add_parser('build', help='从记录目录/native.bin 生成静态字典')
    build_parser.add_argument('target', help='记录目录（含 native.bin/firmware.elf）或 native.bin')
    build_parser.add_argument('--native', default=None)
    build_parser.add_argument('--elf', default=None)
    build_parser.add_argument('--out', default=None)
    build_parser.add_argument('--reference-store', default=None)
    build_parser.add_argument('--entries-json', default=None)
    build_parser.add_argument('--stats', default=None)
    build_parser.add_argument('--no-image-check', action='store_true')
    info_parser = sub.add_parser('info', help='查看 static-dict.bin 概要')
    info_parser.add_argument('file')
    info_parser.add_argument('--entries', type=int, default=0, help='打印前 N 条明细')
    verify_parser = sub.add_parser('verify', help='与参考 store 的 static 块逐项对照')
    verify_parser.add_argument('file')
    verify_parser.add_argument('--reference-store', required=True)
    args = parser.parse_args(argv)
    if args.command == 'build':
        stats = build(args.target, native=args.native, elf=args.elf, out=args.out,
                      reference_store=args.reference_store,
                      entries_json=args.entries_json, stats_path=args.stats,
                      verify_image=not args.no_image_check,
                      progress=lambda message: print(message, flush=True))
        summary = {key: stats[key] for key in
                   ('uniquePCs', 'uniqueRanges', 'predicateFallback', 'sourceMapped',
                    'nativeBuildSeconds', 'totalSeconds', 'peakRssBytes') if key in stats}
        summary['staticDict'] = stats['staticDict']
        summary['comparison'] = (None if stats['comparison'] is None else
                                 {key: stats['comparison'][key] for key in
                                  ('referenceCount', 'candidateCount', 'equal')})
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        if stats['comparison'] is not None and not stats['comparison']['equal']:
            return 1
        return 0
    if args.command == 'info':
        info = read_static_dict(args.file)
        summary = {key: info[key] for key in
                   ('version', 'elfSha256', 'entryCount', 'stringCount', 'tableOffset',
                    'hasExtension', 'bytes')}
        summary['sha256'] = info['sha256']
        summary['entries'] = info['entries'][:args.entries] if args.entries else None
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        return 0
    if args.command == 'verify':
        info = read_static_dict(args.file)
        store = trace_store.TraceStore(args.reference_store, verify='sizes')
        try:
            comparison = compare_entries(info['entries'], store)
        finally:
            store.close()
        print(json.dumps(comparison, ensure_ascii=False, indent=1))
        return 0 if comparison['equal'] else 1
    return 2


if __name__ == '__main__':
    sys.exit(_main())
