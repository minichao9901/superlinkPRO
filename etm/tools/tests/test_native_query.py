"""R1-B 判定回归：R1 导航**不需要** C++ 查询 helper，缺失/不符时显式回退。

背景（t3 设计 §5、t5 合同第 3 条）：t4 的 Python 索引 + t5 的 nav sidecar 已使
19.86M store 的 instruction/NAV/PC/源码导航全部满足 P95 ≤200 ms，且中间结构只在
Python 内批量处理；因此 R1 不引入 `etm_query` C++ 进程/ABI。本文件把该判定固化成测试：

* `trace_store_nav` 不依赖任何 `native_query` 模块或可执行文件；
* nav sidecar 缺失/损坏/代不符时，PC/源码导航**返回慢路径结果**（显式回退，不猜）；
* instruction/NAV 快速路径不依赖 sidecar（即使 sidecar 缺失也仍走快路径）。
"""
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
from test_trace_store import build_fixture  # noqa: E402

TMP_ROOT = Path(tempfile.mkdtemp(dir=str(Path(__file__).resolve().parents[2] / 'records'),
                                 prefix='_r1_nativeq_'))


def tearDownModule():
    shutil.rmtree(TMP_ROOT, ignore_errors=True)


class NativeQueryDecisionCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = TMP_ROOT / 'record'
        build_fixture(cls.root)
        cls.store_dir = cls.root / 'store'
        trace_store.write(cls.root, cls.store_dir, verify_image=False)
        # Exercise the legacy sidecar contract explicitly. R2's default
        # embedded PC/line tables otherwise bypass this fallback fixture.
        trace_store_index.build_store_index(cls.store_dir, with_nav=False)
        trace_store_nav.build_nav_index(cls.store_dir, force_sidecar=True)
        cls.fast = trace_store.TraceStore(cls.store_dir, verify='sizes')
        os.environ['TRACE_STORE_SLOW'] = '1'
        try:
            cls.slow = trace_store.TraceStore(cls.store_dir, verify='sizes')
        finally:
            os.environ.pop('TRACE_STORE_SLOW', None)

    @classmethod
    def tearDownClass(cls):
        cls.fast.close()
        cls.slow.close()

    def test_no_native_query_dependency(self):
        """R1 判定：不引入 C++ 查询 helper；模块/可执行文件缺失不影响快路径。"""
        source = Path(trace_store_nav.__file__).read_text(encoding='utf-8')
        self.assertNotIn('native_query', source)
        self.assertNotIn('etm_query', source)
        self.assertNotIn('native_query', sys.modules)
        # PC/源码快速路径在纯 Python 下可用，并与慢路径一致。
        for pc in [entry.pc for entry in self.fast.static[:4]]:
            fast = self.fast.navigate(seq=self.fast.last_seq // 2, direction=1,
                                      mode='instruction', pc=pc)
            slow = self.slow.navigate(seq=self.fast.last_seq // 2, direction=1,
                                      mode='instruction', pc=pc)
            self.assertEqual(fast, slow)
        self.assertEqual(self.fast.navigate(seq=5, direction=1, mode='source'),
                         self.slow.navigate(seq=5, direction=1, mode='source'))
        stats = trace_store_nav.last_stats(self.fast)
        self.assertEqual(stats['expands'], 0)

    def test_missing_sidecar_is_explicit_fallback(self):
        path = trace_store_nav.nav_sidecar_path(self.fast)
        backup = path.with_name(path.name + '.bak')
        shutil.move(str(path), str(backup))
        try:
            fresh = trace_store.TraceStore(self.store_dir, verify='sizes')
            try:
                status = trace_store_nav.nav_status(fresh)
                self.assertFalse(status['available'])
                self.assertIn('nav-index', status['error'])
                pc = self.fast.static[0].pc
                got = fresh.navigate(seq=1, direction=1, mode='instruction', pc=pc)
                expected = self.slow.navigate(seq=1, direction=1, mode='instruction',
                                              pc=pc)
                self.assertEqual(got, expected)
                # instruction/NAV 不依赖 sidecar：仍走快路径（与慢路径同值）
                self.assertEqual(fresh.navigate(seq=1, direction=1, mode='instruction'),
                                 self.slow.navigate(seq=1, direction=1,
                                                    mode='instruction'))
                self.assertEqual(fresh.navigate(seq=1, direction=1, mode='call'),
                                 self.slow.navigate(seq=1, direction=1, mode='call'))
            finally:
                fresh.close()
        finally:
            shutil.move(str(backup), str(path))

    def test_generation_mismatch_is_explicit(self):
        path = trace_store_nav.nav_sidecar_path(self.fast)
        raw = bytearray(path.read_bytes())
        raw[12:20] = (int.from_bytes(raw[12:20], 'little') ^ 1).to_bytes(8, 'little')
        path.write_bytes(bytes(raw))
        try:
            fresh = trace_store.TraceStore(self.store_dir, verify='sizes')
            try:
                status = trace_store_nav.nav_status(fresh)
                self.assertFalse(status['available'])
                self.assertIn('generation', status['error'])
            finally:
                fresh.close()
        finally:
            trace_store_nav.build_nav_index(self.store_dir)


if __name__ == '__main__':
    unittest.main()
