#!/usr/bin/env python3
"""合成 1 s 规模事件流（非真机）：单元 spool + 流式 seq/offset 重映射 + 显式会话复位标记。

方案依据：``docs/etm-1s-native-pipeline-plan-20261019.md`` §9.1（固定输入）、§3.2（拼接多个
独立 capture 只能用于带显式会话复位的压力测试）、§8 P6。

本模块的产出**不是连续真机 trace**：它把一份 10 ms / 200 ms 事件流按段重复拼接，段与段之间
插入显式会话复位标记；时间、电平、调用栈、周期都不保证跨段连续。它只用于 store 构建、
查询与吞吐的规模化压力测试，不能用于 1 s 硬件验收或性能达标宣称。

数据流
------
1. **单元 spool**：把来源记录的事件流按固定字段顺序写成 ``unit.events.jsonl``（每行一个
   事件，紧凑 JSON）。单元只读一次；来源可以是 ``decoded.json`` 内联 events 或
   ``events.jsonl``（streamed schema 2/3）。
2. **生成**：每段把 spool 逐行读出，只替换逻辑 ``seq`` 与 trace ``offset`` 两个数字
   （字节级替换，不重新做 JSON 解析），段边界插入复位标记。整段 1 s 流始终按行流式处理，
   不整体驻留内存。
3. **消费**：``iter_event_lines()`` / ``iter_event_dicts()`` 是生成器 API，融合写出器可直接
   迭代；``write_event_stream()`` 落单个 ``events.jsonl``（trace-store 参考写入器可直接消费的
   streamed schema 2 布局），``write_event_shards()`` 落 gzip 分片 + 索引。

复位标记
--------
标记默认写成 trace-store 可校验的边界事件（字段集合与 ``EVENT_FIELDS`` 一致）：
``kind=event``、``type=2``（TRACE_ON，含义=解码器重新开始）、``payload=0``、``offset=段首``，
``text`` 以 ``[synthetic-session-reset]`` 开头。它不是真机包：真实 TRACE_ON 由 ETM 流产生，
这里的标记只是人工插入的合成边界。``iter_event_dicts(marker_style='native')`` 可得到
``kind='session-reset'`` 的显式结构（带 segmentIndex/offset/说明），供需要区分标记的消费者使用。
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

SCHEMA = 'etm-synthetic-events/1'
MARKER_PREFIX = '[synthetic-session-reset]'
MARKER_REASON = ('合成段边界：显式会话复位。前后两段是同一独立 capture 的重复拷贝，'
                 '时间/电平/调用栈不保证连续')
MARKER_TEXT = MARKER_PREFIX + ' ' + MARKER_REASON

# 与 etm/tools/trace_store.py 的 INSTRUCTION_VARIANTS / EVENT_FIELDS 保持一致（这里只做
# 合成侧的自检；权威校验仍在 store 写入器里）。字段顺序即序列化顺序。
INSTRUCTION_BASE = ('kind', 'seq', 'offset', 'pc', 'size', 'bytes', 'mnemonic', 'operands',
                    'executed', 'function', 'file', 'line')
INSTRUCTION_CYCLES = ('cycleCount', 'cycleSpan')
INSTRUCTION_TAIL = ('flow', 'conditional', 'executionState', 'executionReason', 'callPath',
                    'cycles')
INSTRUCTION_VARIANTS = (
    INSTRUCTION_BASE + INSTRUCTION_CYCLES + INSTRUCTION_TAIL,
    INSTRUCTION_BASE + INSTRUCTION_TAIL,
    INSTRUCTION_BASE + ('flow', 'conditional', 'executionState', 'executionReason', 'callPath'),
)
EVENT_ORDER = ('kind', 'seq', 'type', 'offset', 'text', 'payload', 'start', 'end', 'flags')

_MAX_INLINE_DECODED = 512 << 20      # 内联 events 的 decoded.json 上限；再大就会吃爆内存
_KIND_INSTRUCTION = b'{"kind":"instruction"'


class SyntheticError(ValueError):
    """合成数据构造失败（参数、来源、校验）。"""


# ---------------------------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------------------------

def sha256_file(path, chunk=1 << 20):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(chunk), b''):
            digest.update(block)
    return digest.hexdigest()


def file_fingerprint(path):
    path = Path(path)
    stat = path.stat()
    return dict(path=str(path), bytes=stat.st_size, sha256=sha256_file(path))


def peak_rss_bytes():
    """当前进程峰值 RSS（Windows 用 psutil/psapi，其它平台退回 resource）。"""
    try:
        import psutil
        info = psutil.Process().memory_info()
        return int(getattr(info, 'peak_wset', None) or info.rss)
    except Exception:
        pass
    try:
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                        ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                        ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t),
                        ('PrivateUsage', ctypes.c_size_t)]

        counters = _PMC()
        counters.cb = ctypes.sizeof(counters)
        process = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
            return int(counters.PeakWorkingSetSize)
    except Exception:
        pass
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return int(usage.ru_maxrss * (1024 if sys.platform != 'darwin' else 1))
    except Exception:
        return 0


def _num(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _require_int(value, what, minimum=0):
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise SyntheticError('%s 必须是不小于 %d 的整数：%r' % (what, minimum, value))
    return value


def _check_positive_segments(segments):
    if not isinstance(segments, int) or isinstance(segments, bool) or segments < 1:
        raise SyntheticError('--segments 必须是正整数，收到 %r' % (segments,))
    if segments > 100000:
        raise SyntheticError('--segments=%d 过大（上限 100000 段）' % segments)
    return segments


# ---------------------------------------------------------------------------------------------
# 事件规范化与来源识别
# ---------------------------------------------------------------------------------------------

def canonical_event(event):
    """校验并返回按固定字段顺序重排的事件字典；字段集合不受支持时明确拒绝。"""
    if not isinstance(event, dict):
        raise SyntheticError('事件不是 JSON 对象：%r' % (event,))
    kind = event.get('kind')
    if kind == 'instruction':
        for variant in INSTRUCTION_VARIANTS:
            if set(event) == set(variant):
                ordered = {key: event[key] for key in variant}
                break
        else:
            raise SyntheticError('指令事件字段集合不受支持：多出 %s，缺少 %s（seq=%s）'
                                 % (sorted(set(event) - set(INSTRUCTION_BASE + INSTRUCTION_CYCLES
                                                           + INSTRUCTION_TAIL)),
                                    sorted(set(INSTRUCTION_BASE) - set(event)),
                                    event.get('seq')))
    elif kind == 'event':
        if set(event) != set(EVENT_ORDER):
            raise SyntheticError('边界事件字段集合不受支持：多出 %s，缺少 %s（seq=%s）'
                                 % (sorted(set(event) - set(EVENT_ORDER)),
                                    sorted(set(EVENT_ORDER) - set(event)), event.get('seq')))
        ordered = {key: event[key] for key in EVENT_ORDER}
    else:
        raise SyntheticError('未知事件 kind：%r（seq=%s）' % (kind, event.get('seq')))
    _require_int(ordered['seq'], '事件 seq')
    _require_int(ordered['offset'], '事件 offset')
    if ordered['kind'] == 'event':
        _require_int(ordered['type'], '事件 type')
    return ordered


def canonical_line(event):
    """单条事件 → 一行紧凑 JSON（不含换行）。"""
    return _num(canonical_event(event)).encode('utf-8')


def detect_events_source(record_dir, prefer='auto'):
    """返回 ('jsonl'|'decoded', 路径)；prefer 可强制 jsonl/decoded，auto 优先 events.jsonl。"""
    record_dir = Path(record_dir)
    events = record_dir / 'events.jsonl'
    decoded = record_dir / 'decoded.json'
    if prefer not in ('auto', 'jsonl', 'decoded'):
        raise SyntheticError('未知的事件来源选择：%r' % (prefer,))
    if prefer == 'jsonl':
        if not events.is_file():
            raise SyntheticError('记录目录没有 events.jsonl：%s' % record_dir)
        return 'jsonl', events
    if prefer == 'decoded':
        if not decoded.is_file():
            raise SyntheticError('记录目录没有 decoded.json：%s' % record_dir)
        return 'decoded', decoded
    if events.is_file():
        return 'jsonl', events
    if decoded.is_file():
        return 'decoded', decoded
    raise SyntheticError('记录目录既没有 events.jsonl 也没有 decoded.json：%s' % record_dir)


def _load_decoded(path):
    path = Path(path)
    size = path.stat().st_size
    if size > _MAX_INLINE_DECODED:
        raise SyntheticError(
            'decoded.json 有 %.1f MiB，超过内联读取上限 %.0f MiB；请改用带 events.jsonl 的 '
            '200 ms 记录作为单元' % (size / 2 ** 20, _MAX_INLINE_DECODED / 2 ** 20))
    try:
        with open(path, 'rb') as handle:
            return json.loads(handle.read().decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SyntheticError('decoded.json 不是合法 UTF-8 JSON：%s' % error)


def _iter_jsonl(path):
    path = Path(path)
    with open(path, encoding='utf-8') as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                raise SyntheticError('events.jsonl 第 %d 行为空行，拒绝静默跳过' % number)
            try:
                yield json.loads(line)
            except json.JSONDecodeError as error:
                raise SyntheticError('events.jsonl 第 %d 行不是合法 JSON：%s' % (number, error))


def decoded_meta(decoded, *, keep=('capture', 'config', 'protocol', 'version', 'status',
                                   'timeBasis', 'targetSnapshot', 'captureScope', 'captureStart',
                                   'laFilter', 'elf', 'analysis', 'sources', 'timing')):
    """从来源 decoded.json 提取小体积元数据（去掉 events/elements/packets 大数组）。"""
    meta = {key: value for key, value in decoded.items()
            if key not in ('events', 'elements', 'packets')}
    meta.setdefault('protocol', decoded.get('protocol'))
    return meta


def _write_json(path, value, *, indent=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=indent, sort_keys=indent is not None)
    path.write_text(data + '\n', encoding='utf-8')


# ---------------------------------------------------------------------------------------------
# 单元 spool
# ---------------------------------------------------------------------------------------------

def unit_paths(out_dir):
    base = Path(out_dir) / 'unit'
    return dict(dir=str(base), spool=str(base / 'unit.events.jsonl'),
                meta=str(base / 'unit.json'), decodedMeta=str(base / 'decoded-meta.json'))


def build_unit_spool(record_dir, spool_path, *, source='auto', meta_path=None,
                     decoded_meta_path=None, progress=None):
    """把来源记录的事件流写成 canonical spool，返回单元描述字典。

    spool 每行一个事件，字段顺序固定；生成 1 s 流时只对 ``seq``/``offset`` 两个数字做字节替换，
    因此要求来源逻辑 seq 从 0 连续（既有记录均满足）。
    """
    record_dir = Path(record_dir)
    if not record_dir.is_dir():
        raise SyntheticError('记录目录不存在：%s' % record_dir)
    kind, path = detect_events_source(record_dir, source)
    spool_path = Path(spool_path)
    spool_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path = Path(meta_path) if meta_path else spool_path.parent / 'unit.json'
    decoded_meta_path = Path(decoded_meta_path) if decoded_meta_path \
        else spool_path.parent / 'decoded-meta.json'

    source_meta = None
    if kind == 'decoded':
        decoded = _load_decoded(path)
        events = decoded.get('events')
        if not isinstance(events, list):
            raise SyntheticError('decoded.json 没有内联 events 数组，也不是 events.jsonl 记录')
        if not events:
            raise SyntheticError('decoded.json 的 events 为空')
        events_iter = iter(events)
        source_meta = decoded_meta(decoded)
        del events, decoded
    else:
        events_iter = _iter_jsonl(path)
        decoded_path = record_dir / 'decoded.json'
        if decoded_path.is_file():
            source_meta = decoded_meta(_load_decoded(decoded_path))

    counts = dict(events=0, instructions=0, boundaryEvents=0)
    type_counts = {}
    min_offset = None
    max_offset = None
    first_instruction_seq = None
    first_instruction_offset = None
    digest = hashlib.sha256()
    written = 0
    started = time.time()
    with spool_path.open('wb') as out:
        for index, event in enumerate(events_iter):
            ordered = canonical_event(event)
            if ordered['seq'] != index:
                raise SyntheticError('来源事件 seq 不连续：第 %d 条 seq=%r（合成器要求从 0 连续）'
                                     % (index + 1, ordered['seq']))
            if ordered['kind'] == 'instruction':
                counts['instructions'] += 1
                if first_instruction_seq is None:
                    first_instruction_seq = index
                    first_instruction_offset = ordered['offset']
            else:
                counts['boundaryEvents'] += 1
                key = str(ordered['type'])
                type_counts[key] = type_counts.get(key, 0) + 1
            offset = ordered['offset']
            min_offset = offset if min_offset is None else min(min_offset, offset)
            max_offset = offset if max_offset is None else max(max_offset, offset)
            line = _num(ordered).encode('utf-8') + b'\n'
            if line.count(b'"seq":') != 1 or line.count(b'"offset":') != 1:
                raise SyntheticError('事件行里 seq/offset 字段不唯一，无法做字节级重映射：seq=%s'
                                     % ordered['seq'])
            out.write(line)
            digest.update(line)
            written += 1
            if progress is not None and written % 500000 == 0:
                progress('  单元 spool：已写 %d 条事件（指令 %d）'
                         % (written, counts['instructions']))
    counts['events'] = written
    if not counts['instructions']:
        raise SyntheticError('来源记录没有指令事件：%s' % record_dir)
    if not counts['boundaryEvents']:
        raise SyntheticError('来源记录没有边界事件：无法证明段边界语义')
    stat = spool_path.stat()
    source_fingerprint = file_fingerprint(path)
    if source_meta is not None:
        _write_json(decoded_meta_path, source_meta)
    unit = dict(
        schema=SCHEMA,
        record=str(record_dir.resolve()),
        eventsSource=kind,
        sourceFile=str(path.resolve()),
        sourceBytes=source_fingerprint['bytes'],
        sourceSha256=source_fingerprint['sha256'],
        spool=str(spool_path.resolve()),
        spoolBytes=stat.st_size,
        spoolSha256=digest.hexdigest(),
        decodedMeta=str(decoded_meta_path.resolve()) if source_meta is not None else None,
        events=counts['events'],
        instructions=counts['instructions'],
        boundaryEvents=counts['boundaryEvents'],
        eventTypes=dict(sorted(type_counts.items(), key=lambda item: int(item[0]))),
        seqFirst=0,
        seqLast=counts['events'] - 1,
        minOffset=min_offset,
        maxOffset=max_offset,
        offsetSpan=(max_offset or 0) + 1,
        firstInstructionSeq=first_instruction_seq,
        firstInstructionOffset=first_instruction_offset,
        elapsedSeconds=round(time.time() - started, 3),
        note='单元 spool 为来源记录事件流的 canonical 拷贝；不是真机 1 s trace',
    )
    _write_json(meta_path, unit, indent=2)
    return unit


def load_unit(spool_path=None, meta_path=None):
    """读取已落盘的单元描述；校验 spool 大小与 sha256（不复算整个 spool）。"""
    if meta_path is None:
        if spool_path is None:
            raise SyntheticError('load_unit 需要 spool_path 或 meta_path')
        meta_path = Path(spool_path).parent / 'unit.json'
    meta_path = Path(meta_path)
    if not meta_path.is_file():
        raise SyntheticError('单元元数据不存在：%s（先运行 --mode events 生成）' % meta_path)
    unit = json.loads(meta_path.read_text(encoding='utf-8'))
    spool = Path(unit.get('spool') or '')
    if not spool.is_file():
        raise SyntheticError('单元 spool 不存在：%s' % spool)
    stat = spool.stat()
    if stat.st_size != unit['spoolBytes']:
        raise SyntheticError('单元 spool 大小与元数据不符：%d≠%d'
                             % (stat.st_size, unit['spoolBytes']))
    if sha256_file(spool) != unit['spoolSha256']:
        raise SyntheticError('单元 spool sha256 与元数据不符：%s' % spool)
    return unit


def _validate_unit(unit):
    for key in ('spool', 'events', 'instructions', 'offsetSpan', 'spoolSha256'):
        if key not in unit:
            raise SyntheticError('单元描述缺少 %s' % key)
    if not Path(unit['spool']).is_file():
        raise SyntheticError('单元 spool 不存在：%s' % unit['spool'])
    _require_int(unit['events'], '单元事件数', 1)
    _require_int(unit['instructions'], '单元指令数', 1)
    _require_int(unit['offsetSpan'], '单元 offsetSpan', 1)
    return unit


# ---------------------------------------------------------------------------------------------
# 复位标记
# ---------------------------------------------------------------------------------------------

def marker_event(seq, offset, segment_index, segments, unit, *, style='event'):
    """段边界显式复位标记。

    ``style='event'``：trace-store 可校验的边界事件（等价于 TRACE_ON，payload=0）；
    ``style='native'``：``kind='session-reset'`` 的显式结构，只有生成器 API 会给出。
    """
    base = dict(segmentIndex=segment_index, segments=segments,
                sourceRecord=unit.get('record'), sourceOffset=offset,
                reason=MARKER_REASON, synthetic=True)
    if style == 'event':
        event = dict(kind='event', seq=seq, type=2, offset=offset, text=MARKER_TEXT,
                     payload=0, start=0, end=0, flags=0)
        return event
    if style == 'native':
        event = dict(kind='session-reset', seq=seq, offset=offset, text=MARKER_TEXT, **base)
        return event
    raise SyntheticError('未知的复位标记样式：%r' % (style,))


def expected_reset_table(unit, segments, offset_span=None):
    """按 seq 递增顺序给出全部段边界复位标记（与生成器一一对应）。"""
    segments = _check_positive_segments(segments)
    span = _resolve_span(unit, offset_span)
    table = []
    seq = 0
    for index in range(segments):
        if index:
            table.append(dict(segmentIndex=index, seq=seq, offset=index * span,
                              sampleOffset=index * span, text=MARKER_TEXT, type=2,
                              payload=0, reason=MARKER_REASON, synthetic=True))
            seq += 1
        seq += unit['events']
    return table


def _resolve_span(unit, offset_span):
    span = unit['offsetSpan'] if offset_span is None else offset_span
    _require_int(span, 'offsetSpan', 1)
    return span


def _markers_arg(markers):
    if markers not in ('emit', 'skip'):
        raise SyntheticError('--markers 只能是 emit 或 skip，收到 %r' % (markers,))
    return markers


def stream_stats(unit, segments, *, markers='emit', offset_span=None):
    segments = _check_positive_segments(segments)
    markers = _markers_arg(markers)
    span = _resolve_span(unit, offset_span)
    marker_count = segments - 1 if markers == 'emit' else 0
    total = segments * unit['events'] + marker_count
    return dict(segments=segments, unitEvents=unit['events'],
                unitInstructions=unit['instructions'], offsetSpan=span,
                markers=markers, resetCount=marker_count,
                totalEvents=total, totalInstructions=segments * unit['instructions'],
                lastSeq=total - 1, lastOffset=segments * span - 1,
                seqEnd=total - 1, sampleDepth=segments * span)


# ---------------------------------------------------------------------------------------------
# 流式生成（字节级 seq/offset 重映射）
# ---------------------------------------------------------------------------------------------

_SEQ_TOKEN = b'"seq":'
_OFFSET_TOKEN = b'"offset":'


def _split_remap(line):
    """把一行 canonical JSON 拆成 (前缀, seq, 中间, offset, 尾部)。"""
    seq_start = line.index(_SEQ_TOKEN) + len(_SEQ_TOKEN)
    seq_end = seq_start
    while line[seq_end:seq_end + 1].isdigit():
        seq_end += 1
    off_start = line.index(_OFFSET_TOKEN, seq_end) + len(_OFFSET_TOKEN)
    off_end = off_start
    while line[off_end:off_end + 1].isdigit():
        off_end += 1
    return (line[:seq_start], int(line[seq_start:seq_end]), line[seq_end:off_start],
            int(line[off_start:off_end]), line[off_end:])


def remap_line(line, seq, offset_base):
    """把一行 spool 事件的逻辑 seq 换成 ``seq``、offset 整体加 ``offset_base``。"""
    prefix, _source_seq, middle, source_offset, tail = _split_remap(line)
    return (prefix + str(seq).encode('ascii') + middle
            + str(source_offset + offset_base).encode('ascii') + tail)


def iter_event_lines(unit, segments, *, offset_span=None, markers='emit'):
    """生成器 API：逐行产出 1 s 规模事件流（含段边界复位标记），yield 的字节带换行。

    1 s 流不整体驻留内存：每次只保留 spool 的一行和当前段计数。
    """
    unit = _validate_unit(unit)
    segments = _check_positive_segments(segments)
    markers = _markers_arg(markers)
    span = _resolve_span(unit, offset_span)
    spool = Path(unit['spool'])
    seq = 0
    for index in range(segments):
        if index and markers == 'emit':
            marker = marker_event(seq, index * span, index, segments, unit)
            yield _num(marker).encode('utf-8') + b'\n'
            seq += 1
        base = index * span
        with spool.open('rb') as handle:
            for line in handle:
                yield remap_line(line, seq, base)
                seq += 1


def iter_event_dicts(unit, segments, *, offset_span=None, markers='emit', marker_style='event'):
    """生成器 API：逐条产出事件字典（供融合写出器直接消费）。

    ``marker_style='native'`` 时复位标记是 ``kind='session-reset'``（带 segmentIndex），
    消费者必须显式处理；默认 ``'event'`` 是 store 可校验的边界事件。
    """
    unit = _validate_unit(unit)
    segments = _check_positive_segments(segments)
    markers = _markers_arg(markers)
    span = _resolve_span(unit, offset_span)
    if marker_style not in ('event', 'native'):
        raise SyntheticError('未知的复位标记样式：%r' % (marker_style,))
    spool = Path(unit['spool'])
    seq = 0
    for index in range(segments):
        if index and markers == 'emit':
            yield marker_event(seq, index * span, index, segments, unit, style=marker_style)
            seq += 1
        base = index * span
        with spool.open('rb') as handle:
            for line in handle:
                event = json.loads(line)
                event['seq'] = seq
                event['offset'] = event['offset'] + base
                yield event
                seq += 1


def iter_batches(unit, segments, batch=8192, *, offset_span=None, markers='emit',
                 marker_style='event'):
    """生成器 API：按批产出事件列表（减少 Python 调用开销）。"""
    if not isinstance(batch, int) or batch < 1:
        raise SyntheticError('batch 必须是正整数')
    chunk = []
    for event in iter_event_dicts(unit, segments, offset_span=offset_span, markers=markers,
                                  marker_style=marker_style):
        chunk.append(event)
        if len(chunk) >= batch:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def write_event_stream(unit, segments, out_path, *, offset_span=None, markers='emit',
                       progress=None):
    """把 1 s 规模事件流写成单个 events.jsonl（streamed schema 2 可直接消费）。"""
    unit = _validate_unit(unit)
    segments = _check_positive_segments(segments)
    markers = _markers_arg(markers)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    lines = 0
    started = time.time()
    with out_path.open('wb') as out:
        for line in iter_event_lines(unit, segments, offset_span=offset_span, markers=markers):
            out.write(line)
            digest.update(line)
            lines += 1
            if progress is not None and lines % 2000000 == 0:
                progress('  事件流：已写 %d 行（%.2f s）' % (lines, time.time() - started))
    stat = out_path.stat()
    return dict(path=str(out_path.resolve()), bytes=stat.st_size, sha256=digest.hexdigest(),
                lines=lines, mtimeNs=stat.st_mtime_ns,
                elapsedSeconds=round(time.time() - started, 3),
                peakRssBytes=peak_rss_bytes())


def write_event_shards(unit, segments, out_dir, *, shard_events=2000000, offset_span=None,
                       markers='emit', gzip_enabled=True, progress=None):
    """把 1 s 规模事件流写成 gzip 分片 + 索引（避免单个巨大 events.jsonl）。

    压缩用固定 mtime=0，分片字节可复现；索引记录每片的行数、事件数、seq/offset 区间与 sha256。
    """
    import gzip

    unit = _validate_unit(unit)
    segments = _check_positive_segments(segments)
    markers = _markers_arg(markers)
    _require_int(shard_events, '--event-shard-events', 1)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in sorted(out_dir.glob('events-*.jsonl*')):
        stale.unlink()
    shards = []
    digest_all = hashlib.sha256()
    total = 0
    total_bytes = 0
    started = time.time()
    current = None
    pending = []      # 每行 (bytes, kind)
    pending_events = 0
    pending_instr = 0
    first_seq = last_seq = None
    first_offset = last_offset = None

    def flush():
        nonlocal current, pending, pending_events, pending_instr, first_seq, last_seq, \
            first_offset, last_offset, total_bytes
        if not pending:
            return
        index = len(shards)
        name = 'events-%04d.jsonl%s' % (index, '.gz' if gzip_enabled else '')
        path = out_dir / name
        data = b''.join(line for line, _kind in pending)
        if gzip_enabled:
            with open(path, 'wb') as raw:
                with gzip.GzipFile(fileobj=raw, mode='wb', compresslevel=6, mtime=0,
                                   filename='') as gz:
                    gz.write(data)
        else:
            path.write_bytes(data)
        shard = dict(index=index, name=name, path=str(path.resolve()), bytes=path.stat().st_size,
                     uncompressedBytes=len(data), lines=len(pending), sha256=sha256_file(path),
                     firstSeq=first_seq, lastSeq=last_seq, firstOffset=first_offset,
                     lastOffset=last_offset, instructions=pending_instr,
                     events=pending_events)
        shards.append(shard)
        total_bytes += shard['bytes']
        pending = []
        pending_events = 0
        pending_instr = 0
        first_seq = last_seq = None
        first_offset = last_offset = None

    for line in iter_event_lines(unit, segments, offset_span=offset_span, markers=markers):
        _prefix, seq, _mid, offset, _tail = _split_remap(line)
        is_instruction = line.startswith(_KIND_INSTRUCTION)
        pending.append((line, is_instruction))
        pending_events += 1
        pending_instr += 1 if is_instruction else 0
        digest_all.update(line)
        total += 1
        first_seq = seq if first_seq is None else first_seq
        first_offset = offset if first_offset is None else first_offset
        last_seq = seq
        last_offset = offset
        if len(pending) >= shard_events:
            flush()
        if progress is not None and total % 2000000 == 0:
            progress('  事件流：已写 %d 行（%.2f s）' % (total, time.time() - started))
    flush()
    stats = stream_stats(unit, segments, markers=markers, offset_span=offset_span)
    index = dict(schema=SCHEMA, kind='shards', compression='gzip' if gzip_enabled else 'none',
                 shardEvents=shard_events, shards=shards, totalLines=total,
                 totalBytes=total_bytes, sha256=digest_all.hexdigest(), stats=stats,
                 elapsedSeconds=round(time.time() - started, 3), peakRssBytes=peak_rss_bytes())
    _write_json(out_dir / 'events.index.json', index, indent=2)
    return index


def _iter_lines_from_source(source):
    """按顺序产出（未压缩的）事件行；source 可以是文件路径或分片索引。"""
    import gzip

    if isinstance(source, dict) and source.get('shards'):
        for shard in source['shards']:
            path = Path(shard['path'])
            with open(path, 'rb') as raw:
                if path.suffix == '.gz':
                    with gzip.GzipFile(fileobj=raw, mode='rb') as gz:
                        for line in gz:
                            yield line
                else:
                    for line in raw:
                        yield line
        return
    path = Path(source)
    if path.suffix == '.gz':
        with open(path, 'rb') as raw:
            with gzip.GzipFile(fileobj=raw, mode='rb') as gz:
                for line in gz:
                    yield line
        return
    with open(path, 'rb') as handle:
        for line in handle:
            yield line


def verify_event_stream(source, unit, segments, *, offset_span=None, markers='emit',
                        sample_every=1000, progress=None):
    """自检：条数/seq 连续性/段边界/offset 重映射/复位标记，并抽样做完整 JSON 比对。"""
    unit = _validate_unit(unit)
    segments = _check_positive_segments(segments)
    markers = _markers_arg(markers)
    span = _resolve_span(unit, offset_span)
    expected = stream_stats(unit, segments, markers=markers, offset_span=span)
    resets = {row['seq']: row for row in expected_reset_table(unit, segments, span)}
    lines = _iter_lines_from_source(source)
    digest = hashlib.sha256()
    total = 0
    instructions = 0
    samples = 0
    seen_resets = 0
    seq = 0
    per_segment = []
    started = time.time()
    spool = Path(unit['spool'])
    for index in range(segments):
        base = index * span
        if index and markers == 'emit':
            row = resets[seq]
            line = next(lines, None)
            if line is None:
                raise SyntheticError('生成流提前结束：缺少段 %d 的复位标记' % index)
            digest.update(line)
            text = json.loads(line)
            want = dict(kind='event', seq=seq, type=2, offset=base, text=MARKER_TEXT,
                        payload=0, start=0, end=0, flags=0)
            if text != want:
                raise SyntheticError('段 %d 复位标记与期望不符：%r' % (index, text))
            seen_resets += 1
            total += 1
            seq += 1
        stats = dict(index=index, sampleOffset=base, seqStart=seq, events=0,
                     instructions=0, reset=False)
        if index and markers == 'emit':
            stats['reset'] = True
            stats['resetSeq'] = resets[seq - 1]['seq']
        with spool.open('rb') as handle:
            for spool_line in handle:
                line = next(lines, None)
                if line is None:
                    raise SyntheticError('生成流提前结束：段 %d 第 %d 条事件缺失（seq=%d）'
                                         % (index, stats['events'] + 1, seq))
                digest.update(line)
                got_prefix, got_seq, got_mid, got_offset, got_tail = _split_remap(line)
                src_prefix, _src_seq, src_mid, src_offset, src_tail = _split_remap(spool_line)
                if got_seq != seq:
                    raise SyntheticError('seq 不连续：期望 %d，实际 %d（段 %d）'
                                         % (seq, got_seq, index))
                want_offset = src_offset + base
                if got_offset != want_offset:
                    raise SyntheticError('offset 重映射不符：seq=%d 期望 %d，实际 %d'
                                         % (seq, want_offset, got_offset))
                if got_prefix != src_prefix or got_mid != src_mid or got_tail != src_tail:
                    raise SyntheticError('seq/offset 之外的字节被改动：seq=%d' % seq)
                is_instruction = got_prefix.startswith(_KIND_INSTRUCTION)
                stats['events'] += 1
                stats['instructions'] += 1 if is_instruction else 0
                instructions += 1 if is_instruction else 0
                if total % sample_every == 0:
                    source_event = json.loads(spool_line)
                    expected_event = dict(source_event, seq=seq, offset=want_offset)
                    if json.loads(line) != expected_event:
                        raise SyntheticError('抽样 JSON 比对不符：seq=%d' % seq)
                    samples += 1
                total += 1
                seq += 1
            if stats['events'] != unit['events'] or stats['instructions'] != unit['instructions']:
                raise SyntheticError('段 %d 事件数不符：%r' % (index, stats))
            per_segment.append(stats)
            if progress is not None and (index + 1) % 10 == 0:
                progress('  自检：已校验 %d/%d 段（%d 行）' % (index + 1, segments, total))
    extra = next(lines, None)
    if extra is not None:
        raise SyntheticError('生成流末尾多出 %d 行（首行 %r）' % (1, extra[:120]))
    report = dict(ok=True, schema=SCHEMA, segments=segments, markers=markers,
                  offsetSpan=span, totalEvents=total, totalInstructions=instructions,
                  resetMarkers=seen_resets, lastSeq=total - 1,
                  expected=dict(totalEvents=expected['totalEvents'],
                                totalInstructions=expected['totalInstructions'],
                                resetCount=expected['resetCount'], lastSeq=expected['lastSeq']),
                  perSegment=per_segment, sampledJsonComparisons=samples,
                  sha256=digest.hexdigest(), elapsedSeconds=round(time.time() - started, 3),
                  peakRssBytes=peak_rss_bytes())
    if total != expected['totalEvents'] or instructions != expected['totalInstructions'] \
            or seen_resets != expected['resetCount']:
        raise SyntheticError('事件流总量与期望不符：%r vs %r' % (report, expected))
    return report


# ---------------------------------------------------------------------------------------------
# 合成记录目录（events.jsonl + 按段线性放大的 decoded.json）
# ---------------------------------------------------------------------------------------------

# 这些计数在重复拼接后按段线性放大；其余字段保持单元值，并在 synthetic.declaration 里标注。
SCALED_SUMMARY_KEYS = ('instructions', 'executed', 'unknownExecution', 'exceptions', 'gaps',
                       'ranges', 'packets', 'packetIssues', 'sourceMapped', 'notTaken')
SCALED_QUALITY_KEYS = ('inputBytes', 'processedBytes', 'accountedBytes', 'overlapBytes',
                       'unaccountedBytes', 'unsynchronizedBytes', 'unsynchronizedRecords',
                       'initialPhysicalBytes', 'unknownSourceBytes', 'trailingFrameBytes',
                       'trailingSyncBytes', 'validPackets', 'packetIssues', 'overflowPackets',
                       'unknownExecution')
RECORD_COPIES = ('firmware.elf', 'native-config.txt', 'session.json', 'sources.json')


def build_synthetic_decoded(unit, segments, *, markers='emit', offset_span=None):
    """生成合成记录目录的 decoded.json：streamed schema 2 + 按段线性放大的摘要计数。

    只有 ``summary``/``quality``/``inputBytes`` 里明确的计数类字段被放大；analysis、physical、
    capture 等仍保留单元值。``summary``/``quality`` 必须齐全，否则 trace-store 写入器会在
    逐项对账时拒绝（这也是本函数拒绝残缺来源的原因）。
    """
    _validate_unit(unit)
    segments = _check_positive_segments(segments)
    markers = _markers_arg(markers)
    stats = stream_stats(unit, segments, markers=markers, offset_span=offset_span)
    meta = {}
    if unit.get('decodedMeta'):
        meta = json.loads(Path(unit['decodedMeta']).read_text(encoding='utf-8'))
    summary = dict(meta.get('summary') or {})
    quality = dict(meta.get('quality') or {})
    for key in ('instructions', 'executed', 'exceptions', 'gaps'):
        if not isinstance(summary.get(key), int):
            raise SyntheticError('来源 decoded.json 的 summary.%s 缺失，无法生成可对账的合成记录'
                                 % key)
    for key in SCALED_SUMMARY_KEYS:
        if isinstance(summary.get(key), int):
            summary[key] = summary[key] * segments
    if isinstance(summary.get('functions'), dict):
        summary['functions'] = {name: count * segments
                                for name, count in summary['functions'].items()
                                if isinstance(count, int)}
    for key in SCALED_QUALITY_KEYS:
        if isinstance(quality.get(key), int):
            quality[key] = quality[key] * segments
    timing = quality.get('timing')
    if isinstance(timing, dict) and isinstance(timing.get('cycleElements'), int):
        timing = dict(timing)
        timing['cycleElements'] = timing['cycleElements'] * segments
        quality['timing'] = timing
    decoded = {key: value for key, value in meta.items()
               if key not in ('events', 'elements', 'packets')}
    decoded['summary'] = summary
    decoded['quality'] = quality
    if isinstance(meta.get('inputBytes'), int):
        decoded['inputBytes'] = meta['inputBytes'] * segments
    decoded['streamed'] = dict(schema=2, events=stats['totalEvents'], packets=0)
    decoded['synthetic'] = dict(
        schema=SCHEMA,
        declaration=dict(synthetic=True, continuousRealTrace=False,
                         notForHardwareAcceptance=True,
                         note=('本记录由既有 10 ms/200 ms 记录的单元事件流重复拼接而成；段边界'
                               '是显式会话复位，不是连续真机 trace，不能用于 1 s 硬件验收')),
        segments=segments, markers=markers, offsetSpan=stats['offsetSpan'],
        unit=dict(record=unit.get('record'), events=unit['events'],
                  instructions=unit['instructions'], sourceFile=unit.get('sourceFile'),
                  sourceSha256=unit.get('sourceSha256')),
        totals=dict(events=stats['totalEvents'], instructions=stats['totalInstructions'],
                    resetMarkers=stats['resetCount'], lastSeq=stats['lastSeq']),
        resetMarkers=expected_reset_table(unit, segments, stats['offsetSpan'])[:64],
        scaledSummaryKeys=list(SCALED_SUMMARY_KEYS),
        scaledQualityKeys=list(SCALED_QUALITY_KEYS),
        note=('summary/quality 的计数类字段按段线性放大；analysis/physical/capture 等字段仍为'
              '单元值，不能当作 1 s 真机统计。'),
    )
    return decoded


def write_events_record_dir(unit, segments, out_dir, *, events_path, markers='emit',
                            offset_span=None):
    """把合成事件流落成 trace-store 参考写入器可直接消费的 streamed 记录目录。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = Path(events_path)
    if not events_path.is_file():
        raise SyntheticError('events.jsonl 不存在：%s' % events_path)
    decoded = build_synthetic_decoded(unit, segments, markers=markers, offset_span=offset_span)
    decoded_path = out_dir / 'decoded.json'
    _write_json(decoded_path, decoded)
    copies = {}
    record_dir = Path(unit.get('record') or '')
    for name in RECORD_COPIES:
        source = record_dir / name
        if not source.is_file():
            continue
        target = out_dir / name
        target.write_bytes(source.read_bytes())
        copies[name] = file_fingerprint(target)
    meta = dict(schema=SCHEMA, kind='events-record', unit=unit, segments=segments,
                markers=markers, offsetSpan=stream_stats(unit, segments, markers=markers,
                                                         offset_span=offset_span)['offsetSpan'],
                events=file_fingerprint(events_path), decoded=file_fingerprint(decoded_path),
                copies=copies)
    _write_json(out_dir / 'synthetic-events.json', meta, indent=2)
    return dict(recordDir=str(out_dir.resolve()), events=meta['events'],
                decoded=meta['decoded'], copies=copies,
                streamedEvents=int(decoded['streamed']['events']))
