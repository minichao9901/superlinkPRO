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


if __name__ == '__main__':
    unittest.main()
