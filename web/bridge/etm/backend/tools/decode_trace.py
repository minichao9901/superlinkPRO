#!/usr/bin/env python3
"""ETMv3/ETMv4 包 → OpenCSD 指令范围 → Thumb 指令 → DWARF 源码位置。"""
import argparse
import bisect
import collections
import hashlib
import io
import json
import os
import subprocess
import threading
import time
from pathlib import Path

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_MCLASS
from elftools.elf.elffile import ELFFile
from trace_port import extract
from tpiu_demux import demux
from flow_analysis import enrich, byte_accounting

ROOT = Path(__file__).resolve().parents[1]
# Read from this H7B0 through SWD; imported records should carry their own snapshot.
DEFAULT_CONFIG = dict(idr0=0x080006E1, idr1=0x4100F401, idr2=4, idr8=1,
                      idr9=0, idr10=0, idr11=0, idr12=1, idr13=0, config=1, traceid=2)
REG_ORDER = list(DEFAULT_CONFIG)
V3_REG_ORDER = ['idr', 'ctrl', 'ccer', 'traceid']


def decoder_config(config=None):
    config = dict(config or {})
    protocol = config.get('protocol', 'ETMv4')
    if protocol == 'ETMv3':
        missing = set(V3_REG_ORDER) - set(config)
        if missing:
            raise ValueError('ETMv3 配置缺少寄存器：' + ', '.join(sorted(missing)))
        if (int(config['idr']) >> 8) & 15 != 2:
            raise ValueError('ETM IDR 架构不是 v3。')
    elif protocol == 'ETMv4':
        config = {**DEFAULT_CONFIG, **config}
        if (int(config['idr1']) >> 8) & 15 != 4:
            raise ValueError('ETM 架构不是 v4。')
    else:
        raise ValueError('不支持的 Trace 协议：' + str(protocol))
    if not 1 <= int(config['traceid']) <= 0x6F:
        raise ValueError('无效 trace ID。')
    for key in V3_REG_ORDER if protocol=='ETMv3' else REG_ORDER:
        if isinstance(config[key],bool) or not isinstance(config[key],int) or not 0<=config[key]<=0xffffffff:
            raise ValueError('ETM 寄存器必须是 32 位无符号整数：'+key)
    return config


def string(value):
    return value.decode('utf-8', 'replace') if isinstance(value, bytes) else str(value)


_IMAGE_CACHE = collections.OrderedDict()
_IMAGE_CACHE_LOCK = threading.RLock()
_IMAGE_CACHE_BYTES = 64 * 1024 * 1024
_IMAGE_CACHE_ENTRIES = 4


