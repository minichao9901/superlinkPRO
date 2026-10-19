"""TraceStore 适配器契约测试：与旧 `RecordStore` 逐接口 JSON 对照。

数据来源（既有只读记录，缺失则跳过）：
* `etm/records/capture-4395612c…`（ETMv4 10 ms，210,731 事件）——参考写入器从
  decoded.json 内联事件生成 store，overview 只有 `traceStore` 一个白名单差异；
* `docs/validation/20261018-ds-review…/capture-be1e78730b…`（200 ms steady，
  3,675,818 事件）——融合写出器生成 store，overview 另有 P3 W-P3-1（streamed
  事件源键）差异、包页说明文本差异（P3 W-P3-2）。
两份记录都镜像到 `etm/records/_p4_*` 临时目录（只读硬链接 + 小文件复制），
旧 `RecordStore` 在镜像目录里重建索引（不写原始证据集合）。

被测内容：
* `signature/ready/ensure` 生命周期与陈旧/损坏拒绝；
* `overview/page/event/function_timeline/window/navigate/issues/source` 与旧后端逐项相等；
* `backend_for()` 的后端选择规则（新格式 / 旧 JSONL / 声明但缺 store）。
"""
import atexit
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import record_store as record_store_module
import trace_store
import trace_store_adapter
import trace_store_write
from record_store import RecordStore
from stream_store import attach_streams

ETM_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = ETM_ROOT.parent
TMP_ROOT = Path(tempfile.mkdtemp(dir=str(ETM_ROOT / 'records'), prefix='_p4_adapter_'))
atexit.register(shutil.rmtree, TMP_ROOT, True)
# 派生索引/store 构建较慢（200 ms steady 约 2 分钟）；把“镜像记录 + 旧索引 + store”
# 缓存在 gitignored 的 etm/records/.p4_test_cache 下，按记录输入/代码版本失效。
# 首次运行仍会真实构建，之后复用缓存（校验 ready.json 与输入指纹）。
CACHE_ROOT = ETM_ROOT / 'records' / '.p4_test_cache'
CACHE_VERSION = 2
SAMPLES = {
    'etm4-10ms': ETM_ROOT / 'records/capture-4395612c313f47ce97af1e1c17e0a0d5',
    'steady': REPO_ROOT / 'docs/validation/20261018-ds-review-bb2f8ee-6ff02745/data/records/'
                           'capture-be1e78730b7f486a9e27d5911cd4a434',
}
_FIXTURES = {}


def _mirror(record, target):
    """复制记录到临时目录：小文件复制，大文件硬链接（同盘；失败退回复制）。

    旧 `browse-v3.sqlite` 是上一版本索引（与当前 RecordStore 版本签名不符），
    不镜像——镜像目录里重建索引，原始证据目录保持完全只读。
    """
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(Path(record).iterdir()):
        if not path.is_file() or path.name == 'browse-v3.sqlite':
            continue
        destination = target / path.name
        if destination.exists():
            continue
        if path.stat().st_size > 8 << 20:
            try:
                os.link(path, destination)
                continue
            except OSError:
                pass
        shutil.copy2(path, destination)
    return target


def _minimal_inputs(record, target):
    """只放融合写出器需要的输入（避免把 1.7 GB events/sidecar 纳入绑定哈希）。"""
    target.mkdir(parents=True, exist_ok=True)
    names = ['etm.bin', 'firmware.elf', 'native-config.txt', 'sources.json', 'session.json',
             'physical.json', 'framing.json']
    names += [path.name for path in sorted(Path(record).glob('image-*.bin'))]
    for name in names:
        source = Path(record) / name
        if source.is_file():
            shutil.copy2(source, target / name)
    return target


def _legacy_loader(folder):
    folder = Path(folder)
    decoded = json.loads((folder / 'decoded.json').read_text(encoding='utf-8'))
    if isinstance(decoded.get('events'), list):
        result = dict(decoded)
        sources = folder / 'sources.json'
        result['sources'] = (json.loads(sources.read_text(encoding='utf-8'))
                             if sources.is_file() else {})
        return result
    return attach_streams(decoded, folder)


