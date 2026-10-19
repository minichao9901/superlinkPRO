"""R0-A 回归测试：适配器缓存换代隔离 + 绑定输入按内容核验。

对应 Codex 2026-10-20 审查（`docs/etm-1s-next-steps-20261020.md` §2.2 / §2.4）：

* §2.2 [P1]：`ensure()` 换代后 `_windows/_timelines` 不失效、缓存键也没有 generation，
  同一记录的 window/function_timeline 会继续返回旧代结果（`probe_cache.py` 已复现）。
  修复后：缓存键绑定实际 store 代（generation + 格式版本），换代/`close()` 清理旧条目；
  在途旧请求完成后不得把旧结果写进新代缓存；换代时旧 store 延迟关闭。
* §2.4 [P2]：`_stale_inputs()` 的 `MAX_HASH_BYTES=64 MiB` 让大输入只比尺寸，声明错误
  sha256 的 67,108,865 B `native.bin` 被返回“无问题”。修复后：声明了 sha256 就按内容
  流式核验（无体积阈值），同长度 + mtime 不变的改写必须被拒绝；`--no-hash-inputs`
  产物（`bindings.inputHash.skipped=true`）显式标记“未核验”。

数据来源：
* 桩（`_StubStore`）复刻 Codex `probe_cache.py` 的双代最小复现（不碰真实记录）；
* 合成记录（`test_trace_store.build_fixture`）经真实 `trace_store.write` 落两代，覆盖
  “同 record ID 重解码 → 查询返回新结果”与“取消/失败保留旧完整代”；
* `docs/validation/20261020-codex-.../binding-probe/native.bin`（67,108,865 B）复现
  大输入错误 sha256 必须报不一致。
"""
import atexit
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import trace_store
import trace_store_adapter
from test_trace_store import build_fixture as build_record_fixture

ETM_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = ETM_ROOT.parent
BINDING_PROBE = (REPO_ROOT / 'docs/validation/20261020-codex-ds-next-1685c01-6e93c42a'
                 / 'binding-probe/native.bin')
# 合成 fixture 很小；放在 etm/records（gitignored）下才能对 binding-probe 用硬链接，
# 不重复复制 64 MiB。
TMP_ROOT = Path(tempfile.mkdtemp(dir=str(ETM_ROOT / 'records'), prefix='_r0_cache_'))
atexit.register(shutil.rmtree, TMP_ROOT, True)


def _work_dir(name):
    path = TMP_ROOT / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


class _StubStore:
    """最小 store 桩：与 Codex `probe_cache.py` 同形（generation/window/timeline/close）。

    ``blocking=True`` 时第一次计算会阻塞，直到测试调用 ``release.set()``，用来验证
    “旧代计算中、新代已发布”的竞态。
    """

    def __init__(self, generation, blocking=False):
        self.generation = generation
        self.blocking = blocking
        self.started = threading.Event()
        self.release = threading.Event()
        self.closed = False

    def _value(self, kind, args):
        if self.blocking:
            self.started.set()
            if not self.release.wait(20):
                raise RuntimeError('测试桩等待释放超时')
        return dict(kind=kind, generation=self.generation, args=list(args))

    def window(self, *args):
        return self._value('window', args)

    def function_timeline(self, *args):
        return self._value('timeline', args)

    def close(self):
        self.closed = True


class _StubCase(unittest.TestCase):
    """桩场景公共装配：``signature()`` 返回一个可变状态，``_open()`` 按状态造 store。"""

    def setUp(self):
        self.root = _work_dir(self._testMethodName)
        self.state = dict(signature='old')
        self.open_calls = []
        self.adapter = trace_store_adapter.TraceStoreAdapter(self.root / 'no-record')
        self.adapter.signature = self._signature
        self.adapter._open = self._open

    def tearDown(self):
        self.adapter.close()

    def _signature(self):
        return self.state['signature']

    def _open(self):
        generation = self.state['signature']
        self.open_calls.append(generation)
        factory = getattr(self, 'opener', None)
        if factory is not None:
            return factory(generation)
        return _StubStore(generation)

    def token(self):
        return self.adapter._store_token(self.adapter._store)


