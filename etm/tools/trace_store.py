"""trace-store v1 参考实现：把既有解码记录写成版本化二进制存储，并按页/窗口无损展开。

本模块是方案 §5（存储契约）与 §6（查询兼容）的参考读写器，只验证数据模型，
不承担生产性能。格式定义见 ``etm/docs/trace-store-v1.md``。

关键模型（与旧 ``events.jsonl`` 语义一一对应）：

* **静态代码字典**：每个唯一 PC 存一次机器码/长度/反汇编/函数/文件/行/flow/谓词标志，
  写入时用 ``firmware.elf`` 的镜像字节与 DWARF 行表交叉校验。
* **动态展开段**：每条记录保存逻辑 seq 起点、指令数、trace offset、末指令执行状态、
  区间周期与 ``cycleSpan``、内部谓词未知计数，以及按增量编码的静态 ID 列表。
  静态字段只出现一次；本格式不保存任何逐指令 JSON 行。
* **调用状态检查点**：调用栈/上下文栈/上一条指令/异常返回点按帧 ID 结构共享保存，
  读取任意 seq 时从最近检查点恢复，再用与旧实现完全相同的 ``flow_analysis.CallTracker``
  重放最多一个检查点间隔，因此不扫描整段逻辑指令、也不逐条保存调用路径字符串。
* **稀疏/分层索引**：块级 seq 区间（AGG）+ 检查点索引 + 记录级顺序解码。
* **摘要**：``overview``/``analysis``/``quality``/``sources``/``issues`` 以 zlib JSON 块保存，
  写入时与事件流逐项对账（计数、首指令 offset、执行状态计数等）。
* **完整性**：magic/schemaVersion/字节序/记录类型/块长度/计数/有效范围显式编码，
  块带 CRC32，长度乘加先做上界检查，manifest 绑定输入/ELF/配置/处理版本指纹，
  独立 generation 目录 + 最后原子发布 manifest。
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import secrets
import struct
import sys
import threading
import time
import zlib
from pathlib import Path

__all__ = ['TraceStore', 'TraceStoreError', 'write', 'open_store', 'FORMAT_NAME',
           'SCHEMA_VERSION', 'IT_REASON', 'peak_rss_bytes', 'describe_store']


# ---------------------------------------------------------------------------------------------
# 常量与错误
# ---------------------------------------------------------------------------------------------

FORMAT_NAME = 'trace-store'
FULL_FORMAT_NAME = 'trace-store v1'
MAGIC = b'ETMTSv1\x00'
SCHEMA_VERSION = 1
BYTE_ORDER_MARK = 0x01020304        # 小端写入该值；读到 0x04030201 即大端文件，明确拒绝
HEADER_SIZE = 56
INDEX_ENTRY_SIZE = 24
TARGET_BLOCK_BYTES = 1 << 20        # 目标块大小：单块独立校验，读取按块加载
MAX_BLOCK_BYTES = 64 << 20          # 单块上界（含压缩块声明长度），超过即拒绝
MAX_BLOCKS = 1 << 20
MAX_STRING_BYTES = 1 << 20
MAX_RECORD_BYTES = 256 << 10
MAX_RUN_INSTRUCTIONS = 4096         # 单条展开段最多指令数：保证检查点间隔与块内可解码
DEFAULT_CHECKPOINT_INSTRUCTIONS = 4096
# 诊断开关（t10 前后对比/复现用）：=1 时恢复“打开即解析 nav/issues、顺序整份 sha256”的旧行为。
# 只改变加载时序，不改变任何校验内容与查询结果。
EAGER_LOAD_ENV = 'TRACE_STORE_EAGER_LOAD'
MAX_CALL_DEPTH = 128                # 与 flow_analysis.CallTracker 的限制一致
FILE_TRACE = 1
MANIFEST_NAME = 'manifest.json'
STORE_FILE = 'store.bin'

# 块类型
BLK_STRINGS = 1
BLK_STATIC = 2
BLK_SEQUENCE = 3
BLK_PACKETS = 4
BLK_NAV = 5
BLK_AGG = 6
BLK_META = 7
BLK_SUMMARY = 8
BLK_SOURCES = 9
BLK_ISSUES = 10
BLK_CHECKPOINTS = 11
BLK_QUERY = 12                      # R1 持久化查询索引（可选块；旧记录没有它）
BLOCK_NAMES = {BLK_STRINGS: 'strings', BLK_STATIC: 'static', BLK_SEQUENCE: 'sequence',
               BLK_PACKETS: 'packets', BLK_NAV: 'nav', BLK_AGG: 'agg', BLK_META: 'meta',
               BLK_SUMMARY: 'summary', BLK_SOURCES: 'sources', BLK_ISSUES: 'issues',
               BLK_CHECKPOINTS: 'checkpoints', BLK_QUERY: 'query'}
NAV_BLOCK_TYPES = {BLK_STRINGS, BLK_STATIC, BLK_SEQUENCE, BLK_PACKETS, BLK_NAV, BLK_AGG,
                   BLK_META, BLK_SUMMARY, BLK_SOURCES, BLK_ISSUES, BLK_CHECKPOINTS,
                   BLK_QUERY}
# 文件头 optional_features 位：存在 12 号查询索引块（required_features 恒为 0，
# 旧读取器读到未知块类型会明确报错而不是错读；见 docs/etm-r1-query-design.md §3.1）。
QUERY_INDEX_FEATURE = 0x0001

# 序列记录类型
REC_RUN = 1
REC_EVENT = 2
REC_CHECKPOINT = 3

# 导航模式（与 record_store 的 event_navigation.mode 同名同义）
NAV_CALL = 1
NAV_RETURN = 2
NAV_EXCEPTION = 3
NAV_ISSUE = 4
NAV_EVENT = 5
NAV_NAMES = {NAV_CALL: 'call', NAV_RETURN: 'return', NAV_EXCEPTION: 'exception',
             NAV_ISSUE: 'issue', NAV_EVENT: 'event'}

FLOW_CODES = {'linear': 0, 'call': 1, 'return': 2, 'branch': 3, 'unknown': 4}
FLOW_NAMES = {value: key for key, value in FLOW_CODES.items()}
EXECUTED_CODES = {False: 0, True: 1, None: 2}
EXECUTED_VALUES = {0: False, 1: True, 2: None}
TAIL_CODES = {True: 0, False: 1, None: 2}
TAIL_VALUES = {0: True, 1: False, 2: None}
STATE_FROM_EXECUTED = {True: 'executed', False: 'not-taken', None: 'unknown'}

# 与 decode_stream.IT_REASON 同文；写入时逐条校验，避免记录里出现别的理由文本
IT_REASON = 'IT 非分支条件指令：当前 trace 未记录条件结果'

INSTRUCTION_FIELDS = ('kind', 'seq', 'offset', 'pc', 'size', 'bytes', 'mnemonic', 'operands',
                      'executed', 'function', 'file', 'line')
CYCLE_FIELDS = ('cycleCount', 'cycleSpan')
TAIL_FIELDS = ('flow', 'conditional', 'executionState', 'executionReason', 'callPath', 'cycles')
# 旧记录实际出现过的三种指令字段集合（当前 ETMv4 / 旧版无周期字段 / 旧版连 cycles 也没有）。
# 读取端只按 meta 里保存的字段顺序展开，绝不补造记录原本没有的字段。
INSTRUCTION_VARIANTS = (
    INSTRUCTION_FIELDS + CYCLE_FIELDS + TAIL_FIELDS,
    INSTRUCTION_FIELDS + ('flow', 'conditional', 'executionState', 'executionReason',
                          'callPath', 'cycles'),
    INSTRUCTION_FIELDS + ('flow', 'conditional', 'executionState', 'executionReason', 'callPath'),
)
INSTRUCTION_REQUIRED = frozenset(INSTRUCTION_FIELDS) | frozenset(
    ('flow', 'conditional', 'executionState', 'executionReason', 'callPath'))
INSTRUCTION_ALLOWED = INSTRUCTION_REQUIRED | frozenset(CYCLE_FIELDS) | frozenset(('cycles',))
INSTRUCTION_LENGTHS = frozenset(len(variant) for variant in INSTRUCTION_VARIANTS)
EVENT_FIELDS = ('kind', 'seq', 'type', 'offset', 'text', 'payload', 'start', 'end', 'flags')
EVENT_TYPES = (1, 2, 3, 6, 7, 8, 9, 10)          # 与 decode_stream 的显式事件类型一致
BOUNDARY_TYPES = (1, 2, 6, 7, 8)                 # record_store 的边界/issue 口径
GAP_TYPES = (1, 6, 7, 8)                         # decode_stream 的 gaps 口径（type 2 另看 payload）
AGG_KEYS = ('blockIndex', 'firstSeq', 'lastSeq', 'recordCount', 'instructions', 'executed',
            'unknown', 'notTaken', 'cycles', 'rareEvents', 'calls', 'returns', 'exceptions',
            'boundaries')


class TraceStoreError(ValueError):
    """trace-store 格式/完整性/版本错误（拒绝返回拼凑结果）。"""


# ---------------------------------------------------------------------------------------------
# 基础编码
# ---------------------------------------------------------------------------------------------

def _crc32(data):
    return zlib.crc32(data) & 0xFFFFFFFF


def _wv(buffer, value):
    """无符号 LEB128。"""
    if value < 0:
        raise TraceStoreError('变长整数不能为负：%d' % value)
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            buffer.append(byte | 0x80)
        else:
            buffer.append(byte)
            return


def _wzv(buffer, value):
    """zigzag + LEB128（有符号增量）。"""
    _wv(buffer, (value << 1) ^ (value >> 63) if value < 0 else (value << 1))


def _dvar(data, pos):
    result = 0
    shift = 0
    length = len(data)
    while True:
        if pos >= length:
            raise TraceStoreError('变长整数被截断')
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise TraceStoreError('变长整数超过 64 位')


def _dzvar(data, pos):
    value, pos = _dvar(data, pos)
    return (value >> 1) ^ -(value & 1), pos


def _wpair(buffer, data):
    """varint 长度 + 原始字节。"""
    _wv(buffer, len(data))
    buffer += data


def _dpair(data, pos):
    length, pos = _dvar(data, pos)
    end = pos + length
    if end > len(data):
        raise TraceStoreError('长度前缀越界')
    return bytes(data[pos:end]), end


def _checked_mul(a, b, limit, what):
    if a < 0 or b < 0:
        raise TraceStoreError('%s：负数长度' % what)
    if a and b > limit // a:
        raise TraceStoreError('%s：长度乘加溢出（%d×%d 超过 %d）' % (what, a, b, limit))
    return a * b


def _checked_add(a, b, limit, what):
    if a < 0 or b < 0:
        raise TraceStoreError('%s：负数长度' % what)
    total = a + b
    if total > limit:
        raise TraceStoreError('%s：长度累加超界（%d+%d 超过 %d）' % (what, a, b, limit))
    return total


def _sha256_file(path, chunk=1 << 20):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(chunk), b''):
            digest.update(block)
    return digest.hexdigest()


def peak_rss_bytes():
    """当前进程峰值 RSS（Windows 用 GetProcessMemoryInfo，其它平台用 resource）。"""
    try:
        import psutil
        info = psutil.Process().memory_info()
        return int(getattr(info, 'peak_wset', None) or info.rss)
    except Exception:
        pass
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                        ('PeakWorkingSetSize', ctypes.c_size_t),
                        ('WorkingSetSize', ctypes.c_size_t),
                        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                        ('PagefileUsage', ctypes.c_size_t),
                        ('PeakPagefileUsage', ctypes.c_size_t),
                        ('PrivateUsage', ctypes.c_size_t)]

        counters = PROCESS_MEMORY_COUNTERS_EX()
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


def eager_load_mode():
    """``TRACE_STORE_EAGER_LOAD=1``：恢复 t10 之前的打开时序（诊断/对照专用）。"""
    return os.environ.get(EAGER_LOAD_ENV, '').strip().lower() in ('1', 'true', 'yes', 'on')


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


# ---------------------------------------------------------------------------------------------
# 只读容器
# ---------------------------------------------------------------------------------------------

class _Block:
    __slots__ = ('offset', 'length', 'count', 'block_type', 'flags', 'crc32')

    def __init__(self, offset, length, count, block_type, flags, crc):
        self.offset, self.length, self.count = offset, length, count
        self.block_type, self.flags, self.crc32 = block_type, flags, crc


def _inflate(data, expected):
    if expected < 0 or expected > MAX_BLOCK_BYTES:
        raise TraceStoreError('压缩块声明解压长度无效：%d（拒绝巨量分配）' % expected)
    decompressor = zlib.decompressobj()
    output = decompressor.decompress(data, expected + 1)
    if len(output) != expected:
        raise TraceStoreError('压缩块解压长度与声明不符：%d≠%d' % (len(output), expected))
    if not decompressor.eof:
        raise TraceStoreError('压缩块数据不完整')
    if decompressor.unused_data:
        raise TraceStoreError('压缩块尾部有多余数据')
    return output


class _Container:
    """单个 ``store.bin`` 的只读视图；头部/索引严格校验，块按需加载并验证 CRC。"""

    def __init__(self, path, expect_type, generation_tag, verify_crc=True):
        self.path = Path(path)
        self.verify_crc = verify_crc
        self._lock = threading.Lock()
        self._cache = {}
        try:
            self._handle = open(self.path, 'rb')
        except OSError as error:
            raise TraceStoreError('无法打开存储文件 %s：%s' % (self.path.name, error))
        size = self.path.stat().st_size
        header = self._handle.read(HEADER_SIZE)
        if len(header) != HEADER_SIZE:
            raise TraceStoreError('存储文件头部被截断：%s' % self.path.name)
        magic = header[:8]
        if magic != MAGIC:
            raise TraceStoreError('magic 不符：%r（不是 trace-store v1 文件）' % magic)
        version, file_type, order = struct.unpack_from('<HHI', header, 8)
        if version != SCHEMA_VERSION:
            raise TraceStoreError('不支持的 schemaVersion=%d（本读取器支持 %d）'
                                  % (version, SCHEMA_VERSION))
        if order != BYTE_ORDER_MARK:
            if order == 0x04030201:
                raise TraceStoreError('文件按大端编码；本实现只支持小端，明确拒绝而不是猜测')
            raise TraceStoreError('字节序标记无效：0x%08x' % order)
        if file_type != expect_type:
            raise TraceStoreError('文件类型不符：%d（期望 %d）' % (file_type, expect_type))
        tag, blocks, required, payload_bytes, file_bytes, header_crc, optional = \
            struct.unpack_from('<QIIQQII', header, 16)
        self.generation_tag, self.block_count = tag, blocks
        self.required_features, self.optional_features = required, optional
        if tag != generation_tag:
            raise TraceStoreError('generation 标记不符：文件 %d，manifest %d（拒绝混读两代结果）'
                                 % (tag, generation_tag))
        if required:
            raise TraceStoreError('文件声明了本读取器不认识的强制特性位：0x%08x' % required)
        if _crc32(header[:48]) != header_crc:
            raise TraceStoreError('文件头校验和错误')
        if file_bytes != size:
            raise TraceStoreError('文件长度与头部声明不符：实际 %d，声明 %d（截断或混入其它数据）'
                                 % (size, file_bytes))
        if blocks <= 0 or blocks > MAX_BLOCKS:
            raise TraceStoreError('块数量无效：%d' % blocks)
        index_bytes = _checked_mul(blocks, INDEX_ENTRY_SIZE, file_bytes, '块索引长度')
        payload_start = _checked_add(HEADER_SIZE, index_bytes, file_bytes, '块索引起点')
        raw = self._handle.read(index_bytes)
        if len(raw) != index_bytes:
            raise TraceStoreError('块索引被截断')
        entries = []
        previous_end = payload_start
        total = 0
        for position in range(blocks):
            offset, length, count, block_type, flags, crc = \
                struct.unpack_from('<QIIHHI', raw, position * INDEX_ENTRY_SIZE)
            if block_type not in NAV_BLOCK_TYPES:
                raise TraceStoreError('块 %d 类型未知：%d' % (position, block_type))
            if offset != previous_end:
                raise TraceStoreError('块 %d 偏移不连续（%d，期望 %d）：索引损坏或块重叠'
                                      % (position, offset, previous_end))
            if length > MAX_BLOCK_BYTES:
                raise TraceStoreError('块 %d 长度 %d 超过上限 %d（拒绝巨量分配）'
                                      % (position, length, MAX_BLOCK_BYTES))
            if flags & ~0x1:
                raise TraceStoreError('块 %d 含未知标志位 0x%04x' % (position, flags))
            end = _checked_add(offset, length, file_bytes, '块 %d 末端' % position)
            previous_end = end
            total = _checked_add(total, length, file_bytes, '块总长度')
            entries.append(_Block(offset, length, count, block_type, flags, crc))
        if total != payload_bytes:
            raise TraceStoreError('块长度之和与头部声明不符')
        if previous_end != file_bytes:
            raise TraceStoreError('块区末端与文件长度不符')
        self.blocks = entries

    def block(self, index):
        if not 0 <= index < len(self.blocks):
            raise TraceStoreError('块索引越界：%d' % index)
        with self._lock:
            cached = self._cache.get(index)
            if cached is not None:
                return cached
            entry = self.blocks[index]
            self._handle.seek(entry.offset)
            payload = self._handle.read(entry.length)
            if len(payload) != entry.length:
                raise TraceStoreError('块 %d 被截断' % index)
            if self.verify_crc and _crc32(payload) != entry.crc32:
                raise TraceStoreError('块 %d 校验和错误（%s 块）'
                                      % (index, BLOCK_NAMES.get(entry.block_type, '?')))
            if entry.flags & 0x1:
                payload = _inflate(payload, entry.count)
            if len(self._cache) >= 8:
                self._cache.pop(next(iter(self._cache)))
            self._cache[index] = payload
            return payload

    def of_type(self, block_type):
        return [index for index, entry in enumerate(self.blocks)
                if entry.block_type == block_type]

    def raw_block(self, index):
        """原始块字节（保留压缩标志，不解压、不写缓存）；索引重建时按块复制用。"""
        if not 0 <= index < len(self.blocks):
            raise TraceStoreError('块索引越界：%d' % index)
        entry = self.blocks[index]
        with self._lock:
            self._handle.seek(entry.offset)
            payload = self._handle.read(entry.length)
        if len(payload) != entry.length:
            raise TraceStoreError('块 %d 被截断' % index)
        if self.verify_crc and _crc32(payload) != entry.crc32:
            raise TraceStoreError('块 %d 校验和错误（%s 块）'
                                  % (index, BLOCK_NAMES.get(entry.block_type, '?')))
        return payload

    def close(self):
        with self._lock:
            self._cache.clear()
            if not self._handle.closed:
                self._handle.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------------------------
# 输出容器（块先落临时文件，最后拼装 header + index + payload）
# ---------------------------------------------------------------------------------------------

class _BlockSink:
    def __init__(self, temp_path, expected_blocks=None):
        if expected_blocks is not None and not 0 < expected_blocks <= MAX_BLOCKS:
            raise TraceStoreError('预留块数量无效')
        self.expected_blocks = expected_blocks
        self.payload_start = HEADER_SIZE + expected_blocks * INDEX_ENTRY_SIZE if expected_blocks is not None else 0
        self.handle = open(temp_path, 'w+b')
        if self.payload_start:self.handle.write(bytes(self.payload_start))
        self.entries = []
        self.total = 0

    def add(self, block_type, payload, count):
        if not isinstance(payload, (bytes, bytearray)):
            payload = bytes(payload)
        if len(payload) > MAX_BLOCK_BYTES:
            raise TraceStoreError('块长度 %d 超过上限 %d' % (len(payload), MAX_BLOCK_BYTES))
        self.entries.append((self.total, len(payload), int(count), int(block_type), 0,
                             _crc32(payload)))
        self.handle.write(payload)
        self.total += len(payload)

    def add_raw(self, block_type, payload, count, flags=0):
        """按已有块的原始字节/标志复制（索引重建复制 store.bin 用；CRC 重新计算）。"""
        if not isinstance(payload, (bytes, bytearray)):
            payload = bytes(payload)
        if len(payload) > MAX_BLOCK_BYTES:
            raise TraceStoreError('块长度 %d 超过上限 %d' % (len(payload), MAX_BLOCK_BYTES))
        if int(flags) & ~0x1:
            raise TraceStoreError('块标志位未知：0x%04x' % int(flags))
        self.entries.append((self.total, len(payload), int(count), int(block_type),
                             int(flags), _crc32(payload)))
        self.handle.write(payload)
        self.total += len(payload)

    def payload(self, index):
        """读取已写入 sink 的第 index 块原始字节（不关闭/不移动文件指针）。"""
        if not 0 <= index < len(self.entries):
            raise TraceStoreError('块索引越界：%d' % index)
        offset, length = self.entries[index][0], self.entries[index][1]
        position = self.handle.tell()
        self.handle.seek(self.payload_start + offset)
        data = self.handle.read(length)
        self.handle.seek(position)
        if len(data) != length:
            raise TraceStoreError('块 %d 被截断' % index)
        return data

    def add_compressed_json(self, block_type, value):
        raw = _json_bytes(value)
        compressed = zlib.compress(raw, 6)
        if len(compressed) < len(raw):
            self.entries.append((self.total, len(compressed), len(raw), int(block_type), 1,
                                 _crc32(compressed)))
            self.handle.write(compressed)
            self.total += len(compressed)
        else:
            self.add(block_type, raw, len(raw))

    def assemble(self, path, generation_tag, optional_features=0):
        """把临时 payload 拼成最终文件；边写边算 sha256。"""
        blocks = len(self.entries)
        if blocks <= 0 or blocks > MAX_BLOCKS:
            raise TraceStoreError('块数量无效：%d' % blocks)
        index_bytes = blocks * INDEX_ENTRY_SIZE
        payload_start = HEADER_SIZE + index_bytes
        index = bytearray(index_bytes)
        for position, (offset, length, count, block_type, flags, crc) in enumerate(self.entries):
            struct.pack_into('<QIIHHI', index, position * INDEX_ENTRY_SIZE,
                             payload_start + offset, length, count, block_type, flags, crc)
        payload_bytes = self.total
        file_bytes = _checked_add(payload_start, payload_bytes, 1 << 62, '文件总长度')
        header = bytearray(HEADER_SIZE)
        header[0:8] = MAGIC
        struct.pack_into('<HHI', header, 8, SCHEMA_VERSION, FILE_TRACE, BYTE_ORDER_MARK)
        struct.pack_into('<QIIQQII', header, 16, generation_tag, blocks, 0,
                         payload_bytes, file_bytes, 0, int(optional_features))
        struct.pack_into('<I', header, 48, _crc32(bytes(header[:48])))
        digest = hashlib.sha256()
        if self.expected_blocks is not None:
            if blocks != self.expected_blocks:
                raise TraceStoreError('实际块数量与预留数量不符')
            # The payload is already in its final position. Complete its
            # directory in place, then hash the finished file before publishing.
            self.handle.seek(0)
            self.handle.write(header);self.handle.write(index)
            self.handle.flush();os.fsync(self.handle.fileno())
            self.handle.seek(0)
            while True:
                chunk = self.handle.read(1 << 20)
                if not chunk:break
                digest.update(chunk)
            temporary = self.handle.name
            self.handle.close()
            if Path(temporary).stat().st_size != file_bytes:
                raise TraceStoreError('直接写出文件长度不符')
            os.replace(temporary, path)
            return dict(bytes=file_bytes, sha256=digest.hexdigest(), blocks=blocks, payloadBytes=payload_bytes)
        with open(path, 'wb') as output:
            output.write(header)
            output.write(index)
            digest.update(header)
            digest.update(index)
            self.handle.flush()
            self.handle.seek(0)
            while True:
                chunk = self.handle.read(1 << 20)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        written = Path(path).stat().st_size
        if written != file_bytes:
            raise TraceStoreError('写出文件长度不符：%d≠%d' % (written, file_bytes))
        return dict(bytes=written, sha256=digest.hexdigest(), blocks=blocks,
                    payloadBytes=payload_bytes)

    def close(self):
        if not self.handle.closed:
            self.handle.close()


# ---------------------------------------------------------------------------------------------
# 写入用：字符串/静态字典/序列缓冲
# ---------------------------------------------------------------------------------------------

class _Strings:
    def __init__(self):
        self.ids = {'': 0}
        self.values = ['']

    def intern(self, value):
        if value is None:
            raise TraceStoreError('不能用 None 作为字符串')
        if not isinstance(value, str):
            raise TraceStoreError('字符串字段必须是 str：%r' % (value,))
        identifier = self.ids.get(value)
        if identifier is None:
            identifier = len(self.values)
            if identifier >= (1 << 31):
                raise TraceStoreError('字符串数量超界')
            self.ids[value] = identifier
            self.values.append(value)
        return identifier

    def id_of(self, value):
        identifier = self.ids.get(value)
        if identifier is None:
            identifier = self.intern(value)
        return identifier


class _StaticEntry:
    __slots__ = ('pc', 'size', 'bytes', 'hexed', 'mnemonic', 'operands', 'function', 'file',
                 'line', 'flow', 'conditional', 'predicated', 'identifier', 'checks')

    def __init__(self, pc, size, code, hexed, mnemonic, operands, function, file, line, flow,
                 conditional, predicated, identifier):
        self.pc, self.size, self.bytes, self.hexed = pc, size, code, hexed
        self.mnemonic, self.operands = mnemonic, operands
        self.function, self.file, self.line = function, file, line
        self.flow, self.conditional, self.predicated = flow, conditional, predicated
        self.identifier = identifier
        self.checks = (size, hexed, mnemonic, operands, function, file, line, flow, conditional,
                       predicated)

    def event_checks(self, event):
        return (event['size'], event['bytes'], event['mnemonic'], event['operands'],
                event['function'], event['file'], event['line'], event['flow'],
                event['conditional'], self.predicated)


class _SequenceBuffer:
    """把序列记录按块缓冲。

    块只在检查点前切分，因此**每个块的第一条记录必然是检查点**；块内记录不跨块，
    读取端可以从块内任意检查点索引项独立解码，不必依赖其它块。
    """

    def __init__(self, sink, checkpoint_instructions):
        self.sink = sink
        self.buffer = bytearray()
        self.checkpoint_instructions = max(256, int(checkpoint_instructions))
        self.agg = []
        self.block_index = 0
        self.since_checkpoint = 0
        self.stats = None
        self.first_seq = None
        self.last_seq = None
        self.record_count = 0
        self.checkpoints = []
        self._reset_block_stats()

    def _reset_block_stats(self):
        self.stats = dict(instructions=0, executed=0, unknown=0, notTaken=0, cycles=0,
                          rareEvents=0, calls=0, returns=0, exceptions=0, boundaries=0)

    def size(self):
        return len(self.buffer)

    def _append(self, record):
        if len(record) > MAX_RECORD_BYTES:
            raise TraceStoreError('序列记录过大：%d' % len(record))
        if len(self.buffer) + len(record) > MAX_BLOCK_BYTES:
            raise TraceStoreError('序列块超过上限：%d' % (len(self.buffer) + len(record)))
        offset = len(self.buffer)
        self.buffer += record
        self.record_count += 1
        return offset

    def rotate(self):
        """落当前块（只在检查点前调用，保证块首是检查点）。"""
        if not self.buffer:
            return
        if self.first_seq is None or self.last_seq is None:
            raise TraceStoreError('内部错误：序列块缺少 seq 范围')
        aggregate = dict(blockIndex=self.block_index, firstSeq=self.first_seq,
                         lastSeq=self.last_seq, recordCount=self.record_count, **self.stats)
        self.sink.add(BLK_SEQUENCE, bytes(self.buffer), self.record_count)
        self.agg.append(aggregate)
        self.block_index += 1
        self.buffer = bytearray()
        self.record_count = 0
        self.first_seq = self.last_seq = None
        self._reset_block_stats()

    def note_seq(self, first, last):
        if self.first_seq is None or first < self.first_seq:
            self.first_seq = first
        if self.last_seq is None or last > self.last_seq:
            self.last_seq = last

    def emit_checkpoint(self, seq, tracker, strings):
        payload = bytearray()
        _wv(payload, seq)
        _encode_tracker(payload, tracker, strings)
        record = bytearray([REC_CHECKPOINT])
        _wpair(record, bytes(payload))
        offset = self._append(record)
        self.checkpoints.append((seq, self.block_index, offset))
        self.since_checkpoint = 0

    def emit_run(self, start_seq, count, identifiers, tail_executed, offset, cycles, cycle_span,
                 internal_unknown):
        payload = bytearray()
        _wv(payload, start_seq)
        _wv(payload, count)
        payload.append(TAIL_CODES[tail_executed])
        _wv(payload, offset)
        _wv(payload, cycles)
        _wv(payload, cycle_span)
        _wv(payload, internal_unknown)
        _wv(payload, identifiers[0])
        previous = identifiers[0]
        for identifier in identifiers[1:]:
            _wzv(payload, identifier - previous)
            previous = identifier
        record = bytearray([REC_RUN])
        _wpair(record, bytes(payload))
        self._append(record)
        self.note_seq(start_seq, start_seq + count - 1)
        self.stats['instructions'] += count
        self.stats['executed'] += (count - 1 - internal_unknown) + (tail_executed is True)
        self.stats['unknown'] += internal_unknown + (tail_executed is None)
        self.stats['notTaken'] += (tail_executed is False)
        self.stats['cycles'] += cycles

    def emit_event(self, seq, etype, offset, payload_value, start, end, flags, text_id):
        payload = bytearray()
        _wv(payload, seq)
        payload.append(etype & 0xFF)
        _wv(payload, offset)
        _wv(payload, payload_value)
        _wv(payload, start)
        _wv(payload, end)
        _wv(payload, flags)
        _wv(payload, text_id)
        record = bytearray([REC_EVENT])
        _wpair(record, bytes(payload))
        self._append(record)
        self.note_seq(seq, seq)
        self.stats['rareEvents'] += 1

    def finish(self):
        self.rotate()


def _encode_optional_int(buffer, value):
    if value is None:
        buffer.append(0)
    else:
        if value < 0:
            raise TraceStoreError('可选整数不能为负')
        buffer.append(1)
        _wv(buffer, value)


def _encode_function(buffer, value, strings):
    if value is None:
        buffer.append(0)
    else:
        buffer.append(1)
        _wv(buffer, strings.id_of(value))


def _encode_previous(buffer, previous, strings):
    if previous is None:
        buffer.append(0)
        return
    buffer.append(1)
    pc, size, flow, executed, function = previous
    _wv(buffer, pc)
    _wv(buffer, size)
    flow_code = FLOW_CODES.get(flow)
    if flow_code is None:
        raise TraceStoreError('未知 flow：%r' % (flow,))
    buffer.append(flow_code)
    buffer.append(EXECUTED_CODES[executed])
    _encode_function(buffer, function, strings)


def _encode_stack(buffer, stack, strings):
    _wv(buffer, len(stack))
    for frame in stack:
        _encode_function(buffer, frame.get('function'), strings)
        flags = 0
        return_pc = frame.get('returnPC')
        origin = frame.get('origin')
        if return_pc is not None:
            flags |= 0x1
        if origin is not None:
            flags |= 0x2
        buffer.append(flags)
        if return_pc is not None:
            _wv(buffer, return_pc)
        if origin is not None:
            _wv(buffer, strings.id_of(origin))


def _encode_tracker(buffer, tracker, strings):
    _wv(buffer, max(0, int(tracker.unknown_restarts)))
    _encode_previous(buffer, tracker.previous, strings)
    _encode_optional_int(buffer, tracker.expected_resume)
    _encode_stack(buffer, tracker.stack, strings)
    _wv(buffer, len(tracker.contexts))
    for stack, previous, expected in tracker.contexts:
        _encode_stack(buffer, stack, strings)
        _encode_previous(buffer, previous, strings)
        _encode_optional_int(buffer, expected)


# ---------------------------------------------------------------------------------------------
# 记录目录读取（两种既有格式）
# ---------------------------------------------------------------------------------------------

def _load_json_strict(path):
    try:
        with open(path, 'rb') as handle:
            raw = handle.read()
    except OSError as error:
        raise TraceStoreError('无法读取 %s：%s' % (path, error))
    try:
        return json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TraceStoreError('%s 不是合法 UTF-8 JSON：%s' % (Path(path).name, error))


def _record_layout(record_dir):
    """判定记录形态：events.jsonl 流式（schema 2/3）或 decoded.json 内联事件。"""
    record_dir = Path(record_dir)
    decoded_path = record_dir / 'decoded.json'
    if not decoded_path.is_file():
        raise TraceStoreError('记录目录缺少 decoded.json：%s' % record_dir)
    decoded = _load_json_strict(decoded_path)
    streamed = decoded.get('streamed') or {}
    schema = streamed.get('schema')
    events_path = record_dir / 'events.jsonl'
    if schema in (2, 3):
        if not events_path.is_file():
            raise TraceStoreError('streamed.schema=%d 但缺少 events.jsonl' % schema)
        source = dict(kind='jsonl', schema=schema, events=streamed.get('events'))
    else:
        events = decoded.get('events')
        if not isinstance(events, list):
            raise TraceStoreError('decoded.json 既不是 streamed schema 2/3，也没有内联 events')
        source = dict(kind='inline', schema=None, events=len(events))
    return decoded, source, events_path


def _iter_source_events(record_dir, decoded, source):
    if source['kind'] == 'jsonl':
        path = Path(record_dir) / 'events.jsonl'
        with path.open(encoding='utf-8') as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    raise TraceStoreError('events.jsonl 第 %d 行为空行，拒绝静默跳过' % number)
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as error:
                    raise TraceStoreError('events.jsonl 第 %d 行不是合法 JSON：%s'
                                          % (number, error))
    else:
        yield from decoded['events']


def _iter_records_jsonl(record_dir, decoded, name):
    path = Path(record_dir) / (name + '.jsonl')
    if path.is_file():
        with path.open(encoding='utf-8') as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)
        return
    values = decoded.get(name)
    if isinstance(values, list):
        yield from values


def _elements_available(record_dir, decoded):
    if (Path(record_dir) / 'elements.jsonl').is_file():
        return True
    return isinstance(decoded.get('elements'), list)


def _iter_ranges(record_dir, decoded):
    """从原生元素流生成范围描述；type 12 周期元素按旧实现折叠到前一个范围末指令。"""
    pending = None
    for element in _iter_records_jsonl(record_dir, decoded, 'elements'):
        if element.get('kind') != 'element':
            raise TraceStoreError('元素流里出现非 element 记录：%r' % element.get('kind'))
        if pending is not None:
            if element.get('type') == 12:
                cycles = int(pending.get('cycleCount') or 0) + int(element.get('cycleCount') or 0)
                yield _range_descriptor(pending, cycles)
                pending = None
                continue
            yield _range_descriptor(pending, int(pending.get('cycleCount') or 0))
            pending = None
        if element.get('type') == 5:
            pending = element
    if pending is not None:
        yield _range_descriptor(pending, int(pending.get('cycleCount') or 0))


def _range_descriptor(element, cycles):
    payload = int(element.get('payload') or 0)
    if payload <= 0:
        raise TraceStoreError('范围元素 payload 非正：%r' % (element,))
    return dict(offset=int(element.get('offset') or 0), start=int(element.get('start') or 0),
                end=int(element.get('end') or 0), payload=payload,
                lastExecuted=bool(element.get('lastExecuted')), cycles=cycles,
                span=payload if cycles else 0)


# ---------------------------------------------------------------------------------------------
# 写入校验
# ---------------------------------------------------------------------------------------------

BINDING_INPUTS = ('etm.bin', 'native-config.txt', 'firmware.elf', 'session.json', 'sources.json',
                  'physical.json', 'framing.json', 'capture.csv', 'capture.bin', 'capture.kvdat',
                  'capture.csv.la.json', 'spi-validation.json', 'phase-validation.json',
                  'native.bin', 'native.log', 'native.jsonl', 'elements.jsonl', 'packets.jsonl',
                  'events.jsonl')


def _binding_files(record_dir):
    folder = Path(record_dir)
    result = {}
    for name in BINDING_INPUTS:
        path = folder / name
        if path.is_file():
            stat = path.stat()
            result[name] = dict(bytes=stat.st_size, sha256=_sha256_file(path))
    for path in sorted(folder.glob('image-*.bin')):
        stat = path.stat()
        result[path.name] = dict(bytes=stat.st_size, sha256=_sha256_file(path))
    return result


def _validate_instruction(event, predicates, strict_predicates=True):
    """首次见到的 PC 做全字段校验，返回 (机器码, 谓词未知标志)。

    ``strict_predicates``：有 ELF 时用 ``predicate_addresses`` 的 IT 集合判定；没有 ELF 时
    按旧记录自身的口径（``executed is None`` ⟺ 谓词结果未被 trace 证实）判定，
    因为它与展开语义完全等价（见 stream 展开规则）。
    """
    keys = set(event)
    if len(event) not in INSTRUCTION_LENGTHS or not INSTRUCTION_REQUIRED <= keys <= INSTRUCTION_ALLOWED:
        raise TraceStoreError('指令事件字段集合不受支持：多出 %s，缺少 %s（seq=%s）'
                              % (sorted(keys - INSTRUCTION_ALLOWED),
                                 sorted(INSTRUCTION_REQUIRED - keys), event.get('seq')))
    if ('cycleCount' in keys) != ('cycleSpan' in keys):
        raise TraceStoreError('cycleCount/cycleSpan 必须成对出现（seq=%s）' % event.get('seq'))
    pc = event['pc']
    if not isinstance(pc, int) or not 0 <= pc <= 0xFFFFFFFF:
        raise TraceStoreError('指令 PC 无效：%r' % (pc,))
    code = event['bytes']
    if not isinstance(code, str) or len(code) % 2 or code != code.lower():
        raise TraceStoreError('指令机器码必须是偶数长度小写十六进制：%r（seq=%s）'
                              % (code, event.get('seq')))
    try:
        raw = bytes.fromhex(code)
    except ValueError:
        raise TraceStoreError('指令机器码非法十六进制：%r' % (code,))
    if not isinstance(event['size'], int) or len(raw) != event['size'] or not 1 <= len(raw) <= 16:
        raise TraceStoreError('指令长度与机器码不符：size=%r len=%d（seq=%s）'
                              % (event.get('size'), len(raw), event.get('seq')))
    flow = event['flow']
    if flow not in FLOW_CODES:
        raise TraceStoreError('未知 flow：%r（seq=%s）' % (flow, event.get('seq')))
    if not isinstance(event['conditional'], bool):
        raise TraceStoreError('conditional 必须是布尔值（seq=%s）' % event.get('seq'))
    executed = event['executed']
    if executed not in (True, False, None):
        raise TraceStoreError('executed 必须是布尔或 null（seq=%s）' % event.get('seq'))
    if strict_predicates:
        predicated = pc in predicates and flow == 'linear'
        if (executed is None) != predicated:
            raise TraceStoreError('谓词未知与 executed 不一致：pc=0x%08x flow=%s executed=%r '
                                  'predicated=%r（seq=%s）' % (pc, flow, executed, predicated,
                                                               event.get('seq')))
    else:
        predicated = executed is None
    if event['executionState'] != STATE_FROM_EXECUTED[executed]:
        raise TraceStoreError('executionState=%r 与 executed=%r 不一致（seq=%s）'
                              % (event['executionState'], executed, event.get('seq')))
    if event['executionReason'] != (IT_REASON if executed is None else ''):
        raise TraceStoreError('executionReason 与旧口径不同（seq=%s）：%r'
                              % (event.get('seq'), event['executionReason']))
    for key in ('mnemonic', 'operands', 'function', 'file'):
        if not isinstance(event[key], str):
            raise TraceStoreError('%s 必须是字符串（seq=%s）' % (key, event.get('seq')))
    line = event['line']
    if line is not None and (not isinstance(line, int) or line < 0):
        raise TraceStoreError('line 必须是非负整数或 null（seq=%s）' % event.get('seq'))
    path = event['callPath']
    if not isinstance(path, list) or not all(isinstance(x, str) for x in path):
        raise TraceStoreError('callPath 必须是字符串数组（seq=%s）' % event.get('seq'))
    if len(path) > MAX_CALL_DEPTH:
        raise TraceStoreError('callPath 深度超过 %d（seq=%s）' % (MAX_CALL_DEPTH, event.get('seq')))
    if 'cycleCount' in event:
        for key in ('cycleCount', 'cycleSpan'):
            value = event[key]
            if not isinstance(value, int) or value < 0:
                raise TraceStoreError('%s 非法（seq=%s）' % (key, event.get('seq')))
        if 'cycles' in event:
            value = event['cycles']
            if not isinstance(value, int) or value < 0:
                raise TraceStoreError('cycles 非法（seq=%s）' % event.get('seq'))
            if value != event['cycleCount']:
                raise TraceStoreError('cycles 与 cycleCount 不同（seq=%s）' % event.get('seq'))
        if event['cycleCount'] == 0 and event['cycleSpan'] != 0:
            raise TraceStoreError('区间周期为 0 但 cycleSpan=%d（seq=%s）'
                                  % (event['cycleSpan'], event.get('seq')))
        if event['cycleSpan'] > MAX_RUN_INSTRUCTIONS:
            raise TraceStoreError('cycleSpan=%d 超过展开段上限（seq=%s）'
                                  % (event['cycleSpan'], event.get('seq')))
    return raw, predicated


def _validate_event(event):
    if set(event) != set(EVENT_FIELDS):
        raise TraceStoreError('边界事件字段集合不受支持：多出 %s，缺少 %s（seq=%s）'
                              % (sorted(set(event) - set(EVENT_FIELDS)),
                                 sorted(set(EVENT_FIELDS) - set(event)), event.get('seq')))
    etype = event['type']
    if etype not in EVENT_TYPES:
        raise TraceStoreError('错误的事件类型：%r（seq=%s）' % (etype, event.get('seq')))
    if not isinstance(event['text'], str):
        raise TraceStoreError('事件 text 必须是字符串（seq=%s）' % event.get('seq'))
    for key in ('offset', 'payload', 'start', 'end', 'flags'):
        value = event[key]
        if not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
            raise TraceStoreError('事件 %s 非法：%r（seq=%s）' % (key, value, event.get('seq')))


# ---------------------------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------------------------

def _ensure_imports():
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)


def write(record_dir, store_dir, *, verify_elements=True, verify_image=True,
          checkpoint_instructions=DEFAULT_CHECKPOINT_INSTRUCTIONS, hash_inputs=True,
          progress=None):
    """把既有记录目录写成 trace-store v1（独立 generation + 最后原子发布 manifest）。

    返回写入摘要字典；失败抛 ``TraceStoreError``，且该 generation 会被清除，
    manifest 不被替换，因此读取端不会看到半成品。
    """
    started = time.time()
    record_dir = Path(record_dir).resolve()
    store_dir = Path(store_dir).resolve()
    if not record_dir.is_dir():
        raise TraceStoreError('记录目录不存在：%s' % record_dir)
    decoded, source, events_file = _record_layout(record_dir)
    if not isinstance(decoded.get('summary'), dict) or not isinstance(decoded.get('quality'), dict):
        raise TraceStoreError('decoded.json 缺少 summary/quality，拒绝生成没有摘要的存储')

    _ensure_imports()
    from flow_analysis import CallTracker, predicate_addresses
    from decode_trace import Image

    image = None
    predicates = set()
    elf_path = record_dir / 'firmware.elf'
    if elf_path.is_file():
        image = Image(elf_path)
        predicates = predicate_addresses(image)
    elif verify_image:
        raise TraceStoreError('记录缺少 firmware.elf，无法交叉校验机器码；'
                              '若确认要跳过请传 verify_image=False')

    strings = _Strings()
    static_by_pc = {}
    static_entries = []
    state = dict(events=0, instructions=0, rareEvents=0, runs=0, executed=0, unknown=0,
                 notTaken=0, firstInstructionOffset=None, minOffset=None, maxOffset=None,
                 firstInstructionSeq=None)
    type_counts = {}
    nav = {mode: [] for mode in NAV_NAMES}
    issues = []
    issue_run = None
    last_seq = None
    expected_seq = 0
    field_variant = None
    exceptions = gaps = 0

    store_dir.mkdir(parents=True, exist_ok=True)
    if store_dir.is_file():
        raise TraceStoreError('store 目录被同名文件占用：%s' % store_dir)
    (store_dir / 'generations').mkdir(exist_ok=True)
    generation_tag = secrets.randbits(63) or 1
    generation = 'g-%s-%s' % (time.strftime('%Y%m%dT%H%M%S'), secrets.token_hex(4))
    generation_dir = store_dir / 'generations' / generation
    try:
        generation_dir.mkdir()
    except FileExistsError:
        raise TraceStoreError('generation 目录已存在：%s' % generation)
    temp_payload = generation_dir / (STORE_FILE + '.payload.tmp')
    sink = _BlockSink(temp_payload)

    def note(message):
        if progress is not None:
            progress(message)

    try:
        sequence = _SequenceBuffer(sink, checkpoint_instructions)
        tracker = CallTracker()
        run_ids = []
        run_start = None
        run_offset = None

        def add_issue(item):
            nonlocal issue_run
            seq = item['seq']
            if item.get('executionState') == 'unknown':
                if issue_run and seq == issue_run['endSeq'] + 1:
                    issue_run['endSeq'] = seq
                    issue_run['records'] += 1
                else:
                    if issue_run:
                        issues.append(issue_run)
                    issue_run = dict(category='predicate', startSeq=seq, endSeq=seq, records=1,
                                     offset=item.get('offset'),
                                     reason=item.get('executionReason')
                                     or '条件执行结果未被当前 trace 证实')
                return
            if issue_run:
                issues.append(issue_run)
                issue_run = None
            if item['kind'] == 'event' and item['type'] in BOUNDARY_TYPES:
                issues.append(dict(category='boundary', startSeq=seq, endSeq=seq,
                                   offset=item.get('offset'),
                                   reason=item.get('text', '边界事件'), records=1))

        def flush_run(tail_executed, cycles, span):
            nonlocal run_ids, run_start, run_offset
            if not run_ids:
                return
            count = len(run_ids)
            # 内部谓词未知数 = 前 count-1 条里 predicated 的条数（与读取端同口径）
            internal_unknown = sum(1 for identifier in run_ids[:-1]
                                   if static_entries[identifier].predicated)
            sequence.emit_run(run_start, count, run_ids, tail_executed, run_offset, cycles, span,
                              internal_unknown)
            state['runs'] += 1
            internal = count - 1
            state['instructions'] += count
            state['executed'] += (internal - internal_unknown) + (tail_executed is True)
            state['unknown'] += internal_unknown + (tail_executed is None)
            state['notTaken'] += (tail_executed is False)
            run_ids = []
            run_start = None
            run_offset = None

        def flush_pending():
            """把挂起的展开段按末指令真实状态落盘：末指令可能是谓词未知的内部指令。"""
            if not run_ids:
                return
            last = static_entries[run_ids[-1]]
            flush_run(None if last.predicated else True, 0, 0)

        def check_checkpoint(next_seq):
            """检查点调度：每 N 条指令或块满时落块 + 写检查点（块首必为检查点）。

            只在“下一条记录存在”时调用，避免产生一个只有检查点的尾块。
            """
            if sequence.since_checkpoint >= sequence.checkpoint_instructions \
                    or sequence.size() >= TARGET_BLOCK_BYTES:
                if run_ids:
                    flush_pending()
                if sequence.size() >= TARGET_BLOCK_BYTES:
                    sequence.rotate()
                sequence.emit_checkpoint(next_seq, tracker, strings)

        # 起点检查点：seq=0 处的调用状态（空栈）必须显式保存
        sequence.emit_checkpoint(0, tracker, strings)

        ranges = _iter_ranges(record_dir, decoded) if \
            (verify_elements and _elements_available(record_dir, decoded)) else None
        current_range = next(ranges, None) if ranges is not None else None
        range_remaining = 0
        range_address = 0
        ranges_seen = 0
        ranges_verified = 0
        declared_ranges = (decoded.get('summary') or {}).get('ranges')

        for event in _iter_source_events(record_dir, decoded, source):
            seq = event.get('seq')
            if not isinstance(seq, int) or seq < 0:
                raise TraceStoreError('事件 seq 无效：%r' % (seq,))
            if seq != expected_seq:
                raise TraceStoreError('seq 不连续：期望 %d，实际 %d（本实现要求逻辑 seq 从 0 连续）'
                                      % (expected_seq, seq))
            expected_seq += 1
            last_seq = seq
            state['events'] += 1
            check_checkpoint(seq)
            kind = event.get('kind')
            if kind == 'instruction':
                pc = event['pc']
                entry = static_by_pc.get(pc)
                if entry is None:
                    raw, predicated = _validate_instruction(event, predicates,
                                                            image is not None)
                    if image is not None:
                        expected_bytes = image.read(pc, len(raw))
                        if expected_bytes.hex() != raw.hex():
                            raise TraceStoreError('机器码与 ELF 不一致：pc=0x%08x 事件=%s 镜像=%s'
                                                  % (pc, raw.hex(), expected_bytes.hex()))
                        location = image.location(pc)
                        if location != (event['function'], event['file'], event['line']):
                            raise TraceStoreError('源码位置与 ELF 不一致：pc=0x%08x 事件=%r 镜像=%r'
                                                  % (pc, (event['function'], event['file'],
                                                          event['line']), location))
                    entry = _StaticEntry(pc, len(raw), raw, event['bytes'], event['mnemonic'],
                                         event['operands'], event['function'], event['file'],
                                         event['line'], event['flow'], event['conditional'],
                                         predicated, len(static_entries))
                    static_by_pc[pc] = entry
                    static_entries.append(entry)
                else:
                    if entry.event_checks(event) != entry.checks:
                        raise TraceStoreError('同一 PC 的静态字段不一致：pc=0x%08x（seq=%s）；'
                                              '静态字典不允许歧义' % (pc, seq))
                    if (event['executed'] is None) != entry.predicated \
                            or event['executionState'] != STATE_FROM_EXECUTED[event['executed']] \
                            or event['executionReason'] != \
                            (IT_REASON if event['executed'] is None else ''):
                        raise TraceStoreError('执行状态与静态谓词不一致：pc=0x%08x（seq=%s）'
                                              % (pc, seq))
                predicated = entry.predicated
                if field_variant is None:
                    field_variant = tuple(event)
                    if len(field_variant) not in INSTRUCTION_LENGTHS:
                        raise TraceStoreError('指令字段组合不受支持：%s' % (field_variant,))
                elif len(event) != len(field_variant) \
                        or ('cycles' in event) != ('cycles' in field_variant) \
                        or ('cycleCount' in event) != ('cycleCount' in field_variant):
                    raise TraceStoreError('同一记录内指令事件字段不一致（seq=%s）' % seq)
                if state['firstInstructionSeq'] is None:
                    state['firstInstructionSeq'] = seq
                    state['firstInstructionOffset'] = event['offset']
                if state['minOffset'] is None or event['offset'] < state['minOffset']:
                    state['minOffset'] = event['offset']
                if state['maxOffset'] is None or event['offset'] > state['maxOffset']:
                    state['maxOffset'] = event['offset']
                # 原生范围交叉校验
                if ranges is not None:
                    if range_remaining == 0:
                        if current_range is None:
                            raise TraceStoreError('元素流的范围不足：事件仍是指令（seq=%s）' % seq)
                        range_remaining = current_range['payload']
                        range_address = current_range['start']
                    if event['offset'] != current_range['offset']:
                        raise TraceStoreError('指令 offset 与原生范围不符：seq=%s 事件=%d 范围=%d'
                                              % (seq, event['offset'], current_range['offset']))
                    if pc != range_address:
                        raise TraceStoreError('范围地址不连续：seq=%s pc=0x%08x 期望 0x%08x'
                                              % (seq, pc, range_address))
                    if pc + entry.size > current_range['end']:
                        raise TraceStoreError('指令越过范围末端：seq=%s' % seq)
                    range_address += entry.size
                    range_remaining -= 1
                    if range_remaining == 0:
                        if range_address != current_range['end']:
                            raise TraceStoreError('范围末端与反汇编不一致：0x%08x≠0x%08x'
                                                  % (range_address, current_range['end']))
                        tail = current_range['lastExecuted']
                        if event['executed'] is not None and bool(event['executed']) != tail:
                            raise TraceStoreError('末指令与元素 lastExecuted 不符：seq=%s' % seq)
                        if 'cycleCount' in event:
                            if event['cycleCount'] != current_range['cycles'] \
                                    or event['cycleSpan'] != current_range['span']:
                                raise TraceStoreError(
                                    '区间周期与元素不符：seq=%s 事件=(%d,%d) 元素=(%d,%d)'
                                    % (seq, event['cycleCount'], event['cycleSpan'],
                                       current_range['cycles'], current_range['span']))
                        ranges_verified += 1
                        current_range = next(ranges, None)
                # 展开段累积：末指令必须是段的最后一条；内部指令要求“可内部化”。
                # 非内部化指令作为当前段的末指令落盘（不再单独切一个一段区间）。
                cycles = int(event.get('cycleCount') or 0)
                span = int(event.get('cycleSpan') or 0)
                internal_ok = (event['executed'] == (None if predicated else True))
                if 'cycleCount' in event:
                    internal_ok = internal_ok and cycles == 0 and span == 0
                if run_ids and (event['offset'] != run_offset
                                or len(run_ids) >= MAX_RUN_INSTRUCTIONS):
                    flush_pending()
                if not run_ids:
                    run_start = seq
                    run_offset = event['offset']
                run_ids.append(entry.identifier)
                if not internal_ok:
                    flush_run(event['executed'], cycles, span)
                tracker.feed_instruction(event['function'], event['file'], event['line'],
                                        event['executed'], event['flow'], pc, entry.size, cycles)
                if event['flow'] == 'call' and event['executed'] is True:
                    nav[NAV_CALL].append(seq)
                if event['flow'] == 'return' and event['executed'] is True:
                    nav[NAV_RETURN].append(seq)
                if event['executed'] is None:
                    nav[NAV_ISSUE].append(seq)
                add_issue(event)
                sequence.since_checkpoint += 1
            elif kind == 'event':
                _validate_event(event)
                if run_ids:                       # 序列记录必须按 seq 递增落盘
                    flush_pending()
                etype = event['type']
                type_counts[etype] = type_counts.get(etype, 0) + 1
                if etype == 9:
                    exceptions += 1
                    sequence.stats['exceptions'] += 1
                if etype in GAP_TYPES or (etype == 2 and event['payload']):
                    gaps += 1
                if etype in BOUNDARY_TYPES:
                    sequence.stats['boundaries'] += 1
                    nav[NAV_ISSUE].append(seq)
                if etype in (9, 10):
                    nav[NAV_EXCEPTION].append(seq)
                nav[NAV_EVENT].append(seq)
                sequence.emit_event(seq, etype, event['offset'], event['payload'],
                                    event['start'], event['end'], event['flags'],
                                    strings.id_of(event['text']))
                add_issue(event)
                state['rareEvents'] += 1
                tracker.feed_event(etype, event.get('flags', 0), event.get('payload'),
                                   event.get('end'))
            else:
                raise TraceStoreError('未知事件 kind：%r（seq=%s）' % (kind, seq))
            if state['events'] % 250000 == 0:
                note('  写入：已处理 %d 条逻辑事件（指令 %d，展开段 %d）'
                     % (state['events'], state['instructions'], state['runs']))

        if run_ids:
            flush_pending()
        if ranges is not None:
            leftover = current_range
            if leftover is not None or range_remaining:
                raise TraceStoreError('元素流仍有未消费的范围；事件流与元素流不一致')
        if issue_run:
            issues.append(issue_run)
        if not state['instructions'] or field_variant is None:
            raise TraceStoreError('记录没有指令事件；拒绝生成空存储')
        if ranges is not None and declared_ranges is not None \
                and int(declared_ranges) != ranges_verified:
            raise TraceStoreError('原生范围数与 summary.ranges 不符：元素=%d，摘要=%d'
                                  % (ranges_verified, declared_ranges))
        sequence.finish()

        # ---- 与旧摘要逐项对账 -----------------------------------------------------------------
        declared = decoded.get('summary') or {}
        quality = decoded.get('quality') or {}
        streamed = decoded.get('streamed') or {}
        expected_events = streamed.get('events', source['events'])
        if int(expected_events) != state['events']:
            raise TraceStoreError('事件条数与记录声明不符：实际 %d，声明 %s'
                                  % (state['events'], expected_events))
        if int(declared.get('instructions', -1)) != state['instructions']:
            raise TraceStoreError('指令条数与 summary.instructions 不符：%d≠%s'
                                  % (state['instructions'], declared.get('instructions')))
        if int(declared.get('executed', -1)) != state['executed']:
            raise TraceStoreError('执行指令数与 summary.executed 不符：%d≠%s'
                                  % (state['executed'], declared.get('executed')))
        if 'unknownExecution' in declared and \
                int(declared['unknownExecution']) != state['unknown']:
            raise TraceStoreError('未知执行数与 summary.unknownExecution 不符：%d≠%s'
                                  % (state['unknown'], declared['unknownExecution']))
        if int(declared.get('exceptions', -1)) != exceptions:
            raise TraceStoreError('异常事件数与 summary.exceptions 不符：%d≠%s'
                                  % (exceptions, declared.get('exceptions')))
        if int(declared.get('gaps', -1)) != gaps:
            raise TraceStoreError('缺口事件数与 summary.gaps 不符：%d≠%s'
                                  % (gaps, declared.get('gaps')))
        if quality.get('unknownExecution') is not None \
                and int(quality['unknownExecution']) != state['unknown']:
            raise TraceStoreError('quality.unknownExecution 与事件不符')
        if quality.get('firstPathOffset') is not None \
                and int(quality['firstPathOffset']) != state['firstInstructionOffset']:
            raise TraceStoreError('quality.firstPathOffset 与首指令 offset 不符：%s≠%s'
                                  % (quality['firstPathOffset'], state['firstInstructionOffset']))

        # ---- 静态字典与字符串表落块（静态条目会继续 intern 助记符等字符串）------------------
        static_buffer = bytearray()
        static_count = 0
        for entry in static_entries:
            _wv(static_buffer, entry.pc)
            static_buffer.append(entry.size)
            flags = 0
            if entry.predicated:
                flags |= 0x01
            if entry.conditional:
                flags |= 0x02
            if entry.function is not None:
                flags |= 0x04
            if entry.file is not None:
                flags |= 0x08
            if entry.line is not None:
                flags |= 0x10
            static_buffer.append(flags)
            static_buffer.append(FLOW_CODES[entry.flow])
            static_buffer += entry.bytes
            _wv(static_buffer, strings.id_of(entry.mnemonic))
            _wv(static_buffer, strings.id_of(entry.operands))
            if entry.function is not None:
                _wv(static_buffer, strings.id_of(entry.function))
            if entry.file is not None:
                _wv(static_buffer, strings.id_of(entry.file))
            if entry.line is not None:
                _wv(static_buffer, entry.line)
            static_count += 1
            if len(static_buffer) >= TARGET_BLOCK_BYTES:
                sink.add(BLK_STATIC, bytes(static_buffer), static_count)
                static_buffer = bytearray()
                static_count = 0
        if static_buffer:
            sink.add(BLK_STATIC, bytes(static_buffer), static_count)

        string_buffer = bytearray()
        string_count = 0
        for value in strings.values:
            encoded = value.encode('utf-8')
            if len(encoded) > MAX_STRING_BYTES:
                raise TraceStoreError('字符串过长：%d 字节' % len(encoded))
            _wpair(string_buffer, encoded)
            string_count += 1
            if len(string_buffer) >= TARGET_BLOCK_BYTES:
                sink.add(BLK_STRINGS, bytes(string_buffer), string_count)
                string_buffer = bytearray()
                string_count = 0
        if string_buffer:
            sink.add(BLK_STRINGS, bytes(string_buffer), string_count)

        # ---- 包记录 ---------------------------------------------------------------------------
        packet_count = 0
        packet_buffer = bytearray()
        packets_in_block = 0
        previous_packet_offset = None
        for packet in _iter_records_jsonl(record_dir, decoded, 'packets'):
            offset = int(packet.get('offset') or 0)
            size = int(packet.get('size') or 0)
            hexed = packet.get('hex') or ''
            text = packet.get('text')
            if not isinstance(text, str):
                raise TraceStoreError('包记录 text 必须是字符串')
            try:
                raw = bytes.fromhex(hexed)
            except ValueError:
                raise TraceStoreError('包记录 hex 非法：%r' % (hexed,))
            if len(raw) != size:
                raise TraceStoreError('包 hex 长度与 size 不符：%d≠%d' % (len(raw), size))
            if previous_packet_offset is not None and offset < previous_packet_offset:
                raise TraceStoreError('包偏移倒序，拒绝伪造字节对账')
            _wv(packet_buffer, offset - (previous_packet_offset or 0))
            _wv(packet_buffer, size)
            _wpair(packet_buffer, raw)
            _wpair(packet_buffer, text.encode('utf-8'))
            previous_packet_offset = offset
            packet_count += 1
            packets_in_block += 1
            if len(packet_buffer) >= TARGET_BLOCK_BYTES:
                sink.add(BLK_PACKETS, bytes(packet_buffer), packets_in_block)
                packet_buffer = bytearray()
                packets_in_block = 0
        if packet_buffer:
            sink.add(BLK_PACKETS, bytes(packet_buffer), packets_in_block)
        if streamed.get('packets') is not None and int(streamed['packets']) != packet_count:
            raise TraceStoreError('包条数与记录声明不符：实际 %d，声明 %s'
                                  % (packet_count, streamed.get('packets')))

        # ---- 导航索引 -------------------------------------------------------------------------
        for mode, values in nav.items():
            if not values:
                continue
            if any(a >= b for a, b in zip(values, values[1:])):
                raise TraceStoreError('导航索引未严格递增（mode=%d）' % mode)
            for start in range(0, len(values), 65536):
                part = values[start:start + 65536]
                payload = bytearray([mode])
                _wv(payload, len(part))
                _wv(payload, part[0])
                previous = part[0]
                for value in part[1:]:
                    _wv(payload, value - previous)
                    previous = value
                sink.add(BLK_NAV, bytes(payload), len(part))

        # ---- 检查点索引 -----------------------------------------------------------------------
        checkpoint_buffer = bytearray()
        previous_checkpoint_seq = 0
        for seq, block_index, record_offset in sequence.checkpoints:
            _wv(checkpoint_buffer, seq - previous_checkpoint_seq)
            _wv(checkpoint_buffer, block_index)
            _wv(checkpoint_buffer, record_offset)
            previous_checkpoint_seq = seq
        sink.add(BLK_CHECKPOINTS, bytes(checkpoint_buffer), len(sequence.checkpoints))

        # ---- 块级聚合（分层摘要/稀疏索引的第二层）---------------------------------------------
        agg_buffer = bytearray()
        for aggregate in sequence.agg:
            for key in AGG_KEYS:
                _wv(agg_buffer, int(aggregate.get(key, 0)))
        sink.add(BLK_AGG, bytes(agg_buffer), len(sequence.agg))

        # ---- JSON 摘要块 -----------------------------------------------------------------------
        sources_data = decoded.get('sources')
        if not isinstance(sources_data, dict):
            source_path = record_dir / 'sources.json'
            sources_data = _load_json_strict(source_path) if source_path.is_file() else {}
        if not all(isinstance(key, str) and isinstance(value, str)
                   for key, value in sources_data.items()):
            raise TraceStoreError('sources 必须是 文件→源码文本 的字符串映射')

        summary = {key: value for key, value in decoded.items()
                   if key not in ('events', 'elements', 'packets', 'sources')}
        summary['eventCount'] = state['events']
        summary['packetCount'] = packet_count
        summary['sourceFiles'] = list(sources_data)
        summary['firstSeq'] = state['firstInstructionSeq']
        summary['traceStore'] = dict(format=FULL_FORMAT_NAME, generation=generation,
                                     runs=state['runs'], instructions=state['instructions'])

        issues.extend(_packet_issues(record_dir, decoded))

        meta = dict(
            format=FORMAT_NAME, formatName=FULL_FORMAT_NAME, schemaVersion=SCHEMA_VERSION,
            byteOrder='little',
            record=dict(name=record_dir.name,
                        eventsSource=('events.jsonl' if source['kind'] == 'jsonl'
                                      else 'decoded.json:events'),
                        streamedSchema=source['schema'],
                        decodedVersion=decoded.get('version'),
                        protocol=decoded.get('protocol')),
            counts=dict(events=state['events'], instructions=state['instructions'],
                        ranges=ranges_verified if ranges is not None
                        else int(declared.get('ranges') or state['runs']),
                        declaredRanges=declared.get('ranges'), runs=state['runs'],
                        rareEvents=state['rareEvents'], staticPcs=len(static_entries),
                        strings=len(strings.values), packets=packet_count,
                        sequenceBlocks=len(sequence.agg), checkpoints=len(sequence.checkpoints),
                        executed=state['executed'], unknownExecution=state['unknown'],
                        notTaken=state['notTaken'], exceptions=exceptions, gaps=gaps),
            eventTypes={str(key): value for key, value in sorted(type_counts.items())},
            seq=dict(first=0, last=last_seq, contiguous=True, count=state['events'],
                     firstInstruction=state['firstInstructionSeq'],
                     offsetRange=[state['minOffset'], state['maxOffset']]),
            fieldPresence=dict(instruction=list(field_variant), event=list(EVENT_FIELDS)),
            hasCycleFields='cycleCount' in field_variant,
            verify=dict(elements=ranges is not None, elementsVerified=ranges_verified,
                        image=image is not None, staticPcs=len(static_entries),
                        machineCodeCrossCheck=image is not None),
            layout=dict(headerBytes=HEADER_SIZE, indexEntryBytes=INDEX_ENTRY_SIZE,
                        targetBlockBytes=TARGET_BLOCK_BYTES, maxBlockBytes=MAX_BLOCK_BYTES,
                        runInstructionLimit=MAX_RUN_INSTRUCTIONS,
                        checkpointInstructions=sequence.checkpoint_instructions),
            created=dict(unix=time.time(), iso=time.strftime('%Y-%m-%dT%H:%M:%S%z')),
            sourceSha256=dict(decoded=_sha256_file(record_dir / 'decoded.json')),
            issues=len(issues),
        )
        sink.add_compressed_json(BLK_META, meta)
        sink.add_compressed_json(BLK_SUMMARY, summary)
        sink.add_compressed_json(BLK_SOURCES, sources_data)
        sink.add_compressed_json(BLK_ISSUES, issues)

        sink.handle.flush()
        info = sink.assemble(generation_dir / STORE_FILE, generation_tag)
        sink.close()
        try:
            temp_payload.unlink()
        except OSError:
            pass

        # ---- manifest（最后原子发布）-----------------------------------------------------------
        inputs = _binding_files(record_dir) if hash_inputs else {}
        if not hash_inputs:
            inputs = {name: dict(bytes=(record_dir / name).stat().st_size, sha256=None)
                      for name in BINDING_INPUTS if (record_dir / name).is_file()}
        bindings = dict(inputs=inputs)
        bindings['record'] = dict(
            eventsSource=meta['record']['eventsSource'], streamedSchema=source['schema'],
            decodedSha256=meta['sourceSha256']['decoded'],
            eventsSha256=inputs.get('events.jsonl', {}).get('sha256'),
            eventsBytes=inputs.get('events.jsonl', {}).get('bytes'),
        )
        bindings['config'] = decoded.get('config')
        bindings['processing'] = dict(
            traceStore=SCHEMA_VERSION, processor='trace_store.py reference writer',
            decodedVersion=decoded.get('version'),
            decoderLibrary=(decoded.get('status') or {}).get('library'),
            protocol=decoded.get('protocol'),
        )
        manifest = dict(
            format=FORMAT_NAME, formatName=FULL_FORMAT_NAME, schemaVersion=SCHEMA_VERSION,
            byteOrder='little', state='complete',
            generation=generation, generationTag='0x%016x' % generation_tag,
            createdAt=time.strftime('%Y-%m-%dT%H:%M:%S%z'), createdUnix=time.time(),
            files=[dict(name=STORE_FILE, bytes=info['bytes'], sha256=info['sha256'],
                        blocks=info['blocks'], payloadBytes=info['payloadBytes'])],
            counts=meta['counts'], fieldPresence=meta['fieldPresence'], bindings=bindings,
            checksums=dict(algorithm='sha256', block='crc32', verifiedAtOpen=['size', 'sha256']),
            notes=['manifest 是唯一发布点：失败/中止的 generation 目录不会被本 manifest 引用',
                   '旧 generation 保留不删，读取只认 manifest 指向的一代'],
        )
        _publish_manifest(store_dir, manifest)

        elapsed = time.time() - started
        result = dict(ok=True, storeDir=str(store_dir), generation=generation,
                      storeBytes=info['bytes'], storeSha256=info['sha256'],
                      elapsedSeconds=round(elapsed, 3), counts=meta['counts'],
                      peakRssBytes=peak_rss_bytes(), issues=len(issues),
                      elementsVerified=ranges_verified)
        note('  完成：%s（%.1f s，%.1f MiB）' % (store_dir, elapsed, info['bytes'] / 1048576))
        return result
    except Exception:
        sink.close()
        _remove_tree(generation_dir)
        raise


def _packet_issues(record_dir, decoded):
    """与 record_store._append_packet_issues 同口径的包字节账/解码器/物理边界问题。"""
    issues = []
    quality = decoded.get('quality') or {}
    streamed = decoded.get('streamed') or {}
    packet_end = 0
    for packet in _iter_records_jsonl(record_dir, decoded, 'packets'):
        start = int(packet.get('offset') or 0)
        size = int(packet.get('size') or 0)
        stop = start + size
        text = packet.get('text') or ''
        if start > packet_end:
            issues.append(dict(category='gap', startSeq=None, endSeq=None, records=0,
                               offset=packet_end, bytes=start - packet_end,
                               reason='ETM 包字节覆盖缺口；不能据此补造指令'))
        if start < packet_end:
            issues.append(dict(category='overlap', startSeq=None, endSeq=None, records=0,
                               offset=start, bytes=max(0, min(stop, packet_end) - start),
                               reason='ETM 包字节覆盖重叠'))
        packet_end = max(packet_end, stop)
        upper = text.upper()
        if any(term in upper for term in ('OVERFLOW', 'BAD_', 'RESERVED', 'INCOMPLETE_EOT')):
            issues.append(dict(category='packet', startSeq=None, endSeq=None, records=0,
                               offset=start, bytes=size, reason='包问题：' + text))
    input_size = quality.get('inputBytes', quality.get('processedBytes', 0))
    if streamed.get('schema') != 3 and input_size and input_size > packet_end:
        issues.append(dict(category='gap', startSeq=None, endSeq=None, records=0,
                           offset=packet_end, bytes=input_size - packet_end,
                           reason='ETM 输入尾部未被包记录覆盖'))
    status = decoded.get('status') or {}
    if status.get('lastErrorCode'):
        issues.append(dict(category='decoder', startSeq=None, endSeq=None, records=0,
                           offset=status.get('processed'),
                           reason='解码器错误：' + str(status.get('lastError') or '未知错误')))
    for key, label in (('initialPhysicalBytes', 'TPIU 同步前物理字节'),
                       ('trailingFrameBytes', '不完整的 TPIU 尾帧'),
                       ('unknownSourceBytes', '未知源 ID 字节')):
        if quality.get(key):
            issues.append(dict(category='physical', startSeq=None, endSeq=None, records=0,
                               bytes=quality[key], reason=label))
    return issues


def _remove_tree(path):
    try:
        for child in sorted(Path(path).rglob('*'), reverse=True):
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        Path(path).rmdir()
    except OSError:
        pass


def _publish_manifest(store_dir, manifest):
    manifest_path = Path(store_dir) / MANIFEST_NAME
    temporary = Path(store_dir) / ('.manifest-%s.tmp' % secrets.token_hex(4))
    payload = _json_bytes(manifest) + b'\n'
    try:
        with open(temporary, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, manifest_path)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------------------------
# 读取器
# ---------------------------------------------------------------------------------------------

class _Static:
    __slots__ = ('pc', 'size', 'bytes', 'mnemonic', 'operands', 'function', 'file', 'line',
                 'flow', 'conditional', 'predicated')

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class _ManifestVerifier(threading.Thread):
    """后台整份 sha256 校验（`verify='full'`）：与块加载/索引解析重叠，不降低完整性。

    manifest 声明的每个文件仍**整份**计算 sha256 并与声明比对（不抽样、不按体积放宽）；
    只是把它从“阻塞在打开前”改为“与后续块加载并行”，打开返回前 join 并抛出同样的错误。
    """

    def __init__(self, generation_dir, entries):
        super().__init__(daemon=True, name='trace-store-verify')
        self.generation_dir = Path(generation_dir)
        self.entries = list(entries)
        self.error = None

    def run(self):
        try:
            for entry in self.entries:
                path = self.generation_dir / str(entry.get('name'))
                if _sha256_file(path) != entry.get('sha256'):
                    self.error = TraceStoreError(
                        '文件 sha256 与 manifest 不符：%s（输入或存储被改动）'
                        % entry.get('name'))
                    return
        except BaseException as error:          # noqa: BLE001 - 打开线程内错误需在主线程重抛
            self.error = error


class TraceStore:
    """trace-store v1 只读视图：按页/窗口/任意 seq 无损展开旧事件。"""

    def __init__(self, folder, verify='full', verify_crc=True):
        self.folder = Path(folder).resolve()
        # R1 查询索引（可选块 12）：缺失/损坏时为 None，window/timeline 走原慢路径。
        self.query_index = None
        self.query_index_error = None
        manifest_path = self.folder / MANIFEST_NAME
        if not manifest_path.is_file():
            raise TraceStoreError('不是已发布的 trace-store：缺少 %s（可能只写入了未完成的 '
                                  'generation，拒绝当成完整记录）' % MANIFEST_NAME)
        manifest = _load_json_strict(manifest_path)
        if manifest.get('format') != FORMAT_NAME:
            raise TraceStoreError('manifest.format=%r 不是 trace-store' % manifest.get('format'))
        if manifest.get('schemaVersion') != SCHEMA_VERSION:
            raise TraceStoreError('manifest.schemaVersion=%r 不受支持'
                                  % manifest.get('schemaVersion'))
        if manifest.get('state') != 'complete':
            raise TraceStoreError('manifest.state=%r：记录未完成发布' % manifest.get('state'))
        generation = manifest.get('generation')
        if not isinstance(generation, str) or '/' in generation or '\\' in generation:
            raise TraceStoreError('manifest.generation 非法：%r' % (generation,))
        generation_dir = self.folder / 'generations' / generation
        if not generation_dir.is_dir():
            raise TraceStoreError('manifest 指向的 generation 目录不存在：%s' % generation)
        files = manifest.get('files')
        if not isinstance(files, list) or not files:
            raise TraceStoreError('manifest.files 为空')
        if verify not in ('full', 'sizes', 'none'):
            raise TraceStoreError('未知校验模式：%r' % (verify,))
        for entry in files:
            name = entry.get('name')
            if not isinstance(name, str) or '/' in name or '\\' in name:
                raise TraceStoreError('manifest.files 名称非法：%r' % (name,))
            path = generation_dir / name
            if not path.is_file():
                raise TraceStoreError('manifest 声明的文件缺失：%s' % name)
            stat = path.stat()
            if stat.st_size != entry.get('bytes'):
                raise TraceStoreError('文件长度与 manifest 不符：%s（%d≠%s）'
                                      % (name, stat.st_size, entry.get('bytes')))
        verifier = None
        if verify == 'full' and eager_load_mode():
            for entry in files:
                if _sha256_file(generation_dir / str(entry.get('name'))) \
                        != entry.get('sha256'):
                    raise TraceStoreError('文件 sha256 与 manifest 不符：%s（输入或存储被改动）'
                                          % entry.get('name'))
        elif verify == 'full':
            # 整份 sha256 仍校验，只是与块加载/索引解析并行；返回前 join 并抛出同样的错误。
            verifier = _ManifestVerifier(generation_dir, files)
            verifier.start()
        tag = manifest.get('generationTag')
        try:
            generation_tag = int(str(tag), 16) if str(tag).lower().startswith('0x') else int(tag)
        except (TypeError, ValueError):
            raise TraceStoreError('manifest.generationTag 非法：%r' % (tag,))
        self.manifest = manifest
        self.generation = generation
        self.container = _Container(generation_dir / STORE_FILE, FILE_TRACE, generation_tag,
                                    verify_crc=verify_crc)
        try:
            self.meta = self._json_block(BLK_META, required=True)
            self.summary = self._json_block(BLK_SUMMARY, required=True)
            self.sources = self._json_block(BLK_SOURCES, required=True)
            # 块 10（issues）与块 5（nav）惰性解析：打开时不付 json/varint 解析成本；
            # 首次访问时才解析（块首次读取仍做 CRC 校验，manifest sha256 已在上面整份校验）。
            self._issues = None
            self._extra_issues = []
            self._nav = None
            if self.meta.get('schemaVersion') != SCHEMA_VERSION:
                raise TraceStoreError('meta.schemaVersion 不受支持')
            self.field_presence = self.meta.get('fieldPresence') or {}
            instruction_fields = tuple(self.field_presence.get('instruction') or ())
            keys = set(instruction_fields)
            if len(instruction_fields) not in INSTRUCTION_LENGTHS \
                    or not INSTRUCTION_REQUIRED <= keys <= INSTRUCTION_ALLOWED \
                    or ('cycleCount' in keys) != ('cycleSpan' in keys):
                raise TraceStoreError('meta.fieldPresence.instruction 不受支持：%s'
                                      % (list(instruction_fields),))
            self.instruction_fields = instruction_fields
            self.has_cycle_fields = 'cycleCount' in instruction_fields
            event_fields = tuple(self.field_presence.get('event') or ())
            if event_fields != EVENT_FIELDS:
                raise TraceStoreError('meta.fieldPresence.event 不受支持')
            self._load_strings()
            self._load_static()
            self._load_aggregates()
            self._load_packets_index()
            counts = self.meta.get('counts') or {}
            self.counts = counts
            seq = self.meta.get('seq') or {}
            self.first_seq = int(seq.get('first') or 0)
            self.last_seq = int(seq.get('last') or -1)
            if not self.block_spans:
                raise TraceStoreError('存储没有序列块')
            if self.block_spans[0]['firstSeq'] != self.first_seq \
                    or self.block_spans[-1]['lastSeq'] != self.last_seq:
                raise TraceStoreError('块级 seq 范围与 meta 声明不符')
            if int(counts.get('events') or 0) != self.last_seq - self.first_seq + 1:
                raise TraceStoreError('seq 有效范围与事件条数不符')
            self._load_checkpoints()
            self._load_query_index()
            if eager_load_mode():
                # 旧行为：打开时即解析 nav 与 issues（含其结构校验）。
                _ = self.nav
                _ = self.issues_list
            if verifier is not None:
                verifier.join()
                if verifier.error is not None:
                    raise verifier.error
        except Exception:
            self.container.close()
            raise

    # -- 惰性块（nav / issues）---------------------------------------------------------------
    @property
    def nav(self):
        """稀疏导航 seq 表（块 5）：首次访问时解析（含严格递增/范围校验）。"""
        if self._nav is None:
            self._load_nav()
        return self._nav

    @nav.setter
    def nav(self, value):
        self._nav = value

    @property
    def issues_list(self):
        """issues（块 10）：首次访问时解析；打开期间产生的 query-index issue 一并合并。"""
        if self._issues is None:
            loaded = self._json_block(BLK_ISSUES, required=False, default=[])
            self._issues = list(loaded) + list(self._extra_issues)
            self._extra_issues = []
        elif self._extra_issues:
            self._issues.extend(self._extra_issues)
            self._extra_issues = []
        return self._issues

    @issues_list.setter
    def issues_list(self, value):
        self._issues = value

    # -- 基础加载 ----------------------------------------------------------------------------
    def _json_block(self, block_type, required, default=None):
        indexes = self.container.of_type(block_type)
        if not indexes:
            if required:
                raise TraceStoreError('缺少必需块：%s' % BLOCK_NAMES.get(block_type))
            return default
        if len(indexes) != 1:
            raise TraceStoreError('块 %s 重复' % BLOCK_NAMES.get(block_type))
        try:
            return json.loads(self.container.block(indexes[0]).decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TraceStoreError('%s 块不是合法 JSON：%s' % (BLOCK_NAMES.get(block_type), error))

    def _load_strings(self):
        self.strings = []
        for index in self.container.of_type(BLK_STRINGS):
            payload = self.container.block(index)
            position = 0
            while position < len(payload):
                raw, position = _dpair(payload, position)
                try:
                    self.strings.append(raw.decode('utf-8'))
                except UnicodeDecodeError:
                    raise TraceStoreError('字符串表不是合法 UTF-8')
        if not self.strings:
            raise TraceStoreError('字符串表为空')

    def _string(self, identifier):
        if not 0 <= identifier < len(self.strings):
            raise TraceStoreError('字符串 ID 越界：%d' % identifier)
        return self.strings[identifier]

    def _load_static(self):
        self.static = []
        for index in self.container.of_type(BLK_STATIC):
            payload = self.container.block(index)
            position = 0
            while position < len(payload):
                pc, position = _dvar(payload, position)
                if position + 3 > len(payload):
                    raise TraceStoreError('静态字典记录被截断')
                size = payload[position]
                flags = payload[position + 1]
                flow_code = payload[position + 2]
                position += 3
                if not 1 <= size <= 16:
                    raise TraceStoreError('静态字典指令长度无效：%d' % size)
                if flags & ~0x1F:
                    raise TraceStoreError('静态字典标志位未知：0x%02x' % flags)
                flow = FLOW_NAMES.get(flow_code)
                if flow is None:
                    raise TraceStoreError('静态字典 flow 未知：%d' % flow_code)
                if position + size > len(payload):
                    raise TraceStoreError('静态字典机器码越界')
                code = bytes(payload[position:position + size])
                position += size
                mnemonic, position = _dvar(payload, position)
                operands, position = _dvar(payload, position)
                function = file = line = None
                if flags & 0x04:
                    function, position = _dvar(payload, position)
                if flags & 0x08:
                    file, position = _dvar(payload, position)
                if flags & 0x10:
                    line, position = _dvar(payload, position)
                self.static.append(_Static(
                    pc=pc, size=size, bytes=code, mnemonic=self._string(mnemonic),
                    operands=self._string(operands),
                    function=None if function is None else self._string(function),
                    file=None if file is None else self._string(file),
                    line=line, flow=flow, conditional=bool(flags & 0x02),
                    predicated=bool(flags & 0x01)))
        if not self.static:
            raise TraceStoreError('静态字典为空')
        self.static_by_pc = {entry.pc: number for number, entry in enumerate(self.static)}
        if len(self.static_by_pc) != len(self.static):
            raise TraceStoreError('静态字典存在重复 PC')
        declared = (self.meta.get('counts') or {}).get('staticPcs')
        if declared is not None and int(declared) != len(self.static):
            raise TraceStoreError('静态字典数量与 meta 声明不符：%d≠%s'
                                  % (len(self.static), declared))

    def _load_aggregates(self):
        indexes = self.container.of_type(BLK_AGG)
        if not indexes:
            raise TraceStoreError('缺少块级聚合（稀疏索引第二层）')
        payload = self.container.block(indexes[0])
        position = 0
        self.block_spans = []
        while position < len(payload):
            values = []
            for _ in range(len(AGG_KEYS)):
                value, position = _dvar(payload, position)
                values.append(value)
            self.block_spans.append(dict(zip(AGG_KEYS, values)))
        self.sequence_blocks = self.container.of_type(BLK_SEQUENCE)
        if len(self.block_spans) != len(self.sequence_blocks):
            raise TraceStoreError('聚合并未覆盖全部序列块')
        self._block_first = [entry['firstSeq'] for entry in self.block_spans]
        for previous, entry in zip(self.block_spans, self.block_spans[1:]):
            if entry['firstSeq'] != previous['lastSeq'] + 1:
                raise TraceStoreError('块级 seq 范围不连续，索引损坏')

    def _load_packets_index(self):
        self.packet_blocks = self.container.of_type(BLK_PACKETS)
        declared = (self.meta.get('counts') or {}).get('packets')
        total = sum(self.container.blocks[index].count for index in self.packet_blocks)
        if declared is not None and int(declared) != total:
            raise TraceStoreError('包块计数与 meta 不符：%d≠%s' % (total, declared))
        self.packet_count = total

    def _load_nav(self):
        self._nav = {mode: [] for mode in NAV_NAMES}
        for index in self.container.of_type(BLK_NAV):
            payload = self.container.block(index)
            if len(payload) < 2:
                raise TraceStoreError('导航块为空')
            position = 0
            mode = payload[position]
            position += 1
            if mode not in NAV_NAMES:
                raise TraceStoreError('未知导航模式：%d' % mode)
            count, position = _dvar(payload, position)
            if count > (1 << 32):
                raise TraceStoreError('导航计数无效：%d' % count)
            target = self._nav[mode]
            if count:
                value, position = _dvar(payload, position)
                target.append(value)
                for _ in range(count - 1):
                    delta, position = _dvar(payload, position)
                    value += delta
                    target.append(value)
            if position != len(payload):
                raise TraceStoreError('导航块有多余数据')
        for values in self._nav.values():
            if any(a >= b for a, b in zip(values, values[1:])):
                raise TraceStoreError('导航索引未严格递增')
            if any(not 0 <= value <= self.last_seq for value in values):
                raise TraceStoreError('导航 seq 越界')

    def _load_checkpoints(self):
        indexes = self.container.of_type(BLK_CHECKPOINTS)
        if not indexes:
            raise TraceStoreError('缺少调用状态检查点索引')
        payload = self.container.block(indexes[0])
        position = 0
        self.checkpoints = []
        previous = 0
        while position < len(payload):
            delta, position = _dvar(payload, position)
            block_index, position = _dvar(payload, position)
            record_offset, position = _dvar(payload, position)
            seq = previous + delta
            previous = seq
            if block_index >= len(self.sequence_blocks):
                raise TraceStoreError('检查点块索引越界')
            self.checkpoints.append((seq, block_index, record_offset))
        if not self.checkpoints:
            raise TraceStoreError('检查点索引为空')
        self._checkpoint_seqs = [entry[0] for entry in self.checkpoints]
        if self._checkpoint_seqs[0] != self.first_seq:
            raise TraceStoreError('首个检查点 seq 与记录起点不符')
        if any(a >= b for a, b in zip(self._checkpoint_seqs, self._checkpoint_seqs[1:])):
            raise TraceStoreError('检查点 seq 未严格递增')

    # -- 查询索引（R1）-----------------------------------------------------------------------
    def _load_query_index(self):
        """加载 12 号查询索引块：缺失 → None（旧记录慢路径）；损坏 → issue + None。

        与 `docs/etm-r1-query-design.md` §3.1 一致：版本/自洽校验失败时**回退慢路径**并记录
        `query-index` 问题，绝不返回拼凑结果；`TRACE_STORE_SLOW=1` 强制走慢路径做 oracle。
        """
        try:
            import trace_store_index
        except ImportError:                     # 裁剪安装：没有索引模块就按旧记录处理
            return
        try:
            self.query_index = trace_store_index.load_query_index(self)
        except Exception as error:              # noqa: BLE001 - 任何索引错误都回退慢路径
            self.query_index = None
            self.query_index_error = str(error)
            self._record_index_issue(error)

    def _record_index_issue(self, error):
        entry = dict(category='query-index', startSeq=self.first_seq,
                     endSeq=self.first_seq, offset=0, records=0,
                     reason='查询索引不可用，已回退慢路径：%s' % error)
        if self._issues is not None:
            self._issues.append(entry)
        else:
            self._extra_issues.append(entry)

    def _disable_query_index(self, what, error):
        self.query_index = None
        self.query_index_error = '%s：%s' % (what, error)
        self._record_index_issue(error)

    # -- 展开 --------------------------------------------------------------------------------
    def _decode_tracker(self, payload, position, end):
        """从检查点记录负载恢复 CallTracker 状态。"""
        from flow_analysis import CallTracker

        def read_optional_int(position):
            if position >= end:
                raise TraceStoreError('检查点被截断')
            flag = payload[position]
            position += 1
            if flag == 0:
                return None, position
            if flag != 1:
                raise TraceStoreError('检查点可选整数标志无效')
            value, position = _dvar(payload, position)
            return value, position

        def read_function(position):
            if position >= end:
                raise TraceStoreError('检查点被截断')
            flag = payload[position]
            position += 1
            if flag == 0:
                return None, position
            if flag != 1:
                raise TraceStoreError('检查点函数标志无效')
            identifier, position = _dvar(payload, position)
            return self._string(identifier), position

        def read_previous(position):
            if position >= end:
                raise TraceStoreError('检查点被截断')
            present = payload[position]
            position += 1
            if present == 0:
                return None, position
            if present != 1:
                raise TraceStoreError('检查点上一条指令标志无效')
            pc, position = _dvar(payload, position)
            size, position = _dvar(payload, position)
            if position + 2 > end:
                raise TraceStoreError('检查点被截断')
            flow_code = payload[position]
            executed_code = payload[position + 1]
            position += 2
            flow = FLOW_NAMES.get(flow_code)
            if flow is None or executed_code not in EXECUTED_VALUES:
                raise TraceStoreError('检查点 flow/executed 无效')
            function, position = read_function(position)
            return (pc, size, flow, EXECUTED_VALUES[executed_code], function), position

        def read_stack(position):
            if position >= end:
                raise TraceStoreError('检查点被截断')
            count, position = _dvar(payload, position)
            if count > MAX_CALL_DEPTH:
                raise TraceStoreError('检查点调用深度超界：%d' % count)
            stack = []
            for _ in range(count):
                function, position = read_function(position)
                if position >= end:
                    raise TraceStoreError('检查点被截断')
                flags = payload[position]
                position += 1
                if flags & ~0x3:
                    raise TraceStoreError('检查点帧标志无效')
                return_pc = origin = None
                if flags & 0x1:
                    return_pc, position = _dvar(payload, position)
                if flags & 0x2:
                    identifier, position = _dvar(payload, position)
                    origin = self._string(identifier)
                stack.append(dict(function=function, returnPC=return_pc, origin=origin))
            return stack, position

        unknown_restarts, position = _dvar(payload, position)
        previous, position = read_previous(position)
        expected, position = read_optional_int(position)
        stack, position = read_stack(position)
        contexts_count, position = _dvar(payload, position)
        if contexts_count > MAX_CALL_DEPTH * 4:
            raise TraceStoreError('检查点上下文数量超界：%d' % contexts_count)
        contexts = []
        for _ in range(contexts_count):
            context_stack, position = read_stack(position)
            context_previous, position = read_previous(position)
            context_expected, position = read_optional_int(position)
            contexts.append((context_stack, context_previous, context_expected))
        if position != end:
            raise TraceStoreError('检查点记录有多余数据')
        tracker = CallTracker()
        tracker.stack = stack
        tracker.contexts = contexts
        tracker.previous = previous
        tracker.expected_resume = expected
        tracker.unknown_restarts = unknown_restarts
        tracker._sync()
        return tracker

    def _sequence_payload(self, block_ordinal):
        return self.container.block(self.sequence_blocks[block_ordinal])

    @staticmethod
    def _records(payload, position=0):
        while position < len(payload):
            record_type = payload[position]
            position += 1
            length, position = _dvar(payload, position)
            end = position + length
            if end > len(payload):
                raise TraceStoreError('序列记录越过块边界')
            yield record_type, position, end
            position = end

    def _checkpoint_at_or_before(self, seq):
        index = bisect.bisect_right(self._checkpoint_seqs, seq) - 1
        if index < 0:
            return None
        return self.checkpoints[index]

    def _block_at(self, seq):
        index = bisect.bisect_right(self._block_first, seq) - 1
        return max(0, index)

    def _expand(self, start=None, end=None):
        """从 start 起展开旧事件字典；先恢复到最近检查点，再重放到 start。"""
        if start is None:
            start = self.first_seq
        if start < self.first_seq:
            start = self.first_seq
        if end is not None and end < start:
            raise TraceStoreError('区间结束 seq 不能小于起点')
        checkpoint = self._checkpoint_at_or_before(start)
        if checkpoint is None:
            raise TraceStoreError('找不到可用于 seq=%d 的检查点' % start)
        seq_at_checkpoint, block_ordinal, record_offset = checkpoint
        tracker = None
        expected = seq_at_checkpoint
        for ordinal in range(block_ordinal, len(self.sequence_blocks)):
            span = self.block_spans[ordinal]
            if ordinal > block_ordinal and end is not None and span['firstSeq'] > end:
                return
            payload = self._sequence_payload(ordinal)
            position = record_offset if ordinal == block_ordinal else 0
            for record_type, begin, finish in self._records(payload, position):
                if record_type == REC_CHECKPOINT:
                    if tracker is None:
                        seq, offset = _dvar(payload, begin)
                        tracker = self._decode_tracker(payload, offset, finish)
                        expected = seq
                    continue
                if tracker is None:
                    raise TraceStoreError('序列块起始缺少检查点')
                if record_type == REC_RUN:
                    cursor = begin
                    seq, cursor = _dvar(payload, cursor)
                    count, cursor = _dvar(payload, cursor)
                    if count < 1 or count > MAX_RUN_INSTRUCTIONS:
                        raise TraceStoreError('展开段指令数无效：%d' % count)
                    tail_code = payload[cursor]
                    cursor += 1
                    if tail_code not in TAIL_VALUES:
                        raise TraceStoreError('展开段末指令状态无效')
                    offset, cursor = _dvar(payload, cursor)
                    cycles, cursor = _dvar(payload, cursor)
                    cycle_span, cursor = _dvar(payload, cursor)
                    internal_unknown, cursor = _dvar(payload, cursor)
                    first_identifier, cursor = _dvar(payload, cursor)
                    if seq != expected:
                        raise TraceStoreError('展开段 seq 不连续：期望 %d，实际 %d'
                                              % (expected, seq))
                    identifiers = [first_identifier]
                    previous = first_identifier
                    for _ in range(count - 1):
                        delta, cursor = _dzvar(payload, cursor)
                        previous += delta
                        identifiers.append(previous)
                    if cursor != finish:
                        raise TraceStoreError('展开段记录长度与内容不符')
                    if any(not 0 <= value < len(self.static) for value in identifiers):
                        raise TraceStoreError('展开段引用越界静态 ID')
                    if cycles == 0 and cycle_span != 0:
                        raise TraceStoreError('展开段周期跨度与周期数矛盾')
                    if cycles and not 1 <= cycle_span <= MAX_RUN_INSTRUCTIONS:
                        # cycleSpan 是“拥有该末指令的原生范围”的指令数，可能大于本展开段
                        # （原生范围可能在周期锚点处被切开），所以只校验上下界。
                        raise TraceStoreError('展开段 cycleSpan 越界：%d（cycles=%d）'
                                              % (cycle_span, cycles))
                    internal_predicated = 0
                    tail = len(identifiers) - 1
                    for position_in_run, identifier in enumerate(identifiers):
                        entry = self.static[identifier]
                        emitted_cycles = 0
                        emitted_span = 0
                        if position_in_run == tail:
                            executed = (None if entry.predicated
                                        else TAIL_VALUES[tail_code])
                            emitted_cycles = cycles
                            emitted_span = cycle_span
                        else:
                            executed = None if entry.predicated else True
                            if entry.predicated:
                                internal_predicated += 1
                        tracker.feed_instruction(entry.function, entry.file, entry.line, executed,
                                                 entry.flow, entry.pc, entry.size,
                                                 emitted_cycles)
                        if expected >= start and (end is None or expected <= end):
                            yield self._instruction_event(expected, offset, entry, executed,
                                                          emitted_cycles, emitted_span, tracker)
                        expected += 1
                        if end is not None and expected > end:
                            return
                    if internal_predicated != internal_unknown:
                        raise TraceStoreError('展开段内部谓词未知计数不符')
                    continue
                if record_type == REC_EVENT:
                    cursor = begin
                    seq, cursor = _dvar(payload, cursor)
                    if seq != expected:
                        raise TraceStoreError('边界事件 seq 不连续：期望 %d，实际 %d'
                                              % (expected, seq))
                    etype = payload[cursor]
                    cursor += 1
                    event_offset, cursor = _dvar(payload, cursor)
                    payload_value, cursor = _dvar(payload, cursor)
                    event_start, cursor = _dvar(payload, cursor)
                    event_end, cursor = _dvar(payload, cursor)
                    flags, cursor = _dvar(payload, cursor)
                    text_id, cursor = _dvar(payload, cursor)
                    if cursor != finish:
                        raise TraceStoreError('边界事件记录长度与内容不符')
                    if etype not in EVENT_TYPES:
                        raise TraceStoreError('边界事件类型无效：%d' % etype)
                    event = {'kind': 'event', 'seq': seq, 'type': etype, 'offset': event_offset,
                             'text': self._string(text_id), 'payload': payload_value,
                             'start': event_start, 'end': event_end, 'flags': flags}
                    tracker.feed_event(etype, flags, payload_value, event_end)
                    expected = seq + 1
                    if seq >= start and (end is None or seq <= end):
                        yield event
                    if end is not None and seq > end:
                        return
                    continue
                raise TraceStoreError('未知序列记录类型：%d' % record_type)

    def _instruction_event(self, seq, offset, entry, executed, cycles, cycle_span, tracker):
        values = {'kind': 'instruction', 'seq': seq, 'offset': offset, 'pc': entry.pc,
                  'size': entry.size, 'bytes': entry.bytes.hex(), 'mnemonic': entry.mnemonic,
                  'operands': entry.operands, 'executed': executed,
                  'function': entry.function, 'file': entry.file, 'line': entry.line,
                  'cycleCount': cycles, 'cycleSpan': cycle_span, 'flow': entry.flow,
                  'conditional': entry.conditional,
                  'executionState': STATE_FROM_EXECUTED[executed],
                  'executionReason': IT_REASON if executed is None else '',
                  'callPath': list(tracker.path), 'cycles': cycles}
        return {key: values[key] for key in self.instruction_fields}

    # -- 公共查询 ----------------------------------------------------------------------------
    def events(self, start=None, end=None):
        """按 seq 顺序流式展开事件字典（O(1) 内存，只重放一个检查点间隔）。"""
        yield from self._expand(start, end)

    def first(self):
        return self.first_seq

    def last(self):
        return self.last_seq

    def bounds(self):
        return [self.first_seq, self.last_seq]

    def overview(self):
        return json.loads(json.dumps(self.summary))

    def event(self, seq):
        seq = int(seq)
        if not self.first_seq <= seq <= self.last_seq:
            raise TraceStoreError('记录序号不存在')
        for item in self._expand(seq, seq):
            if item['seq'] == seq:
                return dict(event=item, callPath=item.get('callPath', []),
                            stackBasis='历史 trace 重建，包含未证实祖先；不是当前寄存器快照')
        raise TraceStoreError('记录序号不存在')

    def seq_position(self, seq):
        """seq → 定位信息（块、检查点、有效范围），用于证据与测试。"""
        seq = int(seq)
        if not self.first_seq <= seq <= self.last_seq:
            raise TraceStoreError('记录序号不存在')
        block = self._block_at(seq)
        checkpoint = self._checkpoint_at_or_before(seq)
        return dict(seq=seq, block=block, blockFirst=self.block_spans[block]['firstSeq'],
                    blockLast=self.block_spans[block]['lastSeq'],
                    checkpointSeq=checkpoint[0], checkpointBlock=checkpoint[1],
                    checkpointOffset=checkpoint[2],
                    replayInstructions=seq - checkpoint[0],
                    validRange=[self.first_seq, self.last_seq])

    def stats(self):
        spans = self.block_spans
        return dict(events=self.counts.get('events'),
                    instructions=self.counts.get('instructions'),
                    runs=self.counts.get('runs'), ranges=self.counts.get('ranges'),
                    rareEvents=self.counts.get('rareEvents'),
                    staticPcs=self.counts.get('staticPcs'),
                    packets=self.counts.get('packets'), blocks=len(spans),
                    checkpoints=len(self.checkpoints),
                    executed=sum(entry['executed'] for entry in spans),
                    unknown=sum(entry['unknown'] for entry in spans),
                    notTaken=sum(entry['notTaken'] for entry in spans),
                    cycles=sum(entry['cycles'] for entry in spans),
                    aggregateMatches=(sum(entry['instructions'] for entry in spans)
                                      == self.counts.get('instructions')
                                      and sum(entry['executed'] for entry in spans)
                                      == self.counts.get('executed')
                                      and sum(entry['rareEvents'] for entry in spans)
                                      == self.counts.get('rareEvents')),
                    seqRange=[self.first_seq, self.last_seq],
                    queryIndex=self._query_index_summary())

    def _query_index_summary(self):
        index = self.query_index
        if index is None:
            return dict(available=False, error=self.query_index_error)
        meta = index.meta
        nav = meta.get('nav') or None
        return dict(available=True, version=int(meta.get('version') or 0),
                    container=(meta.get('container') or 'v1'),
                    cellSize=int(meta.get('cellSize') or 0),
                    cells=int(meta.get('cells') or 0),
                    segmentCount=int(meta.get('segmentCount') or 0),
                    functionSegments=int(meta.get('functionSegments') or 0),
                    defaultViews=sorted((meta.get('defaultViews') or {})),
                    # 块 12 导航表（t14）：旧索引无此表（None）→ nav 回退 sidecar
                    nav=(None if nav is None else dict(
                        cellSize=int(nav.get('cellSize') or 0),
                        pcCellPairs=int(nav.get('pcCellPairs') or 0),
                        lineRuns=int(nav.get('lineRuns') or 0))))

    # -- page ---------------------------------------------------------------------------------
    def page(self, view='events', offset=0, limit=150, search='', hide_skipped=False,
             start=None, end=None):
        """与 record_store.RecordStore.page 同名同义的兼容实现。"""
        offset = max(0, int(offset))
        limit = max(1, min(500, int(limit)))
        if view == 'packets':
            return self._page_packets(offset, limit, search)
        if view not in ('events', None):
            raise TraceStoreError('未知视图：%r' % (view,))
        lo = self.first_seq if start is None else max(self.first_seq, int(start))
        hi = self.last_seq if end is None else min(self.last_seq, int(end))
        if hi < lo:
            hi = lo - 1
        needle = search.lower() if search else ''
        filtered = bool(needle) or bool(hide_skipped)
        contiguous = bool((self.meta.get('seq') or {}).get('contiguous', True))
        if not filtered and (contiguous or (start is None and end is None)):
            # A plain interval of a contiguous sequence has an exact count;
            # scanning millions of events only to count them blocks the UI.
            total = max(0, hi - lo + 1) if contiguous else int(self.counts.get('events') or 0)
            offset = min(offset, max(0, ((total - 1) // limit) * limit)) if total else 0
            # 不整天扫描：连续 seq 记录直接从“首 seq + offset”定位，再按检查点重放少量指令。
            start_seq = lo + offset if contiguous else self.first_seq
            skip = 0 if contiguous else offset
            rows = []
            for item in self.events(start_seq, hi) if total else ():
                if skip:
                    skip -= 1
                    continue
                rows.append(item)
                if len(rows) >= limit:
                    break
            return dict(rows=rows, total=total, offset=offset, limit=limit)
        total = 0
        for item in self.events(lo, hi):
            if hide_skipped and item['kind'] == 'instruction' \
                    and item.get('executionState') == 'not-taken':
                continue
            if needle and needle not in self._search_text(item):
                continue
            total += 1
        offset = min(offset, max(0, ((total - 1) // limit) * limit)) if total else 0
        rows = []
        skip = offset
        for item in self.events(lo, hi):
            if hide_skipped and item['kind'] == 'instruction' \
                    and item.get('executionState') == 'not-taken':
                continue
            if needle and needle not in self._search_text(item):
                continue
            if skip:
                skip -= 1
                continue
            rows.append(item)
            if len(rows) >= limit:
                break
        return dict(rows=rows, total=total, offset=offset, limit=limit)

    def _search_text(self, item):
        if item['kind'] == 'instruction':
            text = ' '.join((item.get('function') or '', item.get('file') or '',
                             item['mnemonic'], item['operands'], ''))
            text = ('%s 0x%08x %d' % (text, item['pc'], item['pc'])).lower()
            return text.replace('\t', ' ').replace('\n', ' ')
        return ('    ' + item.get('text', '')).lower().replace('\t', ' ').replace('\n', ' ')

    def _page_packets(self, offset, limit, search):
        needle = search.lower() if search else ''
        total = 0
        for packet in self._iter_packets():
            if needle:
                text = (packet['text'] + ' ' + str(packet['offset']) + ' ' + packet['hex']).lower()
                if needle not in text:
                    continue
            total += 1
        offset = min(offset, max(0, ((total - 1) // limit) * limit)) if total else 0
        rows = []
        skip = offset
        for packet in self._iter_packets():
            if needle:
                text = (packet['text'] + ' ' + str(packet['offset']) + ' ' + packet['hex']).lower()
                if needle not in text:
                    continue
            if skip:
                skip -= 1
                continue
            rows.append(packet)
            if len(rows) >= limit:
                break
        return dict(rows=rows, total=total, offset=offset, limit=limit)

    def _iter_packets(self):
        previous_offset = 0
        for index in self.packet_blocks:
            payload = self.container.block(index)
            position = 0
            while position < len(payload):
                delta, position = _dvar(payload, position)
                size, position = _dvar(payload, position)
                raw, position = _dpair(payload, position)
                text, position = _dpair(payload, position)
                offset = previous_offset + delta
                previous_offset = offset
                if len(raw) != size:
                    raise TraceStoreError('包记录 hex 长度与 size 不符')
                try:
                    decoded = text.decode('utf-8')
                except UnicodeDecodeError:
                    raise TraceStoreError('包说明不是合法 UTF-8')
                yield {'kind': 'packet', 'offset': offset, 'size': size,
                       'hex': raw.hex(), 'text': decoded}

    # -- window ------------------------------------------------------------------------------
    def window(self, start=None, end=None, bins=1200):
        """全段/任意区间窗口：有查询索引时走快路径，失败明确回退慢路径。

        快路径与 `_window`（JSONL oracle）逐字段同义；`TRACE_STORE_SLOW=1` 时
        `query_index=None`，这里直接走 `_window`，供 50+ 区间对照。
        """
        index = self.query_index
        if index is not None:
            try:
                return index.window(start, end, bins)
            except TraceStoreError:
                raise                            # 区间参数错误：与参考实现同类型
            except Exception as error:           # noqa: BLE001 - 索引内部错误一律回退
                self._disable_query_index('window', error)
        return self._window(start, end, bins)

    def _window(self, start=None, end=None, bins=1200):
        """与 record_store.RecordStore._window 的 JSONL 回退路径逐字段一致。"""
        bins = max(100, min(2000, int(bins)))
        total_start, total_end = self.first_seq, self.last_seq
        lo = total_start if start is None else max(total_start, min(total_end, int(start)))
        hi = total_end if end is None else max(total_start, min(total_end, int(end)))
        if hi < lo:
            raise TraceStoreError('区间结束序号不能小于起点')
        step = max(1, math.ceil((hi - lo + 1) / bins))
        root = dict(name='所选执行区间', value=0, self=0, selfCycles=0, valueCycles=0, children={})
        buckets = {}
        functions = {}
        hotspots = {}
        inclusive = {}
        previous = None
        observed = 0
        first = None
        pending_irq = False
        tree_path = None
        tree_nodes = []
        for event in self.events(lo, hi):
            key = (event['seq'] - lo) // step
            bucket = buckets.get(key)
            if bucket is None:
                bucket = dict(startSeq=event['seq'], endSeq=event['seq'], firstSeq=event['seq'],
                              path=None, records=0, unknown=0, notTaken=0, mixed=False, events=[])
                buckets[key] = bucket
            bucket['endSeq'] = event['seq']
            if event['kind'] != 'instruction':
                bucket['events'].append(dict(seq=event['seq'], type=event['type'],
                                             text=event.get('text', '')))
                pending_irq = event['type'] == 9
                previous = None
                continue
            if first is None:
                first = event['seq']
            name = event.get('function') or '(未知函数)'
            path = event.get('callPath') or ['调用栈未知', name]
            if bucket['path'] is None:
                bucket['path'] = list(path)
            elif bucket['path'] != path:
                common = []
                for left, right in zip(bucket['path'], path):
                    if left != right:
                        break
                    common.append(left)
                bucket['path'] = common
                bucket['mixed'] = True
            bucket['records'] += 1
            bucket['unknown'] += event.get('executed') is None
            bucket['notTaken'] += event.get('executed') is False
            cycles = int(event.get('cycleCount') or 0)
            node = root
            node['value'] += 1
            node['valueCycles'] += cycles
            if path != tree_path:
                tree_nodes = []
                for label in path:
                    child = node['children'].get(label)
                    if child is None:
                        child = dict(name=label, value=0, self=0, selfCycles=0, valueCycles=0,
                                     children={})
                        node['children'][label] = child
                    node = child
                    tree_nodes.append(node)
                tree_path = path
            for node in tree_nodes:
                node['value'] += 1
                node['valueCycles'] += cycles
            node = tree_nodes[-1] if tree_nodes else root
            node['self'] += 1
            node['selfCycles'] += cycles
            stat = functions.get(name)
            if stat is None:
                stat = dict(function=name, instructions=0, knownExecuted=0, unknownExecution=0,
                            notTaken=0, calls=0, interruptEntries=0, selfCycles=0)
                functions[name] = stat
            stat['instructions'] += 1
            stat['knownExecuted'] += event.get('executed') is True
            stat['unknownExecution'] += event.get('executed') is None
            stat['notTaken'] += event.get('executed') is False
            if cycles:
                stat['selfCycles'] += cycles
                for label in path:
                    inclusive[label] = inclusive.get(label, 0) + cycles
            if pending_irq:
                stat['interruptEntries'] += 1
                pending_irq = False
            if previous and previous.get('flow') == 'call' and previous.get('executed') is True:
                stat['calls'] += 1
                observed += 1
            hotkey = (name, event.get('file', ''), event.get('line'))
            hot = hotspots.get(hotkey)
            if hot is None:
                hot = dict(function=name, file=event.get('file', ''), line=event.get('line'),
                           records=0, unknown=0, cycles=0, pc=event['pc'])
                hotspots[hotkey] = hot
            hot['records'] += 1
            hot['unknown'] += event.get('executed') is None
            hot['cycles'] += cycles
            previous = event

        def serialize(node):
            return {**node, 'children': [serialize(child) for child in node['children'].values()]}

        for bucket in buckets.values():
            bucket['path'] = bucket['path'] or []
            if bucket['mixed']:
                bucket['path'].append('多段调用（放大查看）')
        total_cycles = sum(stat['selfCycles'] for stat in functions.values())
        executed = sum(stat['knownExecuted'] for stat in functions.values())
        for stat in functions.values():
            stat['totalCycles'] = inclusive.get(stat['function'], 0)
            stat['cyclePercent'] = 100 * stat['selfCycles'] / total_cycles if total_cycles else 0
            stat['cpi'] = (stat['selfCycles'] / stat['instructions']
                           if (stat['instructions'] and stat['selfCycles']) else None)
        cycle_functions = [dict(function=stat['function'], selfCycles=stat['selfCycles'],
                                cyclePercent=stat['cyclePercent'],
                                instructions=stat['instructions'], cpi=stat['cpi'],
                                calls=stat['calls'])
                           for stat in sorted((item for item in functions.values()
                                               if item['selfCycles']),
                                              key=lambda item: item['selfCycles'], reverse=True)]
        return dict(range=dict(start=lo, end=hi, totalStart=total_start, totalEnd=total_end),
                    firstSeq=first,
                    timing=dict(axis='execution-order', verifiedTime=False,
                                reason='当前记录没有已验收的逐指令时间戳，执行序号不能换算为微秒'),
                    timeline=list(buckets.values()), resolution=step,
                    analysis=dict(flame=serialize(root),
                                  functions=sorted(functions.values(),
                                                   key=lambda item: item['instructions'],
                                                   reverse=True),
                                  hotspots=sorted(hotspots.values(),
                                                  key=lambda item: (item['cycles'],
                                                                    item['records']),
                                                  reverse=True),
                                  observedCalls=observed,
                                  timing=dict(totalCycles=total_cycles,
                                              cycleRanges=len(cycle_functions),
                                              executedInstructions=executed,
                                              instructions=sum(stat['instructions']
                                                               for stat in functions.values()),
                                              cpi=(total_cycles / executed
                                                   if (executed and total_cycles) else None),
                                              functions=cycle_functions,
                                              timeFlameAvailable=bool(total_cycles),
                                              basis=('所选区间的周期计数（区间末指令记录，可求和）'
                                                     if total_cycles else
                                                     '所选区间没有周期计数数据（目标未启用或 '
                                                     'ETMv3 不支持）')),
                                  weight=('所选区间的路径记录数；周期列才是耗时' if total_cycles
                                          else '所选区间的路径记录数（含未成立/未知状态），'
                                               '不是耗时')))

    def function_timeline(self, start=None, end=None, search='', hide_skipped=False,
                          bins=1600, max_functions=6):
        """与 record_store.RecordStore.function_timeline 同义的兼容实现。

        无筛选（`search`/`hide_skipped` 为空）时优先走查询索引快路径；筛选口径仍走慢路径
        （首条指令、隐藏未成立与搜索文本必须与参考逐字段一致，R1 不冒进）。
        """
        index = self.query_index
        if index is not None and not search and not hide_skipped:
            try:
                return index.function_timeline(start, end, search, hide_skipped,
                                               bins, max_functions)
            except TraceStoreError:
                raise
            except Exception as error:           # noqa: BLE001 - 索引内部错误一律回退
                self._disable_query_index('function_timeline', error)
        bins = max(240, min(2400, int(bins)))
        max_functions = max(1, min(10, int(max_functions)))
        total_start, total_end = self.first_seq, self.last_seq
        lo = total_start if start is None else max(total_start, min(total_end, int(start)))
        hi = total_end if end is None else max(total_start, min(total_end, int(end)))
        if hi < lo:
            raise TraceStoreError('区间结束序号不能小于起点')
        needle = search.lower() if search else ''
        counts = {}
        state_counts = {}
        filtered = 0
        not_taken = 0
        marker_count = 0
        marker_bins = {}
        span = max(1, hi - lo + 1)
        for event in self.events(lo, hi):
            if needle and needle not in self._search_text(event):
                continue
            if hide_skipped and event['kind'] == 'instruction' \
                    and event.get('executionState') == 'not-taken':
                continue
            filtered += 1
            if event['kind'] != 'instruction':
                if event['type'] in EVENT_TYPES:
                    marker_count += 1
                    index = min(bins - 1, max(0, (event['seq'] - lo) * bins // span))
                    bucket = marker_bins.get(index)
                    if bucket is None:
                        bucket = dict(startSeq=event['seq'], endSeq=event['seq'],
                                      seq=event['seq'], types={})
                        marker_bins[index] = bucket
                    bucket['endSeq'] = event['seq']
                    bucket['types'][str(event['type'])] = \
                        bucket['types'].get(str(event['type']), 0) + 1
                continue
            state = event.get('executionState')
            state_counts[state] = state_counts.get(state, 0) + 1
            if state == 'executed':
                name = event.get('function') or '(未知函数)'
                counts[name] = counts.get(name, 0) + 1
            elif state == 'not-taken':
                not_taken += 1
        unknown = sum(count for state, count in state_counts.items()
                      if state not in ('executed', 'not-taken'))
        top = [name for name, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
               [:max_functions]]
        other = sum(value for name, value in counts.items() if name not in top)
        labels = [dict(key='fn:' + name, name=name, kind='function', records=counts[name])
                  for name in top]
        if other:
            labels.append(dict(key='other', name='其他函数', kind='other', records=other))
        if unknown:
            labels.append(dict(key='unknown', name='执行状态未知', kind='unknown', records=unknown))
        if not hide_skipped and not_taken:
            labels.append(dict(key='not-taken', name='未成立路径（未证实执行）',
                               kind='not-taken', records=not_taken))
        values = {lane['key']: [0] * bins for lane in labels}
        for event in self.events(lo, hi):
            if needle and needle not in self._search_text(event):
                continue
            if hide_skipped and event['kind'] == 'instruction' \
                    and event.get('executionState') == 'not-taken':
                continue
            if event['kind'] != 'instruction':
                continue
            state = event.get('executionState')
            if state == 'executed':
                name = event.get('function') or '(未知函数)'
                key = 'fn:' + name if ('fn:' + name) in values else 'other'
            elif state == 'not-taken':
                key = 'not-taken'
            else:
                key = 'unknown'
            if key not in values:
                continue
            index = min(bins - 1, max(0, (event['seq'] - lo) * bins // span))
            values[key][index] += 1
        for lane in labels:
            lane['bins'] = values[lane['key']]
        markers = []
        for bucket in marker_bins.values():
            bucket['count'] = sum(bucket['types'].values())
            markers.append(bucket)
        return dict(range=dict(start=lo, end=hi, totalStart=total_start, totalEnd=total_end),
                    lanes=labels, binCount=bins, markers=markers, markerCount=marker_count,
                    summary=dict(executed=sum(counts.values()), unknown=unknown,
                                 notTaken=not_taken, filteredRecords=filtered,
                                 markers=marker_count),
                    timing=dict(axis='execution-order', verifiedTime=False,
                                reason='当前记录没有已验收的逐指令时间戳，横轴只表示执行序号'))

    # -- 导航/源码/问题 ----------------------------------------------------------------------
    def navigate(self, seq=0, direction=1, mode='instruction', pc=None, start=None, end=None):
        """与 record_store.RecordStore.navigate 同名同义。"""
        # R1 导航快路径由 trace_store_nav 提供（p2/t5）；这里只做一行分派 + 异常回退，
        # 不内联导航逻辑。模块缺失/返回 None/抛异常 → 保持下面原有的 _scan 慢路径。
        if self.query_index is not None:
            try:
                from trace_store_nav import navigate_fast
            except ImportError:
                navigate_fast = None
            if navigate_fast is not None:
                try:
                    fast = navigate_fast(self, seq, direction, mode, pc, start, end)
                except Exception:            # noqa: BLE001 - 快路径失败不改语义，回退慢路径
                    fast = None
                if fast is not None:
                    return fast
        seq = int(seq)
        forward = int(direction) > 0
        if mode not in ('instruction', 'call', 'return', 'exception', 'issue', 'source'):
            raise TraceStoreError('未知导航类型')
        scoped_lo = self.first_seq if start is None else max(self.first_seq, int(start))
        scoped_hi = self.last_seq if end is None else min(self.last_seq, int(end))
        if pc is not None:
            target = int(pc)

            def match_pc(event):
                return event['kind'] == 'instruction' and event['pc'] == target

            for item in self._scan(seq, forward, scoped_lo, scoped_hi, match_pc):
                return dict(seq=item['seq'], wrapped=False)
            for item in self._scan(self.first_seq - 1, True, scoped_lo, scoped_hi, match_pc):
                return dict(seq=item['seq'], wrapped=True)
            return dict(seq=None, wrapped=False)
        nav_mode = {'call': NAV_CALL, 'return': NAV_RETURN, 'exception': NAV_EXCEPTION,
                    'issue': NAV_ISSUE}.get(mode)
        if nav_mode is not None:
            values = self.nav[nav_mode]
            if forward:
                index = bisect.bisect_right(values, seq)
                while index < len(values) and values[index] < scoped_lo:
                    index += 1
                if index < len(values) and values[index] <= scoped_hi:
                    return dict(seq=values[index], wrapped=False)
                return dict(seq=None, wrapped=False)
            index = bisect.bisect_left(values, seq) - 1
            while index >= 0 and values[index] > scoped_hi:
                index -= 1
            if index >= 0 and values[index] >= scoped_lo:
                return dict(seq=values[index], wrapped=False)
            return dict(seq=None, wrapped=False)
        if mode == 'instruction':
            for item in self._scan(seq, forward, scoped_lo, scoped_hi,
                                   lambda event: event['kind'] == 'instruction'):
                return dict(seq=item['seq'], wrapped=False)
            return dict(seq=None, wrapped=False)
        current = self.event(seq)['event'] if self.first_seq <= seq <= self.last_seq else {}
        current_file = current.get('file') or ''
        current_line = current.get('line') or 0

        def match_source(event):
            if event['kind'] != 'instruction' or event.get('line') is None:
                return False
            return (event.get('file') or '') != current_file \
                or (event.get('line') or 0) != current_line

        for item in self._scan(seq, forward, scoped_lo, scoped_hi, match_source):
            return dict(seq=item['seq'], wrapped=False)
        return dict(seq=None, wrapped=False)

    def _scan(self, seq, forward, scoped_lo, scoped_hi, predicate):
        """从 seq 起（不含 seq）按方向扫描事件；向后时按块向前回退。"""
        if scoped_hi < scoped_lo:
            return
        if forward:
            start = max(seq + 1, scoped_lo)
            if start > scoped_hi:
                return
            for event in self.events(start, scoped_hi):
                if predicate(event):
                    yield event
                    return
            return
        position = bisect.bisect_right(self._block_first, min(seq - 1, scoped_hi)) - 1
        while position >= 0:
            span = self.block_spans[position]
            hi = min(seq - 1, scoped_hi, span['lastSeq'])
            lo = max(scoped_lo, span['firstSeq'])
            if hi >= lo:
                found = None
                for event in self.events(lo, hi):
                    if predicate(event):
                        found = event
                if found is not None:
                    yield found
                    return
            if span['firstSeq'] <= scoped_lo:
                return
            position -= 1

    def issues(self, offset=0, limit=100):
        offset = max(0, int(offset))
        limit = min(500, max(1, int(limit)))
        total = len(self.issues_list)
        return dict(rows=self.issues_list[offset:offset + limit], total=total)

    def source(self, file):
        text = (self.sources or {}).get(file, '')
        return dict(file=file, text=text)

    def close(self):
        container = getattr(self, 'container', None)
        if container is not None:
            container.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def open_store(folder, verify='full', verify_crc=True):
    return TraceStore(folder, verify=verify, verify_crc=verify_crc)


def describe_store(folder):
    store = TraceStore(folder, verify='sizes')
    try:
        return dict(manifest=store.manifest, meta=store.meta, stats=store.stats(),
                    generation=store.generation)
    finally:
        store.close()


# ---------------------------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------------------------

def _main(argv=None):
    parser = argparse.ArgumentParser(
        prog='trace_store.py',
        description='trace-store v1 参考读写器：把既有解码记录写成版本化二进制存储，'
                    '并按页/窗口无损展开回旧事件。')
    sub = parser.add_subparsers(dest='command', required=True)

    writer = sub.add_parser('write', help='从既有记录目录生成 trace-store')
    writer.add_argument('record_dir', help='既有解码记录目录（events.jsonl 或 decoded.json 内联 events）')
    writer.add_argument('store_dir', help='输出目录（写入新 generation，成功后原子发布 manifest）')
    writer.add_argument('--no-elements', action='store_true', help='跳过元素流交叉校验')
    writer.add_argument('--no-image', action='store_true', help='跳过 ELF 机器码交叉校验')
    writer.add_argument('--checkpoint-instructions', type=int,
                        default=DEFAULT_CHECKPOINT_INSTRUCTIONS, help='检查点最大指令间隔')
    writer.add_argument('--no-hash-inputs', action='store_true', help='不哈希大输入文件（只记录尺寸）')
    writer.add_argument('--quiet', action='store_true', help='不打印进度')
    writer.add_argument('--json', action='store_true', help='以 JSON 输出写入摘要')

    info = sub.add_parser('info', help='打印存储清单与计数')
    info.add_argument('store_dir')
    info.add_argument('--json', action='store_true', help='以 JSON 输出')

    page = sub.add_parser('page', help='按页展开事件（与旧 page 同义）')
    page.add_argument('store_dir')
    page.add_argument('--offset', type=int, default=0)
    page.add_argument('--limit', type=int, default=150)
    page.add_argument('--start', type=int, default=None)
    page.add_argument('--end', type=int, default=None)
    page.add_argument('--search', default='')
    page.add_argument('--hide-skipped', action='store_true')
    page.add_argument('--view', default='events', choices=('events', 'packets'))

    event = sub.add_parser('event', help='展开单条事件')
    event.add_argument('store_dir')
    event.add_argument('seq', type=int)

    window = sub.add_parser('window', help='区间窗口聚合（与旧 window 同义）')
    window.add_argument('store_dir')
    window.add_argument('--start', type=int, default=None)
    window.add_argument('--end', type=int, default=None)
    window.add_argument('--bins', type=int, default=1200)

    export = sub.add_parser('export', help='把存储展开成 JSONL（证据/导出用）')
    export.add_argument('store_dir')
    export.add_argument('out', help='输出 JSONL 路径')
    export.add_argument('--start', type=int, default=None)
    export.add_argument('--end', type=int, default=None)

    args = parser.parse_args(argv)
    try:
        if args.command == 'write':
            def progress(message):
                if not args.quiet:
                    print(message, flush=True)
            result = write(args.record_dir, args.store_dir,
                           verify_elements=not args.no_elements,
                           verify_image=not args.no_image,
                           checkpoint_instructions=args.checkpoint_instructions,
                           hash_inputs=not args.no_hash_inputs,
                           progress=None if args.quiet else progress)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print('store：%s（%.1f MiB，%d 个序列块）'
                      % (result['storeDir'], result['storeBytes'] / 1048576,
                         result['counts']['sequenceBlocks']))
                print('事件 %d / 指令 %d / 展开段 %d / 静态 PC %d / 包 %d'
                      % (result['counts']['events'], result['counts']['instructions'],
                         result['counts']['runs'], result['counts']['staticPcs'],
                         result['counts']['packets']))
                print('耗时 %.2f s，峰值 RSS %.1f MiB，元素校验 %d 个原生范围'
                      % (result['elapsedSeconds'], result['peakRssBytes'] / 1048576,
                         result['elementsVerified']))
        elif args.command == 'info':
            data = describe_store(args.store_dir)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                meta = data['meta']
                print('格式 %s（schema %d，%s）generation=%s'
                      % (meta['formatName'], meta['schemaVersion'], meta['byteOrder'],
                         data['generation']))
                print('记录 %s；事件源 %s；streamed schema=%r'
                      % (meta['record']['name'], meta['record']['eventsSource'],
                         meta['record']['streamedSchema']))
                for key, value in meta['counts'].items():
                    print('  %-16s %s' % (key, value))
                print('  %-16s %s' % ('seqRange', data['stats']['seqRange']))
                print('  %-16s %s' % ('blocks', data['stats']['blocks']))
                print('  %-16s %s' % ('aggregateOK', data['stats']['aggregateMatches']))
        elif args.command == 'page':
            with open_store(args.store_dir, verify='sizes') as store:
                page = store.page(args.view, args.offset, args.limit, args.search,
                                  args.hide_skipped, args.start, args.end)
            print(json.dumps(page, ensure_ascii=False))
        elif args.command == 'event':
            with open_store(args.store_dir, verify='sizes') as store:
                print(json.dumps(store.event(args.seq), ensure_ascii=False))
        elif args.command == 'window':
            with open_store(args.store_dir, verify='sizes') as store:
                print(json.dumps(store.window(args.start, args.end, args.bins),
                                 ensure_ascii=False))
        elif args.command == 'export':
            count = 0
            with open_store(args.store_dir, verify='sizes') as store, \
                    open(args.out, 'w', encoding='utf-8', newline='\n') as output:
                for item in store.events(args.start, args.end):
                    output.write(json.dumps(item, ensure_ascii=False) + '\n')
                    count += 1
            print('已导出 %d 条事件到 %s' % (count, args.out))
        return 0
    except TraceStoreError as error:
        print('trace-store 错误：%s' % error, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(_main())
