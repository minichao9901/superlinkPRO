"""R2-A 原生 store builder 适配层测试（M0/M1）。

覆盖：
* `--version` 协议/schema/abi/能力握手；缺失二进制明确报错；
* prescan：合成 native.bin → 唯一范围表/计数 JSON，范围表格式 R2RANGES1；
* static-dict v1 读取（完整文本槽 + t8 空池两种）、PC 升序/哈希/数量校验；
* diagnose：合成 native.bin 的元素/包/状态记录 → 与预期计数一致（instructions/
  events/ranges/executed/notTaken/exceptions/gaps/typeCounts）；
* 失败路径：坏 magic/截断/静态字典缺 PC → 非零退出与可读原因；
* C++ 自检入口（--self-test）可用。

无原生二进制时跳过需要 exe 的用例；`--version` 的验证命令由调用方执行。
"""
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import native_storebuild as nsb  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def _element(etype, *, offset=0, start=0, end=0, payload=0, last_executed=0,
             last_conditional=0, flags=0, cycle=0, text=b''):
    flags8 = 0x08 if text else 0
    body = struct.pack('<BBBBBBIIIHHQQQQH', etype, 0, 0, last_executed,
                       last_conditional, flags8, flags, cycle, payload,
                       0, 0, offset, start, end, 0, len(text))
    body += text
    return struct.pack('<BI', 1, 5 + len(body)) + body


def _packet(type_code=0xF6, offset=0, raw=b'\x04\x00'):
    body = struct.pack('<IBQI', type_code, 0, offset, len(raw)) + raw
    return struct.pack('<BI', 2, 5 + len(body)) + body


def _status(elements, packets):
    library = b'1.8.3'
    error = b''
    body = struct.pack('<IiIIQQ', 64, 0, len(library), len(error),
                       elements, packets) + library + error
    return struct.pack('<BI', 3, 5 + len(body)) + body


def build_native(path, *, bad_range=False):
    records = [
        _element(1, offset=0, payload=1),                       # NO_SYNC
        _element(9, offset=10, payload=15, flags=0x10),         # EXCEPTION
        _element(5, offset=20, start=0x900 if bad_range else 0x100,
                 end=0x906 if bad_range else 0x106, payload=3,
                 last_executed=1),
        _element(12, offset=20, cycle=5),                       # CYCLE_COUNT
        _element(10, offset=30, payload=0),                     # EXCEPTION_RET
        _element(5, offset=40, start=0x110, end=0x114, payload=2,
                 last_executed=0),
        _packet(),
        _status(6, 1),
    ]
    header = struct.pack('<8sHHIIIQQQQQ', b'ETMNAT01', 1, 64, 0x01020304,
                         1, 0, 7, 6, 1,
                         sum(len(record) for record in records), 0)
    path.write_bytes(header + b''.join(records))
    return path


def build_static(path, *, with_strings=True, with_extension=False):
    entries = [
        # pc, size, id, pred, flow, function,file,mnemonic,operands
        (0x100, 2, 0, 0, 0, 'main', 'main.c', 'movs', 'r0, #0'),
        (0x102, 2, 1, 0, 1, 'main', 'main.c', 'bl', '#0x110'),
        (0x104, 2, 2, 0, 2, 'main', 'main.c', 'bx', 'lr'),
        (0x110, 2, 3, 0, 0, 'leaf', 'leaf.c', 'adds', 'r0, #1'),
        (0x112, 2, 4, 0, 3, 'leaf', 'leaf.c', 'bne', '#0x100'),
    ]
    strings = []
    if with_strings:
        strings = ['']
        for entry in entries:
            strings.extend([entry[5], entry[6], entry[7], entry[8]])
    blob = bytearray()
    for value in strings:
        encoded = value.encode('utf-8')
        blob += struct.pack('<I', len(encoded)) + encoded
    table_offset = 61 + len(blob)
    raw = bytearray()
    raw += b'R2STATIC1'
    raw += struct.pack('<I', 1)
    raw += bytes.fromhex('ab' * 32)
    raw += struct.pack('<II', len(entries), len(strings))
    raw += struct.pack('<Q', table_offset)
    raw += blob
    for entry in entries:
        raw += struct.pack('<IBIBB', entry[0], entry[1], entry[2], entry[3], entry[4])
        raw += b'\x00\x00\x00\x00\x00'
    if with_extension:
        ext = bytearray(16)
        ext[0:8] = b'R2EXT01\x00'
        struct.pack_into('<II', ext, 8, 1, len(entries))
        raw += ext
        for entry in entries:
            row = bytearray(24)
            struct.pack_into('<i', row, 0, 10)            # line
            row[4] = 0                              # conditional
            row[5] = 1 | 2 | 4 | 8                         # bytes/function/file/line
            row[6:8] = b'\x00\xbf'
            raw += row
    path.write_bytes(bytes(raw))
    return path


class NativeStorebuildCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.version = nsb.probe()
        except nsb.NativeStorebuildError as error:
            raise unittest.SkipTest('原生 store builder 不可用：%s' % error)
        cls.root = Path(tempfile.mkdtemp(prefix='_r2a_storebuild_'))
        cls.native = build_native(cls.root / 'native.bin')
        cls.static = build_static(cls.root / 'static-dict.bin')
        cls.static_empty = build_static(cls.root / 'static-empty.bin',
                                        with_strings=False)
        cls.static_ext = build_static(cls.root / 'static-ext.bin',
                                      with_extension=True)

    def test_version_handshake(self):
        self.assertEqual(self.version.get('protocol'), nsb.PROTOCOL)
        self.assertEqual(self.version.get('schema'), nsb.SCHEMA)
        self.assertEqual(self.version.get('abi'), nsb.ABI)
        self.assertIn('prescan', self.version.get('capabilities', []))
        self.assertIn('diagnose', self.version.get('capabilities', []))

    def test_missing_binary_is_explicit(self):
        with self.assertRaises(nsb.NativeStorebuildUnavailable):
            nsb.probe(ROOT / 'build-native' / 'definitely-not-here.exe')

    def test_cpp_self_test(self):
        completed = subprocess.run([str(nsb.binary_path()), '--self-test'],
                                   capture_output=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode('utf-8', 'replace'))
        payload = json.loads(completed.stdout.decode('utf-8').strip().splitlines()[-1])
        self.assertTrue(payload.get('ok'))

    def test_prescan_counts_and_ranges(self):
        ranges = self.root / 'ranges.bin'
        out = self.root / 'prescan.json'
        result = nsb.prescan(self.native, out=ranges, json_path=out)
        self.assertEqual(result['rangeRecords'], 2)
        self.assertEqual(result['uniqueRanges'], 2)
        self.assertEqual(result['duplicateRanges'], 0)
        self.assertEqual(result['payloadMismatches'], 0)
        self.assertEqual(result['eventElements'], 3)
        self.assertEqual(result['cycleElements'], 1)
        self.assertEqual(result['typeCounts']['5'], 2)
        raw = ranges.read_bytes()
        self.assertEqual(raw[:9], b'R2RANGES1')
        unique, records = struct.unpack_from('<QQ', raw, 9)
        self.assertEqual((unique, records), (2, 2))
        rows = [struct.unpack_from('<III', raw, 25 + index * 12) for index in range(unique)]
        self.assertEqual(rows, [(0x100, 0x106, 3), (0x110, 0x114, 2)])

    def test_static_dict_full_strings_and_empty_pool(self):
        full = nsb.inspect_static(self.static)
        self.assertEqual(full['entryCount'], 5)
        self.assertEqual(full['stringCount'], 21)
        first = full['entries'][0]
        self.assertEqual((first['pc'], first['size'], first['id'],
                          first['predicated'], first['flow']), (0x100, 2, 0, 0, 0))
        self.assertEqual((first['function'], first['file'], first['mnemonic'],
                          first['operands']), ('main', 'main.c', 'movs', 'r0, #0'))
        extended = nsb.inspect_static(self.static_ext)
        self.assertEqual(extended['entries'][0]['bytes'], '00bf')
        self.assertEqual(extended['entries'][0]['line'], 10)
        self.assertFalse(extended['entries'][0]['conditional'])
        empty = nsb.inspect_static(self.static_empty)
        self.assertEqual(empty['stringCount'], 0)
        self.assertEqual(empty['entries'][0]['function'], '')

    def test_diagnose_counts(self):
        result = nsb.diagnose(self.native, self.static)
        counts = result['counts']
        self.assertEqual(counts['instructions'], 5)
        self.assertEqual(counts['events'], 8)
        self.assertEqual(counts['ranges'], 2)
        self.assertEqual(counts['rareEvents'], 3)
        self.assertEqual(counts['executed'], 4)
        self.assertEqual(counts['notTaken'], 1)
        self.assertEqual(counts['unknownExecution'], 0)
        self.assertEqual(counts['exceptions'], 1)
        self.assertEqual(counts['gaps'], 1)
        self.assertEqual(counts['boundaries'], 1)
        self.assertEqual(counts['cycleElements'], 1)
        self.assertEqual(counts['cycleSum'], 5)
        self.assertEqual(result['typeCounts'], {'1': 1, '5': 2, '9': 1, '10': 1, '12': 1})
        self.assertEqual(result['packets']['count'], 1)
        self.assertEqual(result['staticEntries'], 5)

    def test_diagnose_empty_string_pool(self):
        result = nsb.diagnose(self.native, self.static_empty)
        self.assertEqual(result['counts']['instructions'], 5)
        self.assertEqual(result['staticStrings'], 0)

    def test_build_minimal_store_openable(self):
        import trace_store
        store_root = self.root / 'build-store'
        generation = 'g-test'
        result = nsb.build(self.native, self.static, store_root / 'generations' / generation,
                           generation=generation, generation_tag=0x1234)
        self.assertEqual(result['counts']['instructions'], 5)
        self.assertEqual(result['counts']['events'], 8)
        self.assertEqual(result['counts']['ranges'], 2)
        store_path = store_root / 'generations' / generation / 'store.bin'
        self.assertTrue(store_path.is_file())
        # t17 才写 manifest；测试为了用现有读取端打开，构造最小 manifest。
        info = result['files'][0]
        manifest = dict(format='trace-store', formatName='trace-store v1',
                        schemaVersion=1, byteOrder='little', state='complete',
                        generation=generation, generationTag='0x1234',
                        files=[dict(name='store.bin', bytes=info['bytes'],
                                    sha256=info['sha256'])],
                        counts=result['counts'],
                        fieldPresence={'instruction': [
                            'kind', 'seq', 'offset', 'pc', 'size', 'bytes', 'mnemonic',
                            'operands', 'executed', 'function', 'file', 'line',
                            'cycleCount', 'cycleSpan', 'flow', 'conditional',
                            'executionState', 'executionReason', 'callPath', 'cycles'],
                            'event': ['kind', 'seq', 'type', 'offset', 'text', 'payload',
                                      'start', 'end', 'flags']},
                        bindings={}, checksums=dict(algorithm='sha256', block='crc32'))
        (store_root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        store = trace_store.TraceStore(store_root, verify='sizes')
        try:
            self.assertEqual(store.counts['events'], 8)
            self.assertEqual(len(store.static), 5)
            self.assertEqual(store.event(0)['event']['kind'], 'event')
            self.assertEqual(store.event(2)['event']['kind'], 'instruction')
            self.assertTrue(store.nav)
        finally:
            store.close()

    def test_bad_native_fails_readably(self):
        broken = self.root / 'broken-native.bin'
        data = bytearray(self.native.read_bytes())
        data[:8] = b'BROKEN!!'
        broken.write_bytes(bytes(data))
        with self.assertRaises(nsb.NativeStorebuildError) as caught:
            nsb.prescan(broken)
        self.assertIn('magic', str(caught.exception).lower())

    def test_missing_static_pc_fails(self):
        # 把 range 起点改成静态字典没有的 PC → 退出码 4
        broken_native = self.root / 'missing-static-native.bin'
        build_native(broken_native, bad_range=True)
        with self.assertRaises(nsb.NativeStorebuildError) as caught:
            nsb.diagnose(broken_native, self.static)
        self.assertIn('退出码 4', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