def _link_copy(source, target):
    """整目录镜像（同盘硬链接；目录内是普通文件）。"""
    target.mkdir(parents=True, exist_ok=True)
    for path in Path(source).iterdir():
        if not path.is_file():
            continue
        destination = target / path.name
        if destination.exists():
            continue
        try:
            os.link(path, destination)
        except OSError:
            shutil.copy2(path, destination)
    return target


def _copy_or_link(source, target):
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _store_decoded(decoded):
    """浏览 store 记录只需要小型元数据；剥掉内联事件本体（10 ms 旧记录 >100 MB）。"""
    value = {key: item for key, item in decoded.items()
             if key not in ('events', 'elements', 'packets', 'sources')}
    value['traceStore'] = dict(format='trace-store v1', schemaVersion=1, store='store',
                               source='store.bin')
    return value


def _cache_key(name, record):
    digest = hashlib.sha256()
    digest.update(('p4-fixture-%d|%s|%s|%s|' % (
        CACHE_VERSION, name, record_store_module.VERSION, trace_store.SCHEMA_VERSION)).encode())
    for source in (trace_store.__file__, trace_store_write.__file__,
                   trace_store_adapter.__file__):
        digest.update(('%s:%s|' % (Path(source).name, Path(source).stat().st_mtime_ns)).encode())
    for path in sorted(Path(record).iterdir()):
        if path.is_file():
            stat = path.stat()
            digest.update(('%s:%d:%d|' % (path.name, stat.st_size,
                                          stat.st_mtime_ns)).encode())
    return digest.hexdigest()[:20]


def _clone_tree(source, target):
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, copy_function=_copy_or_link)
    return target


def _build_fixture_dirs(record, root, fused):
    """在 root 下构建 base / adapter / server_store 三个镜像目录（真实计算）。"""
    base = _mirror(record, root / 'base')
    decoded = json.loads((base / 'decoded.json').read_text(encoding='utf-8'))
    old_store = RecordStore(base, lambda: _legacy_loader(base))
    old_store.ensure()

    adapter_dir = _link_copy(base, root / 'adapter')
    if fused:
        # 融合写出器：native.bin 路径的 store（streamed 事件源/包文本有 P3 白名单差异）。
        build = _minimal_inputs(record, root / 'build')
        trace_store_write.write(build, build / 'store', metadata=decoded,
                                write_decoded=False)
        shutil.move(str(build / 'store'), str(adapter_dir / 'store'))
    else:
        # 参考写入器（从 decoded.json 内联事件）：overview 只有 traceStore 差异。
        trace_store.write(base, adapter_dir / 'store')
    adapter = trace_store_adapter.TraceStoreAdapter(adapter_dir)
    adapter.ensure()
    adapter.close()
    shutil.rmtree(root / 'build', ignore_errors=True)   # native.bin 工作目录不需要缓存

    # server_store：与 adapter 同内容，但去掉 events.jsonl/sidecar/sqlite（新后端选择），
    # decoded.json 换成小型副本 + traceStore 标记（浏览只需要元数据，不含内联事件本体）。
    server_store = root / 'server_store'
    if server_store.exists():
        shutil.rmtree(server_store)
    shutil.copytree(adapter_dir, server_store, copy_function=_copy_or_link)
    for unwanted in ('events.jsonl', 'packets.jsonl', 'elements.jsonl', 'native.jsonl',
                     'browse-columns.tsv', 'browse-v3.sqlite'):
        (server_store / unwanted).unlink(missing_ok=True)
    decoded_path = server_store / 'decoded.json'
    if decoded_path.exists():
        decoded_path.unlink()
    decoded_path.write_text(json.dumps(_store_decoded(decoded), ensure_ascii=False),
                            encoding='utf-8')
    return dict(base=base, adapter=adapter_dir, server_store=server_store)


