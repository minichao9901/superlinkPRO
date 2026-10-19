"""流式展开与派生分析；事件保存在 JSONL，内存不随展开指令数线性增长。

与常规解码一致地保留条件执行未知、调用栈及真实包字节核算。
events/packets/elements 只存 JSONL；decoded.json 是小型元数据与分析摘要。

展开引擎有两条实现，产出逐字节相同、语义相同：

* **fast（默认）**：native.jsonl 只读一遍，同一遍里完成
  「区间展开 → IT 谓词/mark_execution 判定 → 调用栈与周期归因 → 字节核算 →
  浏览索引列 sidecar → JSONL 落盘」。每个 PC 的固定字段（mnemonic/operands/函数/文件/行/
  反汇编流类型/JSON 片段）只算一次，落盘用预生成模板直接拼行，因此 3.67 M 条指令不再需要
  3.67 M 次字典构造 + json.dumps，也没有"再读 7 遍事件流"。
* **legacy（`ETM_DECODE_ENGINE=legacy`）**：历史口径，展开后再分多遍读 JSONL
  （unique PC 扫描 / mark_execution / analyze_calls / byte_accounting ×2 / summary 扫描），
  仅用于对照与故障回退。

原生输出有两种格式（`ETM_NATIVE_FORMAT`）：

* **text（默认）**：etm_decode 写 native.jsonl 文本（每个元素/包带 `ocsd_gen_elem_str()`/
  `ocsd_pkt_str()` 的说明文字），Python 逐行解析；产物与改动前逐字节一致。
* **binary（P2 实验能力）**：etm_decode 写带 magic/版本/记录类型的结构化记录流
  （`native.bin`，见 `native_binary.py`），正常路径不生成说明文字；fast 展开只换记录来源，
  events.jsonl/browse-columns.tsv/decoded.json 的分析口径不变。该模式不生成
  packets.jsonl/elements.jsonl（兼容层留 P3/P4），也不支持 legacy 展开引擎。

注意：物理取样仍加载跳变轨迹，ETM 输入、ELF 和派生调用树也占内存；
本模块不声称整个采集链路 O(1) 或支持无限长度。
"""
import collections
import contextlib
import hashlib
import json
import os
import subprocess
from pathlib import Path

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_MCLASS

import decode_trace as decoder_runtime
import native_binary
from decode_trace import DEFAULT_CONFIG, REG_ORDER, V3_REG_ORDER, Image, decoder_config

from flow_analysis import (possible_predicates, mark_execution, analyze_calls, byte_accounting,
                           CallTracker, instruction_info, predicate_addresses)
from stream_store import JsonLines

FAULT_TERMS = ('OVERFLOW', 'BAD_SEQUENCE', 'BAD_TRACEMODE', 'RESERVED')
CACHE_MAX = 8192
IT_REASON = 'IT 非分支条件指令：当前 trace 未记录条件结果'
SIDECAR_NAME = 'browse-columns.tsv'
# 索引列 sidecar 的头部指纹（v2）：events.jsonl 的字节数 + 事件行数 + mtime_ns + sha256。
# 只比 size/行数无法发现"等长改写"（审查 F03）；sha256 在展开成功后按文件流式计算，
# 索引端逐字节校验，代价约 1 s（相对 30 s+ 的索引构建可以忽略）。
SIDECAR_HEADER = '#etm-browse-columns 2 events=%020d rows=%010d mtime=%020d sha256=%064s\n'


def expansion_engine():
    """展开引擎选择：默认 fast；`ETM_DECODE_ENGINE=legacy` 回退到历史多遍实现。"""
    return 'legacy' if os.environ.get('ETM_DECODE_ENGINE', '').strip().lower() == 'legacy' else 'fast'


def native_format():
    """原生输出格式：默认 text（旧行为）；`ETM_NATIVE_FORMAT=binary` 启用结构化二进制。

    二进制模式（P2 实验能力，默认关闭）把 OpenCSD 回调写成带 magic/版本/记录类型的记录流，
    正常路径不再生成包/元素的说明文字；读取端见 `native_binary.py`。默认路径逐字节不变。
    """
    value = os.environ.get('ETM_NATIVE_FORMAT', '').strip().lower()
    if value in ('', 'text'):
        return 'text'
    if value == 'binary':
        return 'binary'
    raise ValueError('ETM_NATIVE_FORMAT 只支持 text 或 binary：' + value)


def _native():
    # The portable launcher selects the bundled snapshot AFTER module imports.
    # Do not capture a stale copy of decode_trace.ROOT at import time.
    native = decoder_runtime.ROOT / 'build-native/etm_decode.exe'
    if not native.exists():
        raise ValueError('缺少 OpenCSD 解码器，请先运行 build-decoder.ps1。')
    return native


def _write_array(handle, source, key):
    """把 JSONL 文件拼成一个 JSON 数组。"""
    handle.write(json.dumps(key) + ':[')
    first = True
    with source.open(encoding='utf-8') as lines:
        for line in lines:
            line = line.strip()
            if not line:
                continue
            handle.write(('' if first else ',') + line)
            first = False
    handle.write(']')


