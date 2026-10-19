"""OpenCSD 原生二进制元素/包记录（`trace-bin native v1`）读取、校验与故障分类。

写入端是 `etm/native/decoder.cpp` 的 `--binary <out.bin> trace.bin config-and-memory.txt`；
文本 JSONL 仍是默认参考模式。本模块只读文件、不调用 OpenCSD，纯 Python 校验与解析。

文件布局（全部小端；显式编码，不使用编译器结构体布局）::

    头部 64 字节
      0  8  magic         b'ETMNAT01'
      8  2  schemaVersion 1
      10 2  headerBytes   64
      12 4  byteOrder     0x01020304（小端标记；读到别的值即拒绝）
      16 4  protocol      1=ETMv4，2=ETMv3
      20 4  featureFlags  bit0=稀有元素带说明文字；其余位未定义，出现即拒绝
      24 8  recordCount   元素+包记录数（收尾回填）
      32 8  elementCount
      40 8  packetCount
      48 8  bodyBytes     头部之后的全部字节数
      56 8  reserved      0

    记录（每条以 5 字节记录头开始）
      0  1  kind       1=元素，2=包，3=状态
      1  4  recordBytes 整条记录长度（含本记录头）

    元素记录（kind=1，固定 61 字节 + 文字）
      5  1  elemType   OCSD_GEN_TRC_ELEM_*（0..19；未知值不解释，与文本模式同样忽略）
      6  1  isa        仅范围元素非零（已按文本模式归零）
      7  1  id
      8  1  lastExecuted     仅范围元素非零（0/1）
      9  1  lastConditional  仅范围元素非零（0/1）
      10 1  flags8     bit0 has_cc，bit1 has_ts，bit2 excep_ret_addr，bit3 has_text
      11 4  flags      e->flag_bits
      15 4  cycleCount has_cc 时的周期数（否则 0）
      19 4  payload    范围=num_instr_range，异常=exception_number，
                       NO_SYNC/EO_TRACE=unsync_eot_info，TRACE_ON=trace_on_reason，其余 0
      23 2  lastType   OCSD_INSTR_*（仅范围元素非零）
      25 2  lastSubtype
      27 8  offset     ocsd_trc_index_t
      35 8  start      st_addr（仅范围/ADDR_NACC 非零）
      43 8  end        en_addr（仅范围/带返回地址的异常非零）
      51 8  timestamp  has_ts 或 TIMESTAMP 元素时的值（否则 0）
      59 2  textBytes  稀有类型的说明文字字节数，其余 0
      61 .. text        UTF-8（ASCII）说明文字

    包记录（kind=2，固定 22 字节 + 原始字节）
      5  4  pktType    包类型码（= OpenCSD 包结构首字段枚举，ETMv4 如 0x01 / 0x200）
      9  1  flags8     保留（当前为 0）
      10 8  offset
      18 4  size
      22 .. raw        原始包字节（可重算十六进制）

    状态记录（kind=3，固定 37 字节 + 两个字符串，必须是最后一条）
      5  4  processed       OpenCSD 已处理字节数
      9  4  lastErrorCode   有符号错误码
      13 4  libraryBytes
      17 4  lastErrorBytes
      21 8  elementCount   与头部交叉核对
      29 8  packetCount
      37 .. library        库版本字符串（ASCII）
      .. .. lastError      最后一个错误文本（ASCII）

语义等价约定（P2 证据逐项验证过）：

* 元素字段与旧文本 JSON 的取值规则完全相同（范围/异常/存在位归零规则由两模式共享同一 C++
  `make_view()`），因此逐字段可直接对照，没有“按枚举重写诊断含义”。
* 包故障分类只依赖 `ocsd_pkt_str()` 文本：`I_NOT_SYNC`/`NOTSYNC` 前缀算未同步，
  `BAD_`/`RESERVED`/`OVERFLOW`/`INCOMPLETE_EOT` 子串算无效（`OVERFLOW` 另计溢出处数）。
  该文本 = 包类型名 + " : " + 描述 + 可选扩展字段；只有类型名可能命中上述标记，
  所以用**逐字抄自 OpenCSD 1.8.3** 的包类型名表（见下方两个字典及出处）就能复现分类。
  `classify_packet_text()` 保留旧文本口径，`classify_packet()` 走类型码，两者在测试里
  对全部真实输入逐包比对。
"""
import struct
from pathlib import Path