class Image:
    def __init__(self, path, sources=None):
        started = time.perf_counter()
        self.path = Path(path)
        self.bytes = self.path.read_bytes()
        digest = hashlib.sha256(self.bytes).hexdigest()
        read_seconds = time.perf_counter() - started
        self.elf = ELFFile(io.BytesIO(self.bytes))
        if self.elf['e_machine'] != 'EM_ARM' or not self.elf.little_endian:
            raise ValueError('初版需要小端 ARM ELF。')
        self.source_text = dict(sources or {})
        enabled = os.environ.get('ETM_PREPARE_CACHE', '1').strip().lower() not in ('0', 'false', 'off')
        hit = False
        parse_seconds = 0.0
        wait_started = time.perf_counter()
        with _IMAGE_CACHE_LOCK:
            wait_seconds = time.perf_counter() - wait_started
            cached = _IMAGE_CACHE.get(digest) if enabled else None
            phase = time.perf_counter()
            if cached is None:
                self._parse()
                parse_seconds = time.perf_counter() - phase
                # Cache only immutable values. ELF parser streams and source
                # overlays belong to each Image, including on cache hits.
                values = (tuple(self.segments), tuple(self.functions), tuple(self.starts),
                          tuple(self.lines.items()), tuple(self.line_starts))
                weight = len(self.bytes) * 2 + len(self.lines) * 256 + len(self.functions) * 128
                if enabled and weight <= _IMAGE_CACHE_BYTES:
                    _IMAGE_CACHE[digest] = (weight, values)
                    while (len(_IMAGE_CACHE) > _IMAGE_CACHE_ENTRIES
                           or sum(value[0] for value in _IMAGE_CACHE.values()) > _IMAGE_CACHE_BYTES):
                        _IMAGE_CACHE.popitem(last=False)
            else:
                hit = True
                _IMAGE_CACHE.move_to_end(digest)
                _, values = cached
                segments, functions, starts, lines, line_starts = values
                self.segments, self.functions, self.starts = list(segments), list(functions), list(starts)
                self.lines, self.line_starts = dict(lines), list(line_starts)
        self.preparation = dict(cacheHit=hit, sha256=digest, readAndHashSeconds=read_seconds,
                                parseSeconds=parse_seconds, waitSeconds=wait_seconds,
                                totalSeconds=time.perf_counter() - started)

    def _parse(self):
        self.segments = [(s['p_vaddr'], s.data()) for s in self.elf.iter_segments()
                         if s['p_type'] == 'PT_LOAD' and s['p_filesz']]
        self.functions = []
        symbols = self.elf.get_section_by_name('.symtab')
        if symbols:
            self.functions = sorted((s['st_value'] & ~1, s['st_size'], s.name)
                                    for s in symbols.iter_symbols()
                                    if s['st_info']['type'] == 'STT_FUNC' and s['st_size'])
        self.starts = [a for a, _, _ in self.functions]
        self.lines = {}
        if self.elf.has_dwarf_info():
            dwarf = self.elf.get_dwarf_info()
            for cu in dwarf.iter_CUs():
                program = dwarf.line_program_for_CU(cu)
                if not program:
                    continue
                attr = cu.get_top_DIE().attributes.get('DW_AT_comp_dir')
                directory = string(attr.value) if attr else ''
                files = {}
                for index, entry in enumerate(program['file_entry'], 1):
                    d = directory
                    if entry.dir_index:
                        d = string(program['include_directory'][entry.dir_index - 1])
                        if not Path(d).is_absolute():
                            d = str(Path(directory) / d)
                    files[index] = (Path(d) / string(entry.name)).as_posix()
                previous = None
                for entry in program.get_entries():
                    state = entry.state
                    if not state:
                        continue
                    if previous and state.address > previous.address and previous.line:
                        self.lines[previous.address] = (state.address, files.get(previous.file, ''), previous.line)
                    previous = None if state.end_sequence else state
        self.line_starts = sorted(self.lines)

    def read(self, address, size):
        for base, data in self.segments:
            if base <= address < base + len(data):
                return data[address-base:address-base+size]
        return b''

    def location(self, address):
        index = bisect.bisect_right(self.starts, address) - 1
        function = ''
        if index >= 0:
            start, size, name = self.functions[index]
            if address < start + size:
                function = name
        index = bisect.bisect_right(self.line_starts, address) - 1
        file, line = '', None
        if index >= 0:
            start = self.line_starts[index]
            end, candidate, number = self.lines[start]
            if address < end:
                file, line = candidate, number
        return function, file, line

    def source(self, file):
        if file in self.source_text:
            return self.source_text[file]
        # Local DWARF paths must remain inside this project's firmware tree.
        path = Path(file).resolve()
        allowed = (ROOT / 'firmware').resolve()
        if path.is_relative_to(allowed) and path.is_file():
            self.source_text[file] = path.read_text(encoding='utf-8-sig', errors='replace')
            return self.source_text[file]
        matches = [text for name, text in self.source_text.items()
                   if file.replace('\\', '/').endswith('/' + name.replace('\\', '/'))]
        if len(matches) == 1:
            return matches[0]
        return ''


