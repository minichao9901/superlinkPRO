"""R1-B 导航快速路径（instruction ±1 / NAV / PC / 源码）。

设计来源：`docs/etm-r1-query-design.md` §4（导航设计）与 §6.2（p2 接口）。
集成点：`trace_store.TraceStore.navigate()` 在存在查询索引时调用
``navigate_fast(store, seq, direction, mode, pc, start, end)``；返回值
``{"seq": int|None, "wrapped": bool}`` 表示“已用快速路径回答”；返回 ``None``
表示快路径不可用/不适用（调用方回退原 `_scan` 慢路径，语义不变）。

索引
----
* **instruction ±1**：用 ``QueryIndex.event_seqs`` 与 ``next/prev_instruction_seq``
  在事件位次上直接定位，不展开事件字典、不重放 CallTracker（展开数 0）。
* **call / return / exception / issue**：沿用 store 已有的稀疏 NAV 表（bisect）。
* **PC 导航**：静态 ID（``store.static_by_pc``）+ **PC_CELLS**（每个静态 ID 的出现
  单元格 CSR）+ 单元格有界解码（``QueryIndex._decode_instructions``，≤2 个单元格）。
* **源码导航**：静态 ID + **LINE_RUNS**（(file,line) 极大区间），二分定位、无需展开。

PC_CELLS / LINE_RUNS 在 t4 的块 12 索引里不包含（t4 范围只做 window/timeline），
本模块提供独立 **nav sidecar**：``<store>/nav-index/<generation>.nav``。它由索引迁移
阶段一次性构建（``python -m trace_store_nav build --store <dir>``），**不计入查询门槛**；
缺失/损坏/版本不符时 PC/源码快路径返回 ``None``（显式回退慢路径，绝不返回猜测结果）。
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import zlib
import sys
import time
from bisect import bisect_left, bisect_right
from pathlib import Path

import numpy as np

import trace_store

NAV_SIDECAR_DIR = 'nav-index'
NAV_MAGIC = b'ETMNAV1\x00'
NAV_VERSION = 2
NAV_HEADER = struct.Struct('<8sIQIIQQQ32sI')
CELL_INSTRUCTIONS = 4096
VALID_MODES = ('instruction', 'call', 'return', 'exception', 'issue', 'source')
NAV_MODES = {'call': trace_store.NAV_CALL, 'return': trace_store.NAV_RETURN,
             'exception': trace_store.NAV_EXCEPTION, 'issue': trace_store.NAV_ISSUE}
SIDECAR_SUFFIX = '.nav'
# 诊断开关：=sidecar 时跳过块 12 内置表、强制走 sidecar（对照/复现用；不改变结果）。
SOURCE_ENV = 'TRACE_STORE_NAV_SOURCE'


class NavIndexError(ValueError):
    """导航索引缺失/损坏/版本不符（调用方应回退慢路径）。"""


# ---------------------------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------------------------

def _decode_run_record(store, payload, begin, finish):
    """解析 REC_RUN 记录，返回 (seq, count, ids)；与读侧展开段格式一致。"""
    cursor = begin
    seq, cursor = trace_store._dvar(payload, cursor)
    count, cursor = trace_store._dvar(payload, cursor)
    cursor += 1                                  # tail 状态（导航索引不需要）
    _offset, cursor = trace_store._dvar(payload, cursor)
    _cycles, cursor = trace_store._dvar(payload, cursor)
    _span, cursor = trace_store._dvar(payload, cursor)
    _unknown, cursor = trace_store._dvar(payload, cursor)
    first, cursor = trace_store._dvar(payload, cursor)
    identifiers = [first]
    previous = first
    for _ in range(count - 1):
        delta, cursor = trace_store._dzvar(payload, cursor)
        previous += delta
        identifiers.append(previous)
    if cursor != finish:
        raise NavIndexError('展开段记录长度不符（seq=%d）' % seq)
    return seq, count, identifiers


def build_nav_index(store_or_dir, persist=True, progress=None, force_sidecar=False):
    """构建 nav sidecar 并返回摘要；已存在且自洽时直接复用。

    ``persist=False`` 时只在内存返回（测试/诊断用，不写文件）。
    """
    if isinstance(store_or_dir, trace_store.TraceStore):
        store = store_or_dir
        owns_store = False
    else:
        store = trace_store.TraceStore(store_or_dir, verify='sizes')
        owns_store = True
    started = time.perf_counter()
    try:
        block_tables = None
        index = getattr(store, 'query_index', None)
        if index is not None and not force_sidecar:
            try:
                block_tables = index.nav_tables()
            except Exception:                       # noqa: BLE001 - 坏表按“无表”处理
                block_tables = None
        if block_tables:
            summary = dict(store=str(store.folder), generation=store.generation,
                           generationTag='0x%016x' % int(store.container.generation_tag),
                           instructions=int(block_tables['instructions']),
                           staticCount=int(block_tables['static_count']),
                           pcCellPairs=int(block_tables['pcCellPairs']),
                           lineRuns=int(block_tables['lineRuns']),
                           cellSize=int(block_tables['cell_size']),
                           source='block12', sidecarWritten=False,
                           buildSeconds=round(time.perf_counter() - started, 3),
                           bytes=int(block_tables['offsets'].nbytes
                                     + block_tables['cells'].nbytes
                                     + block_tables['line_start'].nbytes
                                     + block_tables['line_id'].nbytes))
            if persist:
                summary['note'] = '块 12 已内置 PC_CELLS/LINE_RUNS，不重复写 sidecar'
            return summary
        static = store.static
        static_count = len(static)
        instructions = int(store.counts.get('instructions') or 0)
        if not instructions:
            raise NavIndexError('记录没有指令，无法构建导航索引')
        file_ids = {}
        file_of_id = np.empty(static_count, np.int32)
        line_of_id = np.empty(static_count, np.int64)
        line_none = np.zeros(static_count, bool)
        for identifier, entry in enumerate(static):
            name = entry.file
            if name is None:
                file_of_id[identifier] = -1
            else:
                file_id = file_ids.get(name)
                if file_id is None:
                    file_id = len(file_ids)
                    file_ids[name] = file_id
                file_of_id[identifier] = file_id
            if entry.line is None:
                line_none[identifier] = True
            else:
                line_of_id[identifier] = int(entry.line)
        file_list = file_of_id.tolist()
        line_list = line_of_id.tolist()
        none_list = line_none.tolist()

        last_cell = [-1] * static_count
        cells_by_id = [[] for _ in range(static_count)]
        line_starts = []
        line_ids = []
        current_file = None
        current_line = None
        current_none = None
        instruction_index = 0
        sequences = store.sequence_blocks
        for ordinal, block_number in enumerate(sequences):
            payload = store.container.block(block_number)
            for record_type, begin, finish in store._records(payload, 0):
                if record_type != trace_store.REC_RUN:
                    continue
                _seq, count, identifiers = _decode_run_record(store, payload, begin, finish)
                base = instruction_index
                for offset, identifier in enumerate(identifiers):
                    index = base + offset
                    cells = cells_by_id[identifier]
                    cell = index // CELL_INSTRUCTIONS
                    if cell != last_cell[identifier]:
                        cells.append(cell)
                        last_cell[identifier] = cell
                    file_value = file_list[identifier]
                    line_value = line_list[identifier]
                    is_none = none_list[identifier]
                    if current_none is None or is_none != current_none \
                            or (not is_none and (file_value != current_file
                                                 or line_value != current_line)):
                        line_starts.append(index)
                        line_ids.append(identifier)
                        current_none = is_none
                        current_file = file_value
                        current_line = line_value
                instruction_index += count
                if progress is not None and instruction_index and \
                        instruction_index // 2000000 != (instruction_index - count) // 2000000:
                    progress('  导航索引：已扫描 %d 条指令' % instruction_index)
        if instruction_index != instructions:
            raise NavIndexError('导航索引指令数与 store 不符：%d≠%d'
                                % (instruction_index, instructions))
        pair_count = sum(len(values) for values in cells_by_id)
        offsets = np.zeros(static_count + 1, np.uint32)
        total = 0
        for identifier, values in enumerate(cells_by_id):
            offsets[identifier] = total
            total += len(values)
        offsets[static_count] = total
        cells = np.empty(pair_count, np.uint32)
        position = 0
        for values in cells_by_id:
            if values:
                cells[position:position + len(values)] = values
                position += len(values)
        line_start_array = np.asarray(line_starts, dtype=np.uint32)
        line_id_array = np.asarray(line_ids, dtype=np.uint32)
        manifest = store.manifest or {}
        files = manifest.get('files') or []
        store_sha = bytes.fromhex(str(files[0].get('sha256')))
        checksum = zlib.crc32(offsets.tobytes())
        checksum = zlib.crc32(cells.tobytes(), checksum)
        checksum = zlib.crc32(line_start_array.tobytes(), checksum)
        checksum = zlib.crc32(line_id_array.tobytes(), checksum) & 0xFFFFFFFF
        header = NAV_HEADER.pack(NAV_MAGIC, NAV_VERSION, int(store.container.generation_tag),
                                 CELL_INSTRUCTIONS, static_count, instructions,
                                 pair_count, len(line_starts), store_sha, checksum)
        summary = dict(store=str(store.folder), generation=store.generation,
                       generationTag='0x%016x' % int(store.container.generation_tag),
                       instructions=instructions, staticCount=static_count,
                       pcCellPairs=pair_count, lineRuns=len(line_starts),
                       cellSize=CELL_INSTRUCTIONS, source='sidecar',
                       buildSeconds=round(time.perf_counter() - started, 3))
        if persist:
            path = nav_sidecar_path(store)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + '.tmp-%d' % os.getpid())
            with open(temporary, 'wb') as handle:
                handle.write(header)
                offsets.tofile(handle)
                cells.tofile(handle)
                line_start_array.tofile(handle)
                line_id_array.tofile(handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            summary['path'] = str(path)
            summary['sidecarBytes'] = path.stat().st_size
        summary['headerBytes'] = NAV_HEADER.size
        summary['bytes'] = (NAV_HEADER.size + offsets.nbytes + cells.nbytes
                            + line_start_array.nbytes + line_id_array.nbytes)
        return summary
    finally:
        if owns_store:
            store.close()


# ---------------------------------------------------------------------------------------------
# 加载与查询
# ---------------------------------------------------------------------------------------------

def nav_sidecar_path(store):
    return Path(store.folder) / NAV_SIDECAR_DIR / (store.generation + SIDECAR_SUFFIX)


class NavData:
    __slots__ = ('offsets', 'cells', 'line_start', 'line_id', 'static_count',
                 'instructions', 'cell_size', 'generation_tag', 'path', 'bytes', 'source')

    def __init__(self, offsets, cells, line_start, line_id, static_count, instructions,
                 cell_size, generation_tag, path, size, source='sidecar'):
        self.offsets = offsets
        self.cells = cells
        self.line_start = line_start
        self.line_id = line_id
        self.static_count = static_count
        self.instructions = instructions
        self.cell_size = cell_size
        self.generation_tag = generation_tag
        self.path = path
        self.bytes = size
        self.source = source

    def cells_of(self, static_id):
        low = int(self.offsets[static_id])
        high = int(self.offsets[static_id + 1])
        return self.cells[low:high]

    def run_at(self, instruction_index):
        position = bisect_right(self.line_start, instruction_index) - 1
        if position < 0:
            raise NavIndexError('源码行区间索引缺失（instruction=%d）' % instruction_index)
        return position


def _nav_from_block12(store):
    """块 12 内置 PC_CELLS/LINE_RUNS 时构造 NavData；无表返回 None，表坏抛 NavIndexError。"""
    if os.environ.get(SOURCE_ENV, '').strip().lower() == 'sidecar':
        return None
    index = getattr(store, 'query_index', None)
    if index is None:
        return None
    try:
        tables = index.nav_tables()
    except Exception as error:                       # noqa: BLE001 - 显式回退 sidecar/慢路径
        raise NavIndexError('块 12 导航表不可用：%s' % error)
    if not tables:
        return None
    if int(tables['cell_size']) != CELL_INSTRUCTIONS:
        raise NavIndexError('块 12 导航表 cellSize 不符：%d' % int(tables['cell_size']))
    if int(tables['static_count']) != len(store.static):
        raise NavIndexError('块 12 导航表 staticCount 不符')
    if int(tables['instructions']) != int(store.counts.get('instructions') or 0):
        raise NavIndexError('块 12 导航表指令数不符')
    return NavData(tables['offsets'], tables['cells'], tables['line_start'],
                   tables['line_id'], int(tables['static_count']),
                   int(tables['instructions']), int(tables['cell_size']),
                   int(store.container.generation_tag),
                   'block12:%s' % store.generation,
                   int(tables['offsets'].nbytes + tables['cells'].nbytes
                       + tables['line_start'].nbytes + tables['line_id'].nbytes),
                   source='block12')


def _load_nav_index(store):
    """加载导航索引：块 12 内置表优先，旧记录回退 sidecar；都不可用返回 None。"""
    cached = getattr(store, '_nav_data', None)
    if cached is not None:
        return cached
    block_error = None
    try:
        data = _nav_from_block12(store)
    except NavIndexError as error:
        data = None
        block_error = str(error)
    if data is not None:
        setattr(store, '_nav_data', data)
        setattr(store, '_nav_error', None)
        return data
    path = nav_sidecar_path(store)
    error = None
    if not path.is_file():
        error = '缺少导航索引 %s（先运行 python -m trace_store_nav build）' % path
        data = None
    else:
        try:
            raw = path.read_bytes()
            if len(raw) < NAV_HEADER.size:
                raise NavIndexError('导航索引头部被截断')
            (magic, version, tag, cell_size, static_count, instructions, pair_count,
             line_count, store_sha, checksum) = NAV_HEADER.unpack_from(raw, 0)
            if magic != NAV_MAGIC:
                raise NavIndexError('导航索引 magic 不符')
            if version != NAV_VERSION:
                raise NavIndexError('导航索引版本不支持：%d' % version)
            if tag != int(store.container.generation_tag):
                raise NavIndexError('导航索引 generation 不符（索引 %d，store %d）'
                                    % (tag, int(store.container.generation_tag)))
            if static_count != len(store.static):
                raise NavIndexError('导航索引 staticCount 不符')
            if instructions != int(store.counts.get('instructions') or 0):
                raise NavIndexError('导航索引指令数不符')
            if cell_size != CELL_INSTRUCTIONS:
                raise NavIndexError('导航索引 cellSize 不符：%d' % cell_size)
            files = (store.manifest or {}).get('files') or []
            expected_sha = bytes.fromhex(str(files[0].get('sha256')))
            if store_sha != expected_sha:
                raise NavIndexError('导航索引与 store.bin sha256 不符')
            offset = NAV_HEADER.size
            expected = (offset + (static_count + 1) * 4 + pair_count * 4
                        + line_count * 8)
            if len(raw) != expected:
                raise NavIndexError('导航索引长度不符：%d≠%d' % (len(raw), expected))
            offsets = np.frombuffer(raw, np.uint32, static_count + 1,
                                    offset).astype(np.int64)
            offset += (static_count + 1) * 4
            cells = np.frombuffer(raw, np.uint32, pair_count, offset).astype(np.int64)
            offset += pair_count * 4
            line_start = np.frombuffer(raw, np.uint32, line_count, offset).astype(np.int64)
            offset += line_count * 4
            line_id = np.frombuffer(raw, np.uint32, line_count, offset).astype(np.int64)
            if line_count and (line_start[0] != 0
                               or not bool(np.all(line_start[1:] > line_start[:-1]))):
                raise NavIndexError('导航索引行区间未严格递增')
            if len(offsets) and (offsets[0] != 0
                                 or not bool(np.all(offsets[1:] >= offsets[:-1]))):
                raise NavIndexError('导航索引 PC 出现位置偏移非法')
            actual = zlib.crc32(offsets.astype(np.uint32).tobytes())
            actual = zlib.crc32(cells.astype(np.uint32).tobytes(), actual)
            actual = zlib.crc32(line_start.astype(np.uint32).tobytes(), actual)
            actual = zlib.crc32(line_id.astype(np.uint32).tobytes(), actual) & 0xFFFFFFFF
            if actual != checksum:
                raise NavIndexError('导航索引内容 CRC32 不符（文件损坏/被改写）')
            data = NavData(offsets, cells, line_start, line_id, static_count,
                           instructions, cell_size, tag, path, len(raw), source='sidecar')
        except Exception as error_value:          # noqa: BLE001 - 一律显式回退慢路径
            data = None
            error = str(error_value)
    if data is None and block_error:
        error = '%s；块 12：%s' % (error, block_error)
    setattr(store, '_nav_data', data)
    setattr(store, '_nav_error', error)
    return data


def nav_status(store):
    """诊断：导航侧车是否可用及原因（只读加载，不触发构建）。"""
    data = _load_nav_index(store)
    error = getattr(store, '_nav_error', None)
    if data is not None:
        return dict(available=True, source=data.source, path=str(data.path),
                    bytes=data.bytes, staticCount=data.static_count,
                    instructions=data.instructions, cellSize=data.cell_size,
                    pcCellPairs=int(data.offsets[-1]), lineRuns=len(data.line_start),
                    generationTag='0x%016x' % data.generation_tag)
    return dict(available=False, error=error)


def last_stats(store):
    """最近一次 navigate_fast 的结构计数（证据/诊断用）。"""
    return dict(getattr(store, '_nav_last_stats', {}))


def _decode_cell(store, index, cell):
    """有界解码一个单元格（≤4096 条指令）；返回 (base, ids)。"""
    low = cell * CELL_INSTRUCTIONS
    high = min(int(store.counts.get('instructions') or 0) - 1,
               low + CELL_INSTRUCTIONS - 1)
    ids, _executed, _cycles, _seqs = index._decode_instructions(low, high)
    stats = getattr(store, '_nav_stats_inner', None)
    if stats is not None:
        stats['decoded_cells'] += 1
        stats['decoded_instructions'] += len(ids)
    return low, ids


def _find_pc_forward(store, index, nav, static_id, from_instruction, hi_instruction):
    cells = nav.cells_of(static_id)
    if not len(cells):
        return None
    position = bisect_left(cells, from_instruction // CELL_INSTRUCTIONS)
    while position < len(cells):
        cell = int(cells[position])
        if cell * CELL_INSTRUCTIONS > hi_instruction:
            return None
        base, ids = _decode_cell(store, index, cell)
        for offset, identifier in enumerate(ids):
            instruction = base + offset
            if instruction < from_instruction:
                continue
            if instruction > hi_instruction:
                return None
            if identifier == static_id:
                return instruction
        position += 1
    return None


def _find_pc_backward(store, index, nav, static_id, from_instruction, lo_instruction):
    cells = nav.cells_of(static_id)
    if not len(cells):
        return None
    position = bisect_right(cells, from_instruction // CELL_INSTRUCTIONS) - 1
    while position >= 0:
        cell = int(cells[position])
        if (cell + 1) * CELL_INSTRUCTIONS - 1 < lo_instruction:
            return None
        base, ids = _decode_cell(store, index, cell)
        for offset in range(len(ids) - 1, -1, -1):
            instruction = base + offset
            if instruction > from_instruction:
                continue
            if instruction < lo_instruction:
                return None
            if ids[offset] == static_id:
                return instruction
        position -= 1
    return None


def _ensure_decode_ready(index):
    """`_decode_instructions` 只需要 event_seqs/cell_size/_st_pred_list。

    t4 的 ``QueryIndex.prepare()`` 会计算 ps_start_seq/fs_* 等窗口派生量（19.86M 上数百 ms），
    对导航是浪费；这里只补齐边界解码真正用到的字段，避免首次 PC 查询被 prepare 拖慢。
    """
    if not hasattr(index, '_st_pred_list'):
        index._st_pred_list = index.arrays['st_pred'].astype(bool).tolist()
    if not hasattr(index, 'cell_size'):
        index.cell_size = int(index.meta['cellSize'])


def _source_current(store, index, nav, seq):
    """慢路径 `current_file/current_line` 的等价取值（事件 → ('',0)）。"""
    if not (store.first_seq <= seq <= store.last_seq):
        return '', 0, None
    events = index.event_seqs
    position = int(np.searchsorted(events, seq, side='left'))
    if position < len(events) and int(events[position]) == seq:
        return '', 0, True                      # seq 本身是事件
    instruction = int(index.insn_of_seq(seq))
    run = nav.run_at(instruction)
    entry = store.static[int(nav.line_id[run])]
    return (entry.file or ''), (entry.line or 0), False


def _source_forward(store, index, nav, seq, current_file, current_line,
                    lo_instruction, hi_instruction):
    start = max(seq + 1, store.first_seq)
    if start > store.last_seq:
        return None
    candidate_seq = index.next_instruction_seq(start)
    if candidate_seq > store.last_seq:
        return None
    candidate = int(index.insn_of_seq(candidate_seq))
    if candidate > hi_instruction:
        return None
    position = bisect_right(nav.line_start, candidate) - 1
    if position < 0:
        position = 0
    count = len(nav.line_start)
    while position < count and int(nav.line_start[position]) <= hi_instruction:
        run_start = int(nav.line_start[position])
        run_end = (int(nav.line_start[position + 1]) - 1 if position + 1 < count
                   else int(store.counts['instructions']) - 1)
        target = max(run_start, candidate)
        if target > hi_instruction:
            return None
        entry = store.static[int(nav.line_id[position])]
        line = entry.line
        if line is not None and ((entry.file or '') != current_file
                                 or int(line) != current_line):
            return target
        position += 1
    return None


def _source_backward(store, index, nav, seq, current_file, current_line,
                     lo_instruction, hi_instruction):
    start = min(seq - 1, store.last_seq)
    if start < store.first_seq:
        return None
    candidate_seq = index.prev_instruction_seq(start)
    if candidate_seq < store.first_seq:
        return None
    candidate = int(index.insn_of_seq(candidate_seq))
    if candidate < lo_instruction:
        return None
    position = bisect_right(nav.line_start, candidate) - 1
    while position >= 0:
        run_start = int(nav.line_start[position])
        run_end = (int(nav.line_start[position + 1]) - 1
                   if position + 1 < len(nav.line_start)
                   else int(store.counts['instructions']) - 1)
        target = candidate if run_start <= candidate <= run_end else run_end
        if target < lo_instruction:
            return None
        entry = store.static[int(nav.line_id[position])]
        line = entry.line
        if line is not None and ((entry.file or '') != current_file
                                 or int(line) != current_line):
            return target
        if run_start <= lo_instruction:
            return None
        candidate = run_start - 1
        position -= 1
    return None


def navigate_fast(store, seq=0, direction=1, mode='instruction', pc=None,
                  start=None, end=None):
    """导航快速路径；返回结果字典或 None（None = 回退慢路径）。"""
    index = getattr(store, 'query_index', None)
    if index is None:
        return None
    seq = int(seq)
    forward = int(direction) > 0
    if mode not in VALID_MODES:
        return None                              # 让慢路径抛出同款“未知导航类型”
    scoped_lo = store.first_seq if start is None else max(store.first_seq, int(start))
    scoped_hi = store.last_seq if end is None else min(store.last_seq, int(end))
    stats = dict(expands=0, decoded_cells=0, decoded_instructions=0, path=mode,
                 direction=1 if forward else -1)
    setattr(store, '_nav_last_stats', stats)
    if scoped_hi < scoped_lo:
        return dict(seq=None, wrapped=False)
    if pc is None and mode in NAV_MODES:
        values = store.nav[NAV_MODES[mode]]
        if forward:
            position = bisect_right(values, seq)
            while position < len(values) and values[position] < scoped_lo:
                position += 1
            if position < len(values) and values[position] <= scoped_hi:
                return dict(seq=values[position], wrapped=False)
            return dict(seq=None, wrapped=False)
        position = bisect_left(values, seq) - 1
        while position >= 0 and values[position] > scoped_hi:
            position -= 1
        if position >= 0 and values[position] >= scoped_lo:
            return dict(seq=values[position], wrapped=False)
        return dict(seq=None, wrapped=False)
    if pc is None and mode == 'instruction':
        if forward:
            candidate = index.next_instruction_seq(max(seq + 1, scoped_lo))
            if candidate <= scoped_hi:
                return dict(seq=int(candidate), wrapped=False)
            return dict(seq=None, wrapped=False)
        candidate = index.prev_instruction_seq(min(seq - 1, scoped_hi))
        if candidate >= scoped_lo:
            return dict(seq=int(candidate), wrapped=False)
        return dict(seq=None, wrapped=False)
    # PC / 源码需要 NAV 侧车 + QueryIndex 派生量。
    if pc is not None:
        target = int(pc)
        static_id = store.static_by_pc.get(target)
        if static_id is None:
            return dict(seq=None, wrapped=False)
    nav = _load_nav_index(store)
    if nav is None:
        return None                              # 显式回退慢路径（原因见 nav_status）
    _ensure_decode_ready(index)
    setattr(store, '_nav_stats_inner', stats)
    try:
        if pc is not None:
            static_id = store.static_by_pc[int(pc)]
            found = None
            if forward:
                start_seq = max(seq + 1, scoped_lo)
                if start_seq <= scoped_hi:
                    target_seq = index.next_instruction_seq(start_seq)
                    if target_seq <= scoped_hi:
                        from_instruction = int(index.insn_of_seq(target_seq))
                        hi_bound = int(index.insn_of_seq(
                            index.prev_instruction_seq(scoped_hi)))
                        found = _find_pc_forward(store, index, nav, static_id,
                                                 from_instruction, hi_bound)
            else:
                start_seq = min(seq - 1, scoped_hi)
                if start_seq >= scoped_lo:
                    target_seq = index.prev_instruction_seq(start_seq)
                    if target_seq >= scoped_lo:
                        from_instruction = int(index.insn_of_seq(target_seq))
                        lo_bound = int(index.insn_of_seq(
                            index.next_instruction_seq(scoped_lo)))
                        found = _find_pc_backward(store, index, nav, static_id,
                                                  from_instruction, lo_bound)
            if found is not None:
                return dict(seq=int(index.seq_of_insn(found)), wrapped=False)
            lo_bound = int(index.insn_of_seq(index.next_instruction_seq(scoped_lo)))
            hi_bound = int(index.insn_of_seq(index.prev_instruction_seq(scoped_hi)))
            wrapped_found = _find_pc_forward(store, index, nav, static_id,
                                             lo_bound, hi_bound)
            if wrapped_found is not None:
                return dict(seq=int(index.seq_of_insn(wrapped_found)), wrapped=True)
            return dict(seq=None, wrapped=False)
        # 源码导航
        if not store.first_seq <= seq <= store.last_seq:
            current_file, current_line, _is_event = '', 0, False
        else:
            current_file, current_line, _is_event = _source_current(store, index, nav, seq)
        if forward:
            start = max(seq + 1, scoped_lo)
            if start > scoped_hi:
                return dict(seq=None, wrapped=False)
            lo_bound = int(index.insn_of_seq(index.next_instruction_seq(scoped_lo)))
            hi_bound = int(index.insn_of_seq(index.prev_instruction_seq(scoped_hi)))
            found = _source_forward(store, index, nav, start - 1, current_file,
                                    current_line, lo_bound, hi_bound)
        else:
            start = min(seq - 1, scoped_hi)
            if start < scoped_lo:
                return dict(seq=None, wrapped=False)
            lo_bound = int(index.insn_of_seq(index.next_instruction_seq(scoped_lo)))
            hi_bound = int(index.insn_of_seq(index.prev_instruction_seq(scoped_hi)))
            found = _source_backward(store, index, nav, start + 1, current_file,
                                     current_line, lo_bound, hi_bound)
        if found is None:
            return dict(seq=None, wrapped=False)
        return dict(seq=int(index.seq_of_insn(found)), wrapped=False)
    finally:
        setattr(store, '_nav_stats_inner', None)


# ---------------------------------------------------------------------------------------------
# CLI（索引迁移）
# ---------------------------------------------------------------------------------------------

def _main(argv=None):
    parser = argparse.ArgumentParser(description='R1-B 导航索引（nav sidecar）')
    sub = parser.add_subparsers(dest='command', required=True)
    build = sub.add_parser('build', help='构建/刷新 nav sidecar')
    build.add_argument('--store', required=True)
    build.add_argument('--progress', action='store_true')
    info = sub.add_parser('info', help='查看 nav sidecar 状态')
    info.add_argument('--store', required=True)
    args = parser.parse_args(argv)
    if args.command == 'info':
        store = trace_store.TraceStore(args.store, verify='sizes')
        try:
            data = _load_nav_index(store)
            payload = nav_status(store)
            if data is not None:
                payload['generation'] = store.generation
            print(json.dumps(payload, ensure_ascii=False, indent=1))
        finally:
            store.close()
        return 0
    progress = (lambda message: print(message, flush=True)) if args.progress else None
    summary = build_nav_index(args.store, persist=True, progress=progress)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(_main())