MAGIC = b'ETMNAT01'
SCHEMA_VERSION = 1
HEADER_BYTES = 64
BYTE_ORDER_MARK = 0x01020304
FEATURE_RARE_ELEMENT_TEXT = 1 << 0
FEATURE_MASK = FEATURE_RARE_ELEMENT_TEXT
RECORD_ELEMENT = 1
RECORD_PACKET = 2
RECORD_STATUS = 3
PROTOCOLS = {1: 'ETMv4', 2: 'ETMv3'}
PROTOCOL_CODES = {name: code for code, name in PROTOCOLS.items()}

# events.jsonl 的 text 字段确实需要的元素类型（与 decode_stream 展开集合一致）：
# NO_SYNC / TRACE_ON / EO_TRACE / I_RANGE_NOPATH / ADDR_NACC / ADDR_UNKNOWN /
# EXCEPTION / EXCEPTION_RET。其余元素类型在二进制模式不再生成说明文字。
RARE_TEXT_TYPES = (1, 2, 3, 6, 7, 8, 9, 10)

# 包故障分类位（对应旧文本口径的三个计数：unsync_records / invalid / overflow）。
PACKET_UNSYNC = 1 << 0
PACKET_INVALID = 1 << 1
PACKET_OVERFLOW = 1 << 2
FAULT_TERMS = ('BAD_', 'RESERVED', 'OVERFLOW', 'INCOMPLETE_EOT')
UNSYNC_PREFIXES = ('I_NOT_SYNC', 'NOTSYNC')

HEADER = struct.Struct('<8sHHIIIQQQQQ')
RECORD_HEADER = struct.Struct('<BI')
ELEMENT = struct.Struct('<BBBBBBIIIHHQQQQH')
PACKET = struct.Struct('<IBQI')
STATUS = struct.Struct('<IiIIQQ')

assert HEADER.size == HEADER_BYTES
assert ELEMENT.size == 56          # 56 + 5 字节记录头 = 61
assert PACKET.size == 17           # 17 + 5 = 22
assert STATUS.size == 32           # 32 + 5 = 37

# OpenCSD 1.8.3 包类型名（逐字抄自 decoder/source/etmv4/trc_pkt_elem_etmv4i.cpp
# EtmV4ITrcPacket::packetTypeName()，未列出的类型缺省名是 "I_UNKNOWN"）。
ETMV4_PACKET_NAMES = {
    0x200: 'I_NOT_SYNC',
    0x201: 'I_INCOMPLETE_EOT',
    0x202: 'I_NO_ERR_TYPE',
    0x300: 'I_BAD_SEQUENCE',
    0x301: 'I_BAD_TRACEMODE',
    0x302: 'I_RESERVED',
    0x303: 'I_RESERVED_CFG',
    0x00: 'I_EXTENSION',
    0x01: 'I_TRACE_INFO',
    0x02: 'I_TIMESTAMP',
    0x04: 'I_TRACE_ON',
    0x05: 'I_FUNC_RET',
    0x06: 'I_EXCEPT',
    0x07: 'I_EXCEPT_RTN',
    0x09: 'I_ITE',
    0x0A: 'I_TRANS_ST',
    0x0B: 'I_TRANS_COMMIT',
    0x0C: 'I_CCNT_F2',
    0x0E: 'I_CCNT_F1',
    0x10: 'I_CCNT_F3',
    0x20: 'I_NUM_DS_MKR',
    0x28: 'I_UNNUM_DS_MKR',
    0x2D: 'I_COMMIT',
    0x2E: 'I_CANCEL_F1',
    0x2F: 'I_CANCEL_F1_MISPRED',
    0x30: 'I_MISPREDICT',
    0x34: 'I_CANCEL_F2',
    0x38: 'I_CANCEL_F3',
    0x40: 'I_COND_I_F2',
    0x43: 'I_COND_FLUSH',
    0x44: 'I_COND_RES_F4',
    0x48: 'I_COND_RES_F2',
    0x50: 'I_COND_RES_F3',
    0x68: 'I_COND_RES_F1',
    0x6C: 'I_COND_I_F1',
    0x6D: 'I_COND_I_F3',
    0x70: 'I_IGNORE',
    0x71: 'I_EVENT',
    0x80: 'I_CTXT',
    0x82: 'I_ADDR_CTXT_L_32IS0',
    0x83: 'I_ADDR_CTXT_L_32IS1',
    0x85: 'I_ADDR_CTXT_L_64IS0',
    0x86: 'I_ADDR_CTXT_L_64IS1',
    0x88: 'I_TS_MARKER',
    0x90: 'I_ADDR_MATCH',
    0x95: 'I_ADDR_S_IS0',
    0x96: 'I_ADDR_S_IS1',
    0x9A: 'I_ADDR_L_32IS0',
    0x9B: 'I_ADDR_L_32IS1',
    0x9D: 'I_ADDR_L_64IS0',
    0x9E: 'I_ADDR_L_64IS1',
    0xA0: 'I_Q',
    0xB0: 'I_SRC_ADDR_MATCH',
    0xB4: 'I_SRC_ADDR_S_IS0',
    0xB5: 'I_SRC_ADDR_S_IS1',
    0xB6: 'I_SRC_ADDR_L_32IS0',
    0xB7: 'I_SRC_ADDR_L_32IS1',
    0xB8: 'I_SRC_ADDR_L_64IS0',
    0xB9: 'I_SRC_ADDR_L_64IS1',
    0xC0: 'I_ATOM_F6',
    0xD5: 'I_ATOM_F5',
    0xD8: 'I_ATOM_F2',
    0xDC: 'I_ATOM_F4',
    0xF6: 'I_ATOM_F1',
    0xF8: 'I_ATOM_F3',
    0x100: 'I_ASYNC',
    0x103: 'I_DISCARD',
    0x105: 'I_OVERFLOW',
    0x400: 'I_PE_RESET',
    0x401: 'I_TRANS_FAIL',
}
ETMV4_DEFAULT_NAME = 'I_UNKNOWN'

