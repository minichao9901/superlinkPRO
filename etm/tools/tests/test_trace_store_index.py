"""R1 查询索引测试：构建/加载/快路径逐字段对照/慢路径回退/结构计数。

覆盖 `docs/etm-r1-query-design.md` 的验收要点（t4 合同）：
* 持久化索引（块类型 12）+ 构建命令/`build_store_index`；旧记录无索引 → 慢路径且结果不变；
* 默认全段视图 + 任意区间/任意 bins 的算法路径都与 `TRACE_STORE_SLOW=1` 的慢路径逐字段一致；
* 索引损坏/版本不符 → 记 `query-index` issue 并回退慢路径（不返回拼凑结果）；
* 结构检查：全段 window/timeline 不调用 `_expand`/`_instruction_event`/`CallTracker.feed_instruction`；
* adapter 走快路径，page/search/hide_skipped/event/source 语义不变。

数据：`test_trace_store.build_fixture` 的合成记录（含调用/异常/谓词未知/周期/边界事件），
经真实 `trace_store.write()` 写成 store 后建索引；不依赖网络/硬件/真实采集。
"""
import atexit
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import flow_analysis
import trace_store
import trace_store_adapter
import trace_store_index
from test_trace_store import build_fixture

ETM_ROOT = Path(__file__).resolve().parents[2]
TMP_ROOT = Path(tempfile.mkdtemp(dir=str(ETM_ROOT / 'records'), prefix='_r1_index_'))
atexit.register(shutil.rmtree, TMP_ROOT, True)


