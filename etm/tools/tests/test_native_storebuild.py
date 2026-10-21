"""R2-A 原生 store builder 适配层测试（M0/M1）。

覆盖：
* `--version` 协议/schema/abi/能力握手；缺失二进制明确报错；
* prescan：合成 native.bin → 唯一范围表/计数 JSON，范围表格式 R2RANGES1；
* static-dict v1 读取（完整文本槽 + t8 空池两种）、PC 升序/哈希/数量校验；
* diagnose：合成 native.bin 的元素/包/状态记录 → 与预期计数一致（instructions/
  events/ranges/executed/notTaken/exceptions/gaps/typeCounts）；
* build：blocks 1-11 可被 TraceStore 打开；
* **facts v2（ETMFACT2）**：header/段长自洽、RUNS 显式 runSkip、EVENTS
  nextFunc/prevFunc/depth/prevEventSeq/textId、PATH_RUNS+NODES+LABELS 覆盖、
  事件文本池、同输入两次字节一致、失败不留 .tmp./manifest；
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


def build_native_nested(path):
    """两层嵌套 IRQ（9,9,10,10）+ 单 RUN：验证 facts depth/prevFunc/路径区间。"""
    records = [
        _element(1, offset=0, payload=1),                       # NO_SYNC
        _element(9, offset=10, payload=15, flags=0x40),         # EXCEPTION 外层
        _element(9, offset=20, payload=5, flags=0x40),          # EXCEPTION 内层
        _element(5, offset=30, start=0x100, end=0x106, payload=3,
                 last_executed=1),
        _element(10, offset=40, payload=0),                     # 内层返回
        _element(10, offset=50, payload=0),                     # 外层返回
        _element(5, offset=60, start=0x110, end=0x114, payload=2,
                 last_executed=0),
        _status(7, 0),
    ]
    header = struct.pack('<8sHHIIIQQQQQ', b'ETMNAT01', 1, 64, 0x01020304,
                         1, 0, 7, 7, 0,
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


FACT_V2_HEADER = struct.Struct('<8sIIQQQQQQ')     # 64 B
FACT_V2_RUN = struct.Struct('<QQIIIBBBBII')       # 40 B
FACT_V2_CYCLE = struct.Struct('<QQQII')           # 32 B
FACT_V2_EVENT = struct.Struct('<QQIBBhhhiII')     # 40 B
FACT_V2_PATH = struct.Struct('<QII')              # 16 B
FACT_V2_NODE = struct.Struct('<iI')               # 8 B


def read_facts_v2(path):
    """读 facts v2（ETMFACT2）并做结构自洽校验；坏文件直接抛 AssertionError。"""
    raw = Path(path).read_bytes()
    (magic, version, header_bytes, instructions, runs, cycles, path_runs,
     events, nodes) = FACT_V2_HEADER.unpack_from(raw, 0)
    assert magic == b'ETMFACT3', magic
    assert version == 3, version
    assert header_bytes == FACT_V2_HEADER.size, header_bytes
    position = header_bytes
    run_rows = []
    for _ in range(runs):
        (ordinal, seq, count, block, offset, tail, _r1, _r2, _r3, skip,
         _reserved) = FACT_V2_RUN.unpack_from(raw, position)
        run_rows.append(dict(ordinal=ordinal, seq=seq, count=count, block=block,
                             offset=offset, tail=tail, runSkip=skip))
        position += FACT_V2_RUN.size
    ids = list(struct.unpack_from('<%dI' % instructions, raw, position))
    position += instructions * 4
    states = list(raw[position:position + instructions])
    position += instructions
    cycle_rows = []
    for _ in range(cycles):
        ordinal, seq, count, span, _reserved = FACT_V2_CYCLE.unpack_from(raw, position)
        cycle_rows.append(dict(ordinal=ordinal, seq=seq, cycles=count, span=span))
        position += FACT_V2_CYCLE.size
    event_rows = []
    for _ in range(events):
        (seq, payload, flags, etype, _pad, next_func, prev_func, depth, prev_seq,
         text_id, _reserved) = FACT_V2_EVENT.unpack_from(raw, position)
        event_rows.append(dict(seq=seq, payload=payload, flags=flags, type=etype,
                               nextFunc=next_func, prevFunc=prev_func, depth=depth,
                               prevSeq=prev_seq, textId=text_id))
        position += FACT_V2_EVENT.size
    path_rows = []
    for _ in range(path_runs):
        start, count, leaf = FACT_V2_PATH.unpack_from(raw, position)
        path_rows.append(dict(start=start, count=count, leaf=leaf))
        position += FACT_V2_PATH.size
    node_rows = []
    for _ in range(nodes):
        parent, label = FACT_V2_NODE.unpack_from(raw, position)
        node_rows.append(dict(parent=parent, label=label))
        position += FACT_V2_NODE.size

    def read_strings():
        nonlocal position
        count, = struct.unpack_from('<Q', raw, position)
        position += 8
        values = []
        for _ in range(count):
            length = 0
            shift = 0
            while True:
                byte = raw[position]
                position += 1
                length |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
            values.append(raw[position:position + length].decode('utf-8'))
            position += length
        return values

    labels = read_strings()
    texts = read_strings()
    assert position == len(raw), (position, len(raw))
    return dict(instructions=instructions, runs=run_rows, ids=ids, states=states,
                cycles=cycle_rows, events=event_rows, pathRuns=path_rows,
                nodes=node_rows, labels=labels, texts=texts)


def store_blocks(path):
    """解析 store.bin 的块目录：返回 [(block_type, payload_bytes)]。"""
    raw = Path(path).read_bytes()
    count, = struct.unpack_from('<I', raw, 24)
    blocks = []
    for position in range(count):
        offset, length, _count, block_type, _flags, _crc = struct.unpack_from(
            '<QIIHHI', raw, 56 + position * 24)
        blocks.append((block_type, raw[offset:offset + length]))
    return blocks


def packet_records(payload):
    """解码 packets 块：返回 [dict(delta, size, raw, text)]。"""
    position = 0
    records = []

    def uv():
        nonlocal position
        value = 0
        shift = 0
        while True:
            byte = payload[position]
            position += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
            shift += 7

    while position < len(payload):
        delta = uv()
        size = uv()
        raw_len = uv()
        raw = payload[position:position + raw_len]
        position += raw_len
        text_len = uv()
        text = payload[position:position + text_len]
        position += text_len
        records.append(dict(delta=delta, size=size, raw=raw, text=text))
    return records


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

    def test_online_facts_equal_replay_with_cell_boundaries(self):
        # Repeated short ranges cross multiple cells, preserving cycles, events,
        # tail states and anchor runSkip without a second sequence scan.
        chunks = [_element(1, offset=0, payload=1)]
        for n in range(9000):
            if n % 97 == 0:
                chunks.extend([_element(9, offset=n, payload=15, flags=0x40),
                               _element(10, offset=n, payload=0)])
            chunks.append(_element(5, offset=n + 1, start=0x100, end=0x106,
                                   payload=3, last_executed=n % 2))
            if n % 5 == 0:chunks.append(_element(12, offset=n + 1, cycle=7))
        count = len(chunks)
        chunks.append(_status(count, 0))
        header = struct.pack('<8sHHIIIQQQQQ', b'ETMNAT01', 1, 64, 0x01020304,
                             1, 0, count, count, 0, sum(map(len, chunks)), 0)
        native = self.root/'cells-native.bin'
        native.write_bytes(header + b''.join(chunks))
        paths = []
        for replay in (False, True):
            out = self.root/('replay' if replay else 'online')
            result = nsb.build(native, self.static_ext, out, facts_out=out/'facts.bin',
                               native_index=replay, reserve_instructions=27000, profile_detail=replay)
            paths.append(out/'facts.bin')
            self.assertEqual(result['counts']['instructions'], 27000)
            self.assertEqual(result['timingDetail'], replay)
            self.assertEqual('tracker' in result['phases'], replay)
            import trace_store
            container = trace_store._Container(out/'store.bin', trace_store.FILE_TRACE, 0)
            try:
                sequences = [(i,b) for i,b in enumerate(container.blocks) if b.block_type == trace_store.BLK_SEQUENCE]
                self.assertTrue(any(b.flags == 1 for i,b in sequences))
                for i,b in sequences:
                    payload = container.block(i)
                    if b.flags:self.assertEqual(len(payload), b.count)
            finally:container.close()
        self.assertEqual(paths[0].read_bytes(), paths[1].read_bytes())
        facts = read_facts_v2(paths[0])
        self.assertEqual([r['ordinal'] for r in facts['runs']], list(range(0, 27000, 4096)))
        self.assertTrue(any(r['runSkip'] for r in facts['runs']))

    def test_large_packet_block_retains_record_count_semantics(self):
        import trace_store
        packets = 20000
        chunks = [_packet(offset=2*n) for n in range(packets)]
        chunks.append(_element(5, offset=2*packets, start=0x100, end=0x106, payload=3, last_executed=1))
        status = bytearray(_status(1, packets));struct.pack_into('<I', status, 5, 2*packets)
        chunks.append(bytes(status))
        native = self.root/'many-packets.bin'
        native.write_bytes(struct.pack('<8sHHIIIQQQQQ', b'ETMNAT01', 1, 64, 0x01020304,
                                       1, 0, packets+1, 1, packets, sum(map(len,chunks)), 0) + b''.join(chunks))
        out = self.root/'many-packets'
        nsb.build(native, self.static_ext, out)
        container = trace_store._Container(out/'store.bin', trace_store.FILE_TRACE, 0)
        try:
            blocks = [b for b in container.blocks if b.block_type == trace_store.BLK_PACKETS]
            self.assertEqual(sum(b.count for b in blocks), packets)
            self.assertTrue(all(b.flags == 0 for b in blocks))
        finally:container.close()

    def test_online_index_facts_match_reference_and_reject_corruption(self):
        import hashlib
        import numpy as np
        import native_facts_index as nf
        import static_dict
        out = self.root/'numeric-index'
        result = nsb.build(self.native, self.static_ext, out, facts_out=out/'facts.bin',
                           index_facts_out=out/'index.bin', reserve_instructions=5)
        expected_sha = hashlib.sha256(self.native.read_bytes()).hexdigest()
        self.assertEqual(result['inputs']['nativeSha256'], expected_sha)
        facts = nf.read_facts_any(out/'facts.bin')
        entries = sorted(static_dict.read_static_dict(self.static_ext)['entries'], key=lambda e:e['id'])
        reference = nf.derive_tables(facts, entries)
        reference_nav = nf.derive_remaining(facts, reference, entries)
        facts['indexFacts'] = nf.read_index_facts(out/'index.bin', facts, entries, expected_sha)
        candidate = nf.derive_tables(facts, entries)
        candidate_nav = nf.derive_remaining(facts, candidate, entries)
        for name in ('cell_unknown', 'cell_not_taken', 'cell_executed', 'cell_hotspot_count',
                     'cell_hotspot_unknown', 'cell_hotspot_first'):
            np.testing.assert_array_equal(reference[name], candidate[name], err_msg=name)
        for name in ('pc_cell_offsets', 'pc_cells', 'line_start', 'line_id'):
            np.testing.assert_array_equal(reference_nav[name], candidate_nav[name], err_msg=name)
        payload = (out/'index.bin').read_bytes()
        for name in ('version', 'reserved', 'sha', 'mapping', 'truncated', 'trailing'):
            broken = bytearray(payload)
            if name == 'version':broken[8] ^= 2
            elif name == 'reserved':broken[36] = 1
            elif name == 'sha':broken[56] ^= 1
            elif name == 'mapping':struct.pack_into('<I', broken, 128, 999)
            elif name == 'truncated':del broken[-1]
            else:broken.extend(b'x')
            path = out/(name+'.bin');path.write_bytes(broken)
            with self.subTest(name=name), self.assertRaises(nf.FactsError):
                nf.read_index_facts(path, facts, entries, expected_sha)

    def test_build_facts_v2_structure(self):
        store_root = self.root / 'facts-store'
        facts = store_root / 'facts.bin'
        # 用带扩展文本槽的静态字典（函数名/文件/行），facts 的 LABELS/nextFunc 才有语义。
        result = nsb.build(self.native, self.static_ext, store_root,
                           generation='g-facts', generation_tag=0x11,
                           facts_out=facts, native_index=True)
        self.assertEqual(result['facts']['format'], 'ETMFACT3')
        self.assertEqual(result['facts']['version'], 3)
        self.assertEqual(result['facts']['bytes'], facts.stat().st_size)
        # 不写 manifest；不留临时文件
        self.assertFalse((store_root / 'manifest.json').exists())
        self.assertEqual(list(store_root.glob('*.tmp.*')), [])
        info = read_facts_v2(facts)
        self.assertEqual(info['instructions'], 5)
        self.assertEqual(len(info['ids']), 5)
        self.assertEqual(len(info['states']), 5)
        self.assertEqual(len(info['cycles']), 1)
        self.assertEqual([event['type'] for event in info['events']], [1, 9, 10])
        # RUNS：连续覆盖 + 显式 runSkip（原生在记录边界分块 → 0）
        covered = 0
        for run in info['runs']:
            self.assertEqual(run['ordinal'], covered)
            self.assertEqual(run['runSkip'], 0)
            covered += run['count']
        self.assertEqual(covered, 5)
        # PATH_RUNS：连续覆盖；NODES：根/父链/标签池
        covered = 0
        for run in info['pathRuns']:
            self.assertEqual(run['start'], covered)
            self.assertLess(run['leaf'], len(info['nodes']))
            covered += run['count']
        self.assertEqual(covered, 5)
        self.assertGreaterEqual(len(info['pathRuns']), 2)
        self.assertEqual(info['nodes'][0]['parent'], -1)
        for index, node in enumerate(info['nodes']):
            self.assertTrue(-1 <= node['parent'] < index)
            self.assertLess(node['label'], len(info['labels']))
        self.assertEqual(info['labels'][0], '')
        self.assertIn('main', info['labels'])
        self.assertIn('中断 15', info['labels'])
        # EVENTS v2：nextFunc（type-9 且下一条指令 seq 紧邻）/prevFunc/depth/prevSeq
        by_type = {event['type']: event for event in info['events']}
        main_label = info['labels'].index('main')
        self.assertEqual(by_type[9]['nextFunc'], main_label)
        self.assertEqual(by_type[1]['prevFunc'], -1)
        self.assertEqual(by_type[1]['depth'], 0)
        self.assertEqual(by_type[9]['depth'], 1)
        self.assertEqual(by_type[10]['depth'], 0)
        self.assertEqual(by_type[10]['prevFunc'], main_label)
        self.assertEqual([event['prevSeq'] for event in info['events']], [-1, 0, 1])
        self.assertTrue(all(event['textId'] < len(info['texts'])
                            for event in info['events']))
        self.assertEqual(info['texts'][0], '')
        # 确定性：同输入两次 facts 字节一致
        again = store_root / 'again' / 'facts.bin'
        nsb.build(self.native, self.static_ext, store_root / 'again',
                  generation='g-facts', generation_tag=0x11,
                  facts_out=again, native_index=True)
        self.assertEqual(facts.read_bytes(), again.read_bytes())
        # 原生块 12 存在，但默认视图/块 12 的查询能力不在此任务声明
        self.assertTrue((store_root / 'store.bin').is_file())

    def test_facts_v2_nested_irq_depth(self):
        """异常嵌套（9/9/10/10）→ depth 0/1/2/1/0，prevFunc/路径区间覆盖跨嵌套。"""
        nested = build_native_nested(self.root / 'nested-native.bin')
        out = self.root / 'nested-store'
        nsb.build(nested, self.static_ext, out, generation='g-nested',
                  generation_tag=0x12, facts_out=out / 'facts.bin')
        info = read_facts_v2(out / 'facts.bin')
        self.assertEqual(info['instructions'], 5)
        self.assertEqual([event['type'] for event in info['events']],
                         [1, 9, 9, 10, 10])
        self.assertEqual([event['depth'] for event in info['events']],
                         [0, 1, 2, 1, 0])
        main_label = info['labels'].index('main')
        # 内层 IRQ 事件后的首条指令（seq 紧邻）→ nextFunc=main；外层事件后是事件 → -1
        self.assertEqual(info['events'][2]['nextFunc'], main_label)
        self.assertEqual(info['events'][1]['nextFunc'], -1)
        # 事件前最后一条指令：第一个 9/10 之前没有指令；第二个 10 之前是 main
        self.assertEqual(info['events'][0]['prevFunc'], -1)
        self.assertEqual(info['events'][1]['prevFunc'], -1)
        self.assertEqual(info['events'][3]['prevFunc'], main_label)
        self.assertEqual(info['events'][4]['prevFunc'], main_label)
        # 路径区间连续覆盖且包含嵌套链（中断 5 在 中断 15 之下）
        covered = 0
        chains = []
        for run in info['pathRuns']:
            self.assertEqual(run['start'], covered)
            covered += run['count']
            chain = []
            node = run['leaf']
            while node > 0:
                chain.append(info['labels'][info['nodes'][node]['label']])
                node = info['nodes'][node]['parent']
            chains.append(tuple(reversed(chain)))
        self.assertEqual(covered, 5)
        # 参考 _PathTracker 语义：IRQ 入口替换当前栈（外层保存在 contexts），
        # 因此内层 IRQ 下的指令路径链是 ('中断 5','main')；嵌套深度由 depth 表达。
        self.assertIn(('中断 5', 'main'), chains)

    def test_packets_raw_uses_body_offset(self):
        """packets 块 raw 按 body+17 取（不是 22）：raw 与 native 逐字节一致且确定性。

        修复前 `body.data() + kPacketHeaderBytes(22)` 会越界 5 字节，packets 块字节
        随堆/参数布局变化；修复后 raw 是 native.bin 记录的真实 payload。
        """
        out = self.root / 'packets-store'
        result = nsb.build(self.native, self.static, out,
                           generation='g-packets', generation_tag=0x13)
        self.assertEqual(result['counts']['packets'], 1)
        packets = [payload for block_type, payload in store_blocks(out / 'store.bin')
                   if block_type == 4]
        self.assertEqual(len(packets), 1)
        records = packet_records(packets[0])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['delta'], 0)
        self.assertEqual(records[0]['size'], 2)
        self.assertEqual(records[0]['raw'], b'\x04\x00')
        self.assertEqual(records[0]['text'], b'')
        # 同参数第二次构建：整个 store 字节一致（修复前 packets 块不确定）
        out2 = self.root / 'packets-store-2'
        nsb.build(self.native, self.static, out2,
                  generation='g-packets', generation_tag=0x13)
        self.assertEqual((out / 'store.bin').read_bytes(),
                         (out2 / 'store.bin').read_bytes())

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