# OpenCSD 1.8.3 ETMv3 包类型名（逐字抄自 decoder/source/etmv3/trc_pkt_elem_etmv3.cpp
# EtmV3TrcPacket::packetTypeName()；缺省名是 "I_RESERVED"，NOERROR/BRANCH_OR_BYPASS_EOT
# 走缺省分支——与库本身一致，因此保留该行为而不是另做解释）。
ETMV3_PACKET_NAMES = {
    1: 'NOTSYNC',
    2: 'INCOMPLETE_EOT.',
    3: 'BRANCH_ADDRESS',
    4: 'A_SYNC',
    5: 'CYCLE_COUNT',
    6: 'I_SYNC',
    7: 'I_SYNC_CYCLE',
    8: 'TRIGGER',
    9: 'P_HDR',
    10: 'STORE_FAIL',
    11: 'OOO_DATA',
    12: 'OOO_ADDR_PLC',
    13: 'NORM_DATA',
    14: 'DATA_SUPPRESSED',
    15: 'VAL_NOT_TRACED',
    16: 'IGNORE',
    17: 'CONTEXT_ID',
    18: 'VMID',
    19: 'EXCEPTION_ENTRY',
    20: 'EXCEPTION_EXIT',
    21: 'TIMESTAMP',
    23: 'BAD_SEQUENCE',
    24: 'BAD_TRACEMODE',
}
ETMV3_DEFAULT_NAME = 'I_RESERVED'


class NativeBinaryError(ValueError):
    """二进制记录流不合法（版本/长度/截断/未知记录/计数不符）。"""


def packet_name(type_code, protocol='ETMv4'):
    """包类型码 → OpenCSD 包类型名（分类只依赖该名字）。"""
    if protocol == 'ETMv3':
        return ETMV3_PACKET_NAMES.get(int(type_code), ETMV3_DEFAULT_NAME)
    return ETMV4_PACKET_NAMES.get(int(type_code), ETMV4_DEFAULT_NAME)


def classify_packet_text(text):
    """旧文本口径的包故障分类（返回 PACKET_* 位）。"""
    upper = str(text).upper()
    flags = 0
    if str(text).startswith(UNSYNC_PREFIXES):
        flags |= PACKET_UNSYNC
    if any(term in upper for term in FAULT_TERMS):
        flags |= PACKET_INVALID
    if 'OVERFLOW' in upper:
        flags |= PACKET_OVERFLOW
    return flags


def classify_packet(type_code, protocol='ETMv4'):
    """类型码口径的包故障分类；与 classify_packet_text(包文本) 逐包等价（见测试）。"""
    return classify_packet_text(packet_name(type_code, protocol))