def _run_native(id_stream, image, workdir, config):
    """写 etm.bin/镜像段/manifest，跑一次 OpenCSD，返回原生产物路径、协议名与格式。"""
    protocol = 'ETMv3' if str(config.get('protocol', 'ETMv4')) == 'ETMv3' else 'ETMv4'
    (workdir / 'etm.bin').write_bytes(id_stream)
    order = V3_REG_ORDER if protocol == 'ETMv3' else REG_ORDER
    manifest = ('ETMv3\n' if protocol == 'ETMv3' else '')
    manifest += '\n'.join(hex(int(config[k])) for k in order) + '\n'
    for index, (base, data) in enumerate(image.segments):
        f = workdir / f'image-{index}.bin'
        f.write_bytes(data)
        manifest += f'{hex(base)} {f.resolve()}\n'
    manifest_file = workdir / 'native-config.txt'
    manifest_file.write_text(manifest, encoding='utf-8')

    fmt = native_format()
    if fmt == 'binary':
        native_path = workdir / 'native.bin'
        command = [str(_native()), '--binary', str(native_path), str(workdir / 'etm.bin'),
                   str(manifest_file)]
        process = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 timeout=900)
    else:
        native_path = workdir / 'native.jsonl'
        with native_path.open('wb') as out:
            process = subprocess.run([str(_native()), str(workdir / 'etm.bin'), str(manifest_file)],
                                     stdout=out, stderr=subprocess.PIPE, timeout=900)
    (workdir / 'native.log').write_bytes(process.stderr or b'')
    if process.returncode:
        raise ValueError('OpenCSD 解码失败：' + (process.stderr or b'').decode('utf-8', 'replace')[-3000:])
    return native_path, protocol, fmt


def quality_from_counters(status, input_bytes, counters, framing=None):
    """byte_accounting 的等价物：用单遍展开累加好的计数直接算（口径逐项相同）。"""
    size = status.get('processed', 0)
    covered = counters['covered']
    return dict(
        inputBytes=input_bytes,
        unprocessedBytes=max(0, input_bytes - size),
        processedBytes=size,
        accountedBytes=covered,
        overlapBytes=counters['overlap'],
        unaccountedBytes=max(0, input_bytes - covered),
        unsynchronizedBytes=counters['unknown'],
        unsynchronizedRecords=counters['unsync_records'],
        validPackets=counters['packet_count'] - counters['unsync_records'] - counters['invalid'],
        packetIssues=counters['invalid'],
        overflowPackets=counters['overflow'],
        decoderLastError=status.get('lastErrorCode', None),
        unknownExecution=counters['unknown_execution'],
        initialPhysicalBytes=framing.get('initial_skipped_bytes') if framing else None,
        unknownSourceBytes=framing.get('unknown_bytes') if framing else None,
        trailingFrameBytes=framing.get('trailing_frame_bytes') if framing else None,
        trailingSyncBytes=(framing.get('trailing_sync_bytes', 0) if framing else 0),
        firstPathOffset=counters['first_path_offset'],
        boundaryPresent=bool(counters['unknown'] or (framing or {}).get('initial_skipped_bytes')
                             or (framing or {}).get('trailing_frame_bytes')
                             or (framing or {}).get('trailing_sync_bytes')),
        timing=dict(cycleElements=counters['cycle_elements'], timeFlameAvailable=False),
        basis='字节核算不等于采集无损证明；源码映射率不等于程序覆盖率')


# ---------------------------------------------------------------------------------------------
# fast 引擎：native.jsonl 单遍展开
# ---------------------------------------------------------------------------------------------

class _Unsupported(Exception):
    """native 行形状超出快速解析范围。"""


def _spaced(head):
    """compact JSON 头（只有键/数字/无转义字符串）→ json.dumps 默认分隔符形式。"""
    return head.replace('":', '": ').replace(',"', ', "')


def _number(part):
    return int(part[part.find(':') + 1:])


def _template(event, slots):
    """事件字典 → `%` 模板：值用占位串顶替，字面量里的 % 先转义；行尾补换行（JSONL）。"""
    text = (json.dumps(event, ensure_ascii=False) + '\n').replace('%', '%%')
    for token, placeholder in slots:
        text = text.replace('"%s"' % token, placeholder)
    return text


class _InstructionTable:
    """每个 PC 的派生信息：只算一次，之后展开就是一次字典查表。"""

    def __init__(self, image, predicates, known):
        self.image = image
        self.predicates = predicates
        self.known = known
        self.decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
        self.decoder.detail = True
        self.table = {}
        self.files = set()
        self._extra = {}

    def predicate(self, pc):
        """possible_predicates 的逐 PC 等价判定（镜像外的 PC 才做 IT 回溯扫描）。"""
        if pc in self.known:
            return pc in self.predicates
        cached = self._extra.get(pc)
        if cached is not None:
            return cached
        found = False
        image = self.image
        for start in range(max(0, pc - 18), pc, 2):
            decoded = list(self.decoder.disasm(image.read(start, pc - start + 4), start))
            if decoded and _is_it(decoded[0]):
                count = len(decoded[0].mnemonic) - 1
                if any(item.address == pc for item in decoded[1:count + 1]):
                    found = True
                    break
        self._extra[pc] = found
        return found

    def info(self, ins):
        """返回该 PC 的信息元组（同一 PC 只计算一次）。"""
        pc = ins.address
        entry = self.table.get(pc)
        if entry is not None:
            return entry
        function, source, line = self.image.location(pc)
        # flow/conditional 必须与 mark_execution 同口径：它把该指令的字节**单独**反汇编
        # （不继承 IT 块状态），因此 IT 块内的指令这里是 unconditional/linear。
        single = next(self.decoder.disasm(ins.bytes, pc), None)
        detail = instruction_info(single) if single else dict(flow='unknown', conditional=True)
        flow = detail['flow']
        forced = flow == 'linear' and self.predicate(pc)
        size = ins.size
        hexed = ins.bytes.hex()
        mnemonic, operands = ins.mnemonic, ins.op_str
        constant = dict(kind='instruction', seq='@@SEQ@@', offset='@@OFF@@', pc=pc, size=size,
                        bytes=hexed, mnemonic=mnemonic, operands=operands,
                        executed=None if forced else True,
                        function=function, file=source, line=line, cycleCount=0, cycleSpan=0,
                        flow=flow, conditional=detail['conditional'],
                        executionState='unknown' if forced else 'executed',
                        executionReason=IT_REASON if forced else '',
                        callPath='@@PATH@@', cycles=0)
        body = _template(constant, (('@@SEQ@@', '%d'), ('@@OFF@@', '%d'), ('@@PATH@@', '%s')))
        search = ' '.join((function or '', source or '', mnemonic, operands, ''))
        search = ('%s 0x%08x %d' % (search, pc, pc)).lower().replace('\t', ' ').replace('\n', ' ')
        prefix = '\tinstruction\t%d\t%s\t%s\t%s\t%s\t' % (
            pc, (function or '').replace('\t', ' '), (source or '').replace('\t', ' '),
            '' if line is None else line, flow)
        suffix = '\t\t' + search + '\n'
        columns = [prefix + 'executed' + suffix, prefix + 'not-taken' + suffix,
                   prefix + 'unknown' + suffix]
        entry = (size, hexed, mnemonic, operands, flow, detail['conditional'], function, source,
                 line, forced, body, columns, {}, pc)
        self.table[pc] = entry
        if source:
            self.files.add(source)
        return entry

    def tail_template(self, entry, executed):
        """区间末指令模板（executed 可能是 None = 条件执行未证实）。"""
        cache = entry[12]
        text = cache.get(executed)
        if text is None:
            size, hexed, mnemonic, operands, flow, conditional, function, source, line = entry[:9]
            if executed is None:
                state, reason = 'unknown', IT_REASON
            else:
                state, reason = ('executed' if executed else 'not-taken'), ''
            event = dict(kind='instruction', seq='@@SEQ@@', offset='@@OFF@@', pc=entry[13], size=size,
                         bytes=hexed, mnemonic=mnemonic, operands=operands, executed=executed,
                         function=function, file=source, line=line, cycleCount='@@CC@@',
                         cycleSpan='@@SPAN@@', flow=flow, conditional=conditional,
                         executionState=state, executionReason=reason, callPath='@@PATH@@',
                         cycles='@@CYC@@')
            text = _template(event, (('@@SEQ@@', '%d'), ('@@OFF@@', '%d'), ('@@CC@@', '%d'),
                                     ('@@SPAN@@', '%d'), ('@@PATH@@', '%s'), ('@@CYC@@', '%d')))
            cache[executed] = text
        return text


