"""trace-store 查询索引（R1）：持久化聚合块的构建、加载与 window/function_timeline 快路径。

背景与依据
----------
上游 Codex 复核（`docs/etm-1s-next-steps-20261020.md` §2.1）已证明：现有
`TraceStore.window()/function_timeline()` 逐事件展开 `_expand`，成本与逻辑事件数同量级
（19.86M 事件 store 全段 295.5 s / 528.6 s）。本模块按 `docs/etm-r1-query-design.md`
（t3 设计 + 可行性原型）实现 R1 的持久化查询索引：

* 承载形式：`store.bin` 新增可选块类型 `BLK_QUERY=12`（feature 位 0x0001），
  `meta.queryIndex` 记录版本/单元格/覆盖范围/默认视图指纹；`SCHEMA_VERSION` 不升；
  旧记录没有 12 号块 → `TraceStore.query_index=None` → 完全走原慢路径（行为不变）；
  块自洽校验失败 → 记 `query-index` issue 并回退慢路径，不返回拼凑结果。
* 语义来源：`build_tables()` 单遍扫描 sequence 块，产出设计 §3.3 的语义表（函数区间、路径区间、
  节点树、4096 指令单元格聚合、稀疏未知/未成立/调用配对/周期锚点、事件表、静态映射），
  构建时**不构造事件字典、不调用 CallTracker**（用 `_PathTracker`：只复制栈/路径逻辑）。
* 查询：`QueryIndex.window()/function_timeline()` 用「覆盖单元格求和 + 至多 2 个边界单元格
  有界解码」回答任意区间；全段默认参数（window bins=1200 / timeline bins=1600）
  命中构建期持久化的默认视图（构建时用同一套算法生成并记录 sha256）。
* 对照：`TRACE_STORE_SLOW=1` 强制 `query_index=None`，作为逐字段 oracle。
* 编码：块负载 = magic + 版本字节 + `np.savez_compressed`（表名与设计 §3.3 同名同义；
  每张表独立 DEFLATE，读取按需解压；默认视图以 JSON 文本表存放）。t3 设计 §6.3 允许
  规范化语义相等而不要求字节相同；R2 原生 builder 可在同一次遍历中产出同名表。

本模块只被 `trace_store.py`（加载/快路径分派）、`trace_store_write.py`（`--build-query-index`）
与 CLI（`python etm/tools/trace_store_index.py build <record/store>`）使用；
查询实现不修改 `_expand`/事件语义，也不为每条指令生成 Python 对象。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import math
import os
import secrets
import struct
import sys
import time
import zlib
from pathlib import Path

import numpy as np

import trace_store

QUERY_INDEX_FORMAT = 'etm-trace-store-query-index'
QUERY_INDEX_VERSION = 1
QUERY_BLOCK_MAGIC = b'ETMQIDX1'
# 块 12 v2 容器（R2 原生 builder 写、Python 读）：magic + 定长目录 + 每表 zlib。
# 目录项按 64B 对齐：README 字段求和 60B，尾部 4B 保留（读取端要求为 0）。
QUERY_V2_MAGIC = b'ETMQIDX2'
QUERY_INDEX_VERSION_V2 = 2
V2_HEADER = struct.Struct('<8sIII')                 # magic/version/tableCount/reserved
V2_ITEM = struct.Struct('<IHBB4sQQQQQII')           # 60B 字段；磁盘上 64B（尾部保留 4B）
V2_ITEM_BYTES = 64
V2_DTYPES = {0: 'uint8', 1: 'int8', 2: 'uint16', 3: 'int16', 4: 'uint32', 5: 'int32',
             6: 'uint64', 7: 'int64', 8: 'float32', 9: 'float64'}
V2_DTYPE_CODES = {value: code for code, value in V2_DTYPES.items()}
DEFAULT_CELL_INSTRUCTIONS = 4096
DEFAULT_WINDOW_BINS = 1200
DEFAULT_TIMELINE_BINS = 1600
DEFAULT_TIMELINE_MAX_FUNCTIONS = 6
SLOW_ENV = 'TRACE_STORE_SLOW'
TAIL_TRUE, TAIL_FALSE, TAIL_NONE = 0, 1, 2
FLOW_CODES = trace_store.FLOW_CODES
MAX_CALL_DEPTH = trace_store.MAX_CALL_DEPTH
_dvar = trace_store._dvar
_dzvar = trace_store._dzvar

_TOP_KEYS = ('format', 'version', 'cellSize', 'instructions', 'events', 'cells',
             'staticCount', 'functionCount', 'hotspotCount', 'nodeCount', 'segmentCount',
             'functionSegments', 'firstSeq', 'lastSeq', 'nav', 'container')
NAV_TABLE_NAMES = ('pc_cell_offsets', 'pc_cells', 'line_start', 'line_id')
NAV_CELL_INSTRUCTIONS = 4096


class QueryIndexError(ValueError):
    """查询索引不可用（版本/自洽校验失败）；调用方必须回退慢路径。"""


def slow_mode():
    """``TRACE_STORE_SLOW=1`` 强制走原慢路径（逐字段对照用的 oracle）。"""
    return os.environ.get(SLOW_ENV, '').strip().lower() in ('1', 'true', 'yes', 'on')


def _json_array(text):
    return np.frombuffer(text.encode('utf-8'), np.uint8)


def _json_text(array):
    return np.asarray(array).tobytes().decode('utf-8')


class _LazyArrays:
    """按需解压 npz 成员：默认视图命中时只读两张 JSON 表，不载入全部聚合表。"""

    def __init__(self, archive):
        self._archive = archive
        self._cache = {}

    def __getitem__(self, name):
        value = self._cache.get(name)
        if value is None:
            if name not in self._archive.files:
                raise KeyError(name)
            value = self._archive[name]
            self._cache[name] = value
        return value

    def get(self, name, default=None):
        try:
            return self[name]
        except KeyError:
            return default

    def __contains__(self, name):
        return name in self._archive.files


class _EagerArrays(dict):
    """构建期直接用内存数组（不经过 npz）。"""


def encode_payload(arrays, meta):
    """把语义表 + meta 编码成 12 号块负载：magic + 版本 + npz（各表独立压缩）。"""
    payload_arrays = dict(arrays)
    payload_arrays['meta_json'] = _json_array(
        json.dumps(meta, ensure_ascii=False, sort_keys=True))
    buffer = io.BytesIO()
    np.savez_compressed(buffer, **payload_arrays)
    return QUERY_BLOCK_MAGIC + bytes([QUERY_INDEX_VERSION]) + buffer.getvalue()


def decode_payload(payload):
    payload = bytes(payload)
    if payload[:len(QUERY_V2_MAGIC)] == QUERY_V2_MAGIC:
        return _V2Archive(payload)
    magic = len(QUERY_BLOCK_MAGIC)
    if len(payload) < magic + 1 or payload[:magic] != QUERY_BLOCK_MAGIC:
        raise QueryIndexError('查询索引块 magic 不符（不是本读取器认识的索引）')
    version = payload[magic]
    if version != QUERY_INDEX_VERSION:
        raise QueryIndexError('查询索引版本不支持：%d（本读取器支持 %d）'
                              % (version, QUERY_INDEX_VERSION))
    try:
        return np.load(io.BytesIO(payload[magic + 1:]), allow_pickle=False)
    except Exception as error:                     # noqa: BLE001 - 明确转成索引错误
        raise QueryIndexError('查询索引数据无法解析：%s' % error)


def encode_payload_v2(arrays, meta, level=6):
    """把语义表 + meta 编码成块 12 v2 负载（表目录 64B/项 + name 区 + 每表 zlib）。

    与 v1(npz) 的表名/dtype/shape 完全一致；C++ builder 可直接写这个容器，
    Python 读取端（``_V2Archive``）按目录懒加载并校验长度/CRC/形状。
    """
    payload_arrays = dict(arrays)
    payload_arrays['meta_json'] = _json_array(
        json.dumps(meta, ensure_ascii=False, sort_keys=True))
    names = sorted(payload_arrays)
    encoded = {name: name.encode('utf-8') for name in names}
    name_area = b''.join(encoded[name] for name in names)
    name_offsets = {}
    cursor = 0
    for name in names:
        name_offsets[name] = cursor
        cursor += len(encoded[name])
    directory_bytes = len(names) * V2_ITEM_BYTES
    data_offset = V2_HEADER.size + directory_bytes + len(name_area)
    directory = bytearray()
    blobs = []
    for name in names:
        array = np.ascontiguousarray(np.asarray(payload_arrays[name]))
        if array.ndim not in (1, 2) or str(array.dtype) not in V2_DTYPE_CODES:
            raise QueryIndexError('v2 容器不支持的表形状/类型：%s %r'
                                  % (name, (array.shape, str(array.dtype))))
        raw = array.tobytes()
        blob = zlib.compress(raw, level)
        shape0 = int(array.shape[0])
        shape1 = int(array.shape[1]) if array.ndim == 2 else 0
        directory += V2_ITEM.pack(
            name_offsets[name], len(encoded[name]), V2_DTYPE_CODES[str(array.dtype)],
            array.ndim, b'\x00' * 4, shape0, shape1, data_offset,
            len(blob), len(raw), zlib.crc32(raw) & 0xFFFFFFFF, 0)
        directory += b'\x00' * (V2_ITEM_BYTES - V2_ITEM.size)
        blobs.append(blob)
        data_offset += len(blob)
    return (V2_HEADER.pack(QUERY_V2_MAGIC, QUERY_INDEX_VERSION_V2, len(names), 0)
            + bytes(directory) + name_area + b''.join(blobs))


class _V2Archive:
    """块 12 v2 容器：解析表目录，成员按需解压（懒加载）+ 长度/CRC/形状校验。"""

    def __init__(self, payload):
        payload = bytes(payload)
        if len(payload) < V2_HEADER.size:
            raise QueryIndexError('v2 索引头部被截断')
        magic, version, count, reserved = V2_HEADER.unpack_from(payload, 0)
        if magic != QUERY_V2_MAGIC:
            raise QueryIndexError('v2 索引 magic 不符：%r' % (magic,))
        if version != QUERY_INDEX_VERSION_V2:
            raise QueryIndexError('v2 索引版本不支持：%d' % version)
        if reserved != 0:
            raise QueryIndexError('v2 索引 reserved 非 0')
        directory_end = V2_HEADER.size + count * V2_ITEM_BYTES
        if directory_end > len(payload):
            raise QueryIndexError('v2 索引目录被截断')
        self._payload = payload
        self._items = {}
        self._cache = {}
        self.files = []
        for index in range(count):
            base = V2_HEADER.size + index * V2_ITEM_BYTES
            (name_offset, name_bytes, dtype_code, ndim, _reserved, shape0, shape1,
             data_offset, compressed_bytes, uncompressed_bytes, crc32, _pad) = \
                V2_ITEM.unpack_from(payload, base)
            if dtype_code not in V2_DTYPES:
                raise QueryIndexError('v2 索引表 dtypeCode 未知：%d' % dtype_code)
            if ndim not in (1, 2):
                raise QueryIndexError('v2 索引表 ndim 非法：%d' % ndim)
            name_start = directory_end + int(name_offset)
            name_end = name_start + int(name_bytes)
            if name_start < directory_end or name_end > len(payload):
                raise QueryIndexError('v2 索引表名越界')
            try:
                name = payload[name_start:name_end].decode('utf-8')
            except UnicodeDecodeError:
                raise QueryIndexError('v2 索引表名不是 UTF-8')
            if data_offset + compressed_bytes > len(payload):
                raise QueryIndexError('v2 索引表 %s 数据越界' % name)
            shape = (int(shape0),) if ndim == 1 else (int(shape0), int(shape1))
            dtype = np.dtype(V2_DTYPES[dtype_code])
            if int(np.prod(shape)) * dtype.itemsize != int(uncompressed_bytes):
                raise QueryIndexError('v2 索引表 %s 形状×dtype 字节数不符' % name)
            self._items[name] = (shape, dtype, int(data_offset), int(compressed_bytes),
                                 int(uncompressed_bytes), int(crc32))
            self.files.append(name)

    def __contains__(self, name):
        return name in self._items

    def __getitem__(self, name):
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        shape, dtype, offset, compressed, uncompressed, crc32 = self._items[name]
        try:
            raw = zlib.decompress(self._payload[offset:offset + compressed])
        except zlib.error as error:
            raise QueryIndexError('v2 索引表 %s 解压失败：%s' % (name, error))
        if len(raw) != uncompressed:
            raise QueryIndexError('v2 索引表 %s 解压长度不符：%d≠%d'
                                  % (name, len(raw), uncompressed))
        if zlib.crc32(raw) & 0xFFFFFFFF != crc32:
            raise QueryIndexError('v2 索引表 %s CRC32 不符（表损坏/被改写）' % name)
        array = np.frombuffer(raw, dtype=dtype).reshape(shape)
        self._cache[name] = array
        return array

    def get(self, name, default=None):
        try:
            return self[name]
        except KeyError:
            return default


def validate_meta(store, meta):
    """廉价自洽校验（不载入全部表）：版本/计数/范围必须与当前代 store 一致。"""
    if not isinstance(meta, dict):
        raise QueryIndexError('查询索引 meta 不是对象')
    if meta.get('format') != QUERY_INDEX_FORMAT:
        raise QueryIndexError('查询索引 format 不符：%r' % (meta.get('format'),))
    if int(meta.get('version') or 0) != QUERY_INDEX_VERSION:
        raise QueryIndexError('查询索引 version 不符：%r' % (meta.get('version'),))
    cell_size = int(meta.get('cellSize') or 0)
    if not 256 <= cell_size <= (1 << 20):
        raise QueryIndexError('查询索引 cellSize 无效：%r' % (meta.get('cellSize'),))
    def _declared(name):
        value = meta.get(name)
        return -1 if value is None else int(value)

    instructions = int(store.counts.get('instructions') or 0)
    events = int(store.counts.get('events') or 0) - instructions
    expected_cells = (instructions + cell_size - 1) // cell_size
    checks = (('instructions', _declared('instructions'), instructions),
              ('events', _declared('events'), events),
              ('cells', _declared('cells'), expected_cells),
              ('firstSeq', _declared('firstSeq'), int(store.first_seq)),
              ('lastSeq', _declared('lastSeq'), int(store.last_seq)),
              ('staticCount', _declared('staticCount'), len(store.static)))
    for name, declared, actual in checks:
        if declared != actual:
            raise QueryIndexError('查询索引 meta.%s 与当前代不符：%r≠%r'
                                  % (name, declared, actual))
    if not instructions or not expected_cells:
        raise QueryIndexError('查询索引用在空记录上（instructions=%d）' % instructions)
    nav = meta.get('nav')
    if nav is not None:
        if not isinstance(nav, dict):
            raise QueryIndexError('查询索引 meta.nav 不是对象')
        if int(nav.get('cellSize') or 0) != cell_size:
            raise QueryIndexError('查询索引 meta.nav.cellSize 与主表不符：%r'
                                  % (nav.get('cellSize'),))
        for name, actual in (('staticCount', len(store.static)),
                             ('instructions', instructions)):
            if int(nav.get(name) or -1) != actual:
                raise QueryIndexError('查询索引 meta.nav.%s 与当前代不符：%r≠%r'
                                      % (name, nav.get(name), actual))
        for name in ('pcCellPairs', 'lineRuns'):
            value = nav.get(name)
            if value is None or int(value) < 0:
                raise QueryIndexError('查询索引 meta.nav.%s 非法：%r' % (name, value))


def load_query_index(store):
    """从已打开的 ``TraceStore`` 加载 12 号块；无块返回 None，块坏抛 QueryIndexError。"""
    if slow_mode():
        return None
    blocks = store.container.of_type(trace_store.BLK_QUERY)
    if not blocks:
        return None
    if len(blocks) != 1:
        raise QueryIndexError('查询索引块重复（%d 个）' % len(blocks))
    payload = store.container.block(blocks[0])
    archive = decode_payload(payload)
    try:
        meta = json.loads(_json_text(archive['meta_json']))
    except KeyError:
        raise QueryIndexError('查询索引缺少 meta_json 表')
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QueryIndexError('查询索引 meta 无法解析：%s' % error)
    validate_meta(store, meta)
    return QueryIndex(_LazyArrays(archive), meta, store)


MAX_CALL_DEPTH = 128


class _PathTracker:
    __slots__ = ('stack', 'contexts', 'previous', 'expected_resume', 'unknown_restarts',
                 'path', 'leaf', 'nodes', 'children', 'sync_count', 'current_seq',
                 'changes')

    def __init__(self):
        self.stack = []
        self.contexts = []
        self.previous = None
        self.expected_resume = None
        self.unknown_restarts = 0
        self.path = []
        self.leaf = 0
        self.nodes = [(-1, '')]          # 0 号节点 = 根（空前缀）
        self.children = {}
        self.sync_count = 0
        self.current_seq = -1
        self.changes = []                # [(seq, leaf)]：路径变化点（seq 为变化后第一条）

    # -- 兼容 CallTracker 的最小接口 ---------------------------------------------------------
    @staticmethod
    def frame(fn, return_pc=None, origin=None):
        return dict(function=fn, returnPC=return_pc, origin=origin)

    def _sync(self):
        self.path = [frame['function'] for frame in self.stack]
        node = 0
        get = self.children.get
        for label in self.path:
            key = (node, label)
            nxt = get(key)
            if nxt is None:
                nxt = len(self.nodes)
                self.nodes.append((node, label))
                self.children[key] = nxt
            node = nxt
        self.leaf = node
        self.changes.append((self.current_seq, node))
        self.sync_count += 1

    def feed_event(self, etype, flags=0, payload=0, end=None):
        if etype in (1, 2, 6, 7, 8):
            self.stack, self.contexts, self.previous = [], [], None
            self.expected_resume = None
            self.unknown_restarts += 1
        elif etype == 9:
            if flags & (1 << 12):
                self.stack, self.contexts, self.previous = [], [], None
                self.unknown_restarts += 1
            self.contexts.append((self.stack, self.previous,
                                  end if flags & (1 << 6) else None))
            self.stack = [self.frame('中断 %d' % payload)]
            self.previous = None
        elif etype == 10:
            if self.contexts:
                self.stack, self.previous, self.expected_resume = self.contexts.pop()
            else:
                self.stack, self.previous = [], None
                self.unknown_restarts += 1
        else:
            return
        self._sync()

    def feed_instruction(self, function, file, line, executed, flow, pc, size, cycles):
        fn = function or '(未知函数)'
        expected_resume = self.expected_resume
        if expected_resume is not None:
            if pc != (expected_resume & ~1):
                self.stack = []
                self.previous = None
                self.unknown_restarts += 1
            self.expected_resume = None
        stack = self.stack
        if not stack:
            stack = self.stack = [self.frame('采集前调用栈未知'), self.frame(fn)]
            self._sync()
        if stack[-1]['function'].startswith('中断 '):
            stack.append(self.frame(fn))
            self._sync()
        previous = self.previous
        if previous and previous[3] is True:
            old_fn = previous[4] or '(未知函数)'
            if previous[2] == 'call':
                stack.append(self.frame(fn, previous[0] + previous[1], fn))
                self._sync()
            elif previous[2] in ('return', 'branch'):
                match = None
                for index in range(len(stack) - 1, -1, -1):
                    if stack[index]['returnPC'] == pc:
                        match = index
                        break
                if match is not None:
                    self.stack = stack[:match]
                    self._sync()
                elif old_fn != fn and previous[2] == 'branch':
                    stack[-1]['function'] = fn
                    self._sync()
                elif old_fn != fn:
                    self.stack = [self.frame('采集前调用栈未知'), self.frame(fn)]
                    self.unknown_restarts += 1
                    self._sync()
        stack = self.stack
        if not stack or stack[-1]['function'] != fn:
            self.stack = [self.frame('调用关系未证实'), self.frame(fn)]
            self.unknown_restarts += 1
            self._sync()
        stack = self.stack
        if len(stack) > MAX_CALL_DEPTH:
            raise ValueError('调用层级超过 128，拒绝生成可能失真的调用图。')
        self.previous = (pc, size, flow, executed, function)

    # -- 路径 → 节点链 ----------------------------------------------------------------------
    def chain(self, leaf):
        """返回从根到叶的节点 ID 链（含根 0）。"""
        nodes = self.nodes
        result = []
        node = leaf
        while node > 0:
            result.append(node)
            node = nodes[node][0]
        result.append(0)
        result.reverse()
        return result

    def chain_labels(self, leaf):
        nodes = self.nodes
        result = []
        node = leaf
        while node > 0:
            result.append(nodes[node][1])
            node = nodes[node][0]
        result.reverse()
        return result

    def path_of(self, leaf):
        return self.chain_labels(leaf)


def unpack_code(entry):
    """静态条目 → 查询索引需要的紧凑编码。"""
    return (entry.pc, entry.size, FLOW_CODES[entry.flow], entry.predicated)


class CellBuffers:
    __slots__ = ('ids', 'hotspot_count', 'hotspot_unknown', 'first',
                 'insn', 'executed', 'unknown', 'notTaken', 'cycles', 'cycleRuns')

    def __init__(self, hotspot_count, hotspot_unknown, first):
        self.ids = []
        self.hotspot_count = hotspot_count
        self.hotspot_unknown = hotspot_unknown
        self.first = first
        self.insn = self.executed = self.unknown = self.notTaken = 0
        self.cycles = 0
        self.cycleRuns = 0

    def reset(self):
        self.ids = []
        self.insn = self.executed = self.unknown = self.notTaken = 0
        self.cycles = 0
        self.cycleRuns = 0


def build_tables(view, cell_size=4096, progress=True, with_nav=True):
    """单遍扫描 view 的 sequence 块，产出设计 §3.3 的语义表（numpy 数组，不落盘）。

    ``view`` 需要提供：``static``/``strings``/``counts``/``first_seq``/``last_seq``/
    ``sequence_blocks`` 与 ``container.block(ordinal)``（TraceStore 与写入期 sink 视图同形）。
    """
    store = view
    started = time.perf_counter()
    timings = {}
    static = store.static
    st_function = [entry.function for entry in static]
    st_file = [entry.file for entry in static]
    st_line = [entry.line for entry in static]
    st_flow_name = [entry.flow for entry in static]
    static_count = len(static)
    # 导航表（PC_CELLS/LINE_RUNS）：与 trace_store_nav.build_nav_index 同口径、同遍采集。
    # 口径：每个静态 ID 首次进入某单元格时记 (id, cell)；源码行区间按 (line is None) 或
    # (file, line) 变化切段（line 为 None 时只比较 None，与 sidecar 实现逐行一致）。
    nav_cells_by_id = [[] for _ in range(static_count)]
    nav_last_cell = [-1] * static_count
    nav_line_starts = []
    nav_line_ids = []
    nav_line_keys = np.empty(static_count, np.int32)
    nav_line_key_index = {}
    for identifier, entry in enumerate(static):
        key = (0,) if entry.line is None else (1, entry.file, entry.line)
        nav_line_keys[identifier] = nav_line_key_index.setdefault(key, len(nav_line_key_index))
    nav_current_line_key = -1
    total_instructions = int(store.counts['instructions'])
    total_events = int(store.counts['events']) - total_instructions
    cell_count = (total_instructions + cell_size - 1) // cell_size
    timings['open'] = round(time.perf_counter() - started, 3)

    # ---- 静态 ID → 紧凑属性 ---------------------------------------------------------------
    phase = time.perf_counter()
    function_names = ['(未知函数)']
    function_index = {'(未知函数)': 0}
    for entry in static:
        name = entry.function or '(未知函数)'
        if name not in function_index:
            function_index[name] = len(function_names)
            function_names.append(name)
    func_of_id = np.empty(len(static), np.int16)
    pc_of_id = np.empty(len(static), np.uint32)
    size_of_id = np.empty(len(static), np.uint8)
    flow_of_id = np.empty(len(static), np.int8)
    predicated_of_id = np.empty(len(static), bool)
    for identifier, entry in enumerate(static):
        func_of_id[identifier] = function_index[entry.function or '(未知函数)']
        pc_of_id[identifier] = entry.pc
        size_of_id[identifier] = entry.size
        flow_of_id[identifier] = FLOW_CODES[entry.flow]
        predicated_of_id[identifier] = entry.predicated
    # 热点 = (function, file, line)；index 0 保留给“(未出现)”
    hotspot_index = {}
    hotspot_func = []
    hotspot_file = []
    hotspot_line = []
    hotspot_order = []
    hotspot_of_id = np.empty(len(static), np.int32)

    def hotspot_id(entry):
        key = (entry.function or '(未知函数)', entry.file, entry.line)
        identifier = hotspot_index.get(key)
        if identifier is None:
            identifier = len(hotspot_func)
            hotspot_index[key] = identifier
            hotspot_func.append(function_index[entry.function or '(未知函数)'])
            hotspot_file.append(entry.file)
            hotspot_line.append(entry.line)
            hotspot_order.append(len(hotspot_order))
        return identifier

    for identifier, entry in enumerate(static):
        hotspot_of_id[identifier] = hotspot_id(entry)
    hotspot_count_total = len(hotspot_func)
    strings = list(store.strings)      # 节点伪标签（中断 N 等）只进索引自己的字符串表
    string_index = {value: index for index, value in enumerate(strings)}
    h_file = np.array([-1 if value is None else string_index[value]
                       for value in hotspot_file], np.int32)
    h_line = np.array([-1 if value is None else value for value in hotspot_line], np.int32)
    file_names = sorted({value for value in hotspot_file if value is not None})
    timings['maps'] = round(time.perf_counter() - phase, 3)

    # ---- 单元格聚合数组 -------------------------------------------------------------------
    phase = time.perf_counter()
    cell_first_seq = np.full(cell_count, -1, np.int64)
    cell_anchor_block = np.full(cell_count, -1, np.int16)
    cell_anchor_offset = np.full(cell_count, -1, np.int32)
    cell_anchor_skip = np.zeros(cell_count, np.int16)
    cell_insn = np.zeros(cell_count, np.int32)
    cell_executed = np.zeros(cell_count, np.int32)
    cell_unknown = np.zeros(cell_count, np.int32)
    cell_not_taken = np.zeros(cell_count, np.int32)
    cell_cycles = np.zeros(cell_count, np.int64)
    cell_cycle_runs = np.zeros(cell_count, np.int32)
    cell_hotspot_count = np.zeros((cell_count, hotspot_count_total), np.uint16)
    cell_hotspot_unknown = np.zeros((cell_count, hotspot_count_total), np.uint16)
    cell_hotspot_first = np.zeros((cell_count, hotspot_count_total), np.uint32)
    node_value_cells = [None] * cell_count      # dict: node → count
    node_value_cycles_cells = [None] * cell_count
    node_self = {}                              # (cell, node) → [count, cycles]
    hotspot_cycles = {}                         # (cell, hotspot) → cycles
    cycle_anchor_seq = []
    cycle_anchor_hotspot = []
    cycle_anchor_cycles = []
    unknown_seq = []
    unknown_func = []
    not_taken_seq = []
    not_taken_func = []
    call_pair_seq = []
    call_pair_func = []
    function_segments = []                      # (start_insn, count, func)
    path_segments = []                          # (start_insn, count, leaf)
    event_seq = []
    event_type = []
    event_text = []
    event_flags = []
    event_payload = []
    event_next_func = []
    timings['allocate'] = round(time.perf_counter() - phase, 3)

    tracker = _PathTracker()
    segment_start = 0
    segment_leaf = 0
    segment_cycles = 0
    # 函数区间状态
    func_seg_start = 0
    func_seg_func = -1
    func_seg_count = 0
    # 调用配对/相邻状态
    prev_insn_seq = -10
    prev_flow = -1
    prev_executed = None
    pending_type9 = -1           # 待补 next_func 的 type-9 事件下标
    pending_type9_seq = -1

    instruction_index = 0
    cell = 0
    anchor_ready = False
    buffers = CellBuffers(np.zeros(hotspot_count_total, np.uint16),
                          np.zeros(hotspot_count_total, np.uint16),
                          np.full(hotspot_count_total, -1, np.int32))

    def close_segment(end_insn):
        nonlocal segment_start, segment_leaf, segment_cycles
        count = end_insn - segment_start
        if count <= 0:
            segment_start = end_insn
            segment_leaf = tracker.leaf
            segment_cycles = 0
            return
        path_segments.append((segment_start, count, segment_leaf))
        # 节点计数：root=0 也在链里
        chain = tracker.chain(segment_leaf)
        start_cell = segment_start // cell_size
        end_cell = (end_insn - 1) // cell_size
        for cell_index in range(start_cell, end_cell + 1):
            low = max(segment_start, cell_index * cell_size)
            high = min(end_insn, (cell_index + 1) * cell_size)
            overlap = high - low
            if overlap <= 0:
                continue
            values = node_value_cells[cell_index]
            if values is None:
                values = node_value_cells[cell_index] = {}
            for node in chain:
                values[node] = values.get(node, 0) + overlap
            entry = node_self.get((cell_index, segment_leaf))
            if entry is None:
                node_self[(cell_index, segment_leaf)] = [overlap, 0]
            else:
                entry[0] += overlap
        segment_start = end_insn
        segment_leaf = tracker.leaf
        segment_cycles = 0

    def close_function_segment(end_insn):
        nonlocal func_seg_start, func_seg_count
        if func_seg_count:
            function_segments.append((func_seg_start, func_seg_count, func_seg_func))
        func_seg_start = end_insn
        func_seg_count = 0

    def flush_cell(cell_index, last=False):
        nonlocal buffers
        if buffers.ids:
            ids = np.array(buffers.ids, np.int64)
            counts = np.bincount(ids, minlength=len(static))
            present = np.flatnonzero(counts)
            hotspots = hotspot_of_id[present]
            np.add.at(buffers.hotspot_count, hotspots, counts[present].astype(np.uint16))
            cell_hotspot_count[cell_index] = buffers.hotspot_count
            unknown_present = present[predicated_of_id[present]]
            if len(unknown_present):
                np.add.at(buffers.hotspot_unknown, hotspot_of_id[unknown_present],
                          counts[unknown_present].astype(np.uint16))
            cell_hotspot_unknown[cell_index] = buffers.hotspot_unknown
            # 首次出现：每个热点在单元格内最早出现的指令偏移与静态 ID
            unique_ids, first_index = np.unique(ids, return_index=True)
            first_of_hotspot = np.full(hotspot_count_total, 1 << 30, np.int64)
            np.minimum.at(first_of_hotspot, hotspot_of_id[unique_ids], first_index)
            first_id_of_hotspot = np.full(hotspot_count_total, -1, np.int64)
            order = np.argsort(first_index, kind='stable')
            hs_sorted = hotspot_of_id[unique_ids[order]]
            _, first_pos = np.unique(hs_sorted, return_index=True)
            chosen_hotspots = hs_sorted[first_pos]
            chosen_ids = unique_ids[order][first_pos]
            first_id_of_hotspot[chosen_hotspots] = chosen_ids
            first_of_hotspot[chosen_hotspots] = first_index[order][first_pos]
            mask = first_of_hotspot < (1 << 30)
            packed = np.zeros(hotspot_count_total, np.uint32)
            packed[mask] = (first_id_of_hotspot[mask].astype(np.uint32) << 12) | \
                first_of_hotspot[mask].astype(np.uint32)
            cell_hotspot_first[cell_index] = packed
            buffers.reset()
        cell_insn[cell_index] = buffers.insn
        cell_executed[cell_index] = buffers.executed
        cell_unknown[cell_index] = buffers.unknown
        cell_not_taken[cell_index] = buffers.notTaken
        cell_cycles[cell_index] = buffers.cycles
        cell_cycle_runs[cell_index] = buffers.cycleRuns

    phase = time.perf_counter()
    for block_ordinal, block_number in enumerate(store.sequence_blocks):
        payload = store.container.block(block_number)
        position = 0
        size = len(payload)
        while position < size:
            record_start = position
            record_type = payload[position]
            position += 1
            length, position = _dvar(payload, position)
            begin = position
            finish = position + length
            if finish > size:
                raise RuntimeError('序列记录越过块边界（block=%d offset=%d）'
                                   % (block_ordinal, record_start))
            position = finish
            if record_type == trace_store.REC_CHECKPOINT:
                continue
            if record_type == trace_store.REC_EVENT:
                cursor = begin
                seq, cursor = _dvar(payload, cursor)
                etype = payload[cursor]
                cursor += 1
                event_offset, cursor = _dvar(payload, cursor)
                payload_value, cursor = _dvar(payload, cursor)
                event_start, cursor = _dvar(payload, cursor)
                event_end, cursor = _dvar(payload, cursor)
                flags, cursor = _dvar(payload, cursor)
                text_id, cursor = _dvar(payload, cursor)
                if cursor != finish:
                    raise RuntimeError('事件记录长度不符 seq=%d' % seq)
                event_seq.append(seq)
                event_type.append(etype)
                event_text.append(text_id)
                event_flags.append(flags)
                event_payload.append(payload_value)
                event_next_func.append(-1)
                tracker.current_seq = seq
                before = tracker.sync_count
                tracker.feed_event(etype, flags, payload_value, event_end)
                if tracker.sync_count != before:
                    close_segment(instruction_index)
                if etype == 9:
                    pending_type9 = len(event_seq) - 1
                    pending_type9_seq = seq
                else:
                    pending_type9 = -1
                    pending_type9_seq = -1
                continue
            if record_type != trace_store.REC_RUN:
                raise RuntimeError('未知序列记录类型 %d' % record_type)
            cursor = begin
            seq, cursor = _dvar(payload, cursor)
            count, cursor = _dvar(payload, cursor)
            tail_code = payload[cursor]
            cursor += 1
            offset, cursor = _dvar(payload, cursor)
            cycles, cursor = _dvar(payload, cursor)
            cycle_span, cursor = _dvar(payload, cursor)
            internal_unknown, cursor = _dvar(payload, cursor)
            first_identifier, cursor = _dvar(payload, cursor)
            identifiers = [first_identifier]
            previous = first_identifier
            for _ in range(count - 1):
                delta, cursor = _dzvar(payload, cursor)
                previous += delta
                identifiers.append(previous)
            if cursor != finish:
                raise RuntimeError('展开段记录长度不符 seq=%d' % seq)
            if cell_first_seq[cell] < 0 and instruction_index == cell * cell_size:
                cell_first_seq[cell] = seq
                cell_anchor_block[cell] = block_ordinal
                cell_anchor_offset[cell] = record_start
                cell_anchor_skip[cell] = 0
            position_in_run = 0
            while position_in_run < count:
                cell_end = (cell + 1) * cell_size
                take = min(count - position_in_run, cell_end - instruction_index)
                if take <= 0:
                    raise RuntimeError('单元格切分异常 cell=%d' % cell)
                last = count - 1
                for step in range(take):
                    run_position = position_in_run + step
                    identifier = identifiers[run_position]
                    instruction_seq = seq + run_position
                    if nav_last_cell[identifier] != cell:
                        nav_cells_by_id[identifier].append(cell)
                        nav_last_cell[identifier] = cell
                    nav_line_key = int(nav_line_keys[identifier])
                    if nav_line_key != nav_current_line_key:
                        nav_line_starts.append(instruction_index)
                        nav_line_ids.append(identifier)
                        nav_current_line_key = nav_line_key
                    if cell_first_seq[cell] < 0:
                        cell_first_seq[cell] = instruction_seq
                        cell_anchor_block[cell] = block_ordinal
                        cell_anchor_offset[cell] = record_start
                        cell_anchor_skip[cell] = run_position
                    entry_func = int(func_of_id[identifier])
                    entry_hotspot = int(hotspot_of_id[identifier])
                    predicated = bool(predicated_of_id[identifier])
                    if run_position == last:
                        if predicated:
                            executed = None
                        elif tail_code == TAIL_TRUE:
                            executed = True
                        elif tail_code == TAIL_FALSE:
                            executed = False
                        else:
                            executed = None
                    else:
                        executed = None if predicated else True
                    instruction_cycles = cycles if run_position == last else 0
                    buffers.ids.append(identifier)
                    buffers.insn += 1
                    if executed is None:
                        buffers.unknown += 1
                        unknown_seq.append(instruction_seq)
                        unknown_func.append(entry_func)
                    elif executed is False:
                        buffers.notTaken += 1
                        not_taken_seq.append(instruction_seq)
                        not_taken_func.append(entry_func)
                    else:
                        buffers.executed += 1
                    if instruction_cycles:
                        buffers.cycles += instruction_cycles
                        buffers.cycleRuns += 1
                        cycle_anchor_seq.append(instruction_seq)
                        cycle_anchor_hotspot.append(entry_hotspot)
                        cycle_anchor_cycles.append(instruction_cycles)
                        hot_cell = instruction_index // cell_size
                        key = (hot_cell, entry_hotspot)
                        hotspot_cycles[key] = hotspot_cycles.get(key, 0) + instruction_cycles
                    # 函数区间
                    if entry_func != func_seg_func:
                        close_function_segment(instruction_index)
                        func_seg_func = entry_func
                    func_seg_count += 1
                    # 路径状态机
                    tracker.current_seq = instruction_seq
                    before = tracker.sync_count
                    tracker.feed_instruction(
                        st_function[identifier], st_file[identifier], st_line[identifier],
                        executed, st_flow_name[identifier], int(pc_of_id[identifier]),
                        int(size_of_id[identifier]), instruction_cycles)
                    if tracker.sync_count != before:
                        close_segment(instruction_index)
                    if instruction_cycles:
                        segment_cycles += instruction_cycles
                    # type-9 事件后的首条指令 → 该事件的中断入口函数
                    if pending_type9 >= 0:
                        if pending_type9_seq == instruction_seq - 1:
                            event_next_func[pending_type9] = entry_func
                        pending_type9 = -1
                        pending_type9_seq = -1
                    # 调用配对（窗口口径：前一条记录就是已成立的 call 指令）
                    if prev_insn_seq == instruction_seq - 1 and prev_flow == 1 \
                            and prev_executed is True:
                        call_pair_seq.append(prev_insn_seq)
                        call_pair_func.append(entry_func)
                    prev_insn_seq = instruction_seq
                    prev_flow = int(flow_of_id[identifier])
                    prev_executed = executed
                    instruction_index += 1
                position_in_run += take
                if instruction_index == cell_end and cell + 1 < cell_count:
                    # 周期锚点/自环计数已经在上面按 cell 记账；节点值在段关闭时按 cell 记账
                    flush_cell(cell)
                    cell += 1
                    if position_in_run < count:
                        cell_first_seq[cell] = -1
                        cell_anchor_block[cell] = block_ordinal
                        cell_anchor_offset[cell] = record_start
                        cell_anchor_skip[cell] = position_in_run
                    buffers = CellBuffers(np.zeros(hotspot_count_total, np.uint16),
                                          np.zeros(hotspot_count_total, np.uint16),
                                          np.full(hotspot_count_total, -1, np.int32))
        if progress and (block_ordinal + 1) % 10 == 0:
            print('  block %d/%d insn=%d' % (block_ordinal + 1, len(store.sequence_blocks),
                                             instruction_index), flush=True)
    close_segment(instruction_index)
    close_function_segment(instruction_index)
    flush_cell(cell, last=True)
    timings['scan'] = round(time.perf_counter() - phase, 3)
    # ---- 导航表（PC_CELLS/LINE_RUNS）--------------------------------------------------------
    nav_pair_count = sum(len(values) for values in nav_cells_by_id) if with_nav else 0
    nav_offsets = np.zeros(static_count + 1, np.uint32)
    if with_nav:
        running = 0
        for identifier, values in enumerate(nav_cells_by_id):
            nav_offsets[identifier] = running
            running += len(values)
        nav_offsets[static_count] = running
        nav_cells = np.empty(nav_pair_count, np.uint32)
        position = 0
        for values in nav_cells_by_id:
            if values:
                nav_cells[position:position + len(values)] = values
                position += len(values)
        nav_line_start = np.asarray(nav_line_starts, np.uint32)
        nav_line_id = np.asarray(nav_line_ids, np.uint32)
    else:
        nav_offsets = np.zeros(1, np.uint32)
        nav_cells = np.empty(0, np.uint32)
        nav_line_start = np.empty(0, np.uint32)
        nav_line_id = np.empty(0, np.uint32)
    if instruction_index != total_instructions:
        raise RuntimeError('索引扫描的指令数 %d 与 meta 声明 %d 不符'
                           % (instruction_index, total_instructions))

    # ---- 汇总稀疏表 / 节点矩阵 -------------------------------------------------------------
    phase = time.perf_counter()
    node_count = len(tracker.nodes)
    cell_node_value = np.zeros((cell_count, node_count), np.uint16)
    cell_node_value_cycles = np.zeros((cell_count, node_count), np.int64)
    for cell_index, values in enumerate(node_value_cells):
        if values:
            nodes = np.fromiter(values.keys(), np.int32, len(values))
            counts = np.fromiter(values.values(), np.uint16, len(values))
            cell_node_value[cell_index, nodes] = counts
    # 周期锚点 → 节点 valueCycles（祖先链）、叶子 selfCycles
    node_parent = np.array([parent for parent, _ in tracker.nodes], np.int32)
    label_ids = []
    for _, label in tracker.nodes:
        if not label:
            label_ids.append(-1)
            continue
        identifier = string_index.get(label)
        if identifier is None:
            identifier = len(strings)
            string_index[label] = identifier
            strings.append(label)
        label_ids.append(identifier)
    node_label = np.array(label_ids, np.int32)
    chain_cache = {}

    def node_chain(leaf):
        chain = chain_cache.get(leaf)
        if chain is None:
            chain = tracker.chain(leaf)
            chain_cache[leaf] = chain
        return chain

    # 每条指令的 path leaf：由 path segment 二分；这里逐条锚点计算
    seg_starts = np.array([item[0] for item in path_segments], np.int64)
    seg_counts = np.array([item[1] for item in path_segments], np.int64)
    seg_leaves = np.array([item[2] for item in path_segments], np.int32)
    anchor_seq = np.array(cycle_anchor_seq, np.int64)
    anchor_cycles = np.array(cycle_anchor_cycles, np.int64)
    if len(anchor_seq):
        # 指令索引 = seq - 该 seq 之前的事件数
        event_seqs_array = np.array(event_seq, np.int64)
        insn_indices = anchor_seq - np.searchsorted(event_seqs_array, anchor_seq)
        seg_index = np.searchsorted(seg_starts, insn_indices, side='right') - 1
        leaves = seg_leaves[seg_index]
        anchor_cells = (insn_indices // cell_size).astype(np.int64)
        for anchor_index in range(len(anchor_seq)):
            leaf = int(leaves[anchor_index])
            cell_index = int(anchor_cells[anchor_index])
            info = node_self.get((cell_index, leaf))
            if info is None:
                node_self[(cell_index, leaf)] = [0, int(anchor_cycles[anchor_index])]
            else:
                info[1] += int(anchor_cycles[anchor_index])
            for node in node_chain(leaf):
                cell_node_value_cycles[cell_index, node] += int(anchor_cycles[anchor_index])
    node_self_cells = np.array([key[0] for key in node_self], np.int32)
    node_self_nodes = np.array([key[1] for key in node_self], np.int32)
    node_self_counts = np.array([value[0] for value in node_self.values()], np.int32)
    node_self_cycles = np.array([value[1] for value in node_self.values()], np.int64)
    hs_cycles_cells = np.array([key[0] for key in hotspot_cycles], np.int32)
    hs_cycles_hotspots = np.array([key[1] for key in hotspot_cycles], np.int32)
    hs_cycles_values = np.array([value for value in hotspot_cycles.values()], np.int64)
    timings['finalize'] = round(time.perf_counter() - phase, 3)

    arrays = dict(
        cell_first_seq=cell_first_seq, cell_anchor_block=cell_anchor_block,
        cell_anchor_offset=cell_anchor_offset, cell_anchor_skip=cell_anchor_skip,
        cell_insn=cell_insn, cell_executed=cell_executed, cell_unknown=cell_unknown,
        cell_not_taken=cell_not_taken, cell_cycles=cell_cycles,
        cell_cycle_runs=cell_cycle_runs,
        cell_hotspot_count=cell_hotspot_count, cell_hotspot_unknown=cell_hotspot_unknown,
        cell_hotspot_first=cell_hotspot_first,
        cell_node_value=cell_node_value, cell_node_value_cycles=cell_node_value_cycles,
        node_self_cells=node_self_cells, node_self_nodes=node_self_nodes,
        node_self_counts=node_self_counts, node_self_cycles=node_self_cycles,
        hs_cycles_cells=hs_cycles_cells, hs_cycles_hotspots=hs_cycles_hotspots,
        hs_cycles_values=hs_cycles_values,
        cycle_anchor_seq=np.array(cycle_anchor_seq, np.int64),
        cycle_anchor_hotspot=np.array(cycle_anchor_hotspot, np.int32),
        cycle_anchor_cycles=np.array(cycle_anchor_cycles, np.int64),
        unknown_seq=np.array(unknown_seq, np.int32),
        unknown_func=np.array(unknown_func, np.int16),
        not_taken_seq=np.array(not_taken_seq, np.int32),
        not_taken_func=np.array(not_taken_func, np.int16),
        call_pair_seq=np.array(call_pair_seq, np.int32),
        call_pair_func=np.array(call_pair_func, np.int16),
        fs_start=np.array([item[0] for item in function_segments], np.int32),
        fs_func=np.array([item[2] for item in function_segments], np.int16),
        fs_count=np.array([item[1] for item in function_segments], np.int32),
        ps_start=seg_starts.astype(np.int32), ps_count=seg_counts.astype(np.int32),
        ps_leaf=seg_leaves,
        ev_seq=np.array(event_seq, np.int32), ev_type=np.array(event_type, np.uint8),
        ev_text=np.array(event_text, np.int32), ev_flags=np.array(event_flags, np.int64),
        ev_payload=np.array(event_payload, np.int64),
        ev_next_func=np.array(event_next_func, np.int16),
        st_func=func_of_id, st_pc=pc_of_id, st_size=size_of_id, st_flow=flow_of_id,
        st_pred=predicated_of_id.astype(np.uint8), st_hotspot=hotspot_of_id,
        h_func=np.array(hotspot_func, np.int16), h_file=h_file, h_line=h_line,
        node_parent=node_parent, node_label=node_label,
        pc_cell_offsets=nav_offsets, pc_cells=nav_cells,
        line_start=nav_line_start, line_id=nav_line_id,
    )
    meta = dict(
        format=QUERY_INDEX_FORMAT, version=QUERY_INDEX_VERSION, cellSize=cell_size,
        instructions=total_instructions, events=total_events, cells=cell_count,
        staticCount=len(static), functionCount=len(function_names),
        hotspotCount=hotspot_count_total, nodeCount=node_count,
        segmentCount=len(path_segments), functionSegments=len(function_segments),
        firstSeq=int(store.first_seq), lastSeq=int(store.last_seq),
        strings=strings, functionNames=function_names,
    )
    if with_nav:
        checksum = zlib.crc32(nav_offsets.tobytes())
        checksum = zlib.crc32(nav_cells.tobytes(), checksum)
        checksum = zlib.crc32(nav_line_start.tobytes(), checksum)
        checksum = zlib.crc32(nav_line_id.tobytes(), checksum) & 0xFFFFFFFF
        meta['nav'] = dict(cellSize=cell_size, staticCount=static_count,
                           instructions=total_instructions,
                           pcCellPairs=int(nav_pair_count),
                           lineRuns=int(len(nav_line_start)), checksum=checksum)
    else:
        meta['nav'] = None
    timings['total'] = round(time.perf_counter() - started, 3)
    return dict(arrays=arrays, meta=meta, timings=timings)


MIXED_PATH_MARKER = '多段调用（放大查看）'
TAIL_TRUE, TAIL_FALSE, TAIL_NONE = 0, 1, 2
REASON_NO_TIME = ('当前记录没有已验收的逐指令时间戳，执行序号不能换算为微秒')


def _lcp(left, right):
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return left[:index]


class QueryIndex:
    def __init__(self, arrays, meta, view):
        self.arrays = arrays
        self.meta = meta
        self.strings = meta['strings']
        self.function_names = meta['functionNames']
        self.cell_size = int(meta['cellSize'])
        self.total_instructions = int(meta['instructions'])
        self.first_seq = int(meta['firstSeq'])
        self.last_seq = int(meta['lastSeq'])
        self.event_seqs = self.arrays['ev_seq'].astype(np.int64)
        self._view = view
        self._prepared = False
        self._default_texts = {}
        self._path_cache = {}
        self._node_children = None
        self._node_function = None
        self._nav_tables = None
        self._nav_tables_loaded = False

    # -- 导航表（PC_CELLS/LINE_RUNS；只有 nav 快路径首次使用时才解压/校验）-------------------
    def nav_tables(self):
        """返回 dict(offsets, cells, line_start, line_id, static_count, instructions,
        cell_size, checksum, pcCellPairs, lineRuns) 或 None（旧索引无导航表）。

        与 window/timeline 的 `_prepare()` 无关：冷窗口查询不会解压这两张大表。
        """
        if self._nav_tables_loaded:
            return self._nav_tables
        self._nav_tables_loaded = True
        nav = self.meta.get('nav')
        if nav is None:
            return None
        arrays = self.arrays
        missing = [name for name in NAV_TABLE_NAMES if name not in arrays]
        if missing:
            raise QueryIndexError('查询索引声明了导航表但缺少：%s' % ', '.join(missing))
        offsets = np.asarray(arrays['pc_cell_offsets'])
        cells = np.asarray(arrays['pc_cells'])
        line_start = np.asarray(arrays['line_start'])
        line_id = np.asarray(arrays['line_id'])
        static_count = int(self.meta['staticCount'])
        if offsets.shape != (static_count + 1,):
            raise QueryIndexError('导航表 pc_cell_offsets 形状 %r≠(%d,)'
                                  % (tuple(offsets.shape), static_count + 1))
        if int(offsets[0]) != 0 or (len(offsets) > 1 and np.any(np.diff(offsets) < 0)):
            raise QueryIndexError('导航表 pc_cell_offsets 非单调')
        if int(offsets[-1]) != len(cells):
            raise QueryIndexError('导航表 pc_cells 数量与偏移不符')
        if len(line_start) != len(line_id):
            raise QueryIndexError('导航表 line_start/line_id 长度不一致')
        if len(line_start):
            if int(line_start[0]) != 0 or np.any(np.diff(line_start) <= 0):
                raise QueryIndexError('导航表 line_start 未严格递增')
            if int(line_start[-1]) >= int(self.meta['instructions']):
                raise QueryIndexError('导航表 line_start 越界')
        if int(nav.get('pcCellPairs') or -1) != len(cells) \
                or int(nav.get('lineRuns') or -1) != len(line_start):
            raise QueryIndexError('导航表数量与 meta.nav 不符')
        checksum = zlib.crc32(offsets.astype(np.uint32).tobytes())
        checksum = zlib.crc32(cells.astype(np.uint32).tobytes(), checksum)
        checksum = zlib.crc32(line_start.astype(np.uint32).tobytes(), checksum)
        checksum = zlib.crc32(line_id.astype(np.uint32).tobytes(), checksum) & 0xFFFFFFFF
        if int(nav.get('checksum') or -1) != checksum:
            raise QueryIndexError('导航表内容 CRC32 与 meta.nav 不符（表损坏/被改写）')
        self._nav_tables = dict(
            offsets=offsets.astype(np.int64), cells=cells.astype(np.int64),
            line_start=line_start.astype(np.int64), line_id=line_id.astype(np.int64),
            static_count=static_count, instructions=int(self.meta['instructions']),
            cell_size=int(nav.get('cellSize') or 0), checksum=checksum,
            pcCellPairs=int(len(cells)), lineRuns=int(len(line_start)))
        return self._nav_tables

    # -- 载入派生量（默认视图不触发；算法路径首次调用时载入）---------------------------------
    def prepare(self):
        self._prepare()

    def _prepare(self):
        if self._prepared:
            return
        self._check_consistency()
        arrays = self.arrays
        self.static_count = int(self.meta['staticCount'])
        self.function_count = int(self.meta['functionCount'])
        self.hotspot_count = int(self.meta['hotspotCount'])
        self.node_count = int(self.meta['nodeCount'])
        self.cell_count = int(self.meta['cells'])
        self.node_parent = arrays['node_parent']
        self.node_label = arrays['node_label']
        self.ps_start = arrays['ps_start'].astype(np.int64)
        self.ps_count = arrays['ps_count'].astype(np.int64)
        self.ps_leaf = arrays['ps_leaf'].astype(np.int32)
        self.fs_start = arrays['fs_start'].astype(np.int64)
        self.fs_count = arrays['fs_count'].astype(np.int64)
        self.fs_func = arrays['fs_func'].astype(np.int32)
        self.ps_start_seq = self.seq_of_insn(self.ps_start)
        self.fs_start_seq = self.seq_of_insn(self.fs_start)
        self.fs_end_seq = self.seq_of_insn(self.fs_start + self.fs_count - 1)
        self.unknown_seq = arrays['unknown_seq'].astype(np.int64)
        self.unknown_func = arrays['unknown_func'].astype(np.int32)
        self.not_taken_seq = arrays['not_taken_seq'].astype(np.int64)
        self.not_taken_func = arrays['not_taken_func'].astype(np.int32)
        self.call_pair_seq = arrays['call_pair_seq'].astype(np.int64)
        self.call_pair_func = arrays['call_pair_func'].astype(np.int32)
        self.ev_seq = arrays['ev_seq'].astype(np.int64)
        self.ev_type = arrays['ev_type']
        self.ev_text = arrays['ev_text']
        self.ev_next_func = arrays['ev_next_func'].astype(np.int32)
        self.st_func = arrays['st_func'].astype(np.int32)
        self.st_pc = arrays['st_pc']
        self.st_pred = arrays['st_pred'].astype(bool)
        self.st_flow = arrays['st_flow'].astype(np.int32)
        # 边界解码/局部聚合用 Python 列表做标量访问（比 numpy 标量索引快一个量级）
        self._st_func_list = self.st_func.tolist()
        self._st_hotspot_list = arrays['st_hotspot'].astype(np.int32).tolist()
        self._st_pred_list = self.st_pred.tolist()
        self._st_flow_list = self.st_flow.tolist()
        self._chain_cache = {}
        self.h_func = arrays['h_func'].astype(np.int32)
        self.h_file = arrays['h_file'].astype(np.int32)
        self.h_line = arrays['h_line'].astype(np.int32)
        # 节点标签 → 函数索引（路径伪标签如“中断 N”不映射）
        function_index = {name: index for index, name in enumerate(self.function_names)}
        node_function = np.full(self.node_count, -1, np.int32)
        for node in range(1, self.node_count):
            label = self.strings[int(self.node_label[node])]
            node_function[node] = function_index.get(label, -1)
        self._node_function = node_function
        # 节点树（子节点按节点号 = 创建顺序）
        children = [[] for _ in range(self.node_count)]
        for node in range(1, self.node_count):
            children[int(self.node_parent[node])].append(node)
        self._node_children = children
        self._prepared = True

    # -- seq ↔ 指令序号 ---------------------------------------------------------------------
    def insn_of_seq(self, seq):
        seq = np.asarray(seq, dtype=np.int64)
        return seq - np.searchsorted(self.event_seqs, seq, side='left')

    def seq_of_insn(self, index):
        """指令序号 → 逻辑 seq（= 指令序号 + 该 seq 之前的事件数）。

        不动点 ``seq = index + #{events < seq}``；若 ``seq`` 恰好落在事件上（记录开头常见
        连续两条边界事件，例如 ``[0, 1]``），必须先跳过该事件并**重新做不动点**，否则
        指令 0 会被算成事件 seq（t9 独立复现：原型返回 1，正确值 2）。
        """
        index = np.asarray(index, dtype=np.int64)
        events = self.event_seqs
        count = len(events)
        seq = index + np.searchsorted(events, index, side='left')
        for _ in range(count + 4):
            following = index + np.searchsorted(events, seq, side='left')
            inner = 0
            while not np.array_equal(following, seq):
                seq = following
                following = index + np.searchsorted(events, seq, side='left')
                inner += 1
                if inner > count + 4:
                    raise RuntimeError('seq_of_insn 不动点迭代未收敛（事件表异常）')
            if not count:
                return seq
            left = np.searchsorted(events, seq, side='left')
            hit = (left < count) & (events[np.minimum(left, count - 1)] == seq)
            if not hit.any():
                return seq
            seq = np.where(hit, seq + 1, seq)
        raise RuntimeError('seq_of_insn 无法收敛（事件表异常）')

    def next_instruction_seq(self, seq):
        """最小的一条指令 seq（≥ 参数；参数本身是指令时返回它）。"""
        seq = int(seq)
        position = int(np.searchsorted(self.event_seqs, seq, side='left'))
        while position < len(self.event_seqs) and int(self.event_seqs[position]) == seq:
            seq += 1
            position += 1
        return seq

    def prev_instruction_seq(self, seq):
        """最大的一条指令 seq（≤ 参数；参数本身是指令时返回它）。"""
        seq = int(seq)
        position = int(np.searchsorted(self.event_seqs, seq, side='right')) - 1
        while position >= 0 and int(self.event_seqs[position]) == seq:
            seq -= 1
            position -= 1
        return seq

    def _clamp(self, start, end):
        lo = self.first_seq if start is None else max(self.first_seq, min(self.last_seq, int(start)))
        hi = self.last_seq if end is None else max(self.first_seq, min(self.last_seq, int(end)))
        return lo, hi

    # -- 路径/节点 ---------------------------------------------------------------------------
    def path_of_leaf(self, leaf):
        cached = self._path_cache.get(leaf)
        if cached is not None:
            return cached
        labels = []
        node = int(leaf)
        while node > 0:
            labels.append(self.strings[int(self.node_label[node])])
            node = int(self.node_parent[node])
        labels.reverse()
        value = tuple(labels)
        if len(self._path_cache) < 200000:
            self._path_cache[leaf] = value
        return value

    def chain_of_leaf(self, leaf):
        cached = self._chain_cache.get(leaf)
        if cached is not None:
            return cached
        nodes = []
        node = int(leaf)
        while node > 0:
            nodes.append(node)
            node = int(self.node_parent[node])
        nodes.append(0)
        nodes.reverse()
        self._chain_cache[leaf] = nodes
        return nodes

    def leaf_at_insn(self, index):
        position = int(np.searchsorted(self.ps_start, index, side='right')) - 1
        return int(self.ps_leaf[max(0, position)])

    # -- 边界解码（有界：≤ 2 个单元格）--------------------------------------------------------
    def _open_store(self):
        """边界单元格的有界解码复用父 TraceStore（不重复打开、不重复校验）。"""
        if self._view is None:
            raise RuntimeError('查询索引未绑定 store，无法对边界单元格做有界解码')
        return self._view

    def close(self):
        self._view = None

    def _decode_instructions(self, i_lo, i_hi):
        """从单元格锚点起解码 [i_lo, i_hi] 的指令（静态 ID/执行状态/周期/seq）。"""
        import trace_store
        store = self._open_store()
        arrays = self.arrays
        cell = i_lo // self.cell_size
        block_ordinal = int(arrays['cell_anchor_block'][cell])
        if block_ordinal < 0:
            raise RuntimeError('单元格锚点缺失')
        skip = int(arrays['cell_anchor_skip'][cell])
        position = int(arrays['cell_anchor_offset'][cell])
        index = cell * self.cell_size
        ids = []
        executed = []
        cycles = []
        seqs = []
        payload = store.container.block(store.sequence_blocks[block_ordinal])
        run_position = skip
        while index <= i_hi:
            block_size = len(payload)
            while position < block_size:
                record_start = position
                record_type = payload[position]
                position += 1
                length, position = trace_store._dvar(payload, position)
                begin = position
                finish = position + length
                position = finish
                if record_type != trace_store.REC_RUN:
                    continue
                cursor = begin
                seq0, cursor = trace_store._dvar(payload, cursor)
                count, cursor = trace_store._dvar(payload, cursor)
                tail = payload[cursor]
                cursor += 1
                _offset, cursor = trace_store._dvar(payload, cursor)
                run_cycles, cursor = trace_store._dvar(payload, cursor)
                _span, cursor = trace_store._dvar(payload, cursor)
                _unknown, cursor = trace_store._dvar(payload, cursor)
                first_id, cursor = trace_store._dvar(payload, cursor)
                identifiers = [first_id]
                previous = first_id
                for _ in range(count - 1):
                    delta, cursor = trace_store._dzvar(payload, cursor)
                    previous += delta
                    identifiers.append(previous)
                if cursor != finish:
                    raise RuntimeError('展开段记录长度不符')
                for run_index in range(run_position, count):
                    if index > i_hi:
                        break
                    identifier = identifiers[run_index]
                    predicated = self._st_pred_list[identifier]
                    if predicated:
                        executed_code = 2
                    elif run_index == count - 1:
                        executed_code = tail if tail != TAIL_NONE else 2
                    else:
                        executed_code = TAIL_TRUE
                    if index >= i_lo:
                        ids.append(identifier)
                        executed.append(executed_code)
                        cycles.append(run_cycles if run_index == count - 1 else 0)
                        seqs.append(seq0 + run_index)
                    index += 1
                run_position = 0
                del record_start
                break
            else:
                # 当前块耗尽：切到下一个序列块
                block_ordinal += 1
                if block_ordinal >= len(store.sequence_blocks):
                    break
                payload = store.container.block(store.sequence_blocks[block_ordinal])
                position = 0
                run_position = 0
        return ids, executed, cycles, seqs

    # -- 聚合 -------------------------------------------------------------------------------
    def _segment_stats(self, i_lo, i_hi, acc):
        """遍历与 [i_lo,i_hi] 相交的路径区间，填充桶路径与火焰图。"""
        start = self.ps_start
        count = self.ps_count
        leaves = self.ps_leaf
        position = int(np.searchsorted(start, i_lo, side='right')) - 1
        if position < 0:
            position = 0
        length = len(start)
        prefix = None
        mixed = False
        while position < length and start[position] <= i_hi:
            seg_start = int(start[position])
            seg_end = seg_start + int(count[position])
            low = max(seg_start, i_lo)
            high = min(seg_end, i_hi + 1)
            if high > low:
                path = self.path_of_leaf(int(leaves[position]))
                if prefix is None:
                    prefix = path
                elif path != prefix:
                    mixed = True
                    prefix = _lcp(prefix, path)
                acc['segments'].append((low, high, int(leaves[position])))
            position += 1
        return prefix, mixed

    def _node_first_occurrence(self, i_lo, i_hi):
        """范围内每个路径节点的首次出现指令序号（火焰图子节点顺序与参考实现一致）。"""
        first = {0: i_lo}
        start = self.ps_start
        count = self.ps_count
        leaves = self.ps_leaf
        length = len(start)
        position = int(np.searchsorted(start, i_lo, side='right')) - 1
        if position < 0:
            position = 0
        while position < length and start[position] <= i_hi:
            seg_start = int(start[position])
            seg_end = seg_start + int(count[position])
            low = max(seg_start, i_lo)
            if min(seg_end, i_hi + 1) > low:
                for node in self.chain_of_leaf(int(leaves[position])):
                    if node not in first:
                        first[node] = low
            position += 1
        return first

    def _bucket_paths(self, bucket_bounds, i_lo, i_hi):
        """每个桶的 common-prefix 路径与 mixed 标记（桶按 seq 划分）。"""
        result = []
        start = self.ps_start
        count = self.ps_count
        leaves = self.ps_leaf
        length = len(start)
        position = int(np.searchsorted(start, i_lo, side='right')) - 1
        if position < 0:
            position = 0
        for low_seq, high_seq, low_insn, high_insn in bucket_bounds:
            if low_insn is None:
                result.append(([], False))
                continue
            while position < length and start[position] + count[position] <= low_insn:
                position += 1
            probe = position
            prefix = None
            mixed = False
            while probe < length and start[probe] < high_insn + 1:
                seg_start = int(start[probe])
                seg_end = seg_start + int(count[probe])
                if min(seg_end, high_insn + 1) > max(seg_start, low_insn):
                    path = self.path_of_leaf(int(leaves[probe]))
                    if prefix is None:
                        prefix = path
                    elif path != prefix:
                        mixed = True
                        prefix = _lcp(prefix, path)
                probe += 1
            result.append((list(prefix or []), mixed))
        return result

    def _covered_and_partial_cells(self, i_lo, i_hi):
        cell_lo = i_lo // self.cell_size
        cell_hi = i_hi // self.cell_size
        cell_start = cell_lo * self.cell_size
        cell_end = min(self.total_instructions - 1, (cell_lo + 1) * self.cell_size - 1)
        if cell_lo == cell_hi:
            if i_lo == cell_start and i_hi == cell_end:
                return (cell_lo, cell_hi), []
            return (-1, -1), [(cell_lo, i_lo, i_hi)]
        partials = []
        covered_lo = cell_lo
        covered_hi = cell_hi
        if i_lo > cell_start:
            partials.append((cell_lo, i_lo, cell_start + self.cell_size - 1))
            covered_lo = cell_lo + 1
        if i_hi < min(self.total_instructions - 1,
                      (cell_hi + 1) * self.cell_size - 1):
            partials.append((cell_hi, cell_hi * self.cell_size, i_hi))
            covered_hi = cell_hi - 1
        if covered_hi < covered_lo:
            covered_lo = covered_hi = -1
        return (covered_lo, covered_hi), partials

    def _range_aggregate(self, i_lo, i_hi):
        arrays = self.arrays
        hotspot_count = np.zeros(self.hotspot_count, np.int64)
        hotspot_unknown = np.zeros(self.hotspot_count, np.int64)
        hotspot_cycles = np.zeros(self.hotspot_count, np.int64)
        hotspot_first = np.full(self.hotspot_count, 1 << 30, np.int64)
        hotspot_first_id = np.full(self.hotspot_count, -1, np.int64)
        node_value = np.zeros(self.node_count, np.int64)
        node_value_cycles = np.zeros(self.node_count, np.int64)
        node_self = np.zeros(self.node_count, np.int64)
        node_self_cycles = np.zeros(self.node_count, np.int64)
        (covered_lo, covered_hi), partials = self._covered_and_partial_cells(i_lo, i_hi)
        if covered_lo >= 0:
            cell_slice = slice(covered_lo, covered_hi + 1)
            hotspot_count += arrays['cell_hotspot_count'][cell_slice].sum(axis=0).astype(np.int64)
            hotspot_unknown += arrays['cell_hotspot_unknown'][cell_slice].sum(axis=0).astype(np.int64)
            node_value += arrays['cell_node_value'][cell_slice].sum(axis=0).astype(np.int64)
            node_value_cycles += arrays['cell_node_value_cycles'][cell_slice].sum(axis=0).astype(np.int64)
            present = arrays['cell_hotspot_count'][cell_slice] > 0
            if present.any():
                first_relative = present.argmax(axis=0)
                any_present = present.any(axis=0)
                first_cell = covered_lo + first_relative
                packed = arrays['cell_hotspot_first'][first_cell, np.arange(self.hotspot_count)]
                offset = (packed & 0xFFF).astype(np.int64)
                identifier = (packed >> 12).astype(np.int64)
                candidate = first_cell.astype(np.int64) * self.cell_size + offset
                chosen = any_present
                hotspot_first[chosen] = candidate[chosen]
                hotspot_first_id[chosen] = identifier[chosen]
            # 周期/自环稀疏表
            hs = arrays['hs_cycles_cells']
            low = int(np.searchsorted(hs, covered_lo, side='left'))
            high = int(np.searchsorted(hs, covered_hi, side='right'))
            if high > low:
                hotspot_cycles += np.bincount(
                    arrays['hs_cycles_hotspots'][low:high],
                    weights=arrays['hs_cycles_values'][low:high],
                    minlength=self.hotspot_count).astype(np.int64)
            self_cells = arrays['node_self_cells']
            low = int(np.searchsorted(self_cells, covered_lo, side='left'))
            high = int(np.searchsorted(self_cells, covered_hi, side='right'))
            if high > low:
                node_self += np.bincount(arrays['node_self_nodes'][low:high],
                                         weights=arrays['node_self_counts'][low:high],
                                         minlength=self.node_count).astype(np.int64)
                node_self_cycles += np.bincount(
                    arrays['node_self_nodes'][low:high],
                    weights=arrays['node_self_cycles'][low:high],
                    minlength=self.node_count).astype(np.int64)
        # 边界单元格：有界解码逐指令累加
        ps_start = self.ps_start
        ps_count = self.ps_count
        ps_leaf = self.ps_leaf
        segment_position = 0
        for _cell, low, high in partials:
            if high < low:
                continue
            ids, executed, cycles, seqs = self._decode_instructions(low, high)
            segment_position = max(0, int(np.searchsorted(ps_start, low, side='right')) - 1)
            for item, (identifier, executed_code, cycle_count, seq) in \
                    enumerate(zip(ids, executed, cycles, seqs)):
                index = low + item
                while segment_position + 1 < len(ps_start) \
                        and int(ps_start[segment_position + 1]) <= index:
                    segment_position += 1
                leaf = int(ps_leaf[segment_position])
                hotspot = self._st_hotspot_list[identifier]
                hotspot_count[hotspot] += 1
                if self._st_pred_list[identifier]:
                    hotspot_unknown[hotspot] += 1
                if index < hotspot_first[hotspot]:
                    hotspot_first[hotspot] = index
                    hotspot_first_id[hotspot] = identifier
                if cycle_count:
                    hotspot_cycles[hotspot] += cycle_count
                for node in self.chain_of_leaf(leaf):
                    node_value[node] += 1
                    node_value_cycles[node] += cycle_count
                node_self[leaf] += 1
                node_self_cycles[leaf] += cycle_count
                del executed_code, seq
        return dict(hotspot_count=hotspot_count, hotspot_unknown=hotspot_unknown,
                    hotspot_cycles=hotspot_cycles, hotspot_first=hotspot_first,
                    hotspot_first_id=hotspot_first_id, node_value=node_value,
                    node_value_cycles=node_value_cycles, node_self=node_self,
                    node_self_cycles=node_self_cycles)

    def _function_aggregate(self, i_lo, i_hi, seq_lo, seq_hi):
        arrays = self.arrays
        function_count = self.function_count
        instructions = np.zeros(function_count, np.int64)
        first_occurrence = np.full(function_count, 1 << 30, np.int64)
        start = self.fs_start
        count = self.fs_count
        functions = self.fs_func
        length = len(start)
        position = int(np.searchsorted(start, i_lo, side='right')) - 1
        if position < 0:
            position = 0
        while position < length and start[position] <= i_hi:
            seg_start = int(start[position])
            seg_end = seg_start + int(count[position])
            low = max(seg_start, i_lo)
            high = min(seg_end, i_hi + 1)
            if high > low:
                function = int(functions[position])
                instructions[function] += high - low
                if first_occurrence[function] > (1 << 29):
                    first_occurrence[function] = low
            position += 1
        unknown = np.zeros(function_count, np.int64)
        not_taken = np.zeros(function_count, np.int64)
        insn_seq_lo = int(self.seq_of_insn(i_lo))
        insn_seq_hi = int(self.seq_of_insn(i_hi))
        low = int(np.searchsorted(self.unknown_seq, insn_seq_lo, side='left'))
        high = int(np.searchsorted(self.unknown_seq, insn_seq_hi, side='right'))
        if high > low:
            unknown += np.bincount(self.unknown_func[low:high], minlength=function_count)
        low = int(np.searchsorted(self.not_taken_seq, insn_seq_lo, side='left'))
        high = int(np.searchsorted(self.not_taken_seq, insn_seq_hi, side='right'))
        if high > low:
            not_taken += np.bincount(self.not_taken_func[low:high], minlength=function_count)
        calls = np.zeros(function_count, np.int64)
        low = int(np.searchsorted(self.call_pair_seq, insn_seq_lo, side='left'))
        high = int(np.searchsorted(self.call_pair_seq, insn_seq_hi + 1, side='right'))
        # 调用配对要求“调用指令之后的指令”也在窗口内：call seq ≤ hi-1
        if high > low:
            usable = self.call_pair_seq[low:high] < insn_seq_hi
            if usable.any():
                calls += np.bincount(self.call_pair_func[low:high][usable],
                                     minlength=function_count)
        interrupts = np.zeros(function_count, np.int64)
        # 参考口径（§4.3-4）：type-9 事件本身在窗口 [lo,hi] 内、且其后一条记录是指令才计
        # 中断入口；窗口从事件开始时也要计入（不能以“首条指令 seq”为下界）。
        low = int(np.searchsorted(self.ev_seq, seq_lo, side='left'))
        high = int(np.searchsorted(self.ev_seq, seq_hi, side='right'))
        for index in range(low, high):
            if int(self.ev_type[index]) == 9 and int(self.ev_seq[index]) < seq_hi:
                target = int(self.ev_next_func[index])
                if target >= 0:
                    interrupts[target] += 1
        return dict(instructions=instructions, unknown=unknown, not_taken=not_taken,
                    calls=calls, interrupts=interrupts, first_occurrence=first_occurrence)

    def insn_of_seq_offset(self, index):
        """指令序号 → seq 的增量（seq - index = 该指令前的事件数）。"""
        return int(self.seq_of_insn(index)) - int(index)
    def window(self, start=None, end=None, bins=1200):
        lo, hi = self._clamp(start, end)
        if hi < lo:
            raise trace_store.TraceStoreError('区间结束序号不能小于起点')
        default = self._default_view('window', lo, hi, bins)
        if default is not None:
            return default
        self._prepare()
        bins = max(100, min(2000, int(bins)))
        span = hi - lo + 1
        step = max(1, math.ceil(span / bins))
        bucket_count = (span + step - 1) // step
        first_instruction = self.next_instruction_seq(lo)
        has_instruction = first_instruction <= hi
        if has_instruction:
            i_lo = int(self.insn_of_seq(first_instruction))
            last_instruction = self.prev_instruction_seq(hi)
            i_hi = int(self.insn_of_seq(last_instruction))
        else:
            i_lo = i_hi = None
        buckets = []
        bucket_bounds = []
        for index in range(bucket_count):
            low_seq = lo + index * step
            high_seq = min(hi, low_seq + step - 1)
            if has_instruction:
                candidate = self.next_instruction_seq(low_seq)
                if candidate <= high_seq:
                    low_insn = int(self.insn_of_seq(candidate))
                    high_insn = int(self.insn_of_seq(self.prev_instruction_seq(high_seq)))
                else:
                    low_insn = high_insn = None
            else:
                low_insn = high_insn = None
            bucket_bounds.append((low_seq, high_seq, low_insn, high_insn))
            records = high_seq - low_seq + 1
            event_low = int(np.searchsorted(self.ev_seq, low_seq, side='left'))
            event_high = int(np.searchsorted(self.ev_seq, high_seq, side='right'))
            events = []
            for position in range(event_low, event_high):
                events.append(dict(seq=int(self.ev_seq[position]),
                                   type=int(self.ev_type[position]),
                                   text=self.strings[int(self.ev_text[position])]))
            records -= len(events)
            if has_instruction and low_insn is not None:
                unknown = int(np.searchsorted(self.unknown_seq, high_seq, side='right')) \
                    - int(np.searchsorted(self.unknown_seq, low_seq, side='left'))
                not_taken = int(np.searchsorted(self.not_taken_seq, high_seq, side='right')) \
                    - int(np.searchsorted(self.not_taken_seq, low_seq, side='left'))
            else:
                unknown = not_taken = 0
            # 参考实现：桶由首条记录创建，firstSeq 即该记录 seq（= startSeq）
            buckets.append(dict(startSeq=low_seq, endSeq=high_seq,
                                firstSeq=low_seq, path=None, records=records,
                                unknown=unknown, notTaken=not_taken, mixed=False,
                                events=events))
        # 参考实现最后统一把 path=None 归一为 []（纯事件桶也一样），这里保持一致。
        paths = self._bucket_paths(bucket_bounds, i_lo, i_hi) if has_instruction             else [([], False) for _ in buckets]
        for bucket, (path, mixed) in zip(buckets, paths):
            bucket['path'] = path
            bucket['mixed'] = mixed
            if mixed:
                bucket['path'] = bucket['path'] + [MIXED_PATH_MARKER]
        # ---- 区间级聚合 ----
        first_seq = int(first_instruction) if has_instruction else None
        if has_instruction:
            aggregate = self._range_aggregate(i_lo, i_hi)
            function_stats = self._function_aggregate(i_lo, i_hi, lo, hi)
        else:
            aggregate = dict(hotspot_count=np.zeros(self.hotspot_count, np.int64),
                             hotspot_unknown=np.zeros(self.hotspot_count, np.int64),
                             hotspot_cycles=np.zeros(self.hotspot_count, np.int64),
                             hotspot_first=np.full(self.hotspot_count, 1 << 30, np.int64),
                             hotspot_first_id=np.full(self.hotspot_count, -1, np.int64),
                             node_value=np.zeros(self.node_count, np.int64),
                             node_value_cycles=np.zeros(self.node_count, np.int64),
                             node_self=np.zeros(self.node_count, np.int64),
                             node_self_cycles=np.zeros(self.node_count, np.int64))
            function_stats = dict(
                instructions=np.zeros(self.function_count, np.int64),
                unknown=np.zeros(self.function_count, np.int64),
                not_taken=np.zeros(self.function_count, np.int64),
                calls=np.zeros(self.function_count, np.int64),
                interrupts=np.zeros(self.function_count, np.int64),
                first_occurrence=np.full(self.function_count, 1 << 30, np.int64))

        total_cycles_per_function = np.zeros(self.function_count, np.int64)
        node_function = self._node_function
        valid_nodes = node_function >= 0
        if valid_nodes.any():
            total_cycles_per_function += np.bincount(
                node_function[valid_nodes],
                weights=aggregate['node_value_cycles'][valid_nodes],
                minlength=self.function_count).astype(np.int64)
        self_cycles_per_function = np.zeros(self.function_count, np.int64)
        if valid_nodes.any():
            self_cycles_per_function += np.bincount(
                node_function[valid_nodes],
                weights=aggregate['node_self_cycles'][valid_nodes],
                minlength=self.function_count).astype(np.int64)
        total_cycles = int(self_cycles_per_function.sum())
        function_entries = []
        for function in range(self.function_count):
            count = int(function_stats['instructions'][function])
            if not count:
                continue
            unknown = int(function_stats['unknown'][function])
            not_taken = int(function_stats['not_taken'][function])
            first_occurrence = int(function_stats['first_occurrence'][function])
            function_entries.append((function, first_occurrence, dict(
                function=self.function_names[function], instructions=count,
                knownExecuted=count - unknown - not_taken,
                unknownExecution=unknown, notTaken=not_taken,
                calls=int(function_stats['calls'][function]),
                interruptEntries=int(function_stats['interrupts'][function]),
                selfCycles=int(self_cycles_per_function[function]),
                totalCycles=int(total_cycles_per_function[function]))))
        function_entries.sort(key=lambda item: (-item[2]['instructions'], item[1]))
        functions = [item[2] for item in function_entries]
        observed_calls = sum(item['calls'] for item in functions)
        for item in functions:
            item['cyclePercent'] = (100 * item['selfCycles'] / total_cycles) if total_cycles else 0
            item['cpi'] = (item['selfCycles'] / item['instructions']
                           if (item['instructions'] and item['selfCycles']) else None)
        cycle_entries = [item for item in function_entries if item[2]['selfCycles']]
        cycle_entries.sort(key=lambda item: (-item[2]['selfCycles'], item[1]))
        cycle_functions = [dict(function=item[2]['function'], selfCycles=item[2]['selfCycles'],
                                cyclePercent=item[2]['cyclePercent'],
                                instructions=item[2]['instructions'], cpi=item[2]['cpi'],
                                calls=item[2]['calls'])
                           for item in cycle_entries]
        hotspots = []
        for hotspot in range(self.hotspot_count):
            records = int(aggregate['hotspot_count'][hotspot])
            if not records:
                continue
            identifier = int(aggregate['hotspot_first_id'][hotspot])
            hotspots.append(dict(function=self.function_names[int(self.h_func[hotspot])],
                                 file=None if int(self.h_file[hotspot]) < 0
                                 else self.strings[int(self.h_file[hotspot])],
                                 line=None if int(self.h_line[hotspot]) < 0
                                 else int(self.h_line[hotspot]),
                                 records=records,
                                 unknown=int(aggregate['hotspot_unknown'][hotspot]),
                                 cycles=int(aggregate['hotspot_cycles'][hotspot]),
                                 pc=int(self.st_pc[identifier]) if identifier >= 0 else 0,
                                 first=int(aggregate['hotspot_first'][hotspot])))
        hotspots.sort(key=lambda item: (-item['cycles'], -item['records'], item['first']))

        node_first = self._node_first_occurrence(i_lo, i_hi) if has_instruction else {0: 0}

        def flame_node(node):
            name = '所选执行区间' if node == 0 else self.strings[int(self.node_label[node])]
            children = [child for child in self._node_children[node]
                        if aggregate['node_value'][child] > 0]
            children.sort(key=lambda child: (node_first.get(child, 1 << 62), child))
            return dict(name=name, value=int(aggregate['node_value'][node]),
                        self=int(aggregate['node_self'][node]),
                        selfCycles=int(aggregate['node_self_cycles'][node]),
                        valueCycles=int(aggregate['node_value_cycles'][node]),
                        children=[flame_node(child) for child in children])

        flame = flame_node(0)
        executed_instructions = sum(item['knownExecuted'] for item in functions)
        instructions_total = sum(item['instructions'] for item in functions)
        analysis = dict(
            flame=flame, functions=functions, hotspots=[
                {key: value for key, value in item.items() if key != 'first'}
                for item in hotspots],
            observedCalls=observed_calls,
            timing=dict(totalCycles=total_cycles,
                        cycleRanges=len(cycle_functions),
                        executedInstructions=executed_instructions,
                        instructions=instructions_total,
                        cpi=(total_cycles / executed_instructions
                             if (executed_instructions and total_cycles) else None),
                        functions=cycle_functions,
                        timeFlameAvailable=bool(total_cycles),
                        basis=('所选区间的周期计数（区间末指令记录，可求和）' if total_cycles
                               else '所选区间没有周期计数数据（目标未启用或 ETMv3 不支持）')),
            weight=('所选区间的路径记录数；周期列才是耗时' if total_cycles
                    else '所选区间的路径记录数（含未成立/未知状态），不是耗时'))
        return dict(range=dict(start=lo, end=hi, totalStart=self.first_seq,
                               totalEnd=self.last_seq),
                    firstSeq=first_seq,
                    timing=dict(axis='execution-order', verifiedTime=False,
                                reason=REASON_NO_TIME),
                    timeline=buckets, resolution=step, analysis=analysis)

    def _function_key(self, index, functions):
        return functions[index]['function']

    # -- function_timeline -------------------------------------------------------------------
    def function_timeline(self, start=None, end=None, search='', hide_skipped=False,
                          bins=1600, max_functions=6):
        if search or hide_skipped:
            raise RuntimeError('索引快路径不支持 search/hide_skipped（调用方应回退慢路径）')
        lo, hi = self._clamp(start, end)
        if hi < lo:
            raise trace_store.TraceStoreError('区间结束序号不能小于起点')
        bins = max(240, min(2400, int(bins)))
        max_functions = max(1, min(10, int(max_functions)))
        default = self._default_view('timeline', lo, hi, bins, search, hide_skipped,
                                     max_functions)
        if default is not None:
            return default
        self._prepare()
        span = hi - lo + 1
        first_instruction = self.next_instruction_seq(lo)
        has_instruction = first_instruction <= hi
        function_bins = np.zeros((bins, self.function_count), np.int64)
        if has_instruction:
            i_lo = int(self.insn_of_seq(first_instruction))
            i_hi = int(self.insn_of_seq(self.prev_instruction_seq(hi)))
            self._distribute_function_segments(lo, hi, bins, span, i_lo, i_hi, function_bins)
        unknown_bins = np.zeros(bins, np.int64)
        not_taken_bins = np.zeros(bins, np.int64)
        function_unknown_bins = np.zeros((bins, self.function_count), np.int64)
        function_not_taken_bins = np.zeros((bins, self.function_count), np.int64)
        for seqs, funcs, target_bins, target_func_bins, target_scalar in (
                (self.unknown_seq, self.unknown_func, None, function_unknown_bins,
                 unknown_bins),
                (self.not_taken_seq, self.not_taken_func, None, function_not_taken_bins,
                 not_taken_bins)):
            low = int(np.searchsorted(seqs, lo, side='left'))
            high = int(np.searchsorted(seqs, hi, side='right'))
            if high <= low:
                continue
            seq_slice = seqs[low:high]
            index = np.minimum(bins - 1, (seq_slice - lo) * bins // span)
            target_scalar += np.bincount(index, minlength=bins)
            combined = index * self.function_count + funcs[low:high]
            target_func_bins += np.bincount(combined, minlength=bins * self.function_count) \
                .reshape(bins, self.function_count)
        executed_bins = function_bins - function_unknown_bins - function_not_taken_bins
        executed_totals = executed_bins.sum(axis=0)
        order = sorted(range(self.function_count),
                       key=lambda f: (-int(executed_totals[f]), self.function_names[f]))
        top = [function for function in order[:max_functions] if executed_totals[function] > 0]
        labels = []
        values = []
        for function in top:
            key = 'fn:' + self.function_names[function]
            labels.append(dict(key=key, name=self.function_names[function], kind='function',
                               records=int(executed_totals[function])))
            values.append(dict(key=key, bins=[int(value) for value in executed_bins[:, function]]))
        if top:
            mask = np.ones(self.function_count, bool)
            mask[top] = False
            other_bins = executed_bins[:, mask].sum(axis=1)
            other_total = int(other_bins.sum())
            if other_total:
                labels.append(dict(key='other', name='其他函数', kind='other',
                                   records=other_total))
                values.append(dict(key='other', bins=[int(value) for value in other_bins]))
        unknown_total = int(unknown_bins.sum())
        if unknown_total:
            labels.append(dict(key='unknown', name='执行状态未知', kind='unknown',
                               records=unknown_total))
            values.append(dict(key='unknown', bins=[int(value) for value in unknown_bins]))
        not_taken_total = int(not_taken_bins.sum())
        if not_taken_total and not hide_skipped:
            labels.append(dict(key='not-taken', name='未成立路径（未证实执行）', kind='not-taken',
                               records=not_taken_total))
            values.append(dict(key='not-taken', bins=[int(value) for value in not_taken_bins]))
        lane_values = {item['key']: item['bins'] for item in values}
        for lane in labels:
            lane['bins'] = lane_values[lane['key']]
        markers = []
        marker_count = 0
        low = int(np.searchsorted(self.ev_seq, lo, side='left'))
        high = int(np.searchsorted(self.ev_seq, hi, side='right'))
        marker_bins = {}
        for position in range(low, high):
            seq = int(self.ev_seq[position])
            etype = int(self.ev_type[position])
            marker_count += 1
            index = min(bins - 1, max(0, (seq - lo) * bins // span))
            bucket = marker_bins.get(index)
            if bucket is None:
                bucket = dict(startSeq=seq, endSeq=seq, seq=seq, types={})
                marker_bins[index] = bucket
            bucket['endSeq'] = seq
            bucket['types'][str(etype)] = bucket['types'].get(str(etype), 0) + 1
        for bucket in marker_bins.values():
            bucket['count'] = sum(bucket['types'].values())
            markers.append(bucket)
        records_total = int(hi - lo + 1)
        return dict(range=dict(start=lo, end=hi, totalStart=self.first_seq,
                               totalEnd=self.last_seq),
                    lanes=labels, binCount=bins, markers=markers,
                    markerCount=marker_count,
                    summary=dict(executed=int(executed_totals.sum()),
                                 unknown=unknown_total, notTaken=not_taken_total,
                                 filteredRecords=records_total, markers=marker_count),
                    timing=dict(axis='execution-order', verifiedTime=False,
                                reason='当前记录没有已验收的逐指令时间戳，横轴只表示执行序号'))

    def _distribute_function_segments(self, lo, hi, bins, span, i_lo, i_hi, target):
        start = self.fs_start
        count = self.fs_count
        functions = self.fs_func
        start_seq_array = self.fs_start_seq
        end_seq_array = self.fs_end_seq
        length = len(start)
        position = int(np.searchsorted(start, i_lo, side='right')) - 1
        if position < 0:
            position = 0
        events = self.event_seqs
        event_position = 0
        while position < length and start[position] <= i_hi:
            seg_start = int(start[position])
            seg_end = seg_start + int(count[position])
            low = max(seg_start, i_lo)
            high = min(seg_end, i_hi + 1)
            if high <= low:
                position += 1
                continue
            seq_start = int(start_seq_array[position]) if low == seg_start \
                else int(self.seq_of_insn(low))
            seq_end = int(end_seq_array[position]) if high == seg_end \
                else int(self.seq_of_insn(high - 1))
            while event_position < len(events) and int(events[event_position]) < seq_start:
                event_position += 1
            first_bin = min(bins - 1, max(0, (seq_start - lo) * bins // span))
            last_bin = min(bins - 1, max(0, (seq_end - lo) * bins // span))
            function = int(functions[position])
            for bucket in range(first_bin, last_bin + 1):
                bucket_start = lo + -(-bucket * span // bins)
                bucket_end = lo + -(-(bucket + 1) * span // bins) - 1
                low_seq = max(seq_start, bucket_start)
                high_seq = min(seq_end, bucket_end)
                if high_seq < low_seq:
                    continue
                while event_position < len(events) and int(events[event_position]) < low_seq:
                    event_position += 1
                event_count = 0
                probe = event_position
                while probe < len(events) and int(events[probe]) <= high_seq:
                    event_count += 1
                    probe += 1
                event_position = probe
                target[bucket, function] += high_seq - low_seq + 1 - event_count
            position += 1


    # -- 默认视图（构建期持久化的全段结果）--------------------------------------------------
    def default_text(self, name):
        cached = self._default_texts.get(name)
        if cached is not None:
            return cached
        member = 'default_%s_json' % name
        array = self.arrays.get(member)
        if array is None:
            return None
        text = _json_text(array)
        declared = ((self.meta.get('defaultViews') or {}).get(name) or {})
        expected = declared.get('sha256')
        if expected and hashlib.sha256(text.encode('utf-8')).hexdigest() != expected:
            raise QueryIndexError('默认视图 %s 的 sha256 与 meta 不符（索引被改动）' % name)
        self._default_texts[name] = text
        return text

    def _default_view(self, name, lo, hi, bins, search='', hide_skipped=False,
                      max_functions=None):
        """默认参数 + 全段区间才命中预计算视图；其余情况返回 None（走算法路径）。"""
        if int(lo) != int(self.first_seq) or int(hi) != int(self.last_seq):
            return None
        if name == 'window':
            if int(bins) != DEFAULT_WINDOW_BINS:
                return None
        else:
            if search or hide_skipped:
                return None
            if int(bins) != DEFAULT_TIMELINE_BINS:
                return None
            if max_functions is not None \
                    and int(max_functions) != DEFAULT_TIMELINE_MAX_FUNCTIONS:
                return None
        text = self.default_text(name)
        if not text:
            return None
        value = json.loads(text)
        span = value.get('range') or {}
        if int(span.get('totalStart', -1)) != int(self.first_seq) \
                or int(span.get('totalEnd', -1)) != int(self.last_seq):
            return None
        return copy.deepcopy(value)

    # -- 自洽校验（载入全部表之前/之后都必须成立）-------------------------------------------
    def _check_consistency(self):
        arrays = self.arrays
        meta = self.meta
        total = int(meta['instructions'])
        cells = int(meta['cells'])
        cell_size = int(meta['cellSize'])
        problems = []
        if (cells - 1) * cell_size >= total or cells * cell_size < total:
            problems.append('单元格划分与指令数不符（%d×%d vs %d）'
                            % (cells, cell_size, total))
        try:
            ps_start = arrays['ps_start']
            ps_count = arrays['ps_count']
            ps_leaf = arrays['ps_leaf']
            fs_start = arrays['fs_start']
            fs_count = arrays['fs_count']
            fs_func = arrays['fs_func']
        except KeyError as error:
            raise QueryIndexError('查询索引缺少必需表：%s' % error)
        if not (len(ps_start) == len(ps_count) == len(ps_leaf)):
            problems.append('路径区间表长度不一致')
        elif len(ps_start) and (int(ps_start[0]) != 0
                                or int(ps_start[-1]) + int(ps_count[-1]) != total):
            problems.append('路径区间未覆盖全部指令')
        if not (len(fs_start) == len(fs_count) == len(fs_func)):
            problems.append('函数区间表长度不一致')
        elif not len(fs_start) or int(fs_start[0]) != 0 \
                or int(fs_start[-1]) + int(fs_count[-1]) != total:
            problems.append('函数区间未覆盖全部指令')
        shapes = (('cell_first_seq', (cells,)), ('cell_insn', (cells,)),
                  ('cell_hotspot_count', (cells, int(meta['hotspotCount']))),
                  ('cell_node_value', (cells, int(meta['nodeCount']))))
        for name, shape in shapes:
            array = arrays.get(name)
            if array is None:
                problems.append('缺少表 %s' % name)
            elif tuple(array.shape) != shape:
                problems.append('表 %s 形状 %r≠%r' % (name, tuple(array.shape), shape))
        for name in ('unknown_seq', 'not_taken_seq', 'call_pair_seq', 'ev_seq',
                     'cycle_anchor_seq'):
            array = arrays.get(name)
            if array is None:
                problems.append('缺少表 %s' % name)
            elif len(array) > 1 and np.any(np.diff(array) < 0):
                problems.append('表 %s 未按 seq/ID 递增' % name)
        if problems:
            raise QueryIndexError('查询索引自洽校验失败：%s' % '；'.join(problems[:4]))


class _SinkView:
    """写入期视图：``_BlockSink`` 里已写入的序列块 + 写入器内存中的静态/字符串/计数。

    属性与 ``TraceStore`` 同形，``build_tables`` 无需区分两者（不重复打开 store）。
    """

    def __init__(self, sink, static, strings, counts, first_seq, last_seq):
        self.sink = sink
        self.static = list(static)
        self.strings = list(strings)
        self.counts = dict(counts)
        self.first_seq = int(first_seq)
        self.last_seq = int(last_seq)
        sequence = trace_store.BLK_SEQUENCE
        self.sequence_blocks = [ordinal for ordinal, entry in enumerate(sink.entries)
                                if entry[3] == sequence]
        self.container = self

    def block(self, ordinal):
        return self.sink.payload(ordinal)

    def close(self):
        pass


def build_index(view, cell_size=DEFAULT_CELL_INSTRUCTIONS, progress=None,
                with_nav=True, container='v1'):
    """构建索引负载：语义表 + 默认视图，返回 {payload, meta, arrays, 计时与体积}。"""
    started = time.perf_counter()
    built = build_tables(view, cell_size=cell_size, progress=bool(progress),
                         with_nav=with_nav)
    arrays, meta = built['arrays'], built['meta']
    if container not in ('v1', 'v2'):
        raise QueryIndexError('未知查询索引容器：%r' % (container,))
    meta['format'] = QUERY_INDEX_FORMAT
    meta['version'] = QUERY_INDEX_VERSION
    meta['container'] = container
    meta['tables'] = sorted(arrays)
    meta['coverage'] = [int(meta['firstSeq']), int(meta['lastSeq'])]
    # 默认全段视图：用同一套查询算法生成并持久化（命中时解压即返回）。
    defaults_started = time.perf_counter()
    index = QueryIndex(_EagerArrays(arrays), meta, view)
    defaults = {}
    for name, value, bins in (
            ('window', index.window(None, None, DEFAULT_WINDOW_BINS), DEFAULT_WINDOW_BINS),
            ('timeline', index.function_timeline(None, None, bins=DEFAULT_TIMELINE_BINS),
             DEFAULT_TIMELINE_BINS)):
        text = json.dumps(value, ensure_ascii=False)
        arrays['default_%s_json' % name] = _json_array(text)
        defaults[name] = dict(bins=bins, bytes=len(text.encode('utf-8')),
                              sha256=hashlib.sha256(text.encode('utf-8')).hexdigest())
    meta['defaultViews'] = defaults
    meta['tables'] = sorted(arrays)
    defaults_seconds = round(time.perf_counter() - defaults_started, 3)
    if container == 'v2':
        payload = encode_payload_v2(arrays, meta)
    else:
        payload = encode_payload(arrays, meta)
    total_seconds = round(time.perf_counter() - started, 3)
    return dict(payload=payload, meta=meta, arrays=arrays,
                buildSeconds=total_seconds, defaultsSeconds=defaults_seconds,
                scanTimings=built['timings'], payloadBytes=len(payload),
                arrayBytes={name: int(np.ascontiguousarray(value).nbytes)
                            for name, value in arrays.items()})


def build_in_sink(sink, static, strings, counts, first_seq, last_seq,
                  cell_size=DEFAULT_CELL_INSTRUCTIONS, progress=None, with_nav=True,
                  container='v1'):
    """`trace_store_write.write(build_query_index=True)` 用：把 12 号块加入 sink。"""
    view = _SinkView(sink, static, strings, counts, first_seq, last_seq)
    result = build_index(view, cell_size=cell_size, progress=progress, with_nav=with_nav,
                         container=container)
    sink.add(trace_store.BLK_QUERY, result['payload'], int(result['meta']['cells']))
    return result


def _index_meta(payload_meta, result):
    """写进 meta.queryIndex / manifest.queryIndex 的精简元信息（不含各表大字段）。"""
    meta = {key: payload_meta[key] for key in _TOP_KEYS if key in payload_meta}
    meta['tables'] = list(payload_meta.get('tables') or [])
    meta['coverage'] = list(payload_meta.get('coverage') or [])
    meta['defaultViews'] = payload_meta.get('defaultViews') or {}
    meta['payloadBytes'] = result['payloadBytes']
    meta['buildSeconds'] = result['buildSeconds']
    meta['defaultsSeconds'] = result['defaultsSeconds']
    meta['builtAtUnix'] = int(time.time())
    meta['builtBy'] = 'python-reference'
    return meta


def build_store_index(store_dir, cell_size=DEFAULT_CELL_INSTRUCTIONS, progress=None,
                       with_nav=True, container='v1'):
    """给已发布 store 建立查询索引：写新 generation（复制块 + 替换 meta + 追加 12 号块），
    最后原子发布 manifest；旧 generation 保留（现有语义）。失败不改变当前代。"""
    started = time.perf_counter()
    store_dir = Path(store_dir).resolve()
    store = trace_store.TraceStore(store_dir, verify='full')
    try:
        manifest = dict(store.manifest or {})
        meta = dict(store.meta or {})
        if not meta:
            raise trace_store.TraceStoreError('store 缺少 meta 块，拒绝建查询索引')
        result = build_index(store, cell_size=cell_size, progress=progress,
                             with_nav=with_nav, container=container)
        index_meta = _index_meta(result['meta'], result)
        generation_tag = secrets.randbits(63) or 1
        generation = 'g-%s-%s' % (time.strftime('%Y%m%dT%H%M%S'), secrets.token_hex(4))
        generation_dir = store_dir / 'generations' / generation
        generation_dir.mkdir(parents=False)
        temp_payload = generation_dir / (trace_store.STORE_FILE + '.payload.tmp')
        sink = trace_store._BlockSink(temp_payload)
        try:
            meta['queryIndex'] = index_meta
            for ordinal, entry in enumerate(store.container.blocks):
                if entry.block_type == trace_store.BLK_META:
                    sink.add_compressed_json(trace_store.BLK_META, meta)
                    continue
                if entry.block_type == trace_store.BLK_QUERY:
                    continue                          # 重建：丢掉旧索引块
                payload = store.container.raw_block(ordinal)
                sink.add_raw(entry.block_type, payload, entry.count, entry.flags)
            sink.add(trace_store.BLK_QUERY, result['payload'], int(result['meta']['cells']))
            sink.handle.flush()
            info = sink.assemble(generation_dir / trace_store.STORE_FILE, generation_tag,
                                 optional_features=trace_store.QUERY_INDEX_FEATURE)
        finally:
            sink.close()
            try:
                temp_payload.unlink()
            except OSError:
                pass
        manifest['generation'] = generation
        manifest['generationTag'] = '0x%016x' % generation_tag
        manifest['files'] = [dict(name=trace_store.STORE_FILE, bytes=info['bytes'],
                                  sha256=info['sha256'], blocks=info['blocks'],
                                  payloadBytes=info['payloadBytes'])]
        counts = dict(manifest.get('counts') or {})
        counts['queryIndex'] = {key: index_meta.get(key) for key in
                                ('version', 'cellSize', 'cells', 'segmentCount',
                                 'functionSegments', 'payloadBytes', 'buildSeconds')}
        manifest['counts'] = counts
        manifest['queryIndex'] = index_meta
        notes = list(manifest.get('notes') or [])
        notes.append('本代含持久化查询索引（BLK_QUERY=12，feature 0x0001）：'
                     'window/function_timeline 走索引快路径；TRACE_STORE_SLOW=1 强制慢路径对照')
        manifest['notes'] = notes
        manifest['state'] = 'complete'
        trace_store._publish_manifest(store_dir, manifest)
        # 发布后自检：新代必须能加载索引（否则明确失败，不假装成功）
        verify = trace_store.TraceStore(store_dir, verify='full')
        try:
            if verify.query_index is None:
                raise trace_store.TraceStoreError('索引代发布后未能加载查询索引：%s'
                                                  % (verify.query_index_error or '未知原因'))
        finally:
            verify.close()
        return dict(ok=True, storeDir=str(store_dir), generation=generation,
                    storeBytes=info['bytes'], storeSha256=info['sha256'],
                    indexPayloadBytes=result['payloadBytes'],
                    indexMeta=index_meta,
                    buildSeconds=result['buildSeconds'],
                    defaultsSeconds=result['defaultsSeconds'],
                    totalSeconds=round(time.perf_counter() - started, 3),
                    peakRssBytes=trace_store.peak_rss_bytes(),
                    scanTimings=result['scanTimings'],
                    defaultsBytes={name: item['bytes']
                                   for name, item in (result['meta']['defaultViews'] or {}).items()})
    finally:
        store.close()


def reencode_store_index(store_dir, container='v2', progress=None):
    """把现有块 12 换成另一种容器（v1↔v2），不重扫 sequence：写新 generation 并原子发布。

    用于 R2 迁移/对照：v1(npz) 与 v2(表目录+zlib) 的表内容相同，只是容器不同；
    本函数只重编码块 12 负载（并更新 meta.queryIndex/manifest），生产构建仍走 build_tables。
    """
    if container not in ('v1', 'v2'):
        raise QueryIndexError('未知查询索引容器：%r' % (container,))
    started = time.perf_counter()
    store_dir = Path(store_dir).resolve()
    store = trace_store.TraceStore(store_dir, verify='full')
    try:
        index = store.query_index
        if index is None:
            raise trace_store.TraceStoreError('store 没有查询索引，无法重编码块 12')
        archive = index.arrays
        names = getattr(archive, 'files', None) or archive._archive.files
        arrays = {name: np.asarray(archive[name]) for name in names}
        meta = dict(index.meta)
        meta['container'] = container
        if container == 'v2':
            payload = encode_payload_v2(arrays, meta)
        else:
            payload = encode_payload(arrays, meta)
        index_meta = _index_meta(meta, dict(payloadBytes=len(payload), buildSeconds=0.0,
                                            defaultsSeconds=0.0))
        manifest = dict(store.manifest or {})
        generation_tag = secrets.randbits(63) or 1
        generation = 'g-%s-%s' % (time.strftime('%Y%m%dT%H%M%S'), secrets.token_hex(4))
        generation_dir = store_dir / 'generations' / generation
        generation_dir.mkdir(parents=False)
        temp_payload = generation_dir / (trace_store.STORE_FILE + '.payload.tmp')
        sink = trace_store._BlockSink(temp_payload)
        try:
            source_meta = dict(store.meta or {})
            source_meta['queryIndex'] = index_meta
            for ordinal, entry in enumerate(store.container.blocks):
                if entry.block_type == trace_store.BLK_META:
                    sink.add_compressed_json(trace_store.BLK_META, source_meta)
                    continue
                if entry.block_type == trace_store.BLK_QUERY:
                    continue
                sink.add_raw(entry.block_type, store.container.raw_block(ordinal),
                             entry.count, entry.flags)
            sink.add(trace_store.BLK_QUERY, payload, int(meta.get('cells') or 0))
            sink.handle.flush()
            info = sink.assemble(generation_dir / trace_store.STORE_FILE, generation_tag,
                                 optional_features=trace_store.QUERY_INDEX_FEATURE)
        finally:
            sink.close()
            try:
                temp_payload.unlink()
            except OSError:
                pass
        manifest['generation'] = generation
        manifest['generationTag'] = '0x%016x' % generation_tag
        manifest['files'] = [dict(name=trace_store.STORE_FILE, bytes=info['bytes'],
                                  sha256=info['sha256'], blocks=info['blocks'],
                                  payloadBytes=info['payloadBytes'])]
        counts = dict(manifest.get('counts') or {})
        counts['queryIndex'] = {key: index_meta.get(key) for key in
                                ('version', 'cellSize', 'cells', 'segmentCount',
                                 'functionSegments', 'payloadBytes', 'buildSeconds',
                                 'container')}
        manifest['counts'] = counts
        manifest['queryIndex'] = index_meta
        notes = list(manifest.get('notes') or [])
        notes.append('本代块 12 容器：%s（reencode_store_index，不重扫 sequence）' % container)
        manifest['notes'] = notes
        manifest['state'] = 'complete'
        trace_store._publish_manifest(store_dir, manifest)
        verify = trace_store.TraceStore(store_dir, verify='full')
        try:
            if verify.query_index is None:
                raise trace_store.TraceStoreError('重编码后索引加载失败：%s'
                                                  % (verify.query_index_error or '未知原因'))
            loaded = verify.stats()['queryIndex']
        finally:
            verify.close()
        del progress
        return dict(ok=True, storeDir=str(store_dir), generation=generation,
                    container=container, sourceContainer=index.meta.get('container'),
                    payloadBytes=len(payload), storeBytes=info['bytes'],
                    storeSha256=info['sha256'], indexMeta=index_meta,
                    loadedSummary=loaded,
                    totalSeconds=round(time.perf_counter() - started, 3),
                    peakRssBytes=trace_store.peak_rss_bytes())
    finally:
        store.close()


def index_summary(store):
    """store.stats()/诊断用：当前查询索要的版本与规模（无索引返回 None）。"""
    index = getattr(store, 'query_index', None)
    if index is None:
        return None
    meta = index.meta
    nav = meta.get('nav') or None
    return dict(version=int(meta.get('version') or 0),
                container=(meta.get('container') or 'v1'),
                cellSize=int(meta.get('cellSize') or 0),
                cells=int(meta.get('cells') or 0),
                segmentCount=int(meta.get('segmentCount') or 0),
                functionSegments=int(meta.get('functionSegments') or 0),
                defaultViews=sorted((meta.get('defaultViews') or {})),
                # 块 12 导航表（t14）：R2 原生 builder 同遍产出；旧索引为 None（回退 sidecar）
                nav=(None if nav is None else dict(
                    cellSize=int(nav.get('cellSize') or 0),
                    pcCellPairs=int(nav.get('pcCellPairs') or 0),
                    lineRuns=int(nav.get('lineRuns') or 0))),
                builtBy=((store.meta.get('queryIndex') or {}).get('builtBy')))


def _main(argv=None):
    parser = argparse.ArgumentParser(
        prog='trace_store_index.py',
        description='trace-store 查询索引（块 12）：为已发布 store 一次性建立/查看索引。')
    sub = parser.add_subparsers(dest='command', required=True)
    build = sub.add_parser('build', help='给 store 建立查询索引（写新 generation 并原子发布）')
    build.add_argument('store', help='store 目录（含 manifest.json），也接受记录目录')
    build.add_argument('--cell', type=int, default=DEFAULT_CELL_INSTRUCTIONS,
                       help='单元格指令数（默认 4096，与设计 §3 一致）')
    build.add_argument('--quiet', action='store_true')
    info = sub.add_parser('info', help='查看当前代的索引概要')
    info.add_argument('store')
    reencode = sub.add_parser('reencode',
                              help='把现有块 12 换成另一种容器（v1↔v2，不重扫）')
    reencode.add_argument('store')
    reencode.add_argument('--container', choices=('v1', 'v2'), default='v2')
    args = parser.parse_args(argv)
    store_dir = Path(args.store)
    if (store_dir / 'store').is_dir() and not (store_dir / 'manifest.json').is_file():
        store_dir = store_dir / 'store'
    if args.command == 'build':
        result = build_store_index(store_dir, cell_size=args.cell,
                                   progress=None if args.quiet else 'progress')
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    if args.command == 'reencode':
        store_dir = Path(args.store)
        if (store_dir / 'store').is_dir() and not (store_dir / 'manifest.json').is_file():
            store_dir = store_dir / 'store'
        result = reencode_store_index(store_dir, container=args.container)
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    if args.command == 'info':
        store = trace_store.TraceStore(store_dir, verify='sizes')
        try:
            summary = dict(generation=store.generation,
                           queryIndex=index_summary(store),
                           meta=(store.meta.get('queryIndex') or None),
                           stats=store.stats())
        finally:
            store.close()
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(_main())