def read_header(path):
    """只读并校验 64 字节头部，返回字段字典；不合法直接抛 NativeBinaryError。"""
    path = Path(path)
    try:
        with path.open('rb') as stream:
            raw = stream.read(HEADER_BYTES)
            size = path.stat().st_size
    except FileNotFoundError:
        raise NativeBinaryError('原生二进制文件不存在：' + str(path))
    return _parse_header(raw, size)


def _parse_header(raw, file_size):
    if len(raw) < HEADER_BYTES:
        raise NativeBinaryError(f'原生二进制头部被截断：{len(raw)} < {HEADER_BYTES}')
    (magic, version, header_bytes, byte_order, protocol, features,
     record_count, element_count, packet_count, body_bytes, _reserved) = HEADER.unpack(raw)
    if magic != MAGIC:
        raise NativeBinaryError('原生二进制 magic 不符：' + repr(magic))
    if version != SCHEMA_VERSION:
        raise NativeBinaryError(f'不支持的原生二进制版本：{version}（本读取器支持 {SCHEMA_VERSION}）')
    if header_bytes != HEADER_BYTES:
        raise NativeBinaryError(f'头部长度不符：{header_bytes} != {HEADER_BYTES}')
    if byte_order != BYTE_ORDER_MARK:
        raise NativeBinaryError(f'字节序标记不符：0x{byte_order:08x}')
    if protocol not in PROTOCOLS:
        raise NativeBinaryError(f'未知协议码：{protocol}')
    if features & ~FEATURE_MASK:
        raise NativeBinaryError(f'存在未定义的强制 featureFlags：0x{features:08x}')
    if file_size < HEADER_BYTES:
        raise NativeBinaryError('原生二进制文件短于头部')
    if body_bytes != file_size - HEADER_BYTES:
        raise NativeBinaryError(
            f'bodyBytes 与文件长度不符：{body_bytes} != {file_size - HEADER_BYTES}（截断或混入其它数据）')
    return dict(version=version, headerBytes=header_bytes, protocol=PROTOCOLS[protocol],
                protocolCode=protocol, features=features, recordCount=record_count,
                elementCount=element_count, packetCount=packet_count, bodyBytes=body_bytes,
                fileBytes=file_size)