def _is_it(ins):
    from capstone.arm import ARM_INS_IT
    return ins.id == ARM_INS_IT


def _range_instructions(start, end, decoder, image, span_cache):
    """从 start 线性反汇编到 end 的指令列表（同一 start 缓存较长前缀，结果与精确长度一致）。"""
    exact = span_cache.get((start, end))
    if exact is not None:
        return exact
    entry = span_cache.get(start)
    if entry is None or entry[0] < end:
        instructions = list(decoder.disasm(image.read(start, end - start), start))
        entry = (end, instructions)
        span_cache[start] = entry
    else:
        instructions = entry[1]
    selected = []
    for ins in instructions:
        if ins.address >= end:
            break
        selected.append(ins)
    if not selected or selected[-1].address + selected[-1].size != end:
        # 缓存窗口的末条指令跨过了 end：按精确长度重解析，与历史实现同口径
        instructions = list(decoder.disasm(image.read(start, end - start), start))
        selected = [ins for ins in instructions if ins.address < end]
        span_cache[start] = (end, instructions)
    if len(span_cache) >= CACHE_MAX:
        span_cache.clear()
    span_cache[(start, end)] = selected
    return selected


def _parse_element(line):
    """native element 行 → (type, offset, start, end, cycleCount, lastExecuted, payload, flags, 元素行)。"""
    head, separator, tail = line.partition(',"text":')
    if not separator:
        raise _Unsupported('element text')
    parts = head.split(',')
    if len(parts) != 15:
        raise _Unsupported('element fields')
    if not (parts[1].startswith('"offset":') and parts[3].startswith('"type":')
            and parts[14].startswith('"payload":')):
        raise _Unsupported('element order')
    etype = _number(parts[3])
    offset = _number(parts[1])
    line_out = _spaced(head) + ', "text": ' + tail
    if etype == 5:
        return (etype, offset, _number(parts[4]), _number(parts[5]), _number(parts[8]),
                _number(parts[10]), _number(parts[14]), 0, line_out)
    if etype == 12:
        return (etype, offset, 0, 0, _number(parts[8]), 0, 0, 0, line_out)
    return (etype, offset, _number(parts[4]), _number(parts[5]), 0, 0, _number(parts[14]),
            _number(parts[7]), line_out)


def _parse_packet(line):
    head, separator, tail = line.partition(',"text":')
    if not separator:
        raise _Unsupported('packet text')
    parts = head.split(',')
    if len(parts) != 4 or not parts[1].startswith('"offset":') or not parts[2].startswith('"size":'):
        raise _Unsupported('packet fields')
    quoted = tail.rstrip()
    return (_spaced(head) + ', "text": ' + tail, _number(parts[1]), _number(parts[2]), quoted[1:-2])


def _text_records(native_jsonl):
    """native.jsonl → 与二进制记录同形的元组流（文本参考路径，逐行解析口径不变）。

    元组形状：
      (2, 包文本行, offset, size, 包文本, 故障分类位)
      (1, type, offset, start, end, cycleCount, lastExecuted, payload, flags, 文本, 元素文本行)
      (3, status 字典)
    只有 events.jsonl 真正使用的稀有元素类型（1/2/3/6/7/8/9/10）在这里解出说明文字。
    """
    with native_jsonl.open(encoding='utf-8', errors='replace') as src:
        for line in src:
            if not line.startswith('{'):
                continue
            if line.startswith('{"kind":"packet"'):
                text_line, offset, size, text = _parse_packet(line)
                yield (2, text_line, offset, size, text,
                       native_binary.classify_packet_text(text))
                continue
            if line.startswith('{"kind":"status"'):
                yield (3, json.loads(line))
                continue
            if not line.startswith('{"kind":"element"'):
                continue
            etype, offset, start, end, cycle_count, last_executed, payload, flags, line_out = \
                _parse_element(line)
            text = ''
            if etype in (1, 2, 3, 6, 7, 8, 9, 10):
                quoted = line.partition(',"text":')[2].rstrip()
                text = json.loads(quoted[:-1])
            yield (1, etype, offset, start, end, cycle_count, last_executed, payload, flags,
                   text, line_out)