def decode(id_stream, elf, workdir, config=None, sources=None, max_events=2000000):
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    config = decoder_config(config)
    protocol = config.get('protocol', 'ETMv4')
    image = Image(elf, sources)
    native = ROOT / 'build-native/etm_decode.exe'
    if not native.exists():
        raise ValueError('缺少 OpenCSD 解码器，请先运行 build-decoder.ps1。')
    input_file = workdir / 'etm.bin'
    input_file.write_bytes(id_stream)
    order = V3_REG_ORDER if protocol == 'ETMv3' else REG_ORDER
    manifest = ('ETMv3\n' if protocol == 'ETMv3' else '')
    manifest += '\n'.join(hex(int(config[k])) for k in order) + '\n'
    for index, (base, data) in enumerate(image.segments):
        file = workdir / f'image-{index}.bin'
        file.write_bytes(data)
        manifest += f'{hex(base)} {file.resolve()}\n'
    manifest_file = workdir / 'native-config.txt'
    manifest_file.write_text(manifest, encoding='utf-8')
    process = subprocess.run([str(native), str(input_file), str(manifest_file)],
                             capture_output=True, timeout=120)
    (workdir / 'native.jsonl').write_bytes(process.stdout)
    (workdir / 'native.log').write_bytes(process.stderr)
    if process.returncode:
        raise ValueError('OpenCSD 解码失败：' + process.stderr.decode('utf-8', 'replace')[-3000:])
    records = []
    for line in process.stdout.decode('utf-8', 'replace').splitlines():
        if line.startswith('{'):
            records.append(json.loads(line))
    packets = [r for r in records if r['kind'] == 'packet']
    elements = [r for r in records if r['kind'] == 'element']
    status = next((r for r in records if r['kind'] == 'status'), {})
    disassembler = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    cache, events, counts, gaps = {}, [], collections.Counter(), []
    # 周期计数：OpenCSD 把 `OCSD_GEN_TRC_ELEM_CYCLE_COUNT`（type 12）作为**独立元素**抛出，
    # 语义是"自上次周期计数以来、**前一个指令区间**消耗的周期数"（见 trc_gen_elem_types.h）。
    # 所以这里记住上一条区间指令，等周期元素到来时**回填**到它身上：
    # 这样"指令事件的 cycleCount 求和 = 总周期"，可直接按函数归因（flow_analysis 的周期聚合）。
    last_range_tail = None
    last_range_size = 0
    for element in elements:
        if element['type'] == 12:
            cycles = int(element.get('cycleCount') or 0)
            if cycles and last_range_tail is not None:
                last_range_tail['cycleCount'] = int(last_range_tail.get('cycleCount') or 0) + cycles
                last_range_tail['cycleSpan'] = last_range_tail.get('cycleSpan') or last_range_size
            continue
        if element['type'] == 5:
            start, end = element['start'], element['end']
            key = (start, end)
            if key not in cache:
                cache[key] = list(disassembler.disasm(image.read(start, end-start), start))
            instructions = cache[key]
            if len(instructions) != element['payload'] or not instructions or instructions[-1].address + instructions[-1].size != end:
                raise ValueError(f'机器码范围核对失败：0x{start:08x}..0x{end:08x}；OpenCSD={element["payload"]}，反汇编={len(instructions)}。')
            # 周期计数挂在**区间元素**上（不是每条指令）：只记在区间最后一条指令，
            # 这样事件求和 = 总周期，又能归因到函数（见 docs 里的周期计数用途说明）。
            range_cycles = int(element.get('cycleCount') or 0)
            last_index = len(instructions) - 1
            for index, ins in enumerate(instructions):
                function, file, line = image.location(ins.address)
                executed = bool(element['lastExecuted']) if index == last_index else True
                event = dict(kind='instruction', seq=len(events), offset=element['offset'],
                             pc=ins.address, size=ins.size, bytes=ins.bytes.hex(),
                             mnemonic=ins.mnemonic, operands=ins.op_str, executed=executed,
                             function=function, file=file, line=line,
                             cycleCount=(range_cycles if index == last_index else 0),
                             cycleSpan=(len(instructions) if index == last_index and range_cycles else 0))
                events.append(event)
                last_range_tail = event
                last_range_size = len(instructions)
                if executed:
                    counts[function or '(未知函数)'] += 1
                if max_events and len(events) > max_events:
                    raise ValueError(f'已超过 --max-events={max_events}；为保护本机内存，请改用流式解码。')
        elif element['type'] in (1, 2, 3, 6, 7, 8, 9, 10):
            event = dict(kind='event', seq=len(events), type=element['type'],
                         offset=element['offset'], text=element['text'], payload=element['payload'],
                         start=element['start'], end=element['end'], flags=element['flags'])
            events.append(event)
            if element['type'] in (1, 6, 7, 8) or (element['type'] == 2 and element['payload']):
                gaps.append(event)
    source_files = {e['file'] for e in events if e['kind'] == 'instruction' and e['file']}
    source_data = {f: image.source(f) for f in sorted(source_files)}
    instruction_count = sum(e['kind'] == 'instruction' for e in events)
    if not instruction_count:
        raise ValueError('没有恢复到可信指令范围；原始采集和 native 包记录已保留。请检查 CPU 是否继续、ETM 同步、Trace ID 与匹配 ELF。')
    faults = [p for p in packets if any(term in p['text'].upper() for term in ('OVERFLOW', 'BAD_SEQUENCE', 'BAD_TRACEMODE', 'RESERVED'))]
    result = dict(version=1, protocol=protocol, config=config,inputBytes=len(id_stream),
                elf=dict(name=Path(elf).name, sha256=hashlib.sha256(image.bytes).hexdigest()),
                packets=packets, elements=elements, events=events, sources=source_data,
                summary=dict(packets=len(packets), ranges=sum(e['type'] == 5 for e in elements),
                             instructions=instruction_count,
                             executed=sum(e['kind'] == 'instruction' and e['executed'] for e in events),
                             sourceMapped=sum(e['kind'] == 'instruction' and bool(e['line']) for e in events),
                             exceptions=sum(e['type'] == 9 for e in elements),
                             gaps=len(gaps), packetIssues=len(faults), functions=dict(counts)),
                status=status, timeBasis='execution order; no per-instruction timestamp')
    return enrich(result, image)


