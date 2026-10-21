"""批量派生完整查询索引，热路径不按指令执行 Python CallTracker。

生产入口 build_full_index 消费 ETMFACT3：IDS/STATES 为逐指令数组，
RUNS 是每 4096 条指令一个的定位锚点（runSkip 定位到原始 RUN 内），
路径与事件保持稀疏。仍支持旧 v1/v2 文件及部分表原型入口。
所有输出复用 trace_store_index 的 v2 编码及查询语义。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import time
import zlib
import mmap
from pathlib import Path

import numpy as np

import trace_store
import trace_store_index


import struct
from pathlib import Path

MAGIC = b'ETMFACT1'
MAGIC_V2 = b'ETMFACT2'
MAGIC_V3 = b'ETMFACT3'
VERSION = 1
VERSION_V2 = 2
VERSION_V3 = 3
RUN_V2 = struct.Struct('<QQIIIB3sII')            # 40B（第 32B 起 runSkip/reserved）
EVENT_V2 = struct.Struct('<QQIBBhhhiII')        # 40B（textId @32）
PATH_RUN_V2 = struct.Struct('<QII')             # 16B startOrdinal/count/leafNode
NODE_V2 = struct.Struct('<iI')                  # 8B parent/label
HEADER = struct.Struct('<8sIIQQQQQQ')          # 64 B
RUN = struct.Struct('<QQIIIB3s')               # 32 B
CYCLE = struct.Struct('<QQQII')                # 32 B
EVENT = struct.Struct('<QBIQi1s')              # 24 B（读侧预留）
STATE_EXECUTED, STATE_NOT_TAKEN, STATE_UNKNOWN = 0, 1, 2
TAIL_TRUE, TAIL_FALSE, TAIL_UNKNOWN = 0, 1, 2


class FactsError(ValueError):
    """事实流文件格式/内容错误（明确拒绝，不猜测）。"""


def write_facts(path, runs, ids, states, cycles, events=()):
    """写 facts v1（只有前四段 + 可选 EVENTS；path 段写 0 计数）。

    runs: list[dict(startOrdinal,startSeq,count,blockOrdinal,recordOffset,tailState)]
    ids/states: 序列或 numpy 数组（长度=instructionCount）
    cycles: list[dict(ordinal,seq,cycles,span)]
    """
    instruction_count = len(ids)
    if len(states) != instruction_count:
        raise FactsError('ids/states 长度不一致')
    run_blob = b''.join(
        RUN.pack(int(item['startOrdinal']), int(item['startSeq']), int(item['count']),
                 int(item.get('blockOrdinal', 0)), int(item.get('recordOffset', 0)),
                 int(item.get('tailState', TAIL_UNKNOWN)), b'\x00' * 3) for item in runs)
    cycle_blob = b''.join(
        CYCLE.pack(int(item['ordinal']), int(item.get('seq', 0)), int(item['cycles']),
                   int(item.get('span', 0)), 0) for item in cycles)
    array_ids = np.asarray(ids, np.uint32)
    array_states = np.asarray(states, np.uint8)
    event_blob = b''.join(
        EVENT.pack(int(item['seq']), int(item['type']), int(item.get('flags', 0)),
                   int(item['payload']), int(item.get('nextFunc', -1)), b'\x00')
        for item in events)
    header = HEADER.pack(MAGIC, VERSION, HEADER.size, instruction_count, len(runs),
                         len(cycles), 0, len(events), 0)
    with open(path, 'wb') as handle:
        handle.write(header)
        handle.write(run_blob)
        handle.write(array_ids.tobytes())
        handle.write(array_states.tobytes())
        handle.write(cycle_blob)
        handle.write(event_blob)
    return dict(path=str(path), bytes=Path(path).stat().st_size,
                instructionCount=instruction_count, runCount=len(runs),
                cycleCount=len(cycles), eventCount=len(events))


def read_facts(path, with_paths=False):
    """读 facts v1；返回 dict（runs 为结构化 numpy，ids/states/cycles 为数组）。

    ``with_paths=False`` 时只解析到 CYCLES（本原型只用四段），忽略后面的 path/event；
    置 True 时按 t36 README 尝试解析（nodeCount 缺失 → 明确报不支持）。
    """
    raw = Path(path).read_bytes()
    if len(raw) < HEADER.size:
        raise FactsError('facts 文件被截断（<64B）')
    (magic, version, header_bytes, instruction_count, run_count, cycle_count,
     path_run_count, event_count, reserved) = HEADER.unpack_from(raw, 0)
    if magic != MAGIC:
        raise FactsError('facts magic 不符：%r' % (magic,))
    if version != VERSION:
        raise FactsError('facts 版本不支持：%d' % version)
    if header_bytes != HEADER.size or reserved != 0:
        raise FactsError('facts headerBytes/reserved 非法')
    position = HEADER.size
    runs_bytes = run_count * RUN.size
    ids_bytes = instruction_count * 4
    states_bytes = instruction_count
    cycles_bytes = cycle_count * CYCLE.size
    required = position + runs_bytes + ids_bytes + states_bytes + cycles_bytes
    if len(raw) < required:
        raise FactsError('facts 四段被截断：需要 %d，实际 %d' % (required, len(raw)))
    runs = np.frombuffer(raw, dtype=np.dtype([('startOrdinal', '<u8'), ('startSeq', '<u8'),
                                              ('count', '<u4'), ('blockOrdinal', '<u4'),
                                              ('recordOffset', '<u4'), ('tailState', 'u1'),
                                              ('pad', 'V3')]),
                         count=run_count, offset=position)
    position += runs_bytes
    ids = np.frombuffer(raw, np.uint32, instruction_count, position)
    position += ids_bytes
    states = np.frombuffer(raw, np.uint8, instruction_count, position)
    position += states_bytes
    cycles = np.frombuffer(raw, dtype=np.dtype([('ordinal', '<u8'), ('seq', '<u8'),
                                                ('cycles', '<u8'), ('span', '<u4'),
                                                ('reserved', '<u4')]),
                           count=cycle_count, offset=position)
    position += cycles_bytes
    if position != len(raw) and with_paths:
        raise FactsError('PATH_RUNS/NODES/EVENTS 解析未实现（t36 README header 缺 nodeCount）')
    # 基本自洽：RUNS 覆盖 [0, instructionCount) 且不重叠
    if run_count:
        starts = np.sort(runs['startOrdinal'])
        ends = np.sort(runs['startOrdinal'] + runs['count'])
        if int(starts[0]) != 0 or int(ends[-1]) != instruction_count:
            raise FactsError('RUNS 未覆盖全部指令：%d..%d / %d'
                             % (starts[0], ends[-1], instruction_count))
        if np.any(starts[1:] < ends[:-1]):
            raise FactsError('RUNS 相互重叠')
    return dict(header=dict(version=version, instructionCount=instruction_count,
                            runCount=run_count, cycleCount=cycle_count,
                            pathRunCount=path_run_count, eventCount=event_count),
                runs=runs, ids=ids, states=states, cycles=cycles)


def _read_varint(raw, position):
    value = 0
    shift = 0
    while True:
        if position >= len(raw) or shift >= 64:
            raise FactsError('facts 字符串长度无效或截断')
        byte = raw[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, position
        shift += 7


def _read_string_table(raw, position, count):
    if count > len(raw) - position:
        raise FactsError('facts 字符串计数超过文件长度')
    strings = []
    for _ in range(count):
        length, position = _read_varint(raw, position)
        if length > len(raw) - position:
            raise FactsError('facts 字符串被截断')
        strings.append(raw[position:position + length].decode('utf-8'))
        position += length
    return strings, position


def read_facts_v2(path):
    """读 facts v2（ETMFACT2）：RUNS40/IDS/STATES/CYCLES/EVENTS40/PATH_RUNS/NODES/LABELS/EVTEXTS。"""
    with Path(path).open('rb') as stream:
        raw = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
    if len(raw) < HEADER.size or raw[:8] not in (MAGIC_V2, MAGIC_V3):
        raise FactsError('facts v2 magic/长度不符')
    (magic, version, header_bytes, instructions, runs, cycles, path_runs, events,
     nodes) = HEADER.unpack_from(raw, 0)
    if (magic, version) not in ((MAGIC_V2, VERSION_V2), (MAGIC_V3, VERSION_V3)) or header_bytes != HEADER.size:
        raise FactsError('facts v2 version/headerBytes 非法')
    position = HEADER.size

    def take(size, count, name):
        nonlocal position
        need = int(count) * int(size)
        if position + need > len(raw):
            raise FactsError('facts v2 %s 段被截断' % name)
        start = position
        position += need
        return start

    run_start = take(RUN_V2.size, runs, 'RUNS')
    ids_start = take(4, instructions, 'IDS')
    states_start = take(1, instructions, 'STATES')
    cycle_start = take(CYCLE.size, cycles, 'CYCLES')
    event_start = take(EVENT_V2.size, events, 'EVENTS')
    path_start = take(PATH_RUN_V2.size, path_runs, 'PATH_RUNS')
    node_start = take(NODE_V2.size, nodes, 'NODES')
    if position + 8 > len(raw):
        raise FactsError('facts v2 LABELS 计数被截断')
    label_count = struct.unpack_from('<Q', raw, position)[0]
    position += 8
    labels, position = _read_string_table(raw, position, int(label_count))
    evtexts = []
    if position < len(raw):
        if position + 8 > len(raw):
            raise FactsError('facts EVTEXTS 计数被截断')
        count = struct.unpack_from('<Q', raw, position)[0]
        position += 8
        evtexts, position = _read_string_table(raw, position, int(count))
    if position != len(raw):
        raise FactsError('facts 存在未识别的尾部数据')
    run_dtype = np.dtype([('startOrdinal', '<u8'), ('startSeq', '<u8'),
                          ('count', '<u4'), ('blockOrdinal', '<u4'),
                          ('recordOffset', '<u4'), ('tailState', 'u1'),
                          ('pad', 'V3'), ('runSkip', '<u4'), ('reserved', '<u4')])
    cycle_dtype = np.dtype([('ordinal', '<u8'), ('seq', '<u8'), ('cycles', '<u8'),
                            ('span', '<u4'), ('reserved', '<u4')])
    event_dtype = np.dtype([('seq', '<u8'), ('payload', '<u8'), ('flags', '<u4'),
                            ('type', 'u1'), ('pad', 'u1'), ('nextFunc', '<i2'),
                            ('prevFunc', '<i2'), ('depth', '<i2'),
                            ('prevEventSeq', '<i4'), ('textId', '<u4'),
                            ('reserved', '<u4')])
    path_dtype = np.dtype([('startOrdinal', '<u8'), ('count', '<u4'), ('leafNode', '<u4')])
    node_dtype = np.dtype([('parent', '<i4'), ('label', '<u4')])
    return dict(version=version, labels=labels, evtexts=evtexts,
                header=dict(instructionCount=int(instructions), runCount=int(runs),
                            cycleCount=int(cycles), pathRunCount=int(path_runs),
                            eventCount=int(events), nodeCount=int(nodes)),
                runs=np.frombuffer(raw, run_dtype, int(runs), run_start),
                ids=np.frombuffer(raw, np.uint32, int(instructions), ids_start),
                states=np.frombuffer(raw, np.uint8, int(instructions), states_start),
                cycles=np.frombuffer(raw, cycle_dtype, int(cycles), cycle_start),
                events=np.frombuffer(raw, event_dtype, int(events), event_start),
                path_runs=np.frombuffer(raw, path_dtype, int(path_runs), path_start),
                nodes=np.frombuffer(raw, node_dtype, int(nodes), node_start))


def read_facts_any(path):
    """按 magic 分派 v1/v2/v3。"""
    with Path(path).open('rb') as stream:
        magic = stream.read(8)
    if magic in (MAGIC_V2, MAGIC_V3):
        return read_facts_v2(path)
    data = read_facts(path)
    data['version'] = VERSION
    data['labels'] = []
    data['evtexts'] = []
    data['path_runs'] = None
    data['nodes'] = None
    data['events'] = None
    return data


INDEX_FACTS_HEADER = struct.Struct('<8sIIQIIIIQQ64s8s')


def read_index_facts(path, facts, static_entries, native_sha256):
    """Read private, versioned numeric reductions bound to the same native input."""
    with Path(path).open('rb') as stream:
        if stream.seek(0, 2) < INDEX_FACTS_HEADER.size:
            raise FactsError('index facts header 被截断')
        raw = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
    magic, version, header, total, cell_size, statics, hotspots, reserved, lines, pairs, sha, tail = INDEX_FACTS_HEADER.unpack_from(raw)
    if (magic, version, header, cell_size, reserved, tail) != (b'ETMIDXF1', 1, 128, 4096, 0, b'\0' * 8):
        raise FactsError('index facts 版本/保留字段不符')
    if total != len(facts['ids']) or statics != len(static_entries) or not 0 < hotspots <= statics:
        raise FactsError('index facts 尺寸与核心事实不符')
    if sha != native_sha256.encode('ascii'):
        raise FactsError('index facts 输入 SHA256 不符')
    cells = (total + cell_size - 1) // cell_size
    expected = 128 + 4 * (statics + 2 * cells + 3 * cells * hotspots + statics + 1 + pairs + 2 * lines)
    if len(raw) != expected:
        raise FactsError('index facts 数据长度不符')
    position = 128
    result = dict(cellSize=cell_size, instructions=total)
    for name, count in [('hotspot_of_id', statics), ('cell_unknown', cells),
                        ('cell_not_taken', cells), ('hotspot_count', cells * hotspots),
                        ('hotspot_unknown', cells * hotspots), ('hotspot_first', cells * hotspots),
                        ('pc_cell_offsets', statics + 1), ('pc_cells', pairs),
                        ('line_start', lines), ('line_id', lines)]:
        result[name] = np.frombuffer(raw, '<u4', int(count), position)
        position += int(count) * 4
    mapping = static_maps(static_entries)
    if not np.array_equal(result['hotspot_of_id'], mapping['hotspotOfId']):
        raise FactsError('index facts hotspot ID 与静态字典不符')
    if hotspots != len(mapping['hotspotFunc']):
        raise FactsError('index facts hotspot 数量不符')
    if np.any(result['hotspot_count'] > cell_size) or int(result['hotspot_count'].sum(dtype=np.uint64)) != total:
        raise FactsError('index facts 指令聚合不符')
    if np.any(result['hotspot_unknown'] > result['hotspot_count']):
        raise FactsError('index facts 谓词计数不符')
    if (np.any(result['cell_unknown'] > cell_size) or np.any(result['cell_not_taken'] > cell_size)
            or np.any(result['cell_unknown'].astype(np.uint64) + result['cell_not_taken'] > cell_size)):
        raise FactsError('index facts 状态计数不符')
    cell_totals = result['hotspot_count'].reshape(cells, hotspots).sum(axis=1, dtype=np.uint64)
    expected_totals = np.full(cells, cell_size, np.uint64)
    if cells:expected_totals[-1] = total - (cells - 1) * cell_size
    if not np.array_equal(cell_totals, expected_totals):
        raise FactsError('index facts 单元格指令数不符')
    if np.any((result['hotspot_first'][result['hotspot_count'] > 0] >> 12) >= statics):
        raise FactsError('index facts 首次指令 ID 越界')
    offsets = result['pc_cell_offsets']
    if offsets[0] != 0 or offsets[-1] != pairs or np.any(np.diff(offsets.astype(np.int64)) < 0):
        raise FactsError('index facts PC 索引偏移不符')
    if np.any(result['pc_cells'] >= cells):
        raise FactsError('index facts PC cell 越界')
    starts = result['line_start']
    if total and (not lines or starts[0] != 0 or np.any(starts >= total)
                  or np.any(np.diff(starts.astype(np.int64)) <= 0)):
        raise FactsError('index facts 源码区间不符')
    if np.any(result['line_id'] >= statics):
        raise FactsError('index facts 源码 ID 越界')
    return result


def cell_ordinals(instruction_count, cell_size):
    """每条指令的 cell 下标（不超过 20M 个整数，一次向量化）。"""
    return (np.arange(instruction_count, dtype=np.int64) // int(cell_size))



import time

import numpy as np

STATE_EXECUTED, STATE_NOT_TAKEN, STATE_UNKNOWN = 0, 1, 2


class Counters:
    """结构性计数：证明热路径只有 NumPy 批量操作 + 与 cell/static/批次同阶的 Python 循环。"""

    def __init__(self):
        self.instructions = 0
        self.batches = 0
        self.python_loops = 0          # 显式 for 循环轮数（不含 NumPy 内部 C 循环）
        self.numpy_calls = 0
        self.static_items = 0
        self.cells = 0
        self.cycle_anchors = 0
        self.runs = 0

    def as_dict(self):
        return dict(instructions=self.instructions, batches=self.batches,
                    pythonLoops=self.python_loops, numpyCalls=self.numpy_calls,
                    staticItems=self.static_items, cells=self.cells,
                    cycleAnchors=self.cycle_anchors, runs=self.runs,
                    pythonLoopsPerMillion=(round(self.python_loops * 1e6 / self.instructions, 3)
                                           if self.instructions else None))


def instance():
    return Counters()


def _nc(counters):
    counters.numpy_calls += 1


def static_maps(static_entries, counters=None):
    """static_id → function/hotspot 映射（唯一静态项各处理一次）。"""
    counters = counters or instance()
    counters.static_items = len(static_entries)
    function_names = ['(未知函数)']
    function_index = {'(未知函数)': 0}
    hotspot_index = {}
    hotspot_func, hotspot_file, hotspot_line = [], [], []
    func_of_id = np.empty(len(static_entries), np.int16)
    hotspot_of_id = np.empty(len(static_entries), np.int32)
    for identifier, entry in enumerate(static_entries):
        counters.python_loops += 1
        name = entry.get('function') or '(未知函数)'
        func_id = function_index.get(name)
        if func_id is None:
            func_id = len(function_names)
            function_index[name] = func_id
            function_names.append(name)
        func_of_id[identifier] = func_id
        key = (name, entry.get('file'), entry.get('line'))
        hotspot_id = hotspot_index.get(key)
        if hotspot_id is None:
            hotspot_id = len(hotspot_func)
            hotspot_index[key] = hotspot_id
            hotspot_func.append(func_id)
            hotspot_file.append(entry.get('file'))
            hotspot_line.append(entry.get('line'))
        hotspot_of_id[identifier] = hotspot_id
    predicated = np.asarray([bool(entry.get('predicated')) for entry in static_entries],
                            dtype=bool)
    flow_of_id = np.asarray([trace_store.FLOW_CODES[str(entry.get('flow'))]
                             for entry in static_entries], dtype=np.int8)
    pc_of_id = np.asarray([int(entry.get('pc')) for entry in static_entries], np.uint32)
    size_of_id = np.asarray([int(entry.get('size')) for entry in static_entries], np.uint8)
    _nc(counters)
    return dict(functionNames=function_names,
                funcOfId=func_of_id,
                hotspotOfId=hotspot_of_id,
                predicatedOfId=predicated,
                flowOfId=flow_of_id, pcOfId=pc_of_id, sizeOfId=size_of_id,
                hotspotFunc=np.asarray(hotspot_func, np.int16),
                hotspotFile=np.asarray(hotspot_file, dtype=object),
                hotspotLine=np.asarray([-1 if value is None else int(value)
                                        for value in hotspot_line], np.int64),
                functionIndex=function_index, hotspotIndex=hotspot_index)


RUN_DTYPE = np.dtype([('startOrdinal', '<u8'), ('startSeq', '<u8'),
                     ('count', '<u4'), ('blockOrdinal', '<u4'),
                     ('recordOffset', '<u4'), ('tailState', 'u1'), ('pad', 'V3')])


CYCLE_DTYPE = np.dtype([('ordinal', '<u8'), ('seq', '<u8'), ('cycles', '<u8'),
                       ('span', '<u4'), ('reserved', '<u4')])


def cycles_array(cycles):
    """把 facts 的 CYCLES 统一成结构化数组（list[dict] 或已读数组都接受）。"""
    if isinstance(cycles, np.ndarray):
        return cycles
    return np.asarray([(int(item['ordinal']), int(item.get('seq', 0)),
                        int(item['cycles']), int(item.get('span', 0)), 0)
                       for item in cycles], dtype=CYCLE_DTYPE)


def runs_array(runs):
    """把 facts 的 RUNS 统一成结构化数组（list[dict] 或已读数组都接受）。"""
    if isinstance(runs, np.ndarray):
        return runs
    return np.asarray([(int(item['startOrdinal']), int(item['startSeq']),
                        int(item['count']), int(item.get('blockOrdinal', 0)),
                        int(item.get('recordOffset', 0)), int(item.get('tailState', 2)),
                        b'\x00' * 3) for item in runs], dtype=RUN_DTYPE)


def _first_seq_per_cell(runs, cells, cell_size, counters):
    """每个非空 cell 的首条指令 seq：ordinal → 所在 RUN（向量化 searchsorted）。"""
    starts = np.asarray(runs['startOrdinal'], np.int64)
    run_starts = starts
    start_seqs = np.asarray(runs['startSeq'], np.int64)
    counts = np.asarray(runs['count'], np.int64)
    ordinal = np.arange(cells, dtype=np.int64) * int(cell_size)
    run_index = np.searchsorted(starts, ordinal, side='right') - 1
    _nc(counters)
    counters.python_loops += 1
    valid = (run_index >= 0) & (run_index < len(starts))
    result = np.full(cells, -1, np.int64)
    if np.any(valid):
        chosen = run_index[valid]
        inside = ordinal[valid] - starts[chosen]
        if np.any(inside >= counts[chosen]):
            raise ValueError('cell 起始 ordinal 落在 RUN 之外（facts 不一致）')
        result[valid] = start_seqs[chosen] + inside
    return result, run_starts


def derive_function_segments(ids, function_of_id):
    """函数区间：相邻指令的 function id 变化处切段（连续覆盖 [0,n)）。"""
    func = np.asarray(function_of_id)[np.asarray(ids)]
    if func.size == 0:
        empty = np.empty(0, np.int64)
        return empty, empty.astype(np.int32), empty.astype(np.int16)
    change = np.flatnonzero(func[1:] != func[:-1]) + 1
    starts = np.concatenate(([0], change))
    counts = np.diff(np.concatenate((starts, [func.size])))
    return starts.astype(np.int64), counts.astype(np.int32), func[starts].astype(np.int16)


def derive_path_segments(path_runs):
    """路径区间：采用 PATH_RUNS（startOrdinal/count/leafNode），校验连续分区。"""
    start = np.asarray(path_runs['startOrdinal'], np.int64)
    count = np.asarray(path_runs['count'], np.int64)
    leaf = np.asarray(path_runs['leafNode'], np.int64)
    if len(start):
        if int(start[0]) != 0 or np.any(start[1:] != start[:-1] + count[:-1]):
            raise FactsError('PATH_RUNS 不是连续分区')
    return start, count, leaf


def derive_cell_anchors(runs, cells, cell_size):
    """cell 首指令 anchor：RUNS block/recordOffset + skip=runSkip+(cellStart-runStart)。"""
    starts = np.asarray(runs['startOrdinal'], np.int64)
    counts = np.asarray(runs['count'], np.int64)
    block = np.asarray(runs['blockOrdinal'], np.int32)
    offset = np.asarray(runs['recordOffset'], np.int64)
    skip_base = np.asarray(runs['runSkip'], np.int64)
    cell_start = np.arange(int(cells), dtype=np.int64) * int(cell_size)
    index = np.searchsorted(starts, cell_start, side='right') - 1
    if np.any(index < 0):
        raise FactsError('cell 起始 ordinal 不在任何 RUN 内')
    inside = cell_start - starts[index]
    if np.any(inside >= counts[index]):
        raise FactsError('cell 起始 ordinal 超出 RUN 范围')
    return (block[index], offset[index], (skip_base[index] + inside).astype(np.int64))


def derive_events(events, labels, evtexts=(), with_text=True):
    """事件表：ev_seq/type/flags/payload/next_func（LABELS id）+ 文本 id（EVTEXTS）。"""
    seq = np.asarray(events['seq'], np.int64)
    result = {
        'ev_seq': seq,
        'ev_type': np.asarray(events['type'], np.uint8),
        'ev_flags': np.asarray(events['flags'], np.int64),
        'ev_payload': np.asarray(events['payload'], np.int64),
        'ev_next_func': np.asarray(events['nextFunc'], np.int64),
        'ev_text_ids': np.asarray(events['textId'], np.int64),
    }
    del labels, evtexts, with_text
    return result


def node_chain_texts(node, nodes, labels):
    """从节点回溯到根的 LABELS 文本链（根 label 允许为 ''）。"""
    chain = []
    guard = 0
    while node >= 0:
        chain.append(str(labels[int(nodes['label'][node])]))
        node = int(nodes['parent'][node])
        guard += 1
        if guard > 10_000:
            raise FactsError('节点父链异常')
    return list(reversed(chain))


def derive_tables(facts, static_entries, cell_size=4096, batch=1_000_000, counters=None):
    """从事实流派生 cell 表 + 静态映射（分批、全 NumPy 归约）。"""
    counters = counters or instance()
    started = time.perf_counter()
    ids = np.asarray(facts['ids'])
    states = np.asarray(facts['states'])
    cycles = cycles_array(facts['cycles'])
    runs = runs_array(facts['runs'])
    total = int(ids.size)
    if facts.get('version') == VERSION_V3 and cell_size != 4096:
        raise FactsError('facts v3 的定位点使用固定4096指令单元格')
    counters.instructions = total
    counters.runs = int(runs.size)
    counters.cycle_anchors = int(cycles.size)
    cells = (total + int(cell_size) - 1) // int(cell_size) if total else 0
    counters.cells = cells
    cell_insn = np.zeros(cells, np.int64)
    cell_executed = np.zeros(cells, np.int64)
    cell_unknown = np.zeros(cells, np.int64)
    cell_not_taken = np.zeros(cells, np.int64)
    cell_cycles = np.zeros(cells, np.int64)
    cell_cycle_runs = np.zeros(cells, np.int64)
    timings = {}
    phase = time.perf_counter()
    mapping = static_maps(static_entries, counters)
    static_count = len(static_entries)
    hotspot_count = max(1, len(mapping['hotspotFunc']))
    hotspot_span = cells * hotspot_count
    aggregate = facts.get('indexFacts')
    if aggregate is not None and aggregate['cellSize'] != cell_size:aggregate = None
    timings['staticMaps'] = round(time.perf_counter() - phase, 4)
    if aggregate is not None:
        cell_unknown[:] = aggregate['cell_unknown']
        cell_not_taken[:] = aggregate['cell_not_taken']
        cell_hotspot_count = aggregate['hotspot_count']
        cell_hotspot_unknown = aggregate['hotspot_unknown']
    else:
        cell_hotspot_count = np.zeros(hotspot_span, np.int64)
        cell_hotspot_unknown = np.zeros(hotspot_span, np.int64)
        hotspot_first = np.full(hotspot_span, np.iinfo(np.uint64).max, np.uint64)
        predicated_of_id = mapping['predicatedOfId']
        hotspot_of_id = mapping['hotspotOfId']
        for start in range(0, total, int(batch)):
            counters.batches += 1
            counters.python_loops += 1
            stop = min(total, start + int(batch))
            ordinal = np.arange(start, stop, dtype=np.int64)
            cell = ordinal // int(cell_size)
            chunk_states = states[start:stop]
            cell_not_taken += np.bincount(cell[chunk_states == STATE_NOT_TAKEN],
                                          minlength=cells)[:cells]
            cell_unknown += np.bincount(cell[chunk_states == STATE_UNKNOWN],
                                        minlength=cells)[:cells]
            _nc(counters)
            # 静态 id 也按批消费（原型先只校验范围；映射在 static_maps 里一次完成）
            chunk_ids = ids[start:stop]
            if chunk_ids.size and int(chunk_ids.max()) >= len(static_entries):
                raise ValueError('facts 的 static_id 超出静态表范围')
            hotspot = hotspot_of_id[chunk_ids]
            key = cell * hotspot_count + hotspot
            cell_hotspot_count += np.bincount(key, minlength=hotspot_span)[:hotspot_span]
            predicated_mask = predicated_of_id[chunk_ids]
            if np.any(predicated_mask):
                cell_hotspot_unknown += np.bincount(key[predicated_mask],
                                                    minlength=hotspot_span)[:hotspot_span]
            # 每个 (cell, hotspot) 的最小 cell 内偏移 + 对应 static_id（打包成 uint64 取 min）
            # A consecutive (cell, hotspot) run can contribute only its first
            # ordinal to the minimum. Typical source lines cover several adjacent
            # instructions, so reduce those candidates before the scattered write.
            first = np.empty(len(key), bool)
            first[0] = True
            first[1:] = key[1:] != key[:-1]
            offsets = (ordinal[first] % int(cell_size)).astype(np.uint64)
            packed = (offsets << np.uint64(32)) | chunk_ids[first].astype(np.uint64)
            np.minimum.at(hotspot_first, key[first], packed)
            _nc(counters)
    cell_insn[:] = int(cell_size)
    if cells:cell_insn[-1] = total - (cells - 1) * int(cell_size)
    cell_executed[:] = cell_insn - cell_unknown - cell_not_taken
    timings['counts'] = round(time.perf_counter() - phase, 4)

    phase = time.perf_counter()
    if counters.cycle_anchors:
        anchor_ordinal = np.asarray(cycles['ordinal'], np.int64)
        anchor_cycles = np.asarray(cycles['cycles'], np.int64)
        anchor_cell = anchor_ordinal // int(cell_size)
        np.add.at(cell_cycles, anchor_cell, anchor_cycles)
        for chunk_start in range(0, len(anchor_cell), int(batch)):
            counters.python_loops += 1
            chunk = anchor_cell[chunk_start:chunk_start + int(batch)]
            cell_cycle_runs += np.bincount(chunk, minlength=cells)[:cells]
        _nc(counters)
        if np.any(cell_cycles < 0):
            raise ValueError('周期累加出现负值（uint64→int64 溢出）')
    timings['cycles'] = round(time.perf_counter() - phase, 4)

    phase = time.perf_counter()
    cell_first_seq, _ = _first_seq_per_cell(runs, cells, cell_size, counters)
    timings['firstSeq'] = round(time.perf_counter() - phase, 4)

    phase = time.perf_counter()
    if aggregate is not None:
        cell_hotspot_first = aggregate['hotspot_first']
    else:
        valid = hotspot_first != np.iinfo(np.uint64).max
        first_offset = np.zeros(hotspot_span, np.uint64)
        first_id = np.zeros(hotspot_span, np.uint64)
        first_offset[valid] = hotspot_first[valid] >> np.uint64(32)
        first_id[valid] = hotspot_first[valid] & np.uint64(0xFFFFFFFF)
        cell_hotspot_first = np.zeros(hotspot_span, np.uint32)
        cell_hotspot_first[valid] = ((first_id[valid].astype(np.uint32) << 12)
                                     | first_offset[valid].astype(np.uint32))
    _nc(counters)
    timings['hotspotFinalize'] = round(time.perf_counter() - phase, 4)
    shape = (cells, hotspot_count)
    extra = {}
    if facts.get('version') in (VERSION_V2, VERSION_V3) and facts.get('path_runs') is not None:
        phase = time.perf_counter()
        labels = list(facts.get('labels') or [''])
        evtexts = list(facts.get('evtexts') or [''])
        strings = ['']
        string_index = {'': 0}

        def string_id(text):
            text = '' if text is None else str(text)
            identifier = string_index.get(text)
            if identifier is None:
                identifier = len(strings)
                string_index[text] = identifier
                strings.append(text)
            return identifier

        counters.python_loops += 1
        fs_start, fs_count, fs_func = derive_function_segments(ids, mapping['funcOfId'])
        ps_start, ps_count, ps_leaf = derive_path_segments(facts['path_runs'])
        anchor_block, anchor_offset, anchor_skip = derive_cell_anchors(runs, cells,
                                                                       cell_size)
        nodes = facts['nodes']
        node_parent = np.asarray(nodes['parent'], np.int32)
        node_label = np.asarray([string_id(labels[int(value)]) for value in nodes['label']],
                                np.int32)
        counters.python_loops += 1
        events = derive_events(facts['events'], labels, evtexts)
        ev_text = np.asarray([string_id(evtexts[int(value)])
                              if int(value) < len(evtexts) else 0
                              for value in events['ev_text_ids']], np.int32)
        function_index = mapping['functionIndex']
        ev_next_func = np.asarray([
            -1 if int(value) < 0 else function_index.get(labels[int(value)], -1)
            for value in events['ev_next_func']], np.int32)
        # h_file 是**每热点**的 file 字符串下标（v1 口径），不是每静态项
        h_file = np.asarray([-1 if name is None else string_id(name)
                             for name in mapping['hotspotFile']], np.int32)
        extra = dict(fs_start=fs_start, fs_count=fs_count, fs_func=fs_func,
                     ps_start=ps_start, ps_count=ps_count, ps_leaf=ps_leaf,
                     cell_anchor_block=anchor_block, cell_anchor_offset=anchor_offset,
                     cell_anchor_skip=anchor_skip,
                     node_parent=node_parent, node_label=node_label,
                     ev_seq=events['ev_seq'], ev_type=events['ev_type'],
                     ev_flags=events['ev_flags'], ev_payload=events['ev_payload'],
                     ev_next_func=ev_next_func, ev_text=ev_text,
                     h_file=h_file, strings=strings, version=VERSION_V2,
                     nodes_raw=facts['nodes'], labels_raw=labels,
                     event_details=dict(prevFunc=np.asarray(facts['events']['prevFunc'],
                                                            np.int64),
                                        depth=np.asarray(facts['events']['depth'], np.int64),
                                        prevEventSeq=np.asarray(
                                            facts['events']['prevEventSeq'], np.int64)))
        timings['v2Tables'] = round(time.perf_counter() - phase, 4)
    elapsed = round(time.perf_counter() - started, 4)
    return dict(cellSize=int(cell_size), cells=cells, instructions=total,
                cell_insn=cell_insn, cell_executed=cell_executed,
                cell_unknown=cell_unknown, cell_not_taken=cell_not_taken,
                cell_cycles=cell_cycles, cell_cycle_runs=cell_cycle_runs,
                cell_first_seq=cell_first_seq,
                cell_hotspot_count=cell_hotspot_count.reshape(shape).astype(np.uint16),
                cell_hotspot_unknown=cell_hotspot_unknown.reshape(shape).astype(np.uint16),
                cell_hotspot_first=cell_hotspot_first.reshape(shape),
                mapping=mapping, timings=timings, elapsedSeconds=elapsed,
                counters=counters.as_dict(), **extra)


def derive_cell(facts, static_entries, cell, cell_size=4096, counters=None):
    """只派生单个 cell（原型验证用）：返回该 cell 的 7 个计数 + 首条指令 seq。

    ordinal/seq 都是**绝对**的（与全量派生同一口径），便于跨 cell 边界对照。
    """
    counters = counters or instance()
    states_all = np.asarray(facts['states'])
    total = int(states_all.size)
    lo = int(cell) * int(cell_size)
    hi = min(total, lo + int(cell_size))
    if lo >= total:
        raise ValueError('cell 超出指令范围：%d' % cell)
    states = states_all[lo:hi]
    counters.instructions += int(states.size)
    counters.python_loops += 1
    counts = np.bincount(states, minlength=3)
    _nc(counters)
    cycles = cycles_array(facts['cycles'])
    ordinal = np.asarray(cycles['ordinal'], np.int64)
    mask = (ordinal >= lo) & (ordinal < hi)
    _nc(counters)
    cell_cycles = int(np.sum(np.asarray(cycles['cycles'], np.int64)[mask])) if mask.any() else 0
    cycle_runs = int(np.count_nonzero(mask))
    runs = runs_array(facts['runs'])
    starts = np.asarray(runs['startOrdinal'], np.int64)
    order = np.argsort(starts)
    start_seqs = np.asarray(runs['startSeq'], np.int64)[order]
    counts_run = np.asarray(runs['count'], np.int64)[order]
    index = int(np.searchsorted(starts[order], lo, side='right') - 1)
    if index < 0 or lo >= starts[order][index] + counts_run[index]:
        raise ValueError('cell 起始 ordinal 不在任何 RUN 内')
    first_seq = int(start_seqs[index] + (lo - starts[order][index]))
    return dict(cell=int(cell), cell_insn=int(states.size),
                cell_executed=int(counts[0]), cell_unknown=int(counts[2]),
                cell_not_taken=int(counts[1]), cell_cycles=cell_cycles,
                cell_cycle_runs=cycle_runs, cell_first_seq=first_seq)


# ---------------------------------------------------------------------------------------------
# C2c：默认/随机查询与导航所需的剩余表（unknown/not_taken/call_pair、node/self/cycles、nav）
# ---------------------------------------------------------------------------------------------

def _seq_per_instruction(runs, total):
    """每条指令的 seq：所在 RUN 的 startSeq + (ordinal - startOrdinal)（RUNS 连续覆盖）。"""
    runs = runs_array(runs)
    starts = np.asarray(runs['startOrdinal'], np.int64)
    counts = np.asarray(runs['count'], np.int64)
    start_seqs = np.asarray(runs['startSeq'], np.int64)
    if not len(starts) or starts[0] != 0 or np.any(starts[1:] != starts[:-1] + counts[:-1]):
        raise FactsError('RUNS 不是连续指令分区')
    if int(counts.sum()) != total:
        raise FactsError('RUNS 指令数不符')
    base = start_seqs - starts
    changes = np.concatenate(([0], np.flatnonzero(base[1:] != base[:-1]) + 1))
    lengths = np.diff(np.concatenate((starts[changes], [total])))
    dtype = np.int32 if int(start_seqs[-1] + counts[-1]) <= np.iinfo(np.int32).max else np.int64
    result = np.repeat(base[changes].astype(dtype), lengths)
    result += np.arange(total, dtype=dtype)
    return result


def _node_chains(nodes):
    """每个节点的祖先链（含自身，根在前）。"""
    parent = np.asarray(nodes['parent'], np.int64)
    chains = [None] * len(parent)
    for node in range(len(parent)):
        chain = []
        current = node
        guard = 0
        while current >= 0:
            chain.append(current)
            current = int(parent[current])
            guard += 1
            if guard > 10_000:
                raise FactsError('节点父链异常')
        chains[node] = tuple(reversed(chain))
    return chains


def derive_remaining(facts, derived, static_entries, materialize_seq=True):
    """派生默认/随机查询与导航所需剩余表（全部 NumPy 批量归约）。"""
    if facts.get('version') not in (VERSION_V2, VERSION_V3):
        raise FactsError('剩余表派生需要 facts v2（PATH_RUNS/NODES/EVENTS）')
    started = time.perf_counter()
    ids = np.asarray(facts['ids'])
    states = np.asarray(facts['states'])
    total = int(ids.size)
    cell_size = int(derived['cellSize'])
    cells = int(derived['cells'])
    mapping = derived['mapping']
    event_seq = np.asarray(facts['events']['seq'], np.int64)
    event_ordinal = event_seq - np.arange(len(event_seq))
    def seq_of(ordinal):
        return (ordinal + np.searchsorted(event_ordinal, ordinal, side='right')).astype(np.int32)
    seq = seq_of(np.arange(total, dtype=np.int32)) if materialize_seq else None
    unknown_ordinal = np.flatnonzero(states == 2)
    not_taken_ordinal = np.flatnonzero(states == 1)
    call_flow = int(trace_store.FLOW_CODES['call'])
    call_ordinal = np.flatnonzero((states[:-1] == 0)
        & (np.asarray(mapping['flowOfId'])[ids[:-1]] == call_flow))
    # An intervening event breaks observed-call attribution, even if the
    # neighbouring instruction ordinals are consecutive.
    call_seq = seq_of(call_ordinal)
    keep = seq_of(call_ordinal + 1) == call_seq + 1
    call_ordinal, call_seq = call_ordinal[keep], call_seq[keep]
    ps_start = np.asarray(derived['ps_start'], np.int64)
    ps_leaf = np.asarray(derived['ps_leaf'], np.int64)
    ps_count = np.asarray(derived['ps_count'], np.int64)
    if int(ps_count.sum()) != total:
        raise FactsError('路径区间未覆盖全部指令')
    nodes = facts['nodes']
    node_count = len(nodes)
    node_parent = np.asarray(nodes['parent'], np.int32)
    chains = _node_chains(nodes)
    anchors = cycles_array(facts['cycles'])
    anchor_ordinal = np.asarray(anchors['ordinal'], np.int64)
    anchor_cycles = np.asarray(anchors['cycles'], np.int64)
    anchor_cell = anchor_ordinal // cell_size
    anchor_leaf = ps_leaf[np.searchsorted(ps_start, anchor_ordinal, side='right') - 1]
    anchor_hotspot = np.asarray(mapping['hotspotOfId'], np.int64)[ids[anchor_ordinal]]
    hotspot_count = int(np.asarray(derived['cell_hotspot_count']).shape[1])
    # hs_cycles：按 (cell, hotspot) 聚合周期（整数）
    key = anchor_cell * hotspot_count + anchor_hotspot
    unique, inverse = np.unique(key, return_inverse=True)
    sums = np.zeros(len(unique), np.int64)
    np.add.at(sums, inverse, anchor_cycles)
    hs_cells = (unique // hotspot_count).astype(np.int32)
    hs_hotspots = (unique % hotspot_count).astype(np.int32)
    # cell_node_value：每条指令按其 path 叶的祖先链计入 cell
    # Split path intervals only where they cross cell boundaries. The number
    # of contributions follows path changes + cells, not instruction count.
    ps_end = ps_start + ps_count
    first_cell = ps_start // cell_size
    pieces = (ps_end - 1) // cell_size - first_cell + 1
    segment_ids = np.repeat(np.arange(len(ps_start)), pieces)
    piece_starts = np.repeat(np.cumsum(pieces) - pieces, pieces)
    piece_cells = np.repeat(first_cell, pieces) + np.arange(len(segment_ids)) - piece_starts
    lengths = np.minimum(ps_end[segment_ids], (piece_cells + 1) * cell_size) - np.maximum(ps_start[segment_ids], piece_cells * cell_size)
    node_self_counts = np.zeros((cells, node_count), np.int32)
    np.add.at(node_self_counts, (piece_cells, ps_leaf[segment_ids]), lengths.astype(np.int32))
    cell_node_value = np.zeros((cells, node_count), np.uint16)
    cell_node_value_cycles = np.zeros((cells, node_count), np.int64)
    node_self_cycles = np.zeros((cells, node_count), np.int64)
    # 周期锚点：祖先链累加 + 叶 self
    unique_anchor_pair, anchor_pair_inverse = np.unique(anchor_cell * node_count + anchor_leaf,
                                                       return_inverse=True)
    anchor_pair_sums = np.zeros(len(unique_anchor_pair), np.int64)
    np.add.at(anchor_pair_sums, anchor_pair_inverse, anchor_cycles)
    node_self_cycles.flat[unique_anchor_pair] = anchor_pair_sums
    for leaf in range(node_count):
        for node in chains[leaf]:
            cell_node_value[:, node] += node_self_counts[:, leaf].astype(np.uint16)
            cell_node_value_cycles[:, node] += node_self_cycles[:, leaf]
    # nav 表：PC_CELLS（每个静态 id 出现在哪些 cell）+ LINE_RUNS（file/line 变化）
    aggregate = facts.get('indexFacts')
    if aggregate is not None and aggregate['cellSize'] == cell_size:
        offsets = aggregate['pc_cell_offsets']
        pc_cells = aggregate['pc_cells']
        line_start = aggregate['line_start']
        line_id = aggregate['line_id']
    else:
        ordinal = np.arange(total, dtype=np.int32)
        cell = ordinal // cell_size
        seen = np.zeros((cells, len(static_entries)), bool)
        for start in range(0, total, 1_000_000):
            stop = min(total, start + 1_000_000)
            seen[cell[start:stop], ids[start:stop]] = True
        sid_values, cell_values = np.nonzero(seen.T)
        offsets = np.zeros(len(static_entries) + 1, np.uint32)
        if len(sid_values):
            counts_per_sid = np.bincount(sid_values, minlength=len(static_entries))
            offsets[1:] = np.cumsum(counts_per_sid).astype(np.uint32)
        pc_cells = cell_values.astype(np.uint32)
        line_keys = np.empty(len(static_entries), np.int32)
        line_key_index = {}
        for index, entry in enumerate(static_entries):
            line = entry.get('line')
            key_value = (0,) if line is None else (1, entry.get('file'), line)
            line_keys[index] = line_key_index.setdefault(key_value, len(line_key_index))
        instruction_line_key = line_keys[ids]
        change = np.flatnonzero(instruction_line_key[1:] != instruction_line_key[:-1]) + 1
        line_start = np.concatenate(([0], change)).astype(np.uint32)
        line_id = ids[line_start].astype(np.uint32)
    timings = dict(remainingSeconds=round(time.perf_counter() - started, 4))
    return dict(
        seq_per_instruction=seq,
        unknown_seq=seq_of(unknown_ordinal),
        unknown_func=np.asarray(mapping['funcOfId'])[ids[unknown_ordinal]].astype(np.int16),
        not_taken_seq=seq_of(not_taken_ordinal),
        not_taken_func=np.asarray(mapping['funcOfId'])[ids[not_taken_ordinal]].astype(np.int16),
        call_pair_seq=call_seq,
        call_pair_func=np.asarray(mapping['funcOfId'])[ids[call_ordinal + 1]].astype(np.int16),
        cell_node_value=cell_node_value,
        cell_node_value_cycles=cell_node_value_cycles,
        node_self_cells=np.nonzero((node_self_counts != 0)
                                   | (node_self_cycles != 0))[0].astype(np.int32),
        node_self_nodes=np.nonzero((node_self_counts != 0)
                                   | (node_self_cycles != 0))[1].astype(np.int32),
        node_self_counts=node_self_counts[(node_self_counts != 0)
                                          | (node_self_cycles != 0)].astype(np.int32),
        node_self_cycles=node_self_cycles[(node_self_counts != 0)
                                          | (node_self_cycles != 0)].astype(np.int64),
        hs_cycles_cells=hs_cells, hs_cycles_hotspots=hs_hotspots,
        hs_cycles_values=sums.astype(np.int64),
        cycle_anchor_seq=np.asarray(anchors['seq'], np.int32),
        cycle_anchor_hotspot=anchor_hotspot.astype(np.int32),
        cycle_anchor_cycles=anchor_cycles.astype(np.int64),
        pc_cell_offsets=offsets, pc_cells=pc_cells,
        line_start=line_start, line_id=line_id,
        nav_meta=dict(cellSize=cell_size, staticCount=len(static_entries),
                      instructions=total, pcCellPairs=int(len(pc_cells)),
                      lineRuns=int(len(line_start))),
        node_parent=node_parent,
        timings=timings)


def build_full_index(facts, static_entries, first_seq, last_seq, view=None,
                     cell_size=4096, batch=1_000_000, compute_defaults=True,
                     static_first_seen=False):
    """完整索引：C2 表 + C2b 表 + C2c 剩余表 + 默认视图，返回 (arrays, meta, timings, payload)。"""
    started = time.perf_counter()
    counters = Counters()
    derived = derive_tables(facts, static_entries, cell_size=cell_size, batch=batch,
                            counters=counters)
    timings = dict(derived['timings'])
    remaining = derive_remaining(facts, derived, static_entries, materialize_seq=False)
    timings.update(remaining['timings'])
    tables = derived_arrays(derived)
    for name in ('cell_node_value', 'cell_node_value_cycles', 'node_self_cells',
                 'node_self_nodes', 'node_self_counts', 'node_self_cycles',
                 'hs_cycles_cells', 'hs_cycles_hotspots', 'hs_cycles_values',
                 'pc_cell_offsets', 'pc_cells', 'line_start', 'line_id',
                 'unknown_seq', 'unknown_func', 'not_taken_seq', 'not_taken_func',
                 'call_pair_seq', 'call_pair_func', 'cycle_anchor_seq',
                 'cycle_anchor_hotspot', 'cycle_anchor_cycles'):
        tables[name] = np.asarray(remaining[name])
    # v1 的 hotspot id 按**指令首次出现顺序**分配；本实现按静态项顺序，这里重排到 v1 口径，
    # 使 window/analysis.hotspots 的顺序与 v1 一致（数值本身不变）。
    ids = np.asarray(facts['ids'])
    mapping = derived['mapping']
    if static_first_seen:
        # Native core assigns static IDs when instructions first enter the
        # logical stream. That producer contract already fixes hotspot order.
        order = np.arange(len(static_entries))
    else:
        first_positions = np.full(len(static_entries), len(ids), np.int64)
        np.minimum.at(first_positions, ids, np.arange(len(ids), dtype=np.int32))
        order = np.argsort(first_positions)
    our_hotspots = np.asarray(mapping['hotspotOfId'], np.int64)[order]
    permutation = np.full(len(mapping['hotspotFunc']), -1, np.int64)
    next_id = 0
    for value in our_hotspots:
        if permutation[value] < 0:
            permutation[value] = next_id
            next_id += 1
    if next_id != len(mapping['hotspotFunc']):
        raise FactsError('hotspot 重排数量不符：%d≠%d' % (next_id, len(mapping['hotspotFunc'])))
    inverse = np.argsort(permutation)

    def reorder_columns(matrix):
        matrix = np.asarray(matrix)
        out = np.empty_like(matrix)
        out[:, permutation] = matrix
        return out

    for name in ('cell_hotspot_count', 'cell_hotspot_unknown', 'cell_hotspot_first'):
        tables[name] = reorder_columns(tables[name])
    tables['h_func'] = np.asarray(mapping['hotspotFunc'])[inverse].astype(np.int16)
    if 'h_file' in tables:
        tables['h_file'] = np.asarray(tables['h_file'])[inverse].astype(np.int32)
    tables['h_line'] = np.asarray(mapping['hotspotLine'])[inverse].astype(np.int32)
    tables['st_hotspot'] = permutation[np.asarray(mapping['hotspotOfId'])].astype(np.int32)
    remaining['hs_cycles_hotspots'] = permutation[
        np.asarray(remaining['hs_cycles_hotspots'], np.int64)].astype(np.int32)
    tables['hs_cycles_hotspots'] = remaining['hs_cycles_hotspots']
    # 事件文本需要 strings 池（derived 已建）
    strings = list(derived['strings'])
    meta = dict(cellSize=int(cell_size), instructions=int(derived['instructions']),
                events=int(facts['header'].get('eventCount') or 0),
                cells=int(derived['cells']),
                staticCount=len(static_entries),
                functionCount=len(derived['mapping']['functionNames']),
                hotspotCount=int(np.asarray(derived['cell_hotspot_count']).shape[1]),
                nodeCount=len(facts['nodes']), segmentCount=len(derived['ps_start']),
                functionSegments=len(derived['fs_start']),
                firstSeq=int(first_seq), lastSeq=int(last_seq),
                strings=strings,
                functionNames=list(derived['mapping']['functionNames']),
                format='etm-trace-store-query-index', version=1, container='v2',
                source='native-facts-numpy')
    nav = remaining['nav_meta']
    checksum = zlib.crc32(np.ascontiguousarray(tables['pc_cell_offsets'],
                                               dtype=np.uint32).tobytes())
    checksum = zlib.crc32(np.ascontiguousarray(tables['pc_cells'],
                                               dtype=np.uint32).tobytes(), checksum)
    checksum = zlib.crc32(np.ascontiguousarray(tables['line_start'],
                                               dtype=np.uint32).tobytes(), checksum)
    checksum = zlib.crc32(np.ascontiguousarray(tables['line_id'],
                                               dtype=np.uint32).tobytes(), checksum)
    nav['checksum'] = checksum & 0xFFFFFFFF
    meta['nav'] = nav
    tables['node_parent'] = remaining['node_parent'].astype(np.int32)
    # node_label 已由 derived 提供（本地 strings 下标）
    timings['tablesSeconds'] = round(time.perf_counter() - started, 4)
    if compute_defaults:
        phase = time.perf_counter()
        index = trace_store_index.QueryIndex(tables, meta, view)
        window = index.window(None, None, trace_store_index.DEFAULT_WINDOW_BINS)
        timeline = index.function_timeline(None, None,
                                           bins=trace_store_index.DEFAULT_TIMELINE_BINS)
        meta['defaultViews'] = {}
        for name, value, bins in (
                ('window', window, trace_store_index.DEFAULT_WINDOW_BINS),
                ('timeline', timeline, trace_store_index.DEFAULT_TIMELINE_BINS)):
            text = json.dumps(value, ensure_ascii=False)
            tables['default_%s_json' % name] = np.frombuffer(text.encode('utf-8'), np.uint8)
            meta['defaultViews'][name] = dict(
                bins=int(bins), bytes=len(text.encode('utf-8')),
                sha256=hashlib.sha256(text.encode('utf-8')).hexdigest())
        timings['defaultViewsSeconds'] = round(time.perf_counter() - phase, 4)
    meta['tables'] = sorted(tables)
    meta['coverage'] = [int(first_seq), int(last_seq)]
    required = [name for name in COMPAT_PENDING
                if compute_defaults or not name.startswith('default_')]
    meta['compatPending'] = [name for name in required if name not in tables]
    phase = time.perf_counter()
    payload = trace_store_index.encode_payload_v2(tables, meta, level=1, workers=4)
    timings['encodeSeconds'] = round(time.perf_counter() - phase, 4)
    timings['totalSeconds'] = round(time.perf_counter() - started, 4)
    return dict(arrays=tables, meta=meta, timings=timings, payload=payload,
                counters=counters.as_dict(), derived=derived, remaining=remaining)


# ---------------------------------------------------------------------------------------------
# v2 编码（复用 trace_store_index 的容器，不改语义）
# ---------------------------------------------------------------------------------------------
COMPAT_PENDING = [
    'fs_start', 'fs_func', 'fs_count',            # 函数区间：下一批（需路径/函数事实）
    'ps_start', 'ps_count', 'ps_leaf',            # 路径区间：等 PATH_RUNS/NODES（t44）
    'node_parent', 'node_label', 'node_self_cells', 'node_self_nodes',
    'node_self_counts', 'node_self_cycles',
    'cell_node_value', 'cell_node_value_cycles',  # 节点 include/self：等路径事实
    'cell_anchor_block', 'cell_anchor_offset', 'cell_anchor_skip',   # anchor：等 t42b
    'ev_seq', 'ev_type', 'ev_text', 'ev_flags', 'ev_payload', 'ev_next_func',  # 本批未用
    'default_window_json', 'default_timeline_json',  # 默认视图：下一批用 QueryIndex 算法
    'pc_cell_offsets', 'pc_cells', 'line_start', 'line_id',          # nav 表：等 anchor/PC 事实
]


def _node_chain_texts(node, nodes, strings):
    chain = []
    guard = 0
    while node >= 0:
        chain.append(strings[int(nodes['label'][node])])
        node = int(nodes['parent'][node])
        guard += 1
        if guard > 10_000:
            raise FactsError('节点父链异常')
    return list(reversed(chain))


def _v1_chain_texts(node, parent, label, index):
    chain = []
    guard = 0
    while node >= 0:
        value = int(label[node])
        chain.append('' if value < 0 else index.strings[value])
        node = int(parent[node])
        guard += 1
        if guard > 10_000:
            raise FactsError('v1 节点父链异常')
    return list(reversed(chain))


def derived_arrays(derived):
    """派生结果 → 可编码数组表（只含真正派生出的表，其余不填零）。"""
    tables = {name: np.asarray(derived[name]) for name in
              ('cell_insn', 'cell_executed', 'cell_unknown', 'cell_not_taken',
               'cell_cycles', 'cell_cycle_runs', 'cell_first_seq',
               'cell_hotspot_count', 'cell_hotspot_unknown', 'cell_hotspot_first')}
    mapping = derived['mapping']
    tables['st_func'] = mapping['funcOfId'].astype(np.int16)
    tables['st_hotspot'] = mapping['hotspotOfId'].astype(np.int32)
    tables['st_flow'] = mapping['flowOfId'].astype(np.int8)
    tables['st_pc'] = mapping['pcOfId'].astype(np.uint32)
    tables['st_size'] = mapping['sizeOfId'].astype(np.uint8)
    tables['st_pred'] = mapping['predicatedOfId'].astype(np.uint8)
    tables['h_func'] = mapping['hotspotFunc'].astype(np.int16)
    tables['h_line'] = mapping['hotspotLine'].astype(np.int32)
    if 'h_file' in derived:
        tables['h_file'] = np.asarray(derived['h_file']).astype(np.int32)
    for name in ('fs_start', 'fs_count', 'fs_func', 'ps_start', 'ps_count', 'ps_leaf',
                 'cell_anchor_block', 'cell_anchor_offset', 'cell_anchor_skip',
                 'node_parent', 'node_label', 'ev_seq', 'ev_type', 'ev_flags',
                 'ev_payload', 'ev_next_func', 'ev_text'):
        if name in derived:
            tables[name] = np.asarray(derived[name])
    return tables


def encode_tables(derived, extra_meta=None):
    """把派生表编码成块 12 v2 payload（复用 trace_store_index.encode_payload_v2）。"""
    arrays = derived_arrays(derived)
    meta = dict(cellSize=int(derived['cellSize']), instructions=int(derived['instructions']),
                cells=int(derived['cells']),
                staticCount=len(derived['mapping']['funcOfId']),
                hotspotCount=int(np.asarray(derived['cell_hotspot_count']).shape[1]),
                format='etm-trace-store-query-index', version=1, container='v2',
                source='native-facts-numpy', tables=sorted(arrays),
                functionNames=list(derived['mapping']['functionNames']),
                strings=derived.get('strings'),
                compatPending=[name for name in COMPAT_PENDING if name not in arrays],
                note='仅派生 static 映射与 cell/hotspot 聚合；其余表未派生，不填零冒充')
    if extra_meta:
        meta.update(extra_meta)
    payload = trace_store_index.encode_payload_v2(arrays, meta)
    return payload, meta


def decode_tables(payload):
    """读回 v2 payload（用 trace_store_index 的读取分支），用于 round-trip 校验。"""
    archive = trace_store_index.decode_payload(payload)
    return {name: np.asarray(archive[name]) for name in archive.files}, archive


def compare_with_v1(derived, store):
    """与 v1 store 的块 12 做**规范化**对照（数组逐项 + 静态热点按 (function,file,line)）。

    v1 的 h_file 是 meta.strings 下标、h_func 是函数 id；这里全部还原成文本/名称再比较，
    不要求内部 ID 相同。返回 dict(problems, ok)。
    """
    index = store.query_index
    if index is None:
        return dict(problems=[dict(kind='no-query-index')], ok=False)
    arrays = index.arrays
    tables = derived_arrays(derived)
    problems = []
    # st_hotspot 只按 (function,file,line) 键规范化比较（v1 的 hotspot id 按指令首现、
    # 本实现按静态项首现，编号不同是允许的），不做数值比较。
    for name in ('cell_insn', 'cell_executed', 'cell_unknown', 'cell_not_taken',
                 'cell_cycles', 'cell_cycle_runs', 'cell_first_seq',
                 'cell_hotspot_count', 'cell_hotspot_unknown', 'cell_hotspot_first',
                 'st_func', 'st_flow', 'st_pc', 'st_size', 'st_pred'):
        reference = np.asarray(arrays[name]).astype(np.int64)
        got = np.asarray(tables[name]).astype(np.int64)
        if reference.shape != got.shape:
            problems.append(dict(array=name, shape=[list(reference.shape), list(got.shape)]))
            continue
        if reference.ndim == 2:
            bad = np.argwhere(reference != got)
            if bad.size:
                problems.append(dict(array=name, count=int(bad.shape[0]),
                                     examples=[[int(row), int(col), int(reference[row, col]),
                                                int(got[row, col])] for row, col in bad[:5]]))
        else:
            bad = np.flatnonzero(reference != got)
            if bad.size:
                problems.append(dict(array=name, count=int(bad.size),
                                     examples=[[int(i), int(reference[i]), int(got[i])]
                                               for i in bad[:5]]))
    # fs_*：区间直接比较，函数 id 按名称归一化
    if 'fs_start' in tables:
        for name in ('fs_start', 'fs_count'):
            reference = np.asarray(arrays[name]).astype(np.int64)
            got = np.asarray(tables[name]).astype(np.int64)
            if reference.shape != got.shape or not np.array_equal(reference, got):
                problems.append(dict(array=name, shape=[list(reference.shape),
                                                        list(got.shape)]))
        if np.array_equal(np.asarray(tables['fs_start']).astype(np.int64),
                          np.asarray(arrays['fs_start']).astype(np.int64)):
            got_names = [derived['mapping']['functionNames'][int(value)]
                         for value in tables['fs_func']]
            ref_names = [index.function_names[int(value)]
                         for value in np.asarray(arrays['fs_func'])]
            bad = [i for i, (left, right) in enumerate(zip(got_names, ref_names))
                   if left != right]
            if bad:
                problems.append(dict(kind='fs_func_text', count=len(bad),
                                     examples=bad[:5]))
    # ps_*：区间直接比较；leaf 用「根到叶的标签文本链」规范化
    if 'ps_start' in tables:
        for name in ('ps_start', 'ps_count'):
            reference = np.asarray(arrays[name]).astype(np.int64)
            got = np.asarray(tables[name]).astype(np.int64)
            if reference.shape != got.shape or not np.array_equal(reference, got):
                problems.append(dict(array=name, shape=[list(reference.shape),
                                                        list(got.shape)]))
        if np.array_equal(np.asarray(tables['ps_start']).astype(np.int64),
                          np.asarray(arrays['ps_start']).astype(np.int64)):
            # 用派生表的 node_parent/node_label（label 已是本实现 strings 池下标）；
            # 注意不能用 facts 原始 NODES（其 label 是 LABELS id，与本池编号不同）。
            nodes = dict(parent=np.asarray(tables['node_parent']),
                         label=np.asarray(tables['node_label']))
            ref_parent = np.asarray(arrays['node_parent'])
            ref_label = np.asarray(arrays['node_label'])
            got_chains = [_node_chain_texts(int(leaf), nodes, derived.get('strings') or [])
                          for leaf in np.asarray(tables['ps_leaf'])]
            ref_chains = [_v1_chain_texts(int(leaf), ref_parent, ref_label, index)
                          for leaf in np.asarray(arrays['ps_leaf'])]
            bad = [i for i, (left, right) in enumerate(zip(got_chains, ref_chains))
                   if left != right]
            if bad:
                problems.append(dict(kind='ps_leaf_text', count=len(bad),
                                     examples=[[i, got_chains[i], ref_chains[i]]
                                               for i in bad[:3]]))
    # events：seq/type/flags/payload 直接比较，文本/nextFunc 按文本归一化
    if 'ev_seq' in tables:
        for name in ('ev_seq', 'ev_type', 'ev_flags', 'ev_payload'):
            reference = np.asarray(arrays[name]).astype(np.int64)
            got = np.asarray(tables[name]).astype(np.int64)
            if reference.shape != got.shape or not np.array_equal(reference, got):
                problems.append(dict(array=name, shape=[list(reference.shape),
                                                        list(got.shape)]))
        strings = derived.get('strings') or ['']
        got_texts = [strings[int(value)] for value in tables['ev_text']]
        ref_texts = [index.strings[int(value)] for value in np.asarray(arrays['ev_text'])]
        bad = [i for i, (left, right) in enumerate(zip(got_texts, ref_texts))
               if left != right]
        if bad:
            problems.append(dict(kind='ev_text', count=len(bad),
                                 examples=[[i, got_texts[i], ref_texts[i]] for i in bad[:3]]))
        labels = derived.get('labels_raw') or []
        got_next = [None if int(value) < 0 else labels[int(value)]
                    for value in tables['ev_next_func']]
        ref_next = [None if int(value) < 0 else index.function_names[int(value)]
                    for value in np.asarray(arrays['ev_next_func'], np.int64)]
        bad = [i for i, (left, right) in enumerate(zip(got_next, ref_next))
               if left != right]
        if bad:
            problems.append(dict(kind='ev_next_func_text', count=len(bad),
                                 examples=[[i, got_next[i], ref_next[i]] for i in bad[:3]]))
    if len(store.static) != len(derived['mapping']['funcOfId']):
        problems.append(dict(kind='staticCount', reference=len(store.static),
                             facts=len(derived['mapping']['funcOfId'])))
        return dict(problems=problems, ok=False)
    reference_names = [index.function_names[int(np.asarray(arrays['h_func'])[
        int(np.asarray(arrays['st_hotspot'])[identifier])])] for identifier in
        range(len(store.static))]
    reference_keys = []
    for identifier, entry in enumerate(store.static):
        hotspot = int(np.asarray(arrays['st_hotspot'])[identifier])
        file_id = int(np.asarray(arrays['h_file'])[hotspot])
        file_name = None if file_id < 0 else index.strings[file_id]
        line = int(np.asarray(arrays['h_line'])[hotspot])
        reference_keys.append([reference_names[identifier], file_name,
                               None if line < 0 else line])
    mapping = derived['mapping']
    got_keys = [[str(mapping['functionNames'][int(mapping['hotspotFunc'][hid])]),
                 None if mapping['hotspotFile'][hid] is None
                 else str(mapping['hotspotFile'][hid]),
                 None if int(mapping['hotspotLine'][hid]) < 0
                 else int(mapping['hotspotLine'][hid])]
                for hid in range(len(mapping['hotspotFunc']))]
    got_keys_per_static = [got_keys[int(value)] for value in mapping['hotspotOfId']]
    bad = [position for position, (left, right) in
           enumerate(zip(reference_keys, got_keys_per_static)) if left != right]
    if bad:
        problems.append(dict(kind='staticHotspotKeys', count=len(bad),
                             examples=[[int(i), reference_keys[i],
                                        got_keys_per_static[i]] for i in bad[:5]]))
    return dict(problems=problems, ok=not problems)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='native_facts_index.py',
        description='C2：NumPy 消费 C1 facts → cell/hotspot 表（v2 编码，不改容器语义）')
    sub = parser.add_subparsers(dest='command', required=True)
    derive = sub.add_parser('derive', help='派生 cell/hotspot 表并报告计时/RSS/计数')
    derive.add_argument('--facts', required=True)
    derive.add_argument('--static-json', required=True)
    derive.add_argument('--reference-store', default=None)
    derive.add_argument('--cell-size', type=int, default=4096)
    derive.add_argument('--batch', type=int, default=1 << 20)
    derive.add_argument('--encode', action='store_true')
    derive.add_argument('--out', default=None)
    args = parser.parse_args(argv)
    if args.command != 'derive':
        return 2
    facts = read_facts_any(args.facts)
    static_entries = json.loads(Path(args.static_json).read_text(encoding='utf-8'))
    counters = Counters()
    started = time.perf_counter()
    derived = derive_tables(facts, static_entries, cell_size=args.cell_size,
                            batch=args.batch, counters=counters)
    seconds = round(time.perf_counter() - started, 4)
    result = dict(facts=args.facts, factsVersion=int(facts.get('version') or 1),
                  seconds=seconds,
                  secondsPerMillion=round(seconds * 1e6 / int(derived['instructions']), 4),
                  timings=derived['timings'], counters=counters.as_dict(),
                  cells=derived['cells'], instructions=derived['instructions'])
    try:
        import psutil
        result['peakRssBytes'] = int(psutil.Process().memory_info().rss)
    except Exception:
        result['peakRssBytes'] = 0
    if args.reference_store:
        import trace_store
        store = trace_store.TraceStore(Path(args.reference_store), verify='sizes')
        try:
            result['reference'] = compare_with_v1(derived, store)
        finally:
            store.close()
    if args.encode:
        payload, meta = encode_tables(derived)
        arrays, _archive = decode_tables(payload)
        result['encode'] = dict(
            payloadBytes=len(payload), tables=sorted(arrays),
            compatPending=meta['compatPending'],
            roundtripEqual=all(
                np.array_equal(np.asarray(arrays[name]), np.asarray(derived[name]))
                for name in ('cell_insn', 'cell_hotspot_count', 'cell_first_seq')))
    result['ok'] = (result.get('reference', {}).get('ok', True)
                    and result.get('encode', {}).get('roundtripEqual', True))
    if args.out:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1),
                                  encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