def build_fixture(name):
    """构造 {base, adapter_dir, server_store, ...} 镜像与两套后端（带跨进程缓存）。

    * base：旧后端镜像，`RecordStore` 在这里重建索引（写临时 sqlite，不动证据）；
    * adapter_dir：base + store，适配器与 RecordStore 对比同一份数据；
    * server_store：无 events.jsonl/sqlite、只有 store（供 server 后端选择测试）。
    """
    if name in _FIXTURES:
        return _FIXTURES[name]
    record = SAMPLES[name]
    if not (record / 'decoded.json').is_file() or not (record / 'firmware.elf').is_file():
        raise unittest.SkipTest('缺少测试记录：%s' % record)
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    cache = CACHE_ROOT / ('%s-%s' % (name, _cache_key(name, record)))
    ready = cache / 'ready.json'
    if not ready.is_file():
        if cache.exists():
            shutil.rmtree(cache)
        cache.mkdir(parents=True)
        _build_fixture_dirs(record, cache, fused=name == 'steady')
        ready.write_text(json.dumps(dict(name=name, createdAt=os.getpid())), encoding='utf-8')
    work = TMP_ROOT
    dirs = {'base': _clone_tree(cache / 'base', work / (name + '-base')),
            'adapter': _clone_tree(cache / 'adapter', work / (name + '-adapter')),
            'server_store': _clone_tree(cache / 'server_store',
                                        work / (name + '-server-store'))}
    decoded = json.loads((dirs['base'] / 'decoded.json').read_text(encoding='utf-8'))
    old_store = RecordStore(dirs['base'], lambda: _legacy_loader(dirs['base']))
    old_store.ensure()
    adapter = trace_store_adapter.TraceStoreAdapter(dirs['adapter'])
    adapter.ensure()
    fixture = dict(record=record, base=dirs['base'], adapter_dir=dirs['adapter'],
                   server_store=dirs['server_store'], decoded=decoded, old=old_store,
                   adapter=adapter)
    _FIXTURES[name] = fixture
    return fixture


def _comparison(case, mine, theirs, ignore=()):
    if isinstance(mine, dict) and isinstance(theirs, dict):
        keys = set(mine) | set(theirs)
        for key in sorted(keys):
            if key in ignore:
                continue
            case.assertIn(key, theirs, '旧后端缺少键 %s' % key)
            case.assertIn(key, mine, '适配器缺少键 %s' % key)
            case.assertEqual(mine[key], theirs[key], '键 %s 不一致' % key)
        return
    case.assertEqual(mine, theirs)


