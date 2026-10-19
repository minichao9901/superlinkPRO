"""原生二进制记录（trace-bin native v1）读取器/分类器单测。

覆盖：版本/magic/字节序/featureFlags/长度/截断/未知记录/计数不符必须拒绝；
元素字段与 has_cc/has_ts 存在位原样读出；包类型码→故障分类与旧文本口径逐项一致。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_binary as nb  # noqa: E402


def element_record(etype, *, offset=0, elem_id=0, start=0, end=0, isa=0, flags=0, cycle=0,
                   has_cc=False, timestamp=0, has_ts=False, last_exec=0, last_cond=0,
                   last_type=0, last_subtype=0, payload=0, text='', flags8_extra=0):
    raw = text.encode('utf-8')
    flags8 = (1 if has_cc else 0) | (2 if has_ts else 0) | (8 if raw else 0) | flags8_extra
    body = nb.ELEMENT.pack(etype, isa, elem_id, last_exec, last_cond, flags8, flags, cycle,
                           payload, last_type, last_subtype, offset, start, end, timestamp,
                           len(raw))
    return nb.RECORD_HEADER.pack(nb.RECORD_ELEMENT, 61 + len(raw)) + body + raw


def packet_record(type_code, raw=b'\x00', offset=0):
    raw = bytes(raw)
    return (nb.RECORD_HEADER.pack(nb.RECORD_PACKET, 22 + len(raw))
            + nb.PACKET.pack(type_code, 0, offset, len(raw)) + raw)


def status_record(processed=0, code=0, library='1.8.3', error='', elements=0, packets=0):
    lib, err = library.encode('utf-8'), error.encode('utf-8')
    body = nb.STATUS.pack(processed, code, len(lib), len(err), elements, packets)
    return nb.RECORD_HEADER.pack(nb.RECORD_STATUS, 37 + len(lib) + len(err)) + body + lib + err


def build(records, *, protocol=1, features=nb.FEATURE_RARE_ELEMENT_TEXT, counts=None,
          body_bytes=None):
    kinds = [record[0] for record in records]
    elements = kinds.count(nb.RECORD_ELEMENT)
    packets = kinds.count(nb.RECORD_PACKET)
    record_count = elements + packets
    if counts is not None:
        record_count, elements, packets = counts
    body = b''.join(records)
    header = nb.HEADER.pack(nb.MAGIC, nb.SCHEMA_VERSION, nb.HEADER_BYTES, nb.BYTE_ORDER_MARK,
                            protocol, features, record_count, elements, packets,
                            len(body) if body_bytes is None else body_bytes, 0)
    return header + body


def sample_file(*, protocol=1):
    """一条元素 + 一个包 + 状态记录的最小合法文件。"""
    element = element_record(
        5, offset=64, elem_id=1, start=0x08000100, end=0x08000106, isa=1, flags=0x0000004F,
        cycle=7, has_cc=True, timestamp=99, has_ts=True, last_exec=1, last_cond=1,
        last_type=2, last_subtype=3, payload=3)
    packet = packet_record(0x01, b'\x01\x02', offset=0)
    status = status_record(processed=2, code=0, elements=1, packets=1)
    return build([element, packet, status], protocol=protocol)


class HeaderValidationTests(unittest.TestCase):
    def write(self, directory, data):
        path = Path(directory) / 'native.bin'
        path.write_bytes(data)
        return path

    def assert_rejected(self, data, pattern):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, data)
            with self.assertRaisesRegex(nb.NativeBinaryError, pattern):
                list(nb.NativeFile(path).records())

    def test_valid_minimal_file(self):
        data = sample_file()
        with tempfile.TemporaryDirectory() as directory:
            native = nb.NativeFile(self.write(directory, data))
            self.assertEqual(native.protocol, 'ETMv4')
            records = list(native.records())
            self.assertEqual([record[0] for record in records],
                             [nb.RECORD_ELEMENT, nb.RECORD_PACKET, nb.RECORD_STATUS])

    def test_element_fields_and_presence_bits(self):
        with tempfile.TemporaryDirectory() as directory:
            native = nb.NativeFile(self.write(directory, sample_file()))
            element = next(native.records())
            self.assertEqual(element, (
                nb.RECORD_ELEMENT, 5, 64, 1, 0x08000100, 0x08000106, 1, 0x0000004F, 7,
                True, 99, True, 1, 1, 2, 3, 3, ''))

    def test_element_text_roundtrip(self):
        text = 'I_NOT_SYNC : I Stream not synchronised; Reason=Overflow'
        record = element_record(1, payload=3, text=text)
        data = build([record, status_record(elements=1)])
        with tempfile.TemporaryDirectory() as directory:
            native = nb.NativeFile(self.write(directory, data))
            element = next(native.records())
            self.assertEqual(element[17], text)

    def test_bad_magic_rejected(self):
        data = bytearray(sample_file())
        data[0:8] = b'NOTMAGIC'
        self.assert_rejected(bytes(data), 'magic')

    def test_unsupported_version_rejected(self):
        data = bytearray(sample_file())
        data[8:10] = (2).to_bytes(2, 'little')
        self.assert_rejected(bytes(data), '版本')

    def test_header_length_mismatch_rejected(self):
        data = bytearray(sample_file())
        data[10:12] = (32).to_bytes(2, 'little')
        self.assert_rejected(bytes(data), '头部长度')

    def test_byte_order_mark_rejected(self):
        data = bytearray(sample_file())
        data[12:16] = (0x04030201).to_bytes(4, 'little')
        self.assert_rejected(bytes(data), '字节序')

    def test_unknown_protocol_rejected(self):
        data = bytearray(sample_file())
        data[16:20] = (9).to_bytes(4, 'little')
        self.assert_rejected(bytes(data), '未知协议码')

    def test_unknown_feature_flags_rejected(self):
        data = bytearray(sample_file())
        data[20:24] = (0x80000001).to_bytes(4, 'little')
        self.assert_rejected(bytes(data), 'featureFlags')

    def test_truncated_header_rejected(self):
        self.assert_rejected(bytes(sample_file()[:40]), '头部被截断')

    def test_body_bytes_mismatch_rejected(self):
        data = bytearray(sample_file())
        data[48:56] = (len(data) - nb.HEADER_BYTES + 3).to_bytes(8, 'little')
        self.assert_rejected(bytes(data), 'bodyBytes')

    def test_missing_file_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(nb.NativeBinaryError, '不存在'):
                nb.NativeFile(Path(directory) / 'none.bin')


class RecordValidationTests(unittest.TestCase):
    def assert_rejected(self, data, pattern):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'native.bin'
            path.write_bytes(bytes(data))
            with self.assertRaisesRegex(nb.NativeBinaryError, pattern):
                list(nb.NativeFile(path).records())

    def test_truncated_last_record_rejected(self):
        self.assert_rejected(sample_file()[:-5], '截断')

    def test_unknown_record_kind_rejected(self):
        records = [element_record(1, text='x'), status_record(elements=1)]
        data = bytearray(build(records))
        data[64] = 9                      # 第一条记录 kind=9
        self.assert_rejected(data, '未知记录类型')

    def test_record_length_below_minimum_rejected(self):
        records = [element_record(5), status_record(elements=1)]
        data = bytearray(build(records))
        data[65:69] = (10).to_bytes(4, 'little')
        self.assert_rejected(data, '小于')

    def test_record_length_past_end_rejected(self):
        records = [element_record(5), status_record(elements=1)]
        data = bytearray(build(records))
        data[65:69] = (10 ** 6).to_bytes(4, 'little')
        self.assert_rejected(data, '截断')

    def test_element_text_length_mismatch_rejected(self):
        records = [element_record(1, text='abc'), status_record(elements=1)]
        data = bytearray(build(records))
        data[64 + 5 + nb.ELEMENT.size - 2:64 + 5 + nb.ELEMENT.size] = (9).to_bytes(2, 'little')
        self.assert_rejected(data, '文字长度')

    def test_element_text_flag_mismatch_rejected(self):
        # 没有文字却声明 has_text（bit3）
        records = [element_record(5, flags8_extra=8), status_record(elements=1)]
        self.assert_rejected(build(records), 'has_text')

    def test_illegal_boolean_flag_rejected(self):
        records = [element_record(5, last_exec=2), status_record(elements=1)]
        self.assert_rejected(build(records), 'lastExecuted')

    def test_packet_size_mismatch_rejected(self):
        records = [packet_record(0x01, b'\x01\x02'), status_record(packets=1)]
        data = bytearray(build(records))
        data[64 + 10 + 8:64 + 10 + 12] = (7).to_bytes(4, 'little')   # size 字段
        self.assert_rejected(data, '包长度')

    def test_unknown_element_type_is_passed_through(self):
        records = [element_record(200, payload=5), status_record(elements=1)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'native.bin'
            path.write_bytes(build(records))
            records_out = list(nb.NativeFile(path).records())
            self.assertEqual(records_out[0][1], 200)

    def test_missing_status_record_rejected(self):
        self.assert_rejected(build([element_record(5)]), '没有状态记录')

    def test_status_must_be_last(self):
        self.assert_rejected(build([status_record(), element_record(5)]), '状态记录')

    def test_header_count_mismatch_rejected(self):
        records = [element_record(5), status_record(elements=1)]
        self.assert_rejected(build(records, counts=(5, 4, 1)), '头部计数')

    def test_header_record_count_mismatch_rejected(self):
        records = [element_record(5), status_record(elements=1)]
        self.assert_rejected(build(records, counts=(5, 1, 0)), 'recordCount')

    def test_status_count_mismatch_rejected(self):
        records = [element_record(5), status_record(elements=2)]
        self.assert_rejected(build(records), '状态记录计数')

    def test_etmv3_protocol_recorded(self):
        data = build([element_record(5), status_record(elements=1)], protocol=2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'native.bin'
            path.write_bytes(data)
            self.assertEqual(nb.NativeFile(path).protocol, 'ETMv3')


class ClassificationTests(unittest.TestCase):
    def test_etmv4_type_codes_match_text_rules(self):
        for code, name in nb.ETMV4_PACKET_NAMES.items():
            self.assertEqual(nb.classify_packet(code, 'ETMv4'),
                             nb.classify_packet_text(name), name)

    def test_etmv3_type_codes_match_text_rules(self):
        for code, name in nb.ETMV3_PACKET_NAMES.items():
            self.assertEqual(nb.classify_packet(code, 'ETMv3'),
                             nb.classify_packet_text(name), name)

    def test_fault_categories(self):
        cases = {
            'I_NOT_SYNC : I Stream not synchronised': nb.PACKET_UNSYNC,
            'NOTSYNC : Trace Stream not synchronised': nb.PACKET_UNSYNC,
            'I_BAD_SEQUENCE : Invalid Sequence in packet.[I_ATOM_F1]': nb.PACKET_INVALID,
            'I_BAD_TRACEMODE : Invalid Packet for trace mode.[I_ATOM_F1]': nb.PACKET_INVALID,
            'I_RESERVED : Reserved Packet Header': nb.PACKET_INVALID,
            'I_RESERVED_CFG : Reserved header for current configuration.[I_CTXT]': nb.PACKET_INVALID,
            'I_INCOMPLETE_EOT : Incomplete packet at end of trace.[I_ATOM_F1]': nb.PACKET_INVALID,
            'I_OVERFLOW : Overflow.': nb.PACKET_INVALID | nb.PACKET_OVERFLOW,
            'I_ATOM_F1 : Atom format 1.; Atom: E': 0,
        }
        for text, expected in cases.items():
            self.assertEqual(nb.classify_packet_text(text), expected, text)

    def test_type_code_path_matches_fault_categories(self):
        self.assertEqual(nb.classify_packet(0x200, 'ETMv4'), nb.PACKET_UNSYNC)
        self.assertEqual(nb.classify_packet(0x300, 'ETMv4'), nb.PACKET_INVALID)
        self.assertEqual(nb.classify_packet(0x302, 'ETMv4'), nb.PACKET_INVALID)
        self.assertEqual(nb.classify_packet(0x105, 'ETMv4'),
                         nb.PACKET_INVALID | nb.PACKET_OVERFLOW)
        self.assertEqual(nb.classify_packet(0x201, 'ETMv4'), nb.PACKET_INVALID)
        self.assertEqual(nb.classify_packet(0x01, 'ETMv4'), 0)
        self.assertEqual(nb.classify_packet(1, 'ETMv3'), nb.PACKET_UNSYNC)
        self.assertEqual(nb.classify_packet(2, 'ETMv3'), nb.PACKET_INVALID)
        self.assertEqual(nb.classify_packet(23, 'ETMv3'), nb.PACKET_INVALID)
        self.assertEqual(nb.classify_packet(13, 'ETMv3'), 0)

    def test_unknown_type_code_uses_library_default_name(self):
        self.assertEqual(nb.packet_name(0x7FFF, 'ETMv4'), nb.ETMV4_DEFAULT_NAME)
        self.assertEqual(nb.classify_packet(0x7FFF, 'ETMv4'), 0)
        self.assertEqual(nb.packet_name(200, 'ETMv3'), nb.ETMV3_DEFAULT_NAME)
        self.assertEqual(nb.classify_packet(200, 'ETMv3'), nb.PACKET_INVALID)

    def test_rare_text_types_cover_event_element_types(self):
        # events.jsonl 使用说明文字的 8 种元素类型必须与展开代码一致。
        self.assertEqual(nb.RARE_TEXT_TYPES, (1, 2, 3, 6, 7, 8, 9, 10))


if __name__ == '__main__':
    unittest.main()
