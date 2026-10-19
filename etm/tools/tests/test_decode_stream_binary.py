"""decode_stream 二进制开关的端到端等价测试（真实 10 ms 记录，ETMv4 + ETMv3）。

断言：默认文本模式与 `ETM_NATIVE_FORMAT=binary` 的 events.jsonl/sources.json 逐字节一致，
decoded.json 的 summary/quality/analysis/status 一致（只有 streamed.nativeJsonl 记为
native.bin），sidecar 除 mtime 外一致；二进制模式不生成 packets.jsonl/elements.jsonl；
损坏的二进制文件与 legacy 引擎组合必须报错而不是静默降级。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_binary  # noqa: E402
import decode_stream  # noqa: E402
from decode_trace import Image  # noqa: E402

ETM_ROOT = Path(__file__).resolve().parents[2]
DECODER = ETM_ROOT / 'build-native/etm_decode.exe'
RECORD_ETMV4 = ETM_ROOT / 'records/capture-4395612c313f47ce97af1e1c17e0a0d5'
RECORD_ETMV3 = ETM_ROOT / 'records/capture-2d6da5829a8c4811b7c5ed2ca9e08cc3'


def sidecar_body(path):
    """browse-columns.tsv 第一行含 events.jsonl 的 mtime，跨运行必然不同；其余字节应相同。"""
    with Path(path).open('rb') as stream:
        stream.readline()
        return stream.read()


def sidecar_header(path):
    with Path(path).open('rb') as stream:
        return stream.readline()


class BinaryExpansionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DECODER.is_file():
            raise unittest.SkipTest('缺少 build-native/etm_decode.exe，请先运行 build-decoder.ps1')

    def config(self, record):
        return json.loads((record / 'decoded.json').read_text(encoding='utf-8'))['config']

    def run_pipeline(self, record, workdir, fmt, engine='fast'):
        environment = {'ETM_NATIVE_FORMAT': fmt, 'ETM_DECODE_ENGINE': engine}
        with patch.dict(os.environ, environment):
            return decode_stream.decode_stream((record / 'etm.bin').read_bytes(),
                                               str(record / 'firmware.elf'), workdir,
                                               self.config(record), None, None, 0)

    def assert_equivalent(self, record, tag):
        self.assertTrue((record / 'etm.bin').is_file(), '缺少测试输入 ' + str(record))
        self.assertTrue((record / 'firmware.elf').is_file(), '缺少测试输入 ' + str(record))
        with tempfile.TemporaryDirectory() as directory:
            text_dir, binary_dir = Path(directory) / 'text', Path(directory) / 'binary'
            text_result = self.run_pipeline(record, text_dir, 'text')
            binary_result = self.run_pipeline(record, binary_dir, 'binary')

            self.assertEqual((text_dir / 'events.jsonl').read_bytes(),
                             (binary_dir / 'events.jsonl').read_bytes(), tag)
            self.assertEqual((text_dir / 'sources.json').read_bytes(),
                             (binary_dir / 'sources.json').read_bytes(), tag)
            self.assertEqual(sidecar_body(text_dir / decode_stream.SIDECAR_NAME),
                             sidecar_body(binary_dir / decode_stream.SIDECAR_NAME), tag)
            self.assertEqual(sidecar_header(text_dir / decode_stream.SIDECAR_NAME).split(
                b'mtime=')[0], sidecar_header(binary_dir / decode_stream.SIDECAR_NAME).split(
                b'mtime=')[0], tag)

            # 文本模式仍是完整 7 项产物；二进制模式不再生成 packets/elements/native.jsonl。
            for name in ('packets.jsonl', 'elements.jsonl', 'native.jsonl'):
                self.assertTrue((text_dir / name).is_file(), name)
                self.assertFalse((binary_dir / name).exists(), name)
            self.assertTrue((binary_dir / 'native.bin').is_file())
            native = native_binary.NativeFile(binary_dir / 'native.bin')
            self.assertEqual(native.protocol, text_result['protocol'])
            records = list(native.records())
            self.assertEqual(len(records), native.meta['recordCount'] + 1)   # +1 = 状态记录
            self.assertEqual(text_result['streamed']['packets'],
                             native.meta['packetCount'])
            self.assertEqual(text_result['streamed']['elements'],
                             native.meta['elementCount'])

            text = json.loads((text_dir / 'decoded.json').read_text(encoding='utf-8'))
            binary = json.loads((binary_dir / 'decoded.json').read_text(encoding='utf-8'))
            for key in ('summary', 'quality', 'analysis', 'status', 'inputBytes', 'protocol',
                        'config', 'elf', 'version', 'timeBasis'):
                self.assertEqual(text[key], binary[key], f'{tag}:{key}')
            self.assertEqual(dict(text['streamed'], nativeJsonl='native.bin'),
                             binary['streamed'])
            self.assertEqual(native.status['processed'], text['status']['processed'])
            return text, binary

    def test_etmv4_10ms_equivalence(self):
        text, binary = self.assert_equivalent(RECORD_ETMV4, 'ETMv4')
        self.assertGreater(text['summary']['instructions'], 0)
        self.assertGreater(text['quality']['packetIssues'], 0)   # 记录含 INCOMPLETE_EOT

    def test_etmv3_10ms_equivalence(self):
        text, binary = self.assert_equivalent(RECORD_ETMV3, 'ETMv3')
        self.assertEqual(text['protocol'], 'ETMv3')
        self.assertGreater(text['quality']['unsynchronizedRecords'], 0)   # 记录含 NOTSYNC

    def test_binary_element_records_match_text_native_lines(self):
        """同一输入：文本 native.jsonl 与二进制记录逐元素/逐包对照（字段 + 顺序 + 分类）。"""
        if not (RECORD_ETMV4 / 'etm.bin').is_file():
            self.skipTest('缺少测试输入')
        with tempfile.TemporaryDirectory() as directory:
            text_dir, binary_dir = Path(directory) / 'text', Path(directory) / 'binary'
            self.run_pipeline(RECORD_ETMV4, text_dir, 'text')
            self.run_pipeline(RECORD_ETMV4, binary_dir, 'binary')
            native = native_binary.NativeFile(binary_dir / 'native.bin')
            records = native.records(with_payload=True)
            elements = packets = 0
            with (text_dir / 'native.jsonl').open(encoding='utf-8') as stream:
                for line in stream:
                    if line.startswith('{"kind":"element"'):
                        obj = json.loads(line)
                        record = next(records)
                        self.assertEqual(record[0], native_binary.RECORD_ELEMENT)
                        (_, etype, offset, elem_id, start, end, isa, flags, cycle, _has_cc,
                         timestamp, _has_ts, last_exec, last_cond, last_type, last_subtype,
                         payload, record_text) = record
                        self.assertEqual(
                            (etype, offset, elem_id, start, end, isa, flags, cycle, timestamp,
                             last_exec, last_cond, last_type, last_subtype, payload),
                            (obj['type'], obj['offset'], obj['id'], obj['start'], obj['end'],
                             obj['isa'], obj['flags'], obj['cycleCount'], obj['timestamp'],
                             obj['lastExecuted'], obj['lastConditional'], obj['lastType'],
                             obj['lastSubtype'], obj['payload']))
                        if etype in native_binary.RARE_TEXT_TYPES:
                            self.assertEqual(record_text, obj['text'])
                        else:
                            self.assertEqual(record_text, '')
                        elements += 1
                    elif line.startswith('{"kind":"packet"'):
                        obj = json.loads(line)
                        record = next(records)
                        self.assertEqual(record[0], native_binary.RECORD_PACKET)
                        _, offset, size, type_code, raw = record
                        self.assertEqual((offset, size), (obj['offset'], obj['size']))
                        self.assertEqual(obj['hex'], raw.hex())
                        self.assertEqual(native_binary.classify_packet_text(obj['text']),
                                         native_binary.classify_packet(type_code, native.protocol))
                        packets += 1
            self.assertGreater(elements, 0)
            self.assertGreater(packets, 0)

    def test_legacy_engine_with_binary_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'fast 展开引擎'):
                self.run_pipeline(RECORD_ETMV4, Path(directory) / 'work', 'binary',
                                  engine='legacy')

    def test_unknown_native_format_is_rejected(self):
        with patch.dict(os.environ, {'ETM_NATIVE_FORMAT': 'json'}):
            with self.assertRaisesRegex(ValueError, 'ETM_NATIVE_FORMAT'):
                decode_stream.native_format()

    def test_truncated_native_binary_is_rejected_by_expansion(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory) / 'work'
            self.run_pipeline(RECORD_ETMV4, workdir, 'binary')
            native_path = workdir / 'native.bin'
            payload = native_path.read_bytes()
            native_path.write_bytes(payload[:-5])
            image = Image(RECORD_ETMV4 / 'firmware.elf')
            with self.assertRaisesRegex(native_binary.NativeBinaryError, '截断|bodyBytes'):
                decode_stream._expand_binary(workdir, image, native_path, 'ETMv4',
                                             self.config(RECORD_ETMV4), len(payload) - 5)

    def test_missing_native_binary_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(native_binary.NativeBinaryError, '不存在'):
                list(native_binary.NativeFile(Path(directory) / 'none.bin').records())


if __name__ == '__main__':
    unittest.main()