class MinimalReproductionTests(_StubCase):
    """Codex `probe_cache.py` 的最小复现：换代后 window/timeline 必须返回新代。"""

    def test_generation_swap_returns_new_window_and_timeline(self):
        before = dict(window=self.adapter.window(), timeline=self.adapter.function_timeline())
        self.state['signature'] = 'new'
        after = dict(window=self.adapter.window(), timeline=self.adapter.function_timeline())
        result = dict(before=before, after=after, activeStore=self.adapter._store.generation,
                      stale=(after['window']['generation'] != 'new'
                             or after['timeline']['generation'] != 'new'))
        self.assertEqual(result['before']['window']['generation'], 'old')
        self.assertEqual(result['after']['window']['generation'], 'new')
        self.assertEqual(result['after']['timeline']['generation'], 'new')
        self.assertEqual(result['activeStore'], 'new')
        self.assertFalse(result['stale'], '换代后仍返回旧代结果（Codex probe_cache 复现项）')
        # 旧代缓存条目被清理，只剩当前代
        self.assertEqual({key[0] for key in self.adapter._windows}, {self.token()})
        self.assertEqual({key[0] for key in self.adapter._timelines}, {self.token()})

    def test_cache_key_is_bound_to_generation(self):
        self.adapter.window(bins=1200)
        first_token = self.token()
        self.assertIn('schema-%s' % trace_store.SCHEMA_VERSION, first_token)
        self.assertIn('old', first_token)
        self.state['signature'] = 'new'
        self.adapter.ensure()
        second_token = self.token()
        self.assertNotEqual(first_token, second_token)
        self.assertIn('new', second_token)
        self.assertEqual(self.adapter._windows, {})

    def test_close_clears_window_and_timeline_caches(self):
        cached_window = self.adapter.window()
        cached_timeline = self.adapter.function_timeline()
        self.assertIs(cached_window, self.adapter.window())          # 缓存命中返回同一对象
        self.adapter.close()
        self.assertEqual(self.adapter._windows, {})                  # 关闭即清理，不留旧代结果
        self.assertEqual(self.adapter._timelines, {})
        self.assertIsNone(self.adapter._store)
        reopened = self.adapter.window()                             # 关闭后重新打开并重算
        self.assertIsNot(reopened, cached_window)
        self.assertEqual(reopened['generation'], 'old')
        self.assertIsNot(self.adapter.function_timeline(), cached_timeline)


class ConcurrencyTests(_StubCase):
    """在途旧请求 + 换代/close 的竞态。"""

    def _start_worker(self):
        results = {}

        def worker():
            try:
                results['value'] = self.adapter.window()
            except Exception as error:                    # noqa: BLE001 - 测试要看到原始异常
                results['error'] = error

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return thread, results

    def test_inflight_old_generation_is_not_written_into_new_cache(self):
        gate = _StubStore('old', blocking=True)

        def opener(generation):
            return gate if generation == 'old' else _StubStore(generation)

        self.opener = opener
        thread, results = self._start_worker()
        self.assertTrue(gate.started.wait(10), '旧代计算没有开始')
        self.state['signature'] = 'new'
        self.adapter.ensure()
        self.assertEqual(self.adapter._store.generation, 'new')
        self.assertFalse(gate.closed, '旧代在计算中被关闭了（应延迟到在途查询释放后再关）')
        self.assertEqual(self.adapter.window()['generation'], 'new')
        gate.release.set()
        thread.join(20)
        self.assertFalse(thread.is_alive(), '旧代查询线程未结束')
        self.assertNotIn('error', results, results.get('error'))
        self.assertEqual(results['value']['generation'], 'old')
        self.assertTrue(gate.closed, '旧代在最后一个在途查询释放后应被关闭')
        # 旧代在途结果没有写进新代缓存：缓存只剩当前代，重查仍是新代结果
        self.assertEqual({key[0] for key in self.adapter._windows}, {self.token()})
        self.assertEqual(self.adapter.window()['generation'], 'new')

    def test_close_during_inflight_keeps_store_open_until_released(self):
        gate = _StubStore('old', blocking=True)
        self.opener = lambda generation: gate
        thread, results = self._start_worker()
        self.assertTrue(gate.started.wait(10), '旧代计算没有开始')
        self.adapter.close()
        self.assertFalse(gate.closed, 'close() 在查询进行中直接关闭了 store')
        self.assertIsNone(self.adapter._store)
        self.assertEqual(self.adapter._windows, {})
        gate.release.set()
        thread.join(20)
        self.assertFalse(thread.is_alive(), '在途查询线程未结束')
        self.assertNotIn('error', results, results.get('error'))
        self.assertEqual(results['value']['generation'], 'old')
        self.assertTrue(gate.closed, '在途查询释放后应关闭 pending 的 store')
        # 关闭后的旧结果不会被写入缓存
        self.assertEqual(self.adapter._windows, {})

    def test_failed_reopen_keeps_previous_store_and_rejects_queries(self):
        old = _StubStore('old')

        def opener(generation):
            if generation == 'new':
                raise ValueError('新一代不可用（模拟打开失败/取消）')
            return old

        self.opener = opener
        self.assertEqual(self.adapter.window()['generation'], 'old')
        self.state['signature'] = 'new'
        with self.assertRaisesRegex(ValueError, '新一代不可用'):
            self.adapter.window()
        # 旧 store 对象保留、未被关闭，但不会把旧内容当成新代结果返回
        self.assertIs(self.adapter._store, old)
        self.assertFalse(old.closed)
        with self.assertRaisesRegex(ValueError, '新一代不可用'):
            self.adapter.window()
        self.assertEqual({key[0] for key in self.adapter._windows},
                         {'old|schema-%s' % trace_store.SCHEMA_VERSION})


