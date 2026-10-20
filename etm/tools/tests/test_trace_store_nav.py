"""R1-B 导航快速路径测试：instruction ±1 / NAV / PC / 源码 / 回退 / 结构计数。

覆盖 t5 合同：
* 正反向、首尾、无命中、末端回绕、稀疏命中、事件边界、作用域 start/end；
* 与 `TRACE_STORE_SLOW=1` 的慢路径逐字段一致（合成中型记录 + 真实 store 抽样）；
* 无索引（无块 12）/nav sidecar 缺失/损坏 → 显式回退慢路径，结果不变；
* 结构计数：instruction/NAV 不展开事件；PC/源码只做有界单元格解码（≤2 个单元格）。
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import trace_store        # noqa: E402
import trace_store_index  # noqa: E402
import trace_store_nav    # noqa: E402

TMP_ROOT = Path(tempfile.mkdtemp(dir=str(Path(__file__).resolve().parents[2] / 'records'),
                                 prefix='_r1_nav_'))
atexit.register(shutil.rmtree, TMP_ROOT, True)

CONFIG = dict(idr0=0x080006E1, idr1=0x4100F401, idr2=4, idr8=1, idr9=0,
              idr10=0, idr11=0, idr12=1, idr13=0, config=1, traceid=2)
IT_REASON = 'IT 非分支条件指令：当前 trace 未记录条件结果'

# (pc, mnemonic, operands, function, file, line, flow, conditional)
PC_TABLE = [
    (0x100, 'movs', 'r0, #0', 'main', 'app.c', 10, 'linear', False),
    (0x102, 'adds', 'r0, #1', 'main', 'app.c', 10, 'linear', False),
    (0x104, 'cmp', 'r0, #4', 'main', 'app.c', 11, 'linear', False),
    (0x106, 'bl', '#0x8000200', 'main', 'app.c', 11, 'call', False),
    (0x108, 'ldr', 'r1, [r0]', 'main', 'app.c', 12, 'linear', False),
    (0x10A, 'bne', '#0x8000100', 'main', 'app.c', 12, 'branch', True),
    (0x10C, 'nop', '', 'main', 'app.c', 13, 'linear', True),
    (0x10E, 'str', 'r1, [r2]', 'main', 'app.c', 13, 'linear', False),
    (0x110, 'pop', '{r4, pc}', 'main', 'app.c', 14, 'return', False),
    (0x200, 'push', '{r4, lr}', 'worker', 'lib.c', 3, 'linear', False),
    (0x202, 'movs', 'r2, #2', 'worker', 'lib.c', 4, 'linear', False),
    (0x204, 'adds', 'r2, #1', 'worker', 'lib.c', 4, 'linear', False),
    (0x206, 'bl', '#0x8000300', 'worker', 'lib.c', 5, 'call', False),
    (0x208, 'cmp', 'r2, #9', 'worker', 'lib.c', 5, 'linear', False),
    (0x20A, 'bx', 'lr', 'worker', 'lib.c', 6, 'return', False),
    (0x300, 'ldr', 'r3, [r4]', 'io', 'io.c', 7, 'linear', False),
    (0x302, 'str', 'r3, [r4, #4]', 'io', 'io.c', 7, 'linear', False),
    (0x304, 'nop', '', 'io', 'io.c', 8, 'linear', False),
    (0x306, 'bx', 'lr', 'io', 'io.c', 8, 'return', False),
]
MAIN = [item[0] for item in PC_TABLE if item[3] == 'main']
WORKER = [item[0] for item in PC_TABLE if item[3] == 'worker']
IO = [item[0] for item in PC_TABLE if item[3] == 'io']


def _ranges():
    """60 个范围，形成 main→worker→io 的调用/返回流与周期锚点。"""
    pattern = [MAIN, WORKER, IO, MAIN, WORKER, MAIN]
    ranges = []
    for number in range(60):
        pool = pattern[number % len(pattern)]
        count = 3 + (number % 5)
        identifiers = []
        for position in range(count):
            pc = pool[position % len(pool)]
            identifiers.append(pc)
        # 尾指令按函数收尾：main 用 call/return，worker/io 用 return
        tail_pc = pool[-1]
        identifiers[-1] = tail_pc
        cycles = 0
        if number % 7 == 3:
            cycles = 3 + (number % 5)
        ranges.append(dict(offset=10 + number * 10, ids=identifiers,
                           cycles=cycles, last=(number % 11 != 5)))
    return ranges


def _instruction(pc, offset, executed, cycle_count, span, seq):
    info = next(item for item in PC_TABLE if item[0] == pc)
    _pc, mnemonic, operands, function, file, line, flow, conditional = info
    event = dict(kind='instruction', seq=seq, offset=offset, pc=pc, size=2,
                 bytes='00bf', mnemonic=mnemonic, operands=operands,
                 executed=executed, function=function, file=file, line=line,
                 cycleCount=cycle_count, cycleSpan=span, flow=flow,
                 conditional=conditional, executionState=(
                     'executed' if executed is True else
                     'not-taken' if executed is False else 'unknown'),
                 executionReason=(IT_REASON if executed is None else ''),
                 callPath=[], cycles=cycle_count)
    return event


def build_medium_record(folder):
    """构造一个 60 范围 / ~300 指令的中型记录（含调用/返回/异常/谓词未知/周期）。"""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    events = []
    boundaries = []
    it_pc = 0x10C

    def add_event(etype, payload=0, text=''):
        events.append(dict(kind='event', seq=len(events), type=etype, offset=0,
                           text=text or 'E%d' % etype, payload=payload, start=0,
                           end=0, flags=0))

    add_event(1, payload=1, text='NO_SYNC')
    add_event(2, text='TRACE_ON')
    exceptions = gaps = 0
    ranges = _ranges()
    for number, spec in enumerate(ranges):
        if number in (12, 40):
            add_event(9, payload=15, text='EXCEPTION')
            exceptions += 1
        if number in (5, 20, 35):
            add_event(6, text='BOUNDARY_6')
            gaps += 1
            boundaries.append(len(events))
        if number in (13, 41):
            add_event(10, text='EXCEPTION_RET')
        ids = spec['ids']
        for position, pc in enumerate(ids):
            tail = position == len(ids) - 1
            if pc == it_pc:
                executed = None
            elif tail and not spec['last']:
                executed = False
            else:
                executed = True
            cycles = spec['cycles'] if tail else 0
            span = len(ids) if (tail and spec['cycles']) else 0
            events.append(_instruction(pc, spec['offset'], executed, cycles, span,
                                       len(events)))
    add_event(3, payload=7, text='EO_TRACE')
    events.append(dict(kind='event', seq=len(events), type=10, offset=0,
                       text='EXCEPTION_RET', payload=0, start=0, end=0, flags=0))
    # callPath / 执行状态：与参考展开器同源（flow_analysis.analyze_calls）
    import flow_analysis
    flow_analysis.analyze_calls(events)
    instructions = [item for item in events if item['kind'] == 'instruction']
    executed = sum(1 for item in instructions if item['executed'] is True)
    unknown = sum(1 for item in instructions if item['executed'] is None)
    exceptions = sum(1 for item in events if item['kind'] == 'event' and item['type'] == 9)
    gaps = sum(1 for item in events if item['kind'] == 'event'
               and (item['type'] in trace_store.GAP_TYPES
                    or (item['type'] == 2 and item['payload'])))
    summary = dict(packets=0, ranges=len(ranges), instructions=len(instructions),
                   executed=executed, sourceMapped=sum(1 for item in instructions
                                                       if item['line']),
                   exceptions=exceptions, gaps=gaps, packetIssues=0,
                   unknownExecution=unknown)
    quality = dict(inputBytes=4096, processedBytes=4096, unprocessedBytes=0,
                   accountedBytes=4096, overlapBytes=0, unaccountedBytes=0,
                   unsynchronizedBytes=0, unsynchronizedRecords=0, validPackets=0,
                   packetIssues=0, overflowPackets=0, decoderLastError=0,
                   unknownExecution=unknown,
                   firstPathOffset=instructions[0]['offset'],
                   timing=dict(cycleElements=sum(1 for item in ranges if item['cycles']),
                               timeFlameAvailable=False),
                   boundaryPresent=True)
    decoded = dict(version=3, protocol='ETMv4', config=CONFIG, inputBytes=4096,
                   elf=dict(name='fixture.elf', sha256='0' * 64),
                   summary=summary, quality=quality, analysis={},
                   status=dict(kind='status', processed=4096, library='1.8.3',
                               lastErrorCode=0, lastError=''),
                   timeBasis='execution order; no per-instruction timestamp',
                   streamed=dict(eventsFile='events.jsonl', events=len(events),
                                 elements=0, packets=0, nativeJsonl=None, schema=2))
    (folder / 'events.jsonl').write_text(
        ''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in events),
        encoding='utf-8')
    (folder / 'decoded.json').write_text(json.dumps(decoded, ensure_ascii=False),
                                         encoding='utf-8')
    (folder / 'etm.bin').write_bytes(b'\x00' * 64)
    (folder / 'session.json').write_text(json.dumps({'name': 'nav-fixture'}),
                                         encoding='utf-8')
    return folder, events


def _events_of(store):
    return [dict(item) for item in store.events()]


class NavCase(unittest.TestCase):
    """中型合成记录：store + 块 12 + nav sidecar；快/慢两个读取器。"""

    @classmethod
    def setUpClass(cls):
        root = TMP_ROOT / 'medium'
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        cls.record = root / 'record'
        build_medium_record(cls.record)
        cls.store_dir = root / 'store'
        trace_store.write(cls.record, cls.store_dir, verify_elements=False,
                          verify_image=False, hash_inputs=False)
        trace_store_index.build_store_index(cls.store_dir, with_nav=True)
        cls.nav_summary = trace_store_nav.build_nav_index(cls.store_dir)
        cls.fast = trace_store.TraceStore(cls.store_dir, verify='sizes')
        os.environ['TRACE_STORE_SLOW'] = '1'
        try:
            cls.slow = trace_store.TraceStore(cls.store_dir, verify='sizes')
        finally:
            os.environ.pop('TRACE_STORE_SLOW', None)
        # 旧记录：块 12 存在但无 PC_CELLS/LINE_RUNS → 必须回退 sidecar（兼容路径）
        cls.legacy_dir = root / 'store-legacy'
        trace_store.write(cls.record, cls.legacy_dir, verify_elements=False,
                          verify_image=False, hash_inputs=False)
        trace_store_index.build_store_index(cls.legacy_dir, with_nav=False)
        cls.legacy_nav = trace_store_nav.build_nav_index(cls.legacy_dir)
        cls.legacy = trace_store.TraceStore(cls.legacy_dir, verify='sizes')

    @classmethod
    def tearDownClass(cls):
        cls.fast.close()
        cls.slow.close()
        cls.legacy.close()

    def _compare(self, **kwargs):
        got = self.fast.navigate(**kwargs)
        expected = self.slow.navigate(**kwargs)
        self.assertEqual(got, expected,
                         'navigate(%r) 快/慢不一致：%r != %r' % (kwargs, got, expected))
        return got

    def test_nav_sidecar_and_index_loaded(self):
        self.assertIsNotNone(self.fast.query_index)
        self.assertIsNone(self.slow.query_index)
        status = trace_store_nav.nav_status(self.fast)
        self.assertTrue(status['available'], status)
        self.assertEqual(status['source'], 'block12',
                         '块 12 有 PC_CELLS/LINE_RUNS 时必须优先使用')
        self.assertGreater(status['pcCellPairs'], 0)
        self.assertGreater(status['lineRuns'], 0)
        self.assertEqual(self.nav_summary['source'], 'block12')
        self.assertFalse(trace_store_nav.nav_sidecar_path(self.fast).is_file(),
                         '块 12 已内置导航表时不应重复写 sidecar')
        self.assertEqual(self.nav_summary['instructions'],
                         self.fast.counts['instructions'])

    def test_block12_tables_match_sidecar_reference(self):
        """块 12 内置表与 sidecar 参考（同口径构建）逐数组一致。"""
        tables = self.fast.query_index.nav_tables()
        self.assertIsNotNone(tables)
        legacy_tables = self.legacy.query_index.nav_tables()
        self.assertIsNone(legacy_tables, 'with_nav=False 的索引不应有导航表')
        reference = trace_store_nav._load_nav_index(self.legacy)
        self.assertIsNotNone(reference, trace_store_nav.nav_status(self.legacy))
        self.assertEqual(reference.source, 'sidecar')
        self.assertEqual(int(tables['pcCellPairs']), len(reference.cells))
        self.assertEqual(int(tables['lineRuns']), len(reference.line_start))
        import numpy as np
        self.assertEqual(np.asarray(tables['offsets']).tolist(),
                         np.asarray(reference.offsets).tolist())
        self.assertEqual(np.asarray(tables['cells']).tolist(),
                         np.asarray(reference.cells).tolist())
        self.assertEqual(np.asarray(tables['line_start']).tolist(),
                         np.asarray(reference.line_start).tolist())
        self.assertEqual(np.asarray(tables['line_id']).tolist(),
                         np.asarray(reference.line_id).tolist())
        # 逐指令对照：两套表对同一 seq 的导航结果必须一致
        for seq in (0, 1, self.fast.last_seq // 2, self.fast.last_seq):
            for mode in ('call', 'return', 'exception', 'issue', 'source'):
                for direction in (1, -1):
                    self.assertEqual(
                        self.fast.navigate(seq=seq, direction=direction, mode=mode),
                        self.legacy.navigate(seq=seq, direction=direction, mode=mode),
                        '块 12 表与 sidecar 结果不一致：seq=%r mode=%r' % (seq, mode))

    def test_legacy_store_uses_sidecar(self):
        status = trace_store_nav.nav_status(self.legacy)
        self.assertTrue(status['available'], status)
        self.assertEqual(status['source'], 'sidecar')
        self.assertTrue(trace_store_nav.nav_sidecar_path(self.legacy).is_file())
        self.assertEqual(self.legacy_nav['source'], 'sidecar')

    def test_instruction_and_nav_exhaustive(self):
        last = self.fast.last_seq
        modes = ('instruction', 'call', 'return', 'exception', 'issue', 'source')
        for seq in range(-2, last + 3):
            for direction in (1, -1):
                for mode in modes:
                    self._compare(seq=seq, direction=direction, mode=mode)

    def test_event_boundaries(self):
        events = [int(seq) for seq in self.fast.nav[trace_store.NAV_EVENT]]
        self.assertTrue(events)
        for seq in events[:20]:
            for direction in (1, -1):
                for mode in ('instruction', 'call', 'return', 'exception', 'issue',
                             'source'):
                    self._compare(seq=seq, direction=direction, mode=mode)
                for pc in [entry.pc for entry in self.fast.static[:5]]:
                    self._compare(seq=seq, direction=direction, mode='instruction', pc=pc)

    def test_pc_hits_wrap_and_no_hit(self):
        last = self.fast.last_seq
        pcs = [entry.pc for entry in self.fast.static]
        for pc in pcs:
            for seq in (0, 1, last // 3, last // 2, last - 1, last):
                for direction in (1, -1):
                    self._compare(seq=seq, direction=direction, mode='instruction', pc=pc)
        self._compare(seq=last // 2, direction=1, mode='instruction', pc=0xFFFFFFFE)
        self._compare(seq=last // 2, direction=-1, mode='instruction', pc=0xFFFFFFFE)
        # 早期 PC 从末端前向 → 必须回绕
        nav = trace_store_nav._load_nav_index(self.fast)
        for pc in pcs:
            static_id = self.fast.static_by_pc[pc]
            cells = nav.cells_of(static_id)
            if len(cells) and int(cells[0]) == 0:
                result = self._compare(seq=last - 1, direction=1, mode='instruction', pc=pc)
                self.assertTrue(result['wrapped'])
        # 作用域排除全部出现 → 无命中（不回绕到作用域外）
        result = self._compare(seq=0, direction=1, mode='instruction', pc=pcs[0],
                               start=last // 2, end=last)
        self.assertIn('wrapped', result)

    def test_source_cross_file_and_scope(self):
        last = self.fast.last_seq
        for seq in (0, 1, 2, last // 4, last // 2, last - 1, last):
            for direction in (1, -1):
                self._compare(seq=seq, direction=direction, mode='source')
                self._compare(seq=seq, direction=direction, mode='source',
                              start=1, end=max(2, last // 2))
                self._compare(seq=seq, direction=direction, mode='source',
                              start=last // 3, end=last)

    def test_scope_and_out_of_range_seq(self):
        last = self.fast.last_seq
        scopes = [(0, last), (1, last - 1), (last // 3, last // 2),
                  (last - 5, last), (0, 5), (last, 0)]
        for start, end in scopes:
            for seq in (-5, 0, 1, last // 2, last, last + 5):
                for direction in (1, -1):
                    for mode in ('instruction', 'call', 'return', 'exception',
                                 'issue', 'source'):
                        self._compare(seq=seq, direction=direction, mode=mode,
                                      start=start, end=end)
                    for pc in [entry.pc for entry in self.fast.static[:4]]:
                        self._compare(seq=seq, direction=direction, mode='instruction',
                                      pc=pc, start=start, end=end)

    def test_invalid_args_match_slow_error(self):
        with self.assertRaises(trace_store.TraceStoreError):
            self.fast.navigate(seq=0, direction=1, mode='bogus')
        with self.assertRaises(trace_store.TraceStoreError):
            self.slow.navigate(seq=0, direction=1, mode='bogus')

    def test_structure_counts(self):
        import trace_store as store_module
        epochs = dict(expand=0, decode_cells=0)
        original_expand = store_module.TraceStore._expand
        original_decode = trace_store_nav._decode_cell

        def counted_expand(store, *args, **kwargs):
            epochs['expand'] += 1
            yield from original_expand(store, *args, **kwargs)

        def counted_decode(store, index, cell):
            epochs['decode_cells'] += 1
            return original_decode(store, index, cell)

        store_module.TraceStore._expand = counted_expand
        trace_store_nav._decode_cell = counted_decode
        try:
            for seq in range(0, self.fast.last_seq, 11):
                self.fast.navigate(seq=seq, direction=1, mode='instruction')
                self.fast.navigate(seq=seq, direction=-1, mode='instruction')
                self.fast.navigate(seq=seq, direction=1, mode='call')
                self.fast.navigate(seq=seq, direction=-1, mode='issue')
                self.fast.navigate(seq=seq, direction=1, mode='source')
            self.assertEqual(epochs['expand'], 0, 'instruction/NAV/source 不应展开事件')
            before_cells = epochs['decode_cells']
            for pc in [entry.pc for entry in self.fast.static]:
                self.fast.navigate(seq=self.fast.last_seq // 2, direction=1,
                                   mode='instruction', pc=pc)
                self.assertLessEqual(epochs['decode_cells'] - before_cells, 2)
                before_cells = epochs['decode_cells']
        finally:
            store_module.TraceStore._expand = original_expand
            trace_store_nav._decode_cell = original_decode

    def test_fallback_without_sidecar(self):
        path = trace_store_nav.nav_sidecar_path(self.legacy)
        backup = path.with_name(path.name + '.bak')
        shutil.move(str(path), str(backup))
        try:
            fresh = trace_store.TraceStore(self.legacy_dir, verify='sizes')
            try:
                self.assertFalse(trace_store_nav.nav_status(fresh)['available'])
                pc = self.legacy.static[0].pc
                fast_result = fresh.navigate(seq=self.legacy.last_seq // 2, direction=1,
                                             mode='instruction', pc=pc)
                expected = self.slow.navigate(seq=self.legacy.last_seq // 2, direction=1,
                                              mode='instruction', pc=pc)
                self.assertEqual(fast_result, expected)
                # instruction/NAV 不依赖 nav sidecar，仍走快路径
                got = fresh.navigate(seq=10, direction=1, mode='instruction')
                self.assertEqual(got, self.slow.navigate(seq=10, direction=1,
                                                         mode='instruction'))
            finally:
                fresh.close()
        finally:
            shutil.move(str(backup), str(path))

    def test_corrupt_sidecar_falls_back(self):
        path = trace_store_nav.nav_sidecar_path(self.legacy)
        raw = bytearray(path.read_bytes())
        raw[9:13] = (999).to_bytes(4, 'little')
        path.write_bytes(bytes(raw))
        try:
            fresh = trace_store.TraceStore(self.legacy_dir, verify='sizes')
            try:
                self.assertFalse(trace_store_nav.nav_status(fresh)['available'])
                pc = self.legacy.static[0].pc
                got = fresh.navigate(seq=5, direction=1, mode='instruction', pc=pc)
                expected = self.slow.navigate(seq=5, direction=1, mode='instruction', pc=pc)
                self.assertEqual(got, expected)
            finally:
                fresh.close()
        finally:
            self.legacy_nav = trace_store_nav.build_nav_index(self.legacy_dir)

    def test_corrupt_payload_sidecar_falls_back(self):
        path = trace_store_nav.nav_sidecar_path(self.legacy)
        raw = bytearray(path.read_bytes())
        raw[-1] ^= 0xFF                      # 改坏数组内容（header 结构仍合法）
        path.write_bytes(bytes(raw))
        try:
            fresh = trace_store.TraceStore(self.legacy_dir, verify='sizes')
            try:
                status = trace_store_nav.nav_status(fresh)
                self.assertFalse(status['available'], status)
                self.assertIn('CRC32', status['error'])
                pc = self.legacy.static[0].pc
                got = fresh.navigate(seq=2, direction=1, mode='instruction', pc=pc)
                expected = self.slow.navigate(seq=2, direction=1, mode='instruction',
                                              pc=pc)
                self.assertEqual(got, expected)
            finally:
                fresh.close()
        finally:
            trace_store_nav.build_nav_index(self.legacy_dir)

    def test_stale_generation_sidecar_ignored(self):
        path = trace_store_nav.nav_sidecar_path(self.legacy)
        raw = bytearray(path.read_bytes())
        tag = int.from_bytes(raw[12:20], 'little') ^ 0xFFFF
        raw[12:20] = tag.to_bytes(8, 'little')
        path.write_bytes(bytes(raw))
        try:
            fresh = trace_store.TraceStore(self.legacy_dir, verify='sizes')
            try:
                status = trace_store_nav.nav_status(fresh)
                self.assertFalse(status['available'], status)
            finally:
                fresh.close()
        finally:
            trace_store_nav.build_nav_index(self.legacy_dir)

    def test_no_index_store_falls_back(self):
        no_index = TMP_ROOT / 'medium-noindex'
        if no_index.exists():
            shutil.rmtree(no_index)
        shutil.copytree(self.record, no_index)
        store_dir = no_index / 'store'
        trace_store.write(no_index, store_dir, verify_elements=False,
                          verify_image=False, hash_inputs=False)
        store = trace_store.TraceStore(store_dir, verify='sizes')
        try:
            self.assertIsNone(store.query_index)
            got = store.navigate(seq=3, direction=1, mode='instruction')
            self.assertEqual(got, self.slow.navigate(seq=min(3, self.slow.last_seq),
                                                     direction=1, mode='instruction'))
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()
