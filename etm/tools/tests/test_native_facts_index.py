"""C2 NumPy 事实派生（t37）：facts 读写/派生/边界/v2 编码 的定向测试。

夹具是 10,000 条指令的合成 facts（3 个 cell、2 段 RUN、6 个周期锚点），
期望值用**逐指令循环**独立计算（小规模夹具 oracle），不复用产品派生公式。
不依赖真实记录/ELF/硬件。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import native_facts_index as facts_module

CELL_SIZE = 4096
STATIC = [
    dict(pc=0x8000000, size=2, function='F1', file='a.c', line=10, flow='linear', predicated=False),
    dict(pc=0x8000002, size=2, function='F1', file='a.c', line=11, flow='call', predicated=False),
    dict(pc=0x8000004, size=2, function='F2', file='b.c', line=20, flow='linear', predicated=True),
    dict(pc=0x8000006, size=2, function='F2', file='b.c', line=21, flow='return', predicated=False),
    dict(pc=0x8000008, size=4, function='F3', file='c.c', line=30, flow='linear', predicated=False),
]


def build_facts(instructions=10000):
    n = int(instructions)
    ids = ((np.arange(n, dtype=np.int64) * 3 + 1) % len(STATIC)).astype(np.uint32)
    raw = (np.arange(n, dtype=np.int64) * 7 + 3) % 11
    states = np.where(raw < 7, 0, np.where(raw < 9, 1, 2)).astype(np.uint8)
    runs = [dict(startOrdinal=0, startSeq=0, count=6000, blockOrdinal=0, recordOffset=0,
                 tailState=0),
            dict(startOrdinal=6000, startSeq=6001, count=n - 6000, blockOrdinal=1,
                 recordOffset=64, tailState=2)]
    cycles = []
    for ordinal in (0, 4095, 4096, 4097, 6000, n - 1):
        run = next(item for item in runs
                   if item['startOrdinal'] <= ordinal < item['startOrdinal'] + item['count'])
        cycles.append(dict(ordinal=ordinal,
                           seq=run['startSeq'] + (ordinal - run['startOrdinal']),
                           cycles=(2 ** 40 + ordinal) if ordinal == 4096 else 7 + ordinal,
                           span=0))
    expected = dict(cell_insn=[0] * 3, cell_executed=[0] * 3, cell_unknown=[0] * 3,
                    cell_not_taken=[0] * 3, cell_cycles=[0] * 3,
                    cell_cycle_runs=[0] * 3, cell_first_seq=[-1] * 3,
                    cell_hotspot_count=[[0] * len(STATIC) for _ in range(3)],
                    cell_hotspot_unknown=[[0] * len(STATIC) for _ in range(3)])
    first_by_cell_id = {}
    cycle_map = {item['ordinal']: item['cycles'] for item in cycles}
    # static 映射（独立计算）
    functions = ['(未知函数)']
    function_index = {'(未知函数)': 0}
    hotspots = []
    hotspot_index = {}
    func_of_id = []
    hotspot_of_id = []
    for identifier, entry in enumerate(STATIC):
        name = entry['function']
        if name not in function_index:
            function_index[name] = len(functions)
            functions.append(name)
        func_of_id.append(function_index[name])
        key = (name, entry['file'], entry['line'])
        if key not in hotspot_index:
            hotspot_index[key] = len(hotspots)
            hotspots.append(key)
        hotspot_of_id.append(hotspot_index[key])
    for ordinal in range(n):
        cell = ordinal // CELL_SIZE
        sid = int(ids[ordinal])
        hid = hotspot_of_id[sid]
        expected['cell_insn'][cell] += 1
        state = int(states[ordinal])
        if state == 0:
            expected['cell_executed'][cell] += 1
        elif state == 1:
            expected['cell_not_taken'][cell] += 1
        else:
            expected['cell_unknown'][cell] += 1
        expected['cell_hotspot_count'][cell][hid] += 1
        if STATIC[sid]['predicated']:
            expected['cell_hotspot_unknown'][cell][hid] += 1
        if expected['cell_first_seq'][cell] < 0:
            run = next(item for item in runs
                       if item['startOrdinal'] <= ordinal
                       < item['startOrdinal'] + item['count'])
            expected['cell_first_seq'][cell] = (run['startSeq']
                                                + (ordinal - run['startOrdinal']))
        if ordinal in cycle_map:
            expected['cell_cycles'][cell] += cycle_map[ordinal]
            expected['cell_cycle_runs'][cell] += 1
        if (cell, sid) not in first_by_cell_id:
            first_by_cell_id[(cell, sid)] = ordinal % CELL_SIZE
    best = {}
    for (cell, sid), offset in first_by_cell_id.items():
        hid = hotspot_of_id[sid]
        current = best.get((cell, hid))
        if current is None or offset < current[0] or (offset == current[0]
                                                     and sid < current[1]):
            best[(cell, hid)] = (offset, sid)
    expected['cell_hotspot_first'] = [[0] * len(STATIC) for _ in range(3)]
    for (cell, hid), (offset, sid) in best.items():
        expected['cell_hotspot_first'][cell][hid] = (sid << 12) | offset
    return dict(ids=ids, states=states, runs=runs, cycles=cycles, expected=expected,
                functions=functions, func_of_id=func_of_id,
                hotspot_keys=[list(key) for key in hotspots],
                hotspot_of_id=hotspot_of_id)


class NativeFactsIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        fixture = build_facts()
        self.fixture = fixture
        self.facts_path = self.root / 'facts.bin'
        facts_module.write_facts(self.facts_path, fixture['runs'], fixture['ids'],
                                 fixture['states'], fixture['cycles'])
        self.facts = facts_module.read_facts(self.facts_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_write_roundtrip(self):
        self.assertEqual(int(self.facts['header']['instructionCount']), 10000)
        self.assertEqual(int(self.facts['header']['runCount']), 2)
        self.assertEqual(int(self.facts['header']['cycleCount']), 6)
        self.assertTrue(np.array_equal(self.facts['ids'], self.fixture['ids']))
        self.assertTrue(np.array_equal(self.facts['states'], self.fixture['states']))
        self.assertTrue(np.array_equal(self.facts['cycles']['cycles'],
                                       [item['cycles'] for item in self.fixture['cycles']]))

    def test_bad_magic_and_truncated_rejected(self):
        raw = bytearray(self.facts_path.read_bytes())
        bad = self.root / 'bad-magic.bin'
        raw[0:8] = b'NOTFACT1'
        bad.write_bytes(bytes(raw))
        with self.assertRaises(facts_module.FactsError):
            facts_module.read_facts(bad)
        truncated = self.root / 'truncated.bin'
        truncated.write_bytes(bytes(raw[:80]))
        with self.assertRaises(facts_module.FactsError):
            facts_module.read_facts(truncated)

    def test_derive_cell_tables_match_independent_expectation(self):
        counters = facts_module.Counters()
        derived = facts_module.derive_tables(self.facts, STATIC, cell_size=CELL_SIZE,
                                             batch=1 << 20, counters=counters)
        self.assertEqual(derived['cells'], 3)
        for name in ('cell_insn', 'cell_executed', 'cell_unknown', 'cell_not_taken',
                     'cell_cycles', 'cell_cycle_runs', 'cell_first_seq'):
            self.assertEqual([int(value) for value in derived[name]],
                             list(self.fixture['expected'][name]), name)
        for name in ('cell_hotspot_count', 'cell_hotspot_unknown', 'cell_hotspot_first'):
            self.assertTrue(np.array_equal(np.asarray(derived[name], np.int64),
                                           np.asarray(self.fixture['expected'][name],
                                                      np.int64)), name)

    def test_static_mapping(self):
        derived = facts_module.derive_tables(self.facts, STATIC, cell_size=CELL_SIZE)
        mapping = derived['mapping']
        self.assertEqual(list(mapping['functionNames']), self.fixture['functions'])
        self.assertEqual([int(value) for value in mapping['funcOfId']],
                         self.fixture['func_of_id'])
        self.assertEqual([int(value) for value in mapping['hotspotOfId']],
                         self.fixture['hotspot_of_id'])
        keys = [[mapping['functionNames'][int(mapping['hotspotFunc'][hid])],
                 mapping['hotspotFile'][hid], int(mapping['hotspotLine'][hid])]
                for hid in range(len(mapping['hotspotFunc']))]
        self.assertEqual(keys, self.fixture['hotspot_keys'])

    def test_boundary_single_cell_matches_full_derivation(self):
        full = facts_module.derive_tables(self.facts, STATIC, cell_size=CELL_SIZE)
        for cell in (0, 1, 2):
            single = facts_module.derive_cell(self.facts, STATIC, cell)
            for name in ('cell_insn', 'cell_executed', 'cell_unknown', 'cell_not_taken',
                         'cell_cycles', 'cell_cycle_runs', 'cell_first_seq'):
                self.assertEqual(int(single[name]), int(full[name][cell]),
                                 '%s cell=%d' % (name, cell))

    def test_batch_path_has_no_per_instruction_python_loops(self):
        counters = facts_module.Counters()
        facts_module.derive_tables(self.facts, STATIC, cell_size=CELL_SIZE,
                                batch=256, counters=counters)
        bound = (counters.batches + counters.static_items + counters.cells + 20)
        self.assertLessEqual(counters.python_loops, bound,
                             'Python 循环次数应只与批次/静态项/cell 同阶')
        self.assertLessEqual(counters.numpy_calls, counters.batches * 10 + 30)
        self.assertEqual(counters.instructions, 10000)

    def test_v2_encode_roundtrip_and_compat_pending(self):
        derived = facts_module.derive_tables(self.facts, STATIC, cell_size=CELL_SIZE)
        payload, meta = facts_module.encode_tables(derived)
        self.assertEqual(payload[:8], b'ETMQIDX2')
        arrays, archive = facts_module.decode_tables(payload)
        self.assertIn('cell_insn', arrays)
        self.assertIn('cell_hotspot_count', arrays)
        for name in ('cell_insn', 'cell_first_seq', 'cell_hotspot_first'):
            self.assertTrue(np.array_equal(np.asarray(arrays[name]),
                                           np.asarray(derived[name])), name)
        self.assertTrue(meta['compatPending'])
        self.assertEqual(meta['container'], 'v2')
        self.assertEqual(sorted(name for name in arrays if name != 'meta_json'),
                         sorted(meta['tables']))

    def test_derived_arrays_do_not_include_unimplemented_tables(self):
        derived = facts_module.derive_tables(self.facts, STATIC, cell_size=CELL_SIZE)
        tables = facts_module.derived_arrays(derived)
        for pending in ('ps_start', 'fs_start', 'node_parent', 'ev_seq',
                        'cell_anchor_block', 'h_file'):
            self.assertNotIn(pending, tables)
        for name in ('cell_insn', 'cell_hotspot_count', 'st_func', 'st_hotspot'):
            self.assertIn(name, tables)


# ---------------------------------------------------------------------------------------------
# facts v2（t45 布局）：版本分派、PATH_RUNS/NODES/LABELS/EVTEXTS、派生 fs_*/ps_*/anchor/ev_*
# ---------------------------------------------------------------------------------------------
import struct


def write_facts_v2(path):
    """写一个最小 facts v2（10,000 指令 / 2 RUN / 3 PATH_RUNS / 4 NODES / 3 EVENTS / 2 texts）。"""
    n = 10000
    ids = ((np.arange(n, dtype=np.int64) * 3 + 1) % len(STATIC)).astype(np.uint32)
    raw = (np.arange(n, dtype=np.int64) * 7 + 3) % 11
    states = np.where(raw < 7, 0, np.where(raw < 9, 1, 2)).astype(np.uint8)
    runs = [(0, 0, 6000, 0, 0, 0, 0), (6000, 6001, 4000, 1, 64, 2, 3)]
    cycles = [(0, 0, 1000, 0), (4096, 4096, 2 ** 40 + 1, 0), (8192, 8193, 7, 0)]
    events = [(6000, 1, 0, 9, 1, 2, 1, -1, 1),
              (8192, 0, 3, 2, 2, -1, 0, 6000, 2),
              (9999, 0, 0, 5, 0, -1, 0, 8192, 0)]
    path_runs = [(0, 4096, 1), (4096, 4096, 2), (8192, n - 8192, 1)]
    nodes = [(-1, 0), (0, 1), (1, 2), (1, 3)]
    labels = ['', 'F1', 'F2', 'F3']
    evtexts = ['', 'irq-enter', 'cycle']
    header = facts_module.HEADER.pack(facts_module.MAGIC_V2, 2, facts_module.HEADER.size,
                                      n, len(runs), len(cycles), len(path_runs),
                                      len(events), len(nodes))
    blob = bytearray(header)
    for start, seq, count, block, offset, tail, skip in runs:
        blob += facts_module.RUN_V2.pack(start, seq, count, block, offset, tail,
                                         b'\x00' * 3, skip, 0)
    blob += ids.tobytes() + states.tobytes()
    for ordinal, seq, value, span in cycles:
        blob += facts_module.CYCLE.pack(ordinal, seq, value, span, 0)
    for seq, payload, flags, etype, text, next_func, prev_func, prev_seq, depth in events:
        blob += facts_module.EVENT_V2.pack(seq, payload, flags, etype, 0, next_func,
                                           prev_func, depth, prev_seq, text, 0)
    for start, count, leaf in path_runs:
        blob += facts_module.PATH_RUN_V2.pack(start, count, leaf)
    for parent, label in nodes:
        blob += facts_module.NODE_V2.pack(parent, label)
    for table in (labels, evtexts):
        blob += struct.pack('<Q', len(table))
        for text in table:
            encoded = text.encode('utf-8')
            blob += bytes([len(encoded)]) + encoded
    Path(path).write_bytes(bytes(blob))
    return dict(instructions=n, ids=ids, states=states, runs=runs, cycles=cycles,
                events=events, path_runs=path_runs, nodes=nodes, labels=labels,
                evtexts=evtexts)


class FactsV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.fixture = write_facts_v2(self.root / 'facts-v2.bin')
        self.facts = facts_module.read_facts_any(self.root / 'facts-v2.bin')

    def tearDown(self):
        self.tmp.cleanup()

    def test_version_dispatch_v1_and_v2(self):
        self.assertEqual(self.facts['version'], 2)
        self.assertEqual(int(self.facts['header']['pathRunCount']), 3)
        v1_path = self.root / 'facts-v1.bin'
        fixture = build_facts()
        facts_module.write_facts(v1_path, fixture['runs'], fixture['ids'],
                                 fixture['states'], fixture['cycles'])
        v1 = facts_module.read_facts_any(v1_path)
        self.assertEqual(v1['version'], 1)
        self.assertIsNone(v1['path_runs'])

    def test_v2_derived_tables(self):
        derived = facts_module.derive_tables(self.facts, STATIC, cell_size=CELL_SIZE)
        mapping = derived['mapping']
        # fs_*：相邻函数 id 变化切段，连续覆盖
        self.assertEqual(int(derived['fs_start'][0]), 0)
        self.assertEqual(int(derived['fs_start'][-1]) + int(derived['fs_count'][-1]), 10000)
        self.assertGreater(len(derived['fs_start']), 1)
        # ps_*：采用 PATH_RUNS
        self.assertEqual([int(v) for v in derived['ps_start']], [0, 4096, 8192])
        self.assertEqual([int(v) for v in derived['ps_count']], [4096, 4096, 1808])
        self.assertEqual([int(v) for v in derived['ps_leaf']], [1, 2, 1])
        # node_parent/node_label（label 是本地 strings 下标）
        self.assertEqual([int(v) for v in derived['node_parent']], [-1, 0, 1, 1])
        strings = derived['strings']
        self.assertEqual([strings[int(value)] for value in derived['node_label']],
                         ['', 'F1', 'F2', 'F3'])
        # cell anchor：cell2 从 run1 的 runSkip=3 起算
        self.assertEqual([int(v) for v in derived['cell_anchor_block']], [0, 0, 1])
        self.assertEqual([int(v) for v in derived['cell_anchor_offset']], [0, 0, 64])
        self.assertEqual([int(v) for v in derived['cell_anchor_skip']], [0, 4096, 2195])
        # events：seq/nextFunc(LABELS id)/text
        self.assertEqual([int(v) for v in derived['ev_seq']], [6000, 8192, 9999])
        self.assertEqual([int(v) for v in derived['ev_next_func']], [2, -1, -1])
        self.assertEqual([strings[int(value)] for value in derived['ev_text']],
                         ['irq-enter', 'cycle', ''])
        self.assertIn('h_file', derived)
        del mapping

    def test_v2_encode_includes_new_tables(self):
        derived = facts_module.derive_tables(self.facts, STATIC, cell_size=CELL_SIZE)
        payload, meta = facts_module.encode_tables(derived)
        arrays, _archive = facts_module.decode_tables(payload)
        for name in ('fs_start', 'ps_start', 'node_parent', 'cell_anchor_block',
                     'ev_seq', 'ev_text', 'h_file'):
            self.assertIn(name, arrays)
            self.assertIn(name, meta['tables'])
        for name in ('fs_start', 'ps_start', 'node_parent', 'cell_anchor_block',
                     'ev_seq', 'ev_text', 'h_file'):
            self.assertNotIn(name, meta['compatPending'])
        self.assertTrue(np.array_equal(np.asarray(arrays['ev_text']),
                                       np.asarray(derived['ev_text'])))
        self.assertEqual(len(meta['strings']), len(derived['strings']))


if __name__ == '__main__':
    unittest.main()