class RealStoreGenerationTests(unittest.TestCase):
    """真实 store（合成记录 + 真实 writer）：同一 record ID 重解码、取消/失败场景。"""

    def setUp(self):
        self.root = _work_dir(self._testMethodName)
        self.record = self.root / 'record'
        build_record_fixture(self.record)
        trace_store.write(self.record, self.record / 'store', verify_image=False)
        self.adapter = trace_store_adapter.TraceStoreAdapter(self.record)
        self.adapter.ensure()

    def tearDown(self):
        self.adapter.close()

    def _rename_function(self):
        """模拟同 record ID 重解码：事件内容变了（main → main-2），重新发布新 generation。"""
        events_file = self.record / 'events.jsonl'
        items = [json.loads(line) for line in
                 events_file.read_text(encoding='utf-8').splitlines() if line.strip()]
        changed = 0
        for item in items:
            if item.get('kind') == 'instruction' and item.get('function') == 'main':
                item['function'] = 'main-2'
                item['file'] = 'main2.c'
                changed += 1
        self.assertGreater(changed, 0, '合成记录里没有 main 指令')
        events_file.write_text(
            ''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in items),
            encoding='utf-8')

    def test_same_record_redecode_returns_new_results(self):
        old_generation = self.adapter.overview()['traceStore']['generation']
        old_window = self.adapter.window()
        old_timeline = self.adapter.function_timeline()
        self.assertIs(old_window, self.adapter.window())              # 换代前是缓存命中
        self.assertIn('main', {item['function'] for item in old_window['analysis']['functions']})
        self._rename_function()
        trace_store.write(self.record, self.record / 'store', verify_image=False)
        self.adapter.ensure()
        new_generation = self.adapter.overview()['traceStore']['generation']
        self.assertNotEqual(old_generation, new_generation, '重写后应是新 generation')
        new_window = self.adapter.window()
        new_timeline = self.adapter.function_timeline()
        self.assertIsNot(new_window, old_window)
        functions = {item['function'] for item in new_window['analysis']['functions']}
        self.assertIn('main-2', functions)
        self.assertNotIn('main', functions)
        self.assertIn('main-2', {lane['name'] for lane in new_timeline['lanes']})
        self.assertNotIn('main', {lane['name'] for lane in new_timeline['lanes']})
        self.assertEqual({key[0] for key in self.adapter._windows}, {self.token()})
        self.assertEqual({key[0] for key in self.adapter._timelines}, {self.token()})

    def token(self):
        return self.adapter._store_token(self.adapter._store)

    def test_cancelled_generation_keeps_old_complete_generation(self):
        old_generation = self.adapter.overview()['traceStore']['generation']
        old_window = self.adapter.window()
        # 模拟构建取消：generation 目录写了一半，manifest 没有发布（writer 只在成功时发布）
        cancelled = self.record / 'store' / 'generations' / 'g-cancelled-20260101T000000-deadbeef'
        cancelled.mkdir()
        (cancelled / 'store.bin').write_bytes(b'not-a-complete-store')
        self.adapter.ensure()
        self.assertEqual(self.adapter.overview()['traceStore']['generation'], old_generation)
        self.assertIs(self.adapter.window(), old_window)             # 旧完整代继续服务
        fresh = trace_store_adapter.TraceStoreAdapter(self.record)
        try:
            self.assertEqual(fresh.overview()['traceStore']['generation'], old_generation)
            self.assertEqual(fresh.window(), old_window)
        finally:
            fresh.close()

    def test_failed_redecode_metadata_keeps_complete_generation(self):
        old_generation = self.adapter.overview()['traceStore']['generation']
        old_window = self.adapter.window()
        decoded_path = self.record / 'decoded.json'
        decoded = json.loads(decoded_path.read_text(encoding='utf-8'))
        decoded['decodeStatus'] = 'failed'                          # 模拟解码失败留下的元数据
        decoded_path.write_text(json.dumps(decoded, ensure_ascii=False), encoding='utf-8')
        self.adapter.ensure()                                        # 签名变化 → 重新打开
        self.assertEqual(self.adapter.overview()['traceStore']['generation'], old_generation)
        self.assertEqual(self.adapter.window(), old_window)

    def test_published_but_broken_generation_is_rejected_without_fallback(self):
        broken = self.root / 'broken'
        (broken / 'store').mkdir(parents=True)
        shutil.copy2(self.record / 'store' / 'manifest.json', broken / 'store' / 'manifest.json')
        manifest_path = broken / 'store' / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['generation'] = 'g-missing-20260101T000000-00000000'
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        adapter = trace_store_adapter.TraceStoreAdapter(broken)
        with self.assertRaises(ValueError):
            adapter.ensure()
        # 明确拒绝，不回退到旧代/半成品
        with self.assertRaises(ValueError):
            adapter.window()
        adapter.close()