class AdapterCase(unittest.TestCase):
    sample = None

    @classmethod
    def setUpClass(cls):
        if cls is AdapterCase:              # Python unittest 不认 __test__，显式跳过抽象基类
            raise unittest.SkipTest('抽象适配器基类，由 Etm4TenMs/Steady200Ms 参数化运行')
        cls.fixture = build_fixture(cls.sample)
        cls.adapter = cls.fixture['adapter']
        cls.old = cls.fixture['old']
        cls.overview = cls.old.overview()
        cls.total = int(cls.overview['eventCount']) - 1

    def test_overview_matches_old_backend(self):
        mine = self.adapter.overview()
        trace = mine.pop('traceStore', None)
        self.assertIsNotNone(trace, '适配器 overview 应带 traceStore 概要键（白名单 W-P4-1）')
        self.assertEqual(trace['format'], 'trace-store v1')
        theirs = dict(self.overview)
        if self.sample == 'steady':
            # P3 W-P3-1：融合写出器记录的事件源就是 store.bin。
            self.assertEqual(mine['streamed']['eventsFile'], 'store.bin')
            self.assertEqual(mine['streamed']['nativeJsonl'], 'native.bin')
            mine = dict(mine)
            mine.pop('streamed')
            theirs.pop('streamed')
        _comparison(self, mine, theirs)

    def test_first_middle_last_and_search_pages(self):
        for offset in (0, (self.total // 2 // 150) * 150, (self.total // 150) * 150):
            _comparison(self, self.adapter.page('events', offset, 150),
                        self.old.page('events', offset, 150))
        limit = self.total if self.sample == 'etm4-10ms' else 200000
        _comparison(self, self.adapter.page('events', 0, 40, 'main', False, 0, limit),
                    self.old.page('events', 0, 40, 'main', False, 0, limit))
        _comparison(self, self.adapter.page('events', 0, 40, '', True, 0, limit),
                    self.old.page('events', 0, 40, '', True, 0, limit))

    def test_packet_page(self):
        mine = self.adapter.page('packets', 0, 40)
        theirs = self.old.page('packets', 0, 40)
        self.assertEqual(mine['total'], theirs['total'])
        self.assertEqual(mine['offset'], theirs['offset'])
        if self.sample == 'steady':
            # P3 W-P3-2：融合写出器不保存 OpenCSD 包说明文本。
            for row in mine['rows']:
                self.assertEqual(row['text'], '')
                self.assertGreater(len(row['hex']), 0)
        else:
            _comparison(self, mine, theirs)

    def test_event_and_navigation(self):
        for seq in (0, 1, self.total // 2, self.total - 1):
            _comparison(self, self.adapter.event(seq), self.old.event(seq))
        for mode in ('instruction', 'call', 'return', 'source'):
            for seq in (0, self.total // 2, max(0, self.total - 5)):
                for direction in (1, -1):
                    self.assertEqual(self.adapter.navigate(seq, direction, mode),
                                     self.old.navigate(seq, direction, mode),
                                     'navigate %s seq=%d dir=%d' % (mode, seq, direction))
        # exception/issue 在 trace 里很稀疏：旧后端用 SQL、适配器要扫区间（P1 U5 已知退化），
        # 这里围绕首个出现位置对照双向导航，覆盖语义而不把最坏扫描时间带进测试。
        for mode in ('exception', 'issue'):
            anchor = self.adapter.navigate(0, 1, mode)['seq']
            self.assertEqual(anchor, self.old.navigate(0, 1, mode)['seq'])
            if anchor is not None:
                for seq, direction in ((max(0, anchor - 1), 1), (anchor + 1, -1)):
                    self.assertEqual(self.adapter.navigate(seq, direction, mode),
                                     self.old.navigate(seq, direction, mode),
                                     'navigate %s seq=%d dir=%d' % (mode, seq, direction))
        for pc in (0x08000332, 0x0800A9C):
            self.assertEqual(self.adapter.navigate(0, 1, 'instruction', pc),
                             self.old.navigate(0, 1, 'instruction', pc), 'pc=0x%x' % pc)

    def test_window_and_function_timeline(self):
        end = self.total if self.sample == 'etm4-10ms' else 200000
        _comparison(self, self.adapter.window(0, end), self.old.window(0, end))
        _comparison(self, self.adapter.function_timeline(0, end),
                    self.old.function_timeline(0, end))
        # 缓存：同一区间第二次返回同一对象（与旧后端的窗口缓存语义一致）
        self.assertIs(self.adapter.window(0, end), self.adapter.window(0, end))

    def test_issues_and_source(self):
        _comparison(self, self.adapter.issues(0, 100), self.old.issues(0, 100))
        files = sorted(self.overview.get('sourceFiles') or [])
        self.assertTrue(files, '记录应带源码文件')
        _comparison(self, self.adapter.source(files[0]), self.old.source(files[0]))

    def test_signature_ready_and_staleness(self):
        self.assertTrue(self.adapter.ready())
        self.assertTrue(self.adapter.signature())
        # 在独立副本上改 etm.bin：不触碰被多个用例共享的镜像目录（旧索引的签名会跟着变）。
        stale_dir = TMP_ROOT / ('stale-' + self.sample)
        if stale_dir.exists():
            shutil.rmtree(stale_dir)
        _link_copy(self.fixture['adapter_dir'], stale_dir)
        shutil.copytree(self.fixture['adapter_dir'] / 'store', stale_dir / 'store',
                        copy_function=_copy_or_link)
        etm = stale_dir / 'etm.bin'
        original = etm.read_bytes()
        etm.unlink()
        etm.write_bytes(original + b'\x00')
        adapter = trace_store_adapter.TraceStoreAdapter(stale_dir)
        self.assertTrue(adapter.ready())
        with self.assertRaisesRegex(ValueError, '输入不一致'):
            adapter.ensure()
        etm.write_bytes(original)
        adapter.ensure()                             # 恢复后可重新打开
        adapter.close()

    def test_missing_or_incomplete_store_is_rejected(self):
        fixture = self.fixture
        self.assertTrue(trace_store_adapter.store_available(fixture['adapter_dir']))
        marker = dict(version=3, protocol='ETMv4', traceStore=dict(format='trace-store v1'))
        broken = TMP_ROOT / ('broken-' + self.sample)
        broken.mkdir(exist_ok=True)
        (broken / 'decoded.json').write_text(json.dumps(marker), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '缺少已发布的 store'):
            trace_store_adapter.backend_for(broken)
        self.assertFalse(trace_store_adapter.store_available(broken))
        # 损坏的 manifest：明确拒绝，不返回拼凑结果。
        corrupt = TMP_ROOT / ('corrupt-' + self.sample)
        (corrupt / 'store').mkdir(parents=True, exist_ok=True)
        (corrupt / 'decoded.json').write_text(json.dumps(marker), encoding='utf-8')
        (corrupt / 'store' / 'manifest.json').write_text('{not json', encoding='utf-8')
        adapter = trace_store_adapter.TraceStoreAdapter(corrupt)
        self.assertFalse(adapter.ready())
        with self.assertRaises(ValueError):
            adapter.ensure()
        # 已发布但 store.bin 被截断：打开即拒绝（长度/校验声明不符）。
        truncated = TMP_ROOT / ('truncated-' + self.sample)
        (truncated / 'store').mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixture['adapter_dir'] / 'store' / 'manifest.json',
                     truncated / 'store' / 'manifest.json')
        manifest = json.loads((fixture['adapter_dir'] / 'store' / 'manifest.json')
                              .read_text(encoding='utf-8'))
        generation = manifest['generation']
        source = fixture['adapter_dir'] / 'store' / 'generations' / generation / 'store.bin'
        target = truncated / 'store' / 'generations' / generation / 'store.bin'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes()[:-32])
        (truncated / 'decoded.json').write_text(json.dumps(marker), encoding='utf-8')
        adapter = trace_store_adapter.TraceStoreAdapter(truncated)
        with self.assertRaises(ValueError):
            adapter.ensure()


class Etm4TenMs(AdapterCase):
    sample = 'etm4-10ms'


class Steady200Ms(AdapterCase):
    sample = 'steady'


class FrontendCapabilityWiringTests(unittest.TestCase):
    """前端采集时长校验读取后端能力（源码级接线检查；Node 运行时由页面自测覆盖）。

    `etm/web` 是源，`web/bridge/etm/backend/web` 是运行时快照（发版时用
    `web/tools/dev/sync-etm-runtime.py --etm-root etm` 同步，不属于本任务 inScope）。
    """

    def _read(self, *parts):
        path = ETM_ROOT.joinpath(*parts)
        self.assertTrue(path.is_file(), path)
        return path.read_text(encoding='utf-8')

    def test_view_reads_capabilities_and_keeps_default_limit(self):
        view = self._read('web', 'app', 'view.js')
        self.assertIn("api('capabilities')", view)
        self.assertIn('state.captureMaxSeconds', view)
        self.assertIn('state.captureMaxSamples', view)
        # 校验使用能力值，而不是新的硬编码上限
        self.assertNotIn('seconds<=0.2', view)
        self.assertNotIn('captureSeconds>0.2', view)
        self.assertIn('captureMaxSeconds:0.2', view)      # 能力缺失时的默认值仍是 200 ms

    def test_index_and_history_show_capability_hint(self):
        index = self._read('web', 'index.html')
        self.assertIn('id="capture-capability-hint"', index)
        self.assertIn('id="browse-capability"', index)
        self.assertIn('max="200"', index)                # 初始上限仍是 200 ms
        history = self._read('web', 'app', 'history.js')
        self.assertIn('setCapability(capabilities)', history)
        self.assertIn('captureMaxSeconds', history)


class BackendSelectionTests(unittest.TestCase):
    def test_legacy_jsonl_record_stays_legacy(self):
        base = build_fixture('etm4-10ms')['base']
        self.assertEqual(trace_store_adapter.backend_for(base), 'legacy')

    def test_store_backed_record_selects_trace_store(self):
        fixture = build_fixture('etm4-10ms')
        self.assertEqual(trace_store_adapter.backend_for(fixture['adapter_dir']),
                         'trace-store')
        self.assertEqual(trace_store_adapter.backend_for(fixture['server_store']),
                         'trace-store')

    def test_manifest_without_marker_and_with_events_stays_legacy(self):
        fixture = build_fixture('steady')
        # steady 的 adapter 目录同时有 events.jsonl 与 store，但 decoded 没有 traceStore
        # 标记（参考写入器不改 decoded.json）→ 视为旧记录，避免陈旧 store 抢先。
        self.assertEqual(trace_store_adapter.backend_for(fixture['adapter_dir']), 'legacy')


if __name__ == '__main__':
    unittest.main()