def _work_dir(name):
    path = TMP_ROOT / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class IndexCase(unittest.TestCase):
    """公共夹具：合成记录 → 无索引 store → 建索引 → 快/慢两个读取器。"""

    def setUp(self):
        self.root = _work_dir(self._testMethodName)
        self.record = self.root / 'record'
        build_fixture(self.record)
        self.store_dir = self.record / 'store'
        trace_store.write(self.record, self.store_dir, verify_image=False)
        # 建索引前的读取器：旧记录（无 12 号块）→ 慢路径；迁移后旧 generation 仍在。
        self.plain = trace_store.TraceStore(self.store_dir, verify='sizes')
        self.build = trace_store_index.build_store_index(self.store_dir)
        self.fast = trace_store.TraceStore(self.store_dir, verify='sizes')
        os.environ['TRACE_STORE_SLOW'] = '1'
        try:
            self.slow = trace_store.TraceStore(self.store_dir, verify='sizes')
        finally:
            os.environ.pop('TRACE_STORE_SLOW', None)

    def tearDown(self):
        for name in ('plain', 'fast', 'slow'):
            store = getattr(self, name, None)
            if store is not None:
                store.close()

    # -- 索引本身 ---------------------------------------------------------------------------
    def test_migration_publishes_query_block_and_metadata(self):
        self.assertIsNone(self.plain.query_index, '旧记录（迁移前）不应有查询索引')
        self.assertIsNotNone(self.fast.query_index, self.fast.query_index_error)
        manifest = json.loads((self.store_dir / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['generation'], self.build['generation'])
        index = manifest.get('queryIndex') or {}
        self.assertEqual(index.get('version'), trace_store_index.QUERY_INDEX_VERSION)
        self.assertEqual(index.get('cellSize'), trace_store_index.DEFAULT_CELL_INSTRUCTIONS)
        self.assertEqual((manifest.get('counts') or {}).get('queryIndex', {}).get('cells'),
                         index.get('cells'))
        self.assertIn('defaultViews', index)
        self.assertEqual(sorted(index['defaultViews']), ['timeline', 'window'])
        meta_query = self.fast.meta.get('queryIndex') or {}
        self.assertEqual(meta_query.get('cells'), index.get('cells'))
        # 12 号块只有一个，且 optional_features 置了 QUERY_INDEX_FEATURE
        blocks = self.fast.container.of_type(trace_store.BLK_QUERY)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(self.fast.container.optional_features,
                         trace_store.QUERY_INDEX_FEATURE)
        stats = self.fast.stats()['queryIndex']
        self.assertTrue(stats['available'])
        self.assertEqual(stats['cells'], index.get('cells'))
        self.assertFalse(self.plain.stats()['queryIndex']['available'])
        # 重建（再次 build）仍然只有一个索引块、旧 generation 保留
        again = trace_store_index.build_store_index(self.store_dir)
        self.assertNotEqual(again['generation'], self.build['generation'])
        reopened = trace_store.TraceStore(self.store_dir, verify='sizes')
        try:
            self.assertEqual(len(reopened.container.of_type(trace_store.BLK_QUERY)), 1)
            self.assertIsNotNone(reopened.query_index)
        finally:
            reopened.close()

    def test_payload_encodes_version_and_default_views(self):
        payload = self.fast.container.block(
            self.fast.container.of_type(trace_store.BLK_QUERY)[0])
        self.assertTrue(payload.startswith(trace_store_index.QUERY_BLOCK_MAGIC))
        self.assertEqual(payload[len(trace_store_index.QUERY_BLOCK_MAGIC)],
                         trace_store_index.QUERY_INDEX_VERSION)
        archive = trace_store_index.decode_payload(payload)
        for name in ('default_window_json', 'default_timeline_json', 'ps_start', 'fs_start',
                     'cell_insn', 'ev_seq', 'st_func'):
            self.assertIn(name, archive.files)
        meta = self.fast.query_index.meta
        for name in ('window', 'timeline'):
            text = trace_store_index._json_text(archive['default_%s_json' % name])
            import hashlib
            self.assertEqual(hashlib.sha256(text.encode('utf-8')).hexdigest(),
                             meta['defaultViews'][name]['sha256'])
        # 默认视图区间必须与 store 一致
        window = json.loads(trace_store_index._json_text(archive['default_window_json']))
        self.assertEqual(window['range']['totalStart'], self.fast.first_seq)
        self.assertEqual(window['range']['totalEnd'], self.fast.last_seq)

    # -- 默认视图 / 算法路径逐字段对照 ------------------------------------------------------
    def test_default_full_range_uses_persisted_view(self):
        index = self.fast.query_index
        self.assertIsNotNone(index._default_view('window', self.fast.first_seq,
                                                 self.fast.last_seq, 1200))
        self.assertIsNotNone(index._default_view('timeline', self.fast.first_seq,
                                                 self.fast.last_seq, 1600,
                                                 '', False, 6))
        # 非默认参数/非全段必须走算法路径（不命中预计算视图）
        self.assertIsNone(index._default_view('window', self.fast.first_seq,
                                             self.fast.last_seq, 600))
        self.assertIsNone(index._default_view('window', self.fast.first_seq,
                                             self.fast.last_seq - 1, 1200))
        self.assertIsNone(index._default_view('timeline', self.fast.first_seq,
                                             self.fast.last_seq, 1600, 'main', False, 6))
        self.assertEqual(_dumps(self.fast.window()), _dumps(self.slow.window()))
        self.assertEqual(_dumps(self.fast.function_timeline()),
                         _dumps(self.slow.function_timeline()))

    def test_algorithmic_ranges_match_slow_oracle(self):
        total = self.fast.last_seq
        cases = [(0, total, 600), (0, total, 100), (0, total, 2000),
                 (0, 0, 300), (0, 1, 100), (total, total, 300),
                 (1, 14, 100), (2, 2, 100), (3, 13, 240), (0, 15, 1200),
                 (total - 3, total - 1, 100), (0, total, 1200)]
        for start, end, bins in cases:
            mine = self.fast.window(start, end, bins)
            theirs = self.slow.window(start, end, bins)
            self.assertEqual(_dumps(mine), _dumps(theirs),
                             'window(%r,%r,%r) 不一致' % (start, end, bins))
            self.assertEqual(mine['range'], theirs['range'])
        for start, end, bins, max_functions in ((0, total, 240, 3), (2, total, 1600, 6),
                                                (0, 10, 600, 10), (1, 14, 2400, 2)):
            mine = self.fast.function_timeline(start, end, bins=bins,
                                               max_functions=max_functions)
            theirs = self.slow.function_timeline(start, end, bins=bins,
                                                 max_functions=max_functions)
            self.assertEqual(_dumps(mine), _dumps(theirs),
                             'timeline(%r,%r,%r) 不一致' % (start, end, bins))

    def test_seq_of_insn_skips_leading_consecutive_events(self):
        """回归：记录开头连续事件 [0,1] 后，指令 0 的 seq 必须是 2（t9 复现缺陷）。"""
        import numpy as np
        index = self.fast.query_index
        index.prepare()
        events = np.asarray(index.event_seqs)
        self.assertTrue(len(events) >= 2 and events[0] == 0 and events[1] == 1,
                        '合成记录应以两条连续事件开头：%r' % events[:4].tolist())
        self.assertEqual(int(index.seq_of_insn(0)), 2)
        self.assertEqual(int(index.seq_of_insn(0)), index.next_instruction_seq(0))
        self.assertEqual(int(index.seq_of_insn(0)),
                         self.fast.navigate(0, 1, 'instruction')['seq'])
        # 全量往返：insn_of_seq(seq_of_insn(i)) == i，且不落在事件 seq 上
        indices = np.arange(index.total_instructions, dtype=np.int64)
        seqs = index.seq_of_insn(indices)
        self.assertTrue(np.array_equal(index.insn_of_seq(seqs), indices))
        self.assertFalse(bool(np.isin(seqs, events).any()),
                         'seq_of_insn 结果落到了事件 seq 上')
        self.assertTrue(bool(np.all(np.diff(seqs) > 0)), '指令 seq 必须严格递增')

    def test_irq_entry_when_window_starts_at_exception_event(self):
        """回归：窗口起点正好是 type-9 事件时，中断入口必须计入（§4.3-4）。"""
        index = self.fast.query_index
        index.prepare()
        events = self.slow.nav[trace_store.NAV_EVENT]
        exception_seq = None
        for seq in events:
            event = self.slow.event(seq)['event']
            if event.get('type') == 9:
                exception_seq = int(seq)
                break
        self.assertIsNotNone(exception_seq, '合成记录应含 type-9 异常事件')
        end = self.fast.last_seq
        mine = self.fast.window(exception_seq, end, 120)
        theirs = self.slow.window(exception_seq, end, 120)
        self.assertEqual(_dumps(mine), _dumps(theirs))
        mine_interrupts = sum(item['interruptEntries']
                              for item in mine['analysis']['functions'])
        self.assertGreater(mine_interrupts, 0,
                           '该用例必须真正覆盖中断入口（否则测试是空转）')
        # 起点在事件之前一条（指令）也同样一致
        self.assertEqual(_dumps(self.fast.window(exception_seq - 1, end, 120)),
                         _dumps(self.slow.window(exception_seq - 1, end, 120)))

    def test_event_boundary_windows_match_slow_oracle(self):
        """把窗口起点/终点放在每条事件 seq 上：window + timeline 均需逐字段一致。"""
        events = list(self.slow.nav[trace_store.NAV_EVENT])
        self.assertTrue(events)
        end = self.fast.last_seq
        for seq in events:
            for start in (max(0, seq), min(end, seq + 1)):
                for stop in (start, min(end, start + 50), end):
                    self.assertEqual(_dumps(self.fast.window(start, stop, 100)),
                                     _dumps(self.slow.window(start, stop, 100)),
                                     'window(%d,%d) 与慢路径不一致' % (start, stop))
                    self.assertEqual(
                        _dumps(self.fast.function_timeline(start, stop, bins=100)),
                        _dumps(self.slow.function_timeline(start, stop, bins=100)),
                        'timeline(%d,%d) 与慢路径不一致' % (start, stop))

    def test_inverted_range_raises_store_error(self):
        with self.assertRaises(trace_store.TraceStoreError):
            self.fast.window(10, 5, 300)
        with self.assertRaises(trace_store.TraceStoreError):
            self.fast.function_timeline(10, 5)

    def test_search_and_hide_skipped_keep_slow_semantics(self):
        total = self.fast.last_seq
        for hide in (False, True):
            self.assertEqual(
                _dumps(self.fast.function_timeline(0, total, hide_skipped=hide)),
                _dumps(self.slow.function_timeline(0, total, hide_skipped=hide)))
        self.assertEqual(_dumps(self.fast.function_timeline(0, total, search='main')),
                         _dumps(self.slow.function_timeline(0, total, search='main')))
        self.assertEqual(_dumps(self.fast.page('events', 0, 40, 'main', False, 0, total)),
                         _dumps(self.slow.page('events', 0, 40, 'main', False, 0, total)))
        self.assertEqual(_dumps(self.fast.page('events', 0, 40, '', True, 0, total)),
                         _dumps(self.slow.page('events', 0, 40, '', True, 0, total)))
        for seq in (0, 1, total // 2, total):
            self.assertEqual(_dumps(self.fast.event(seq)), _dumps(self.slow.event(seq)))
        files = sorted((self.slow.sources or {}))
        self.assertTrue(files)
        self.assertEqual(_dumps(self.fast.source(files[0])),
                         _dumps(self.slow.source(files[0])))
        self.assertEqual(_dumps(self.fast.issues(0, 50)), _dumps(self.slow.issues(0, 50)))

    # -- 冷打开优化（t10）：惰性 nav/issues + 并行整份 sha256 ------------------------------
    def test_nav_and_issues_are_parsed_lazily(self):
        """打开时不解析块 5（nav）与块 10（issues）；首次访问才解析，结果不变。"""
        store = trace_store.TraceStore(self.store_dir, verify='full')
        try:
            self.assertIsNone(store._nav, '打开时不应解析 nav 块')
            self.assertIsNone(store._issues, '打开时不应解析 issues 块')
            self.assertEqual(store.stats()['queryIndex']['available'], True)
            events = list(store.nav[trace_store.NAV_EVENT])
            self.assertTrue(events, '首次访问 nav 时必须解析出稀疏表')
            self.assertIsNotNone(store._nav)
            self.assertIsInstance(store.issues(0, 50), dict)
            self.assertIsNotNone(store._issues)
            self.assertEqual(_dumps(store.issues(0, 50)),
                             _dumps(self.slow.issues(0, 50)))
            self.assertEqual(_dumps(store.navigate(0, 1, 'exception')),
                             _dumps(self.slow.navigate(0, 1, 'exception')))
        finally:
            store.close()

    def test_eager_load_env_restores_old_open_timing(self):
        """TRACE_STORE_EAGER_LOAD=1（诊断）恢复“打开即解析”的旧时序，语义不变。"""
        os.environ['TRACE_STORE_EAGER_LOAD'] = '1'
        try:
            store = trace_store.TraceStore(self.store_dir, verify='full')
        finally:
            os.environ.pop('TRACE_STORE_EAGER_LOAD', None)
        try:
            self.assertIsNotNone(store._nav)
            self.assertIsNotNone(store._issues)
            self.assertEqual(_dumps(store.window()), _dumps(self.slow.window()))
        finally:
            store.close()

    def test_tampered_store_bin_rejected_by_whole_file_sha256(self):
        """整份 sha256 仍生效：改 store.bin 任意一个字节（不更新 manifest）→ 打开被拒。"""
        copy = self.root / 'tampered-record'
        shutil.copytree(self.record, copy)
        manifest = json.loads((copy / 'store' / 'manifest.json').read_text(encoding='utf-8'))
        store_bin = (copy / 'store' / 'generations' / manifest['generation'] / 'store.bin')
        data = bytearray(store_bin.read_bytes())
        data[len(data) // 2] ^= 0xFF
        store_bin.write_bytes(bytes(data))
        with self.assertRaises(trace_store.TraceStoreError) as caught:
            trace_store.TraceStore(copy / 'store', verify='full')
        self.assertIn('sha256', str(caught.exception))

    def test_lazy_block_corruption_is_detected_on_first_use(self):
        """惰性块的损坏检测：manifest sha 被同步改写时，打开不解析 nav/issues；
        首次访问该块时按块 CRC 拒绝（整份 sha256 仍是打开时的第一道门）。"""
        copy = self.root / 'nav-corrupt-record'
        shutil.copytree(self.record, copy)
        store_dir = copy / 'store'
        manifest_path = store_dir / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        generation_dir = store_dir / 'generations' / manifest['generation']
        store_bin = generation_dir / 'store.bin'
        probe = trace_store.TraceStore(store_dir, verify='sizes')
        try:
            ordinal = probe.container.of_type(trace_store.BLK_NAV)[0]
            offset = probe.container.blocks[ordinal].offset
        finally:
            probe.close()
        data = bytearray(store_bin.read_bytes())
        data[offset + 8] ^= 0xFF
        store_bin.write_bytes(bytes(data))
        import hashlib
        manifest['files'][0]['sha256'] = hashlib.sha256(bytes(data)).hexdigest()
        manifest['files'][0]['bytes'] = len(data)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        store = trace_store.TraceStore(store_dir, verify='full')      # 整份 sha 匹配 → 可打开
        try:
            with self.assertRaises(trace_store.TraceStoreError) as caught:
                _ = store.nav
            self.assertIn('校验和', str(caught.exception))
        finally:
            store.close()

    # -- 块 12 导航表（t14）：PC_CELLS / LINE_RUNS ------------------------------------------
    def test_query_index_contains_nav_tables(self):
        """新构建的块 12 应带 PC_CELLS/LINE_RUNS，且与 sidecar 参考同口径。"""
        index = self.fast.query_index
        self.assertIsNotNone(index)
        self.assertIsNotNone(index.meta.get('nav'))
        tables = index.nav_tables()
        self.assertIsNotNone(tables)
        self.assertEqual(tables['static_count'], len(self.fast.static))
        self.assertEqual(tables['instructions'], int(self.fast.counts['instructions']))
        self.assertGreater(tables['pcCellPairs'], 0)
        self.assertGreater(tables['lineRuns'], 0)
        self.assertEqual(int(tables['offsets'][0]), 0)
        self.assertEqual(int(tables['offsets'][-1]), tables['pcCellPairs'])
        self.assertEqual(int(tables['line_start'][0]), 0)
        self.assertEqual(len(tables['line_id']), tables['lineRuns'])

    def test_nav_tables_checksum_mismatch_rejected(self):
        """meta.nav.checksum 不符 → nav_tables() 明确拒绝（nav 侧回退 sidecar/慢路径）。"""
        index = self.fast.query_index
        nav = index.meta['nav']
        original = nav['checksum']
        try:
            nav['checksum'] = (int(original) ^ 1) & 0xFFFFFFFF
            index._nav_tables_loaded = False
            index._nav_tables = None
            with self.assertRaises(trace_store_index.QueryIndexError):
                index.nav_tables()
        finally:
            nav['checksum'] = original
            index._nav_tables_loaded = False
            index._nav_tables = None
        self.assertIsNotNone(index.nav_tables())

    def test_nav_tables_absent_in_legacy_index(self):
        """with_nav=False 的索引（等价旧块 12）没有导航表，但窗口查询照常。"""
        legacy = self.root / 'record-legacy-nav'
        shutil.copytree(self.record, legacy)
        store_dir = legacy / 'store'
        trace_store_index.build_store_index(store_dir, with_nav=False)
        store = trace_store.TraceStore(store_dir, verify='sizes')
        try:
            self.assertIsNotNone(store.query_index)
            self.assertIsNone(store.query_index.nav_tables())
            self.assertIsNone(store.query_index.meta.get('nav'))
            self.assertEqual(_dumps(store.window()), _dumps(self.slow.window()))
            self.assertEqual(_dumps(store.function_timeline()),
                             _dumps(self.slow.function_timeline()))
        finally:
            store.close()

    # -- 块 12 v2 容器（C++ 友好：表目录 + 每表 zlib）--------------------------------------
    def test_v2_payload_roundtrip_matches_v1_tables(self):
        """v2 编码/解码与 v1 表集逐表一致（表名/dtype/shape/字节）。"""
        archive = self.fast.query_index.arrays._archive
        arrays = {name: np.asarray(archive[name]) for name in archive.files}
        meta = dict(self.fast.query_index.meta)
        payload = trace_store_index.encode_payload_v2(arrays, meta)
        self.assertEqual(payload[:len(trace_store_index.QUERY_V2_MAGIC)],
                         trace_store_index.QUERY_V2_MAGIC)
        decoded = trace_store_index.decode_payload(payload)
        self.assertIsInstance(decoded, trace_store_index._V2Archive)
        self.assertEqual(sorted(decoded.files), sorted(arrays))
        for name in arrays:
            expected = np.ascontiguousarray(arrays[name])
            actual = np.ascontiguousarray(decoded[name])
            self.assertEqual(actual.dtype, expected.dtype, name)
            self.assertEqual(actual.shape, expected.shape, name)
            self.assertEqual(actual.tobytes(), expected.tobytes(), name)
        self.assertEqual(np.asarray(decoded['meta_json']).tobytes(),
                         np.asarray(arrays['meta_json']).tobytes())

    def test_v2_store_query_nav_and_lazy_loading(self):
        """用 Python 参考构建 v2 索引：window/timeline/nav 与 v1/慢路径一致，导航表懒加载。"""
        if not hasattr(trace_store_index, 'encode_payload_v2'):
            self.skipTest('本版本没有 v2 容器')
        v2_dir = self.root / 'v2-store'
        shutil.copytree(self.store_dir, v2_dir)
        trace_store_index.build_store_index(v2_dir, container='v2')
        store = trace_store.TraceStore(v2_dir, verify='sizes')
        try:
            manifest = json.loads((v2_dir / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual((manifest.get('queryIndex') or {}).get('container'), 'v2')
            self.assertIsNotNone(store.query_index, store.query_index_error)
            self.assertEqual(_dumps(store.window()), _dumps(self.fast.window()))
            self.assertEqual(_dumps(store.function_timeline()),
                             _dumps(self.fast.function_timeline()))
            loaded = set(getattr(store.query_index.arrays, '_cache', {}))
            self.assertFalse(loaded & set(trace_store_index.NAV_TABLE_NAMES),
                             '默认窗口命中不应解压 PC/line 四表：%r' % (sorted(loaded),))
            self.assertIsNotNone(store.query_index.nav_tables())
            for seq in (self.fast.first_seq, self.fast.last_seq // 2, self.fast.last_seq):
                for mode in ('instruction', 'call', 'return', 'exception', 'issue',
                             'source'):
                    self.assertEqual(
                        _dumps(store.navigate(seq=seq, direction=1, mode=mode)),
                        _dumps(self.slow.navigate(seq=seq, direction=1, mode=mode)),
                        'v2 导航与慢路径不一致：seq=%r mode=%r' % (seq, mode))
        finally:
            store.close()

    def test_v2_unknown_version_and_corrupt_table_rejected(self):
        """v2 读取端的明确拒绝：未知版本 / 表 CRC 不符 / 未知 magic。"""
        archive = self.fast.query_index.arrays._archive
        arrays = {name: np.asarray(archive[name]) for name in archive.files}
        meta = dict(self.fast.query_index.meta)
        payload = bytearray(trace_store_index.encode_payload_v2(arrays, meta))
        bad_version = bytearray(payload)
        struct.pack_into('<I', bad_version, 8, 9)
        with self.assertRaises(trace_store_index.QueryIndexError):
            trace_store_index.decode_payload(bytes(bad_version))
        unknown = bytearray(payload)
        unknown[:8] = b'ETMQIDX9'
        with self.assertRaises(trace_store_index.QueryIndexError):
            trace_store_index.decode_payload(bytes(unknown))
        decoded = trace_store_index.decode_payload(bytes(payload))
        name = 'ev_seq'
        _shape, _dtype, offset, compressed, _uncompressed, _crc = decoded._items[name]
        corrupted = bytearray(payload)
        corrupted[offset] ^= 0xFF
        broken = trace_store_index.decode_payload(bytes(corrupted))
        with self.assertRaises(trace_store_index.QueryIndexError):
            _ = broken[name]

    # -- 回退路径 ---------------------------------------------------------------------------
    def test_slow_env_forces_slow_path(self):
        self.assertIsNone(self.slow.query_index)
        self.assertEqual(_dumps(self.slow.window()), _dumps(self.plain.window()))
        self.assertFalse(self.slow.stats()['queryIndex']['available'])

    def test_corrupt_query_block_falls_back_with_issue(self):
        # 在独立副本上破坏 12 号块的一个字节：打开时 CRC 失败 → issue + 慢路径。
        copy = self.root / 'corrupt-record'
        shutil.copytree(self.record, copy)
        copy_store = copy / 'store'
        store = trace_store.TraceStore(copy_store, verify='sizes')
        try:
            ordinal = store.container.of_type(trace_store.BLK_QUERY)[0]
            offset = store.container.blocks[ordinal].offset
        finally:
            store.close()
        with open(copy_store / 'generations' / self.build['generation'] / 'store.bin',
                  'r+b') as handle:
            handle.seek(offset + 12)
            value = handle.read(1)
            handle.seek(offset + 12)
            handle.write(bytes([value[0] ^ 0xFF]))
        broken = trace_store.TraceStore(copy_store, verify='sizes')
        try:
            self.assertIsNone(broken.query_index)
            self.assertTrue(broken.query_index_error)
            self.assertTrue(any(item.get('category') == 'query-index'
                                for item in broken.issues_list),
                            '损坏的查询索引必须记录 query-index issue')
            os.environ['TRACE_STORE_SLOW'] = '1'
            try:
                oracle = trace_store.TraceStore(copy_store, verify='sizes')
            finally:
                os.environ.pop('TRACE_STORE_SLOW', None)
            try:
                self.assertEqual(_dumps(broken.window()), _dumps(oracle.window()))
            finally:
                oracle.close()
        finally:
            broken.close()

    def test_wrong_version_payload_is_rejected(self):
        payload = trace_store_index.encode_payload({'a': [1]}, {'x': 1})
        bad = payload[:len(trace_store_index.QUERY_BLOCK_MAGIC)] + b'\x7f' + \
            payload[len(trace_store_index.QUERY_BLOCK_MAGIC) + 1:]
        with self.assertRaises(trace_store_index.QueryIndexError):
            trace_store_index.decode_payload(bad)
        with self.assertRaises(trace_store_index.QueryIndexError):
            trace_store_index.decode_payload(b'not-an-index')
        # meta 与当前代不符：明确拒绝（由 validate_meta 判定）
        meta = dict(self.fast.query_index.meta)
        meta['instructions'] = int(meta['instructions']) + 1
        with self.assertRaises(trace_store_index.QueryIndexError):
            trace_store_index.validate_meta(self.fast, meta)

    # -- 结构检查 ---------------------------------------------------------------------------
    def test_fast_path_has_no_per_instruction_dict_or_tracker(self):
        counters = dict(expand=0, instruction_event=0, feed=0, events=0)
        original_expand = trace_store.TraceStore._expand
        original_event = trace_store.TraceStore._instruction_event
        original_feed = flow_analysis.CallTracker.feed_instruction
        original_events = trace_store.TraceStore.events

        def expand(self, *args, **kwargs):
            counters['expand'] += 1
            return original_expand(self, *args, **kwargs)

        def instruction_event(self, *args, **kwargs):
            counters['instruction_event'] += 1
            return original_event(self, *args, **kwargs)

        def feed(self, *args, **kwargs):
            counters['feed'] += 1
            return original_feed(self, *args, **kwargs)

        def events(self, *args, **kwargs):
            counters['events'] += 1
            return original_events(self, *args, **kwargs)

        with patch.object(trace_store.TraceStore, '_expand', expand), \
                patch.object(trace_store.TraceStore, '_instruction_event', instruction_event), \
                patch.object(flow_analysis.CallTracker, 'feed_instruction', feed), \
                patch.object(trace_store.TraceStore, 'events', events):
            self.fast.window()                                  # 默认视图
            self.fast.function_timeline()                        # 默认视图
            self.fast.window(0, self.fast.last_seq, 600)         # 算法路径（全段）
            self.fast.window(1, 14, 300)                         # 算法路径（边界单元格解码）
            self.fast.function_timeline(0, 15, bins=240, max_functions=3)
        self.assertEqual(counters, dict(expand=0, instruction_event=0, feed=0, events=0),
                         '快路径出现了逐指令展开/事件字典/CallTracker：%r' % counters)

    # -- 写入期构建（--build-query-index 的核心路径）----------------------------------------
    def test_build_in_sink_matches_migration_payload_semantics(self):
        source = trace_store.TraceStore(self.store_dir, verify='sizes')
        try:
            sink = trace_store._BlockSink(self.root / 'sink.payload')
            try:
                for ordinal, entry in enumerate(source.container.blocks):
                    payload = source.container.raw_block(ordinal)
                    sink.add_raw(entry.block_type, payload, entry.count, entry.flags)
                result = trace_store_index.build_in_sink(
                    sink, source.static, source.strings, source.counts,
                    source.first_seq, source.last_seq)
                self.assertGreater(result['payloadBytes'], 0)
                archive = trace_store_index.decode_payload(result['payload'])
                meta = json.loads(trace_store_index._json_text(archive['meta_json']))
                self.assertEqual(meta['instructions'], source.counts['instructions'])
                self.assertEqual(sorted((meta.get('defaultViews') or {})),
                                 ['timeline', 'window'])
                self.assertIn(trace_store.BLK_QUERY,
                              [entry[3] for entry in sink.entries])
            finally:
                sink.close()
        finally:
            source.close()

    # -- adapter ----------------------------------------------------------------------------
    def test_adapter_uses_fast_path_and_reports_index(self):
        adapter = trace_store_adapter.TraceStoreAdapter(self.record)
        try:
            adapter.ensure()
            self.assertEqual(_dumps(adapter.window()), _dumps(self.slow.window()))
            self.assertEqual(_dumps(adapter.function_timeline()),
                             _dumps(self.slow.function_timeline()))
            store = adapter._store
            self.assertIsNotNone(store.query_index)
            self.assertTrue(store.stats()['queryIndex']['available'])
            trace = adapter.overview()['traceStore']
            self.assertTrue(trace['queryIndex']['available'])
            self.assertEqual(trace['queryIndex']['cells'],
                             store.stats()['queryIndex']['cells'])
            self.assertEqual(_dumps(adapter.page('events', 0, 10)),
                             _dumps(self.slow.page('events', 0, 10)))
            self.assertEqual(_dumps(adapter.event(1)), _dumps(self.slow.event(1)))
        finally:
            adapter.close()
            trace_store_adapter.forget(self.record)


if __name__ == '__main__':
    unittest.main()