class InputFingerprintTests(unittest.TestCase):
    """§2.4：输入按内容核验（无 64 MiB 静默降级），skipped=true 显式标“未核验”。"""

    def setUp(self):
        self.root = _work_dir(self._testMethodName)
        self.record = self.root / 'record'
        build_record_fixture(self.record)
        trace_store.write(self.record, self.record / 'store', verify_image=False)
        self.manifest_path = self.record / 'store' / 'manifest.json'
        self.manifest = json.loads(self.manifest_path.read_text(encoding='utf-8'))

    def _rewrite_manifest(self, value):
        self.manifest_path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def test_verified_store_reports_content_hash_and_timing(self):
        adapter = trace_store_adapter.TraceStoreAdapter(self.record)
        try:
            adapter.ensure()
            report = adapter.input_verification()
            self.assertTrue(report['verified'])
            self.assertFalse(report['skipped'])
            self.assertEqual(report['problems'], [])
            self.assertEqual(report['unverified'], [])
            etm = self.record / 'etm.bin'
            # 决定事件流的输入（etm.bin）与元数据输入（session.json/sources.json）都按内容核验
            self.assertEqual(report['blockingHashed'], 1)            # 合成记录只绑定 etm.bin
            self.assertEqual(report['hashed'], 3)
            self.assertEqual(report['hashedBytes'],
                             etm.stat().st_size
                             + (self.record / 'session.json').stat().st_size
                             + (self.record / 'sources.json').stat().st_size)
            self.assertGreater(report['hashSeconds'], 0.0)
            self.assertGreaterEqual(report['storeCheckSeconds'], 0.0)
            self.assertGreaterEqual(report['openSeconds'], report['storeCheckSeconds'])
            self.assertEqual(report['generation'], adapter.overview()['traceStore']['generation'])
            # 元数据输入单独报告（构建后被服务端改写属正常，不参与拒绝）
            self.assertEqual([item['name'] for item in report['metadata']],
                             ['session.json', 'sources.json'])
            self.assertEqual([item['status'] for item in report['metadata']], ['ok', 'ok'])
            self.assertEqual(report['metadataChanged'], [])
            # 旧布局派生文本不是 store 的事件源（事件源是 store.bin，每次打开全量校验），
            # 只列入 excluded，不按内容核验。
            self.assertIn('events.jsonl', report['excluded'])
            self.assertNotIn('etm.bin', report['excluded'])
            self.assertNotIn('session.json', report['excluded'])
            overview = adapter.overview()
            view = overview['traceStore']['inputHash']
            self.assertTrue(view['verified'])
            self.assertEqual(view['blockingHashed'], 1)
            self.assertEqual(view['hashed'], 3)
            self.assertEqual(view['generation'], report['generation'])
        finally:
            adapter.close()

    def test_same_size_same_mtime_rewrite_is_rejected(self):
        etm = self.record / 'etm.bin'
        original = etm.read_bytes()
        stat = etm.stat()
        # 同长度改写 + 把 mtime 也复原：旧实现只比 size（且 <=64 MiB 才算 sha），签名不变就放过
        etm.unlink()
        etm.write_bytes(bytes(byte ^ 0xFF for byte in original))
        os.utime(etm, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(etm.stat().st_size, stat.st_size)
        self.assertEqual(etm.stat().st_mtime_ns, stat.st_mtime_ns)
        self.assertEqual(trace_store_adapter._stat_key(etm),
                         '%d:%d' % (stat.st_size, stat.st_mtime_ns))
        report = trace_store_adapter.verify_inputs(self.record, self.manifest)
        self.assertFalse(report['verified'])
        self.assertGreaterEqual(report['hashedBytes'], stat.st_size)
        self.assertIn('sha256 不符', report['problems'][0])
        self.assertEqual(trace_store_adapter._stale_inputs(self.record, self.manifest),
                         report['problems'])
        adapter = trace_store_adapter.TraceStoreAdapter(self.record)
        try:
            with self.assertRaisesRegex(ValueError, '输入不一致'):
                adapter.ensure()
        finally:
            adapter.close()
        # 复原后可重新打开（拒绝不是永久锁死）
        etm.unlink()
        etm.write_bytes(original)
        os.utime(etm, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        again = trace_store_adapter.TraceStoreAdapter(self.record)
        try:
            again.ensure()
            self.assertTrue(again.input_verification()['verified'])
        finally:
            again.close()

    def test_binding_probe_large_native_bin_wrong_sha_is_rejected(self):
        if not BINDING_PROBE.is_file():
            raise unittest.SkipTest('缺少 Codex binding-probe：%s' % BINDING_PROBE)
        folder = self.root / 'binding-probe'
        folder.mkdir()
        target = folder / 'native.bin'
        try:
            os.link(BINDING_PROBE, target)
        except OSError:
            shutil.copyfile(BINDING_PROBE, target)
        size = BINDING_PROBE.stat().st_size
        self.assertEqual(size, 67_108_865)
        manifest = dict(bindings=dict(inputs={'native.bin': dict(bytes=size, sha256='0' * 64)}))
        report = trace_store_adapter.verify_inputs(folder, manifest)
        self.assertEqual(report['hashed'], 1, '超过 64 MiB 的声明输入必须按内容核验')
        self.assertEqual(report['hashedBytes'], size)
        self.assertGreater(report['hashSeconds'], 0.0)
        self.assertEqual(report['inputs'][0]['status'], 'mismatch')
        self.assertEqual(report['problems'], ['native.bin（sha256 不符）'])
        self.assertFalse(report['verified'])
        self.assertEqual(trace_store_adapter._stale_inputs(folder, manifest),
                         ['native.bin（sha256 不符）'])
        # 正确 sha256 时不误报（同一份 67 MiB 文件只再算一次）
        digest = hashlib.sha256()
        with target.open('rb') as handle:
            for block in iter(lambda: handle.read(1 << 20), b''):
                digest.update(block)
        ok = trace_store_adapter.verify_inputs(
            folder, dict(bindings=dict(inputs={'native.bin': dict(bytes=size,
                                                                   sha256=digest.hexdigest())})))
        self.assertEqual(ok['problems'], [])
        self.assertTrue(ok['verified'])

    def test_no_hash_inputs_store_is_marked_unverified(self):
        manifest = json.loads(json.dumps(self.manifest))
        bindings = manifest.setdefault('bindings', {})
        bindings['inputHash'] = dict(
            algorithm='sha256', computed=False, skipped=True,
            reason='--no-hash-inputs：不读取输入文件内容、不计算 sha256；inputs 只记录字节数')
        for entry in (bindings.get('inputs') or {}).values():
            entry['sha256'] = None
        self._rewrite_manifest(manifest)
        adapter = trace_store_adapter.TraceStoreAdapter(self.record)
        try:
            adapter.ensure()                                        # 诊断用：允许打开
            report = adapter.input_verification()
            self.assertTrue(report['skipped'])
            self.assertFalse(report['verified'], '--no-hash-inputs 产物不得默认为已核验')
            self.assertEqual(report['hashed'], 0)
            self.assertIn('etm.bin', report['unverified'])
            self.assertIn('skipped', report['reason'])
            view = adapter.overview()['traceStore']['inputHash']
            self.assertFalse(view['verified'])
            self.assertTrue(view['skipped'])
            self.assertIn('etm.bin', view['unverified'])
            # 正常浏览仍然可用（行为写清：允许诊断浏览，但标注“未核验”）
            page = adapter.page('events', 0, 5)
            self.assertEqual(len(page['rows']), 5)
        finally:
            adapter.close()


if __name__ == '__main__':
    unittest.main()