def _binary_records(native):
    """原生二进制记录 → 与文本路径同形的元组流（字段已按同一规则归一化）。"""
    for record in native.records():
        kind = record[0]
        if kind == native_binary.RECORD_ELEMENT:
            (_, etype, offset, _id, start, end, _isa, flags, cycle_count, _has_cc, _timestamp,
             _has_ts, last_executed, _last_cond, _last_type, _last_subtype, payload,
             text) = record
            yield (1, etype, offset, start, end, cycle_count, last_executed, payload, flags,
                   text, None)
        elif kind == native_binary.RECORD_PACKET:
            _, offset, size, type_code = record
            yield (2, None, offset, size, '',
                   native_binary.classify_packet(type_code, native.protocol))
        else:
            yield (3, record[1])


def _expand_records(workdir, image, records, protocol, config, input_bytes, max_events=None,
                    progress_every=500000, write_packets=True, write_elements=True,
                    native_name='native.jsonl'):
    """单遍展开记录流，返回与历史实现相同的 result 结构。

    文本与二进制只差“记录来源”与是否落 packets.jsonl/elements.jsonl；events.jsonl、
    browse-columns.tsv、sources.json 与 decoded.json 的计数/质量/分析口径完全相同。
    """
    known = set()
    probe = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    for start, size, _ in image.functions:
        known.update(item.address for item in probe.disasm(image.read(start, size), start))
    table = _InstructionTable(image, predicate_addresses(image), known)
    decoder = table.decoder

    events_jsonl = workdir / 'events.jsonl'
    packets_jsonl = workdir / 'packets.jsonl'
    elements_jsonl = workdir / 'elements.jsonl'
    sidecar_path = workdir / SIDECAR_NAME

    tracker = CallTracker()
    seq = 0
    instruction_count = mapped = ranges = exceptions = gaps = 0
    packets_n = elements_n = 0
    counters = dict(covered=0, overlap=0, unknown=0, unsync_records=0, invalid=0, overflow=0,
                    packet_count=0, unknown_execution=0, first_path_offset=None, cycle_elements=0)
    status = {}
    previous_packet = -1
    packet_end = 0
    pending = None
    span_cache = {}
    range_entries = {}
    body_lines, side_lines, element_lines, packet_lines = [], [], [], []

    def flush_tail():
        nonlocal seq, pending
        entry, offset, executed, cycle_count, cycle_span, reserved, function, source, line_no, span, pc = pending
        tracker.feed_instruction(function, source, line_no, executed, entry[4], pc, entry[0], cycle_count)
        body_lines.append(table.tail_template(entry, executed) % (
            reserved, offset, cycle_count, cycle_span, tracker.path_json, cycle_count))
        side_lines.append(str(reserved) + entry[11][0 if executed is True else (1 if executed is False else 2)])
        pending = None
        if len(body_lines) >= 8192:
            ev_out.write(''.join(body_lines))
            body_lines.clear()
            side_out.write(''.join(side_lines))
            side_lines.clear()

    with contextlib.ExitStack() as stack:
        ev_out = stack.enter_context(events_jsonl.open('w', encoding='utf-8'))
        side_out = stack.enter_context(sidecar_path.open('w', encoding='utf-8', newline='\n'))
        # 包/元素 JSONL 是文本模式的历史产物；二进制模式不再生成（兼容层留 P3/P4）。
        pk_out = (stack.enter_context(packets_jsonl.open('w', encoding='utf-8'))
                  if write_packets else None)
        el_out = (stack.enter_context(elements_jsonl.open('w', encoding='utf-8'))
                  if write_elements else None)
        # 头部先写占位（定宽），展开成功后回填 events.jsonl 的 size/行数/mtime/sha256 指纹；
        # 索引端据此判断 sidecar 是否对得上（展开中断则保持占位=视为过期）。
        side_out.write(SIDECAR_HEADER % (0, 0, 0, '0' * 64))
        for record in records:
            kind = record[0]
            if kind == 2:
                _, text_line, offset, size, _text, packet_flags = record
                if pk_out is not None:
                    packet_lines.append(text_line)
                    if len(packet_lines) >= 8192:
                        pk_out.write(''.join(packet_lines))
                        packet_lines.clear()
                packets_n += 1
                if offset < previous_packet:
                    raise ValueError('包偏移倒序，拒绝伪造字节对账')
                previous_packet = offset
                stop = offset + size
                if offset < packet_end:
                    counters['overlap'] += max(0, min(packet_end, stop) - offset)
                counters['covered'] += max(0, stop - max(offset, packet_end))
                packet_end = max(packet_end, stop)
                if packet_flags & native_binary.PACKET_UNSYNC:
                    counters['unknown'] += size
                    counters['unsync_records'] += 1
                if packet_flags & native_binary.PACKET_INVALID:
                    counters['invalid'] += 1
                    if packet_flags & native_binary.PACKET_OVERFLOW:
                        counters['overflow'] += 1
                counters['packet_count'] += 1
                continue
            if kind == 3:
                status = record[1]
                continue
            if kind != 1:
                continue
            (_, etype, offset, start, end, cycle_count, last_executed, payload, flags, text,
             line_out) = record
            if el_out is not None:
                element_lines.append(line_out)
                if len(element_lines) >= 8192:
                    el_out.write(''.join(element_lines))
                    element_lines.clear()
            elements_n += 1
            if etype == 12:
                counters['cycle_elements'] += 1
                if pending is not None:
                    if cycle_count:
                        pending[3] += cycle_count
                        if not pending[4]:
                            pending[4] = pending[9]
                    flush_tail()
                continue
            if pending is not None:
                flush_tail()
            if etype == 5:
                ranges += 1
                plan = range_entries.get((start,end))
                if plan is None:
                    selected = _range_instructions(start, end, decoder, image, span_cache)
                    entries = tuple(table.info(ins) for ins in selected)
                    groups={};begin=0
                    while begin<len(entries)-1:
                        entry=entries[begin];stop=begin+1
                        if entry[4]=='linear':
                            key=(entry[6],entry[7],entry[8],entry[9])
                            while stop<len(entries)-1 and entries[stop][4]=='linear' and (entries[stop][6],entries[stop][7],entries[stop][8],entries[stop][9])==key:stop+=1
                        groups[begin]=stop-begin;begin=stop
                    plan=(entries,groups)
                    if len(range_entries) >= CACHE_MAX:range_entries.clear()
                    range_entries[(start,end)] = plan
                entries,groups=plan
                if (len(entries) != payload or not entries
                        or entries[-1][13] + entries[-1][0] != end):
                    raise ValueError(
                        f'机器码范围核对失败：0x{start:08x}..0x{end:08x}；'
                        f'OpenCSD={payload}，反汇编={len(entries)}。')
                last = len(entries) - 1
                tracked_until=0
                for index,entry in enumerate(entries):
                    pc = entry[13]
                    function, source, line_no = entry[6], entry[7], entry[8]
                    if counters['first_path_offset'] is None:
                        counters['first_path_offset'] = offset
                    if index == last:
                        executed = bool(last_executed)
                        if entry[9]:
                            executed = None
                        pending = [entry, offset, executed, cycle_count,
                                   (len(entries) if cycle_count else 0), seq, function, source,
                                   line_no, len(entries), pc]
                        if executed is None:
                            counters['unknown_execution'] += 1
                        seq += 1
                    else:
                        executed = None if entry[9] else True
                        if index>=tracked_until:
                            group=groups[index]
                            if group>1:
                                tail=entries[index+group-1]
                                tracker.feed_linear_many(function,source,line_no,executed,pc,entry[0],tail[13],tail[0],group)
                            else:
                                tracker.feed_instruction(function,source,line_no,executed,entry[4],pc,entry[0],0)
                            tracked_until=index+group
                        body_lines.append(entry[10] % (seq, offset, tracker.path_json))
                        side_lines.append(str(seq) + entry[11][0 if executed is True else (1 if executed is False else 2)])
                        if executed is None:
                            counters['unknown_execution'] += 1
                        seq += 1
                        if len(body_lines) >= 8192:
                            ev_out.write(''.join(body_lines))
                            body_lines.clear()
                            side_out.write(''.join(side_lines))
                            side_lines.clear()
                    instruction_count += 1
                    if line_no:
                        mapped += 1
                    if max_events and instruction_count > max_events:
                        raise ValueError(f'已超过 --max-events={max_events}；流式解码本身没有上限。')
                    if progress_every and instruction_count % progress_every == 0:
                        print(f'  流式解码：已展开 {instruction_count} 条指令（seq={seq}）', flush=True)
            elif etype in (1, 2, 3, 6, 7, 8, 9, 10):
                event = dict(kind='event', seq=seq, type=etype, offset=offset, text=text,
                             payload=payload, start=start, end=end, flags=flags)
                body_lines.append(json.dumps(event, ensure_ascii=False) + '\n')
                side_lines.append('\t'.join((str(seq), 'event', '', '', '', '', '', '',
                                             str(etype),
                                             ('    ' + text).lower().replace('\t', ' ').replace('\n', ' '))) + '\n')
                tracker.feed_event(etype, flags, payload, end)
                seq += 1
                if etype == 9:
                    exceptions += 1
                if etype in (1, 6, 7, 8) or (etype == 2 and payload):
                    gaps += 1
                if len(body_lines) >= 8192:
                    ev_out.write(''.join(body_lines))
                    body_lines.clear()
                    side_out.write(''.join(side_lines))
                    side_lines.clear()
        if pending is not None:
            flush_tail()
        if body_lines:
            ev_out.write(''.join(body_lines))
        if side_lines:
            side_out.write(''.join(side_lines))
        if el_out is not None and element_lines:
            el_out.write(''.join(element_lines))
        if pk_out is not None and packet_lines:
            pk_out.write(''.join(packet_lines))

    if not instruction_count:
        raise ValueError('没有恢复到可信指令范围；原始采集和包记录已保留。'
                         '请检查 CPU 是否继续、ETM 同步、Trace ID 与匹配 ELF。')

    # 展开成功后回填 sidecar 头部指纹（events.jsonl 的 size/行数/mtime/sha256）。
    events_digest = hashlib.sha256()
    with events_jsonl.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            events_digest.update(chunk)
    events_stat = events_jsonl.stat()
    with sidecar_path.open('r+b') as handle:
        handle.write((SIDECAR_HEADER % (events_stat.st_size, seq, events_stat.st_mtime_ns,
                                        events_digest.hexdigest())).encode('utf-8'))

    sources_data = {name: image.source(name) for name in sorted(table.files)}
    (workdir / 'sources.json').write_text(json.dumps(sources_data, ensure_ascii=False),
                                          encoding='utf-8')
    processed = int(status.get('processed') or 0)
    result = dict(
        version=1, protocol=protocol, config=config, inputBytes=input_bytes,
        elf=dict(name=Path(image.path).name, sha256=hashlib.sha256(image.bytes).hexdigest()),
        summary=dict(packets=packets_n, ranges=ranges, instructions=instruction_count,
                     executed=0, sourceMapped=mapped, exceptions=exceptions,
                     gaps=gaps, packetIssues=counters['invalid']),
        status=status, timeBasis='execution order; no per-instruction timestamp',
        streamed=dict(eventsFile='events.jsonl', events=seq, elements=elements_n,
                      packets=packets_n, nativeJsonl=native_name),
        quality=dict(inputBytes=input_bytes, processedBytes=processed,
                     unprocessedBytes=max(0, input_bytes - processed),
                     overlapBytes=0, unaccountedBytes=max(0, input_bytes - processed)))
    analysis = tracker.finish()
    result['analysis'] = analysis
    result['quality'] = quality_from_counters(status, input_bytes, counters)
    result['summary']['executed'] = analysis['timing']['executedInstructions']
    result['summary']['unknownExecution'] = result['quality']['unknownExecution']
    result['summary']['packetIssues'] = result['quality']['packetIssues']
    result['summary']['functions'] = {row['function']: row['knownExecuted']
                                      for row in analysis['functions'] if row['knownExecuted']}
    result['version'] = 3
    result['streamed']['schema'] = 2
    (workdir / 'decoded.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    return result


def _expand_fast(workdir, image, native_jsonl, protocol, config, input_bytes, max_events=None,
                 progress_every=500000):
    """单遍展开 native.jsonl（文本参考路径），返回与历史实现相同的 result 结构。"""
    return _expand_records(workdir, image, _text_records(native_jsonl), protocol, config,
                           input_bytes, max_events, progress_every,
                           write_packets=True, write_elements=True, native_name='native.jsonl')


def _expand_binary(workdir, image, native_bin, protocol, config, input_bytes, max_events=None,
                   progress_every=500000):
    """单遍展开原生二进制记录（`ETM_NATIVE_FORMAT=binary`）。

    与 fast 引擎同口径；区别是不再生成 packets.jsonl / elements.jsonl（兼容层留 P3/P4），
    decoded.json 的 streamed.nativeJsonl 记 native.bin。
    """
    native = native_binary.NativeFile(native_bin)
    if native.protocol != protocol:
        raise ValueError(f'原生二进制协议与配置不符：{native.protocol} != {protocol}')
    return _expand_records(workdir, image, _binary_records(native), protocol, config,
                           input_bytes, max_events, progress_every,
                           write_packets=False, write_elements=False, native_name='native.bin')


# ---------------------------------------------------------------------------------------------
# legacy 引擎：历史多遍实现（对照/回退）
# ---------------------------------------------------------------------------------------------

def _expand_legacy(workdir, image, native_jsonl, protocol, config, input_bytes, max_events=None,
                   progress_every=500000):
    """历史口径：先展开成三个 JSONL，再分别做 unique/mark/calls/accounting/summary 多遍。"""
    disassembler = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    cache = {}
    counts = collections.Counter()
    source_files = set()
    seq = 0
    instruction_count = executed = mapped = 0
    ranges = exceptions = gaps = faults = 0
    packets_n = elements_n = 0
    status = {}

    events_jsonl = workdir / 'events.jsonl'
    packets_jsonl = workdir / 'packets.jsonl'
    elements_jsonl = workdir / 'elements.jsonl'

    def emit(handle, obj):
        # 一行一个 JSON 对象（真正的 JSONL）：便于流式再读（导出简化 C、统计），
        # 拼 decoded.json 时由 _write_array() 负责补逗号。
        handle.write(json.dumps(obj, ensure_ascii=False) + '\n')

    with events_jsonl.open('w', encoding='utf-8') as ev_out, \
            packets_jsonl.open('w', encoding='utf-8') as pk_out, \
            elements_jsonl.open('w', encoding='utf-8') as el_out, \
            native_jsonl.open(encoding='utf-8', errors='replace') as src:

        # 周期计数（OCSD_GEN_TRC_ELEM_CYCLE_COUNT，type 12）是**独立元素**，
        # 语义是"自上次周期计数以来、前一个指令区间消耗的周期"。流式写出时无法回头改已发出的行，
        # 所以把**区间最后一条指令**先挂起（pending_tail），等周期元素来了再回填、随后发出。
        pending_tail = None
        pending_range_size = 0

        for line in src:
            if not line.startswith('{'):
                continue
            record = json.loads(line)
            kind = record.get('kind')
            if kind == 'packet':
                emit(pk_out, record)
                packets_n += 1
                text = str(record.get('text', '')).upper()
                if any(term in text for term in FAULT_TERMS):
                    faults += 1
                continue
            if kind == 'status':
                status = record
                continue
            if kind != 'element':
                continue
            emit(el_out, record)
            elements_n += 1
            etype = record['type']
            if etype == 12:
                cycles = int(record.get('cycleCount') or 0)
                if cycles and pending_tail is not None:
                    pending_tail['cycleCount'] = int(pending_tail.get('cycleCount') or 0) + cycles
                    pending_tail['cycleSpan'] = pending_tail.get('cycleSpan') or pending_range_size
                if pending_tail is not None:
                    emit(ev_out, pending_tail)
                    pending_tail = None
                continue
            if pending_tail is not None:
                emit(ev_out, pending_tail)
                pending_tail = None
            if etype == 5:
                ranges += 1
                start, end = record['start'], record['end']
                key = (start, end)
                ins_list = cache.get(key)
                if ins_list is None:
                    if len(cache) > CACHE_MAX:
                        cache.clear()
                    ins_list = list(disassembler.disasm(image.read(start, end - start), start))
                    cache[key] = ins_list
                if (len(ins_list) != record['payload'] or not ins_list
                        or ins_list[-1].address + ins_list[-1].size != end):
                    raise ValueError(
                        f'机器码范围核对失败：0x{start:08x}..0x{end:08x}；'
                        f'OpenCSD={record["payload"]}，反汇编={len(ins_list)}。')
                last = len(ins_list) - 1
                # 周期计数（ETMv4 的 cycle-count）挂在**整个区间元素**上，不是每条指令上。
                range_cycles = int(record.get('cycleCount') or 0)
                for index, ins in enumerate(ins_list):
                    function, file, line_no = image.location(ins.address)
                    executed_here = bool(record['lastExecuted']) if index == last else True
                    event = dict(kind='instruction', seq=seq, offset=record['offset'],
                                 pc=ins.address, size=ins.size, bytes=ins.bytes.hex(),
                                 mnemonic=ins.mnemonic, operands=ins.op_str,
                                 executed=executed_here, function=function, file=file,
                                 line=line_no,
                                 cycleCount=(range_cycles if index == last else 0),
                                 cycleSpan=(len(ins_list) if index == last and range_cycles else 0))
                    # 最后一条指令先挂起：紧随其后的周期计数元素要回填到它身上
                    if index == last:
                        pending_tail = event
                        pending_range_size = len(ins_list)
                    else:
                        emit(ev_out, event)
                    seq += 1
                    instruction_count += 1
                    if executed_here:
                        executed += 1
                        counts[function or '(未知函数)'] += 1
                    if line_no:
                        mapped += 1
                    if file:
                        source_files.add(file)
                    if max_events and instruction_count > max_events:
                        raise ValueError(f'已超过 --max-events={max_events}；流式解码本身没有上限。')
                    if progress_every and instruction_count % progress_every == 0:
                        print(f'  流式解码：已展开 {instruction_count} 条指令（seq={seq}）', flush=True)
            elif etype in (1, 2, 3, 6, 7, 8, 9, 10):
                event = dict(kind='event', seq=seq, type=etype, offset=record['offset'],
                             text=record['text'], payload=record['payload'],
                             start=record['start'], end=record['end'], flags=record['flags'])
                emit(ev_out, event)
                seq += 1
                if etype == 9:
                    exceptions += 1
                if etype in (1, 6, 7, 8) or (etype == 2 and record['payload']):
                    gaps += 1
        if pending_tail is not None:
            emit(ev_out, pending_tail)
            pending_tail = None

    if not instruction_count:
        raise ValueError('没有恢复到可信指令范围；原始采集和包记录已保留。'
                         '请检查 CPU 是否继续、ETM 同步、Trace ID 与匹配 ELF。')

    sources_data = {f: image.source(f) for f in sorted(source_files)}
    (workdir / 'sources.json').write_text(json.dumps(sources_data, ensure_ascii=False),
                                          encoding='utf-8')
    processed = int(status.get('processed') or 0)
    result = dict(
        version=1, protocol=protocol, config=config, inputBytes=input_bytes,
        elf=dict(name=Path(image.path).name, sha256=hashlib.sha256(image.bytes).hexdigest()),
        summary=dict(packets=packets_n, ranges=ranges, instructions=instruction_count,
                     executed=executed, sourceMapped=mapped, exceptions=exceptions,
                     gaps=gaps, packetIssues=faults, functions=dict(counts)),
        status=status, timeBasis='execution order; no per-instruction timestamp',
        streamed=dict(eventsFile='events.jsonl', events=seq, elements=elements_n,
                      packets=packets_n, nativeJsonl='native.jsonl'),
        quality=dict(inputBytes=input_bytes, processedBytes=processed,
                     unprocessedBytes=max(0, input_bytes - processed),
                     overlapBytes=0, unaccountedBytes=max(0, input_bytes - processed)))

    # Keep the same predicate/call-stack semantics as the ordinary web decoder.
    # Unique PCs are bounded by the executable image, not by loop iterations.
    unique = {}
    for event in JsonLines(events_jsonl, seq):
        if event['kind'] == 'instruction':
            unique.setdefault(event['pc'], event)
    predicates = possible_predicates(image, unique.values())
    del unique
    enriched = workdir / 'events.enriched.jsonl'
    with enriched.open('w', encoding='utf-8') as output:
        def events():
            block = []
            def drain():
                mark_execution(block, predicates)
                for event in block:
                    yield event  # analyze_calls adds callPath before iteration resumes
                    output.write(json.dumps(event, ensure_ascii=False) + '\n')
            for event in JsonLines(events_jsonl, seq):
                block.append(event)
                if len(block) >= 4096:
                    yield from drain()
                    block.clear()
            yield from drain()
        result['analysis'] = analyze_calls(events())
    enriched.replace(events_jsonl)
    result['events'] = JsonLines(events_jsonl, seq)
    result['packets'] = JsonLines(packets_jsonl, packets_n)
    result['elements'] = JsonLines(elements_jsonl, elements_n)
    result['quality'] = byte_accounting(result)
    result['summary']['executed'] = sum(e['kind'] == 'instruction' and e.get('executed') is True for e in result['events'])
    result['summary']['unknownExecution'] = result['quality']['unknownExecution']
    result['summary']['packetIssues'] = result['quality']['packetIssues']
    result['summary']['functions'] = {r['function']: r['knownExecuted'] for r in result['analysis']['functions'] if r['knownExecuted']}
    result['version'] = 3
    result['streamed']['schema'] = 2
    for key in ('events', 'packets', 'elements'):
        result.pop(key)
    (workdir / 'decoded.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    return result


def decode_stream(id_stream, elf, workdir, config=None, sources=None, max_events=None,
                  progress_every=500000):
    """流式解码；返回精简结果（不含 events/packets/elements 本体）。

    原生输出格式由 `ETM_NATIVE_FORMAT` 选择：默认 `text`（旧路径，逐字节不变）；
    `binary` 走结构化二进制记录（P2 实验能力，只支持 fast 展开引擎）。
    """
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    config = decoder_config(config)
    image = Image(elf, sources)
    native_path, protocol, fmt = _run_native(id_stream, image, workdir, config)
    engine = expansion_engine()
    if fmt == 'binary':
        if engine == 'legacy':
            raise ValueError('原生二进制输出只支持 fast 展开引擎，请设置 ETM_DECODE_ENGINE=fast。')
        return _expand_binary(workdir, image, native_path, protocol, config, len(id_stream),
                              max_events, progress_every)
    expand = _expand_legacy if engine == 'legacy' else _expand_fast
    return expand(workdir, image, native_path, protocol, config, len(id_stream), max_events,
                  progress_every)


def decode_capture_stream(csv_path, elf, workdir, config=None, sources=None, fraction=0.25,
                          clock=0, data=None, max_events=None, glitch_ns=0):
    """从 LA 的 CSV/KVDAT 走完整链路（取样 + TPIU 解复用 + 解码）。"""
    from trace_port import extract
    from tpiu_demux import demux

    csv_path, workdir = Path(csv_path).resolve(), Path(workdir).resolve()
    config = decoder_config(config)
    physical, candidates = extract(csv_path, clock, data or [1, 2, 3, 4], fraction, glitch_ns)
    valid = []
    failures=[]
    workdir.mkdir(parents=True,exist_ok=True)
    for phase, raw in enumerate(candidates):
        try:
            frames, streams = demux(raw)
            if int(config['traceid']) in streams and frames['frames']:
                valid.append((phase, frames, streams))
        except ValueError as error:
            failures.append({'phase':phase,'error':str(error)})
            continue
    if len(valid) != 1:
        (workdir/'demux-errors.json').write_text(json.dumps(failures,ensure_ascii=False),encoding='utf8')
        (workdir/'physical.json').write_text(json.dumps(physical,ensure_ascii=False),encoding='utf8')
        raise ValueError(f'不能唯一确定 TPIU 字节相位：{len(valid)} 个候选；请检查通道和取样位置。'+ (' '+failures[-1]['error'] if failures else ''))
    phase, frames, streams = valid[0]
    (workdir / 'tpiu.bin').write_bytes(candidates[phase])
    framing = {**frames, 'phase': phase}
    (workdir / 'framing.json').write_text(json.dumps(framing, ensure_ascii=False), encoding='utf-8')
    (workdir / 'physical.json').write_text(json.dumps(physical, ensure_ascii=False), encoding='utf-8')
    result = decode_stream(streams[int(config['traceid'])], elf, workdir, config, sources,
                           max_events=max_events)
    result['physical'] = physical
    result['framing'] = framing
    # extract() 已对整份 CSV 做过 sha256，直接复用（同一文件、同一算法），不再重读 500 MB。
    result['capture'] = {'name': csv_path.name, 'sha256': physical.get('input_sha256')}
    # 字节核算的边界字段依赖 framing：fast 引擎已在单遍里算好与 framing 无关的全部字段，
    # 这里只补 5 个依赖 framing 的字段，不再为了几个标量重新遍历 3.67 M 条事件。
    if expansion_engine() == 'legacy':
        streams_result = {**result, 'events': JsonLines(workdir / 'events.jsonl', result['streamed']['events']),
                          'packets': JsonLines(workdir / 'packets.jsonl', result['streamed']['packets']),
                          'elements': JsonLines(workdir / 'elements.jsonl', result['streamed']['elements'])}
        result['quality'] = byte_accounting(streams_result)
    else:
        quality = result['quality']
        quality['initialPhysicalBytes'] = framing.get('initial_skipped_bytes')
        quality['unknownSourceBytes'] = framing.get('unknown_bytes')
        quality['trailingFrameBytes'] = framing.get('trailing_frame_bytes')
        quality['trailingSyncBytes'] = framing.get('trailing_sync_bytes', 0)
        quality['boundaryPresent'] = bool(quality['unsynchronizedBytes'] or quality['initialPhysicalBytes']
                                          or quality['trailingFrameBytes'] or quality['trailingSyncBytes'])
    (workdir / 'decoded.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    return result


def export_simplified_stream(workdir, out_path):
    """从 events.jsonl 流式生成「简化源码 .c」（与网页同一条行规则）。"""
    from source_export import base

    workdir, out_path = Path(workdir).resolve(), Path(out_path).resolve()
    sources = json.loads((workdir / 'sources.json').read_text(encoding='utf-8'))
    source_lines = {f: t.splitlines() for f, t in sources.items()}
    events = workdir / 'events.jsonl'
    clean = lambda text: str(text).replace('*/', '* /')          # noqa: E731

    files, seen = [], set()
    for event in JsonLines(events,None):
        f = event.get('file')
        if f and f not in seen:
            seen.add(f)
            files.append(f)

    rows = 0
    with out_path.open('w', encoding='utf-8') as out:
        out.write('/* ETMv4 简化源码执行流：按恢复顺序排列，不是可编译程序。 */\n')
        out.write(f'/* 流式导出：events.jsonl 共 {events.stat().st_size} 字节 */\n')
        for f in files:
            out.write('/* 配套文件：' + clean(base(f)) + ' = ' + clean(f) + ' */\n')
        last = None
        context = None
        for event in JsonLines(events,None):
            seq = event.get('seq', rows)
            if event.get('kind') != 'instruction':
                if event.get('type') in (1, 2, 3, 6, 7, 8, 9, 10):
                    out.write('/* #' + str(seq) + ' ' + clean(event.get('text', '')) + ' */\n')
                    rows += 1
                    last = None
                continue
            file, line_no = event.get('file', ''), event.get('line')
            function = event.get('function', '')
            if event.get('executionState') == 'unknown' or event.get('executed') is None:
                out.write('/* #' + str(seq) + ' 条件执行状态未知，不视为已证实执行 */\n')
                rows += 1
                last = context = None
                continue
            if not event.get('executed'):
                continue
            content = source_lines.get(file, [])
            if not line_no or not 0 < line_no <= len(content):
                out.write('/* #' + str(seq) + f' PC=0x{event["pc"]:08x} 无配套源码 */\n')
                rows += 1
                last = None
                continue
            key = (function, file, line_no)
            if key == last:
                continue
            if (function, file) != context:
                out.write('/* ' + clean(function) + ' · ' + clean(base(file)) + ' */\n')
                context = (function, file)
            out.write(content[line_no - 1] + '\n')
            rows += 1
            last = key
    return rows