def decode_capture(path, elf, workdir, config=None, sources=None, fraction=.25, clock=0, data=None, glitch_ns=0):
    path, workdir = Path(path), Path(workdir)
    config = decoder_config(config)
    physical = None
    if path.suffix.lower() in ('.csv', '.kvdat'):
        physical, candidates = extract(path, clock, data or [1, 2, 3, 4], fraction, glitch_ns)
        valid = []
        failures=[]
        workdir.mkdir(parents=True,exist_ok=True)
        (workdir/'physical.json').write_text(json.dumps(physical,ensure_ascii=False),encoding='utf8')
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
            raise ValueError(f'不能唯一确定 TPIU 字节相位：{len(valid)} 个候选；请检查通道和取样位置。'+ (' '+failures[-1]['error'] if failures else ''))
        phase, frames, streams = valid[0]
        raw = candidates[phase]
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / 'tpiu.bin').write_bytes(raw)
        payload = streams[int(config['traceid'])]
        framing = {**frames, 'phase': phase}
    else:
        # .bin import is specifically an already-demultiplexed ETM source stream.
        payload = path.read_bytes()
        framing = {'mode': 'ETM source stream; TPIU already removed'}
    result = decode(payload, elf, workdir, config, sources)
    result['physical'] = physical
    result['framing'] = framing
    result['capture'] = {'name': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    result['quality'] = byte_accounting(result)
    (workdir / 'decoded.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    return result


def export_source(result, simplified=False):
    if simplified:
        from source_export import simplified_c
        return simplified_c(result)
    lines = [f"/* {result.get('protocol', 'ETMv4')} 源码执行流记录：按实际恢复顺序排列，不是可编译程序。 */"]
    if result.get('quality',{}).get('unprocessedBytes'):
        lines.append(f"/* 另有 {result['quality']['unprocessedBytes']} 字节 ETM 输入未处理；缺失的执行路径保持未知。 */")
    last = None
    source_lines = {file: text.splitlines() for file, text in result['sources'].items()}
    for event in result['events']:
        if event['kind'] != 'instruction':
            if event['type'] in (1, 2, 3, 6, 7, 8, 9, 10):
                lines.append('/* ' + event['text'].replace('*/', '* /') + ' */')
                last = None
            continue
        if event.get('executionState') == 'unknown':
            lines.append(f"/* #{event['seq']} PC=0x{event['pc']:08x} 条件执行状态未知，不视为已证实执行 */")
            last = None
            continue
        if not event['executed']:
            continue
        key = (event['function'], event['file'], event['line'])
        content = source_lines.get(event['file'], [])
        number = event['line']
        if number and 0 < number <= len(content):
            if simplified and key == last:
                continue
            label = f"{event['function']} · {event['file']}:{number}"
            lines.append('/* ' + (label if simplified else f"#{event['seq']} PC=0x{event['pc']:08x} {label}").replace('*/', '* /') + ' */')
            lines.append(content[number-1])
            last = key
        else:
            lines.append(f"/* #{event['seq']} PC=0x{event['pc']:08x} 无配套源码 */")
            last = None
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--elf', type=Path, default=ROOT / 'firmware/stm32h7b0_etm_board_v1/fw.elf')
    parser.add_argument('--out', type=Path, default=ROOT / 'decoded/first')
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    result = decode_capture(args.input, args.elf, args.out,
                            json.loads(args.config.read_text()) if args.config else None)
    (args.out / 'execution.c').write_text(export_source(result), encoding='utf-8')
    (args.out / 'simplified.c').write_text(export_source(result, True), encoding='utf-8')
    print(json.dumps(result['summary'], indent=2, ensure_ascii=False))
    print(result['status'])


if __name__ == '__main__':
    main()