class NativeFile:
    """一个已校验的原生二进制文件；`records()` 逐条产出展开需要的元组。

    元组形状（与 decode_stream 的文本解析结果一一对应）：

    * 元素 ``(RECORD_ELEMENT, etype, offset, id, start, end, isa, flags, cycleCount, has_cc,
      timestamp, has_ts, lastExecuted, lastConditional, lastType, lastSubtype, payload, text)``；
      start/end/isa/cycleCount/timestamp/last* 已是与文本 JSON 相同的归一化值，
      has_cc/has_ts 是原存在位，text 只在 RARE_TEXT_TYPES 存在时非空。
    * 包 ``(RECORD_PACKET, offset, size, typeCode)``。
    * 状态 ``(RECORD_STATUS, dict(kind/processed/library/lastErrorCode/lastError))``，
      键顺序与旧文本 JSON 相同，保证 decoded.json 逐字节可比。
    """

    def __init__(self, path):
        self.path = Path(path)
        try:
            self.payload = self.path.read_bytes()
        except FileNotFoundError:
            raise NativeBinaryError('原生二进制文件不存在：' + str(self.path))
        self.meta = _parse_header(self.payload[:HEADER_BYTES], len(self.payload))
        self.protocol = self.meta['protocol']
        self.status = None
        self._finished = False

    def records(self, with_payload=False):
        """逐条产出记录；任何版本/长度/截断/未知记录/计数不符都抛 NativeBinaryError。

        `with_payload=True` 时包记录额外带原始字节（`(kind, offset, size, typeCode, raw)`），
        供逐字节对照/诊断使用；展开热路径用默认的轻量元组。
        """
        if self._finished:
            raise NativeBinaryError('记录流只能读取一次')
        payload = self.payload
        end = len(payload)
        position = HEADER_BYTES
        elements = packets = 0
        status = None
        while position < end:
            if end - position < RECORD_HEADER.size:
                raise NativeBinaryError(f'记录头被截断：偏移 {position}（文件 {end} 字节）')
            kind, record_bytes = RECORD_HEADER.unpack_from(payload, position)
            if kind not in (RECORD_ELEMENT, RECORD_PACKET, RECORD_STATUS):
                raise NativeBinaryError(f'未知记录类型 {kind}（偏移 {position}）')
            minimum = {RECORD_ELEMENT: 61, RECORD_PACKET: 22, RECORD_STATUS: 37}[kind]
            if record_bytes < minimum:
                raise NativeBinaryError(
                    f'记录长度 {record_bytes} 小于 {kind} 类最小长度 {minimum}（偏移 {position}）')
            if position + record_bytes > end:
                raise NativeBinaryError(
                    f'记录被截断：偏移 {position} 需要 {record_bytes} 字节，仅剩 {end - position}')
            if status is not None:
                raise NativeBinaryError(f'状态记录之后仍有记录（偏移 {position}）')
            body = position + RECORD_HEADER.size
            if kind == RECORD_PACKET:
                type_code, _flags, offset, size = PACKET.unpack_from(payload, body)
                if size != record_bytes - 22:
                    raise NativeBinaryError(
                        f'包长度与记录长度不符：size={size} recordBytes={record_bytes}')
                packets += 1
                if with_payload:
                    yield (RECORD_PACKET, offset, size, type_code,
                           payload[body + PACKET.size:body + PACKET.size + size])
                else:
                    yield (RECORD_PACKET, offset, size, type_code)
            elif kind == RECORD_ELEMENT:
                (etype, isa, elem_id, last_exec, last_cond, flags8, flags, cycle, payload32,
                 last_type, last_subtype, offset, start, stop, timestamp,
                 text_bytes) = ELEMENT.unpack_from(payload, body)
                if text_bytes != record_bytes - 61:
                    raise NativeBinaryError(
                        f'元素文字长度与记录长度不符：textBytes={text_bytes} recordBytes={record_bytes}')
                if last_exec > 1 or last_cond > 1:
                    raise NativeBinaryError(f'非法的 lastExecuted/lastConditional：{last_exec}/{last_cond}')
                if bool(flags8 & 0x08) != bool(text_bytes):
                    raise NativeBinaryError('元素 has_text 标志与文字长度不一致')
                text = ''
                if text_bytes:
                    try:
                        text = payload[body + ELEMENT.size:body + ELEMENT.size + text_bytes].decode('utf-8')
                    except UnicodeDecodeError:
                        raise NativeBinaryError(f'元素说明文字不是 UTF-8（偏移 {position}）')
                elements += 1
                yield (RECORD_ELEMENT, etype, offset, elem_id, start, stop, isa, flags, cycle,
                       bool(flags8 & 0x01), timestamp, bool(flags8 & 0x02), last_exec,
                       last_cond, last_type, last_subtype, payload32, text)
            else:
                (processed, error_code, library_bytes, error_bytes, elem_count,
                 packet_count) = STATUS.unpack_from(payload, body)
                if record_bytes != 37 + library_bytes + error_bytes:
                    raise NativeBinaryError('状态记录长度与字符串长度不符')
                strings = body + STATUS.size
                library = payload[strings:strings + library_bytes].decode('utf-8', 'replace')
                error = payload[strings + library_bytes:strings + library_bytes + error_bytes] \
                    .decode('utf-8', 'replace')
                if position + record_bytes != end:
                    raise NativeBinaryError('状态记录不是最后一条记录')
                if (elem_count, packet_count) != (elements, packets):
                    raise NativeBinaryError(
                        f'状态记录计数不符：{elem_count}/{packet_count} != {elements}/{packets}')
                status = dict(kind='status', processed=processed, library=library,
                              lastErrorCode=error_code, lastError=error)
                self.status = status
                yield (RECORD_STATUS, status)
            position += record_bytes
        self._finished = True
        if status is None:
            raise NativeBinaryError('没有状态记录：文件不完整（解码可能中断）')
        meta = self.meta
        if (meta['elementCount'], meta['packetCount']) != (elements, packets):
            raise NativeBinaryError(
                f'头部计数不符：{meta["elementCount"]}/{meta["packetCount"]} != {elements}/{packets}')
        if meta['recordCount'] != elements + packets:
            raise NativeBinaryError(
                f'头部 recordCount 不符：{meta["recordCount"]} != {elements + packets}')
