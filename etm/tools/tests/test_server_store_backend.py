"""server 端 trace-store 集成测试：后端选择、/api/browse 全接口对照、能力声明、导入导出。

复用 `test_trace_store_adapter` 的记录镜像/索引/store 构建（同一进程内只构建一次）：
* 10 ms 记录：参考写入器 store → `/api/browse` 与旧 JSONL 后端逐项 JSON 一致
  （仅 overview 的 `traceStore` 概要键按白名单摘出）；
* 200 ms steady：融合写出器 store → 除 `traceStore` 与 P3 W-P3-1（streamed 事件源键）
  外一致；包页只有 P3 W-P3-2 说明文本差异。
"""
import io
import json
import os
import shutil
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import server
import trace_store_adapter
from record_store import RecordStore
from test_trace_store_adapter import TMP_ROOT, build_fixture

SANDBOX = TMP_ROOT


def _json(response):
    assert response.status_code == 200, (response.status_code, response.get_json())
    return response.get_json()


def _normalize_overview(payload, fused):
    value = dict(payload)
    value.pop('traceStore', None)
    if fused:
        # P3 W-P3-1：融合写出器记录的事件源是 store.bin。
        value.pop('streamed', None)
    return value


def _packets_without_text(page):
    value = dict(page)
    value['rows'] = [{key: item[key] for key in item if key != 'text'} for item in page['rows']]
    return value


class _SandboxCase(unittest.TestCase):
    """把 server.RECORDS 指向测试镜像目录；结束后恢复。"""

    @classmethod
    def setUpClass(cls):
        if cls in (_SandboxCase, _ServerStoreCase):   # unittest 不认 __test__，显式跳过基类
            raise unittest.SkipTest('抽象 server 测试基类')
        cls._old_records = server.RECORDS
        server.RECORDS = SANDBOX
        cls.client = server.app.test_client()

    @classmethod
    def tearDownClass(cls):
        server.RECORDS = cls._old_records
        cls.client = None


class _ServerStoreCase(_SandboxCase):
    sample = None
    fused = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fixture = build_fixture(cls.sample)
        cls.legacy_sid = cls.sample + '-base'
        cls.store_sid = cls.sample + '-server-store'
        cls.overview_old = _json(cls.client.get('/api/browse/%s/overview' % cls.legacy_sid))
        cls.overview_new = _json(cls.client.get('/api/browse/%s/overview' % cls.store_sid))
        cls.total = int(cls.overview_old['eventCount']) - 1

    @classmethod
    def tearDownClass(cls):
        for name in (cls.sample + '-base', cls.sample + '-server-store',
                     cls.sample + '-adapter'):
            trace_store_adapter.forget(SANDBOX / name)
        super().tearDownClass()

    # -- 后端选择 ---------------------------------------------------------------------------
    def test_backend_selection(self):
        self.assertIsInstance(server.browse_store(self.legacy_sid), RecordStore)
        self.assertIsInstance(server.browse_store(self.store_sid),
                              trace_store_adapter.TraceStoreAdapter)
        self.assertEqual(trace_store_adapter.backend_for(server.directory(self.store_sid)),
                         'trace-store')

    def test_old_jsonl_record_endpoint_still_works(self):
        if self.sample != 'steady':
            self.skipTest('10 ms 记录是内联 decoded.json，整段 JSON 端点不用于对照')
        response = self.client.get('/api/records/' + self.legacy_sid)
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload['summary']['instructions'],
                         self.overview_old['summary']['instructions'])
        self.assertNotIn('events', payload)

    # -- /api/browse 接口对照 ----------------------------------------------------------------
    def test_overview_matches(self):
        self.assertEqual(_normalize_overview(self.overview_new, self.fused),
                         _normalize_overview(self.overview_old, self.fused))
        if self.fused:
            self.assertEqual(self.overview_new['streamed']['eventsFile'], 'store.bin')
            self.assertEqual(self.overview_old['streamed']['eventsFile'], 'events.jsonl')

    def test_pages_match(self):
        for offset in (0, (self.total // 2 // 150) * 150, (self.total // 150) * 150):
            mine = _json(self.client.get('/api/browse/%s/page' % self.store_sid,
                                         query_string={'offset': offset}))
            theirs = _json(self.client.get('/api/browse/%s/page' % self.legacy_sid,
                                           query_string={'offset': offset}))
            self.assertEqual(mine, theirs, 'offset=%d' % offset)

    def test_search_and_hide_skipped_match(self):
        end = self.total if self.sample == 'etm4-10ms' else 200000
        for query in ({'search': 'main', 'fromSeq': 0, 'toSeq': end},
                      {'hideSkipped': '1', 'fromSeq': 0, 'toSeq': end}):
            mine = _json(self.client.get('/api/browse/%s/page' % self.store_sid,
                                         query_string=query))
            theirs = _json(self.client.get('/api/browse/%s/page' % self.legacy_sid,
                                           query_string=query))
            self.assertEqual(mine, theirs, str(query))

    def test_packet_page_matches(self):
        mine = _json(self.client.get('/api/browse/%s/page' % self.store_sid,
                                     query_string={'view': 'packets', 'limit': 20}))
        theirs = _json(self.client.get('/api/browse/%s/page' % self.legacy_sid,
                                       query_string={'view': 'packets', 'limit': 20}))
        self.assertEqual(mine['total'], theirs['total'])
        self.assertEqual(mine['offset'], theirs['offset'])
        if self.fused:
            # P3 W-P3-2：融合写出器不保存包说明文本；原始字节/偏移/长度一致。
            self.assertEqual(_packets_without_text(mine), _packets_without_text(theirs))
        else:
            self.assertEqual(mine, theirs)

    def test_event_endpoint_matches(self):
        for seq in (0, 1, self.total // 2, self.total - 1):
            mine = _json(self.client.get('/api/browse/%s/event/%d' % (self.store_sid, seq)))
            theirs = _json(self.client.get('/api/browse/%s/event/%d' % (self.legacy_sid, seq)))
            self.assertEqual(mine, theirs, 'seq=%d' % seq)

    def test_navigation_matches(self):
        for mode in ('instruction', 'call', 'return', 'source'):
            for seq in (0, self.total // 2, max(0, self.total - 5)):
                for direction in (1, -1):
                    query = {'seq': seq, 'direction': direction, 'mode': mode}
                    mine = _json(self.client.get('/api/browse/%s/navigate' % self.store_sid,
                                                 query_string=query))
                    theirs = _json(self.client.get('/api/browse/%s/navigate' % self.legacy_sid,
                                                   query_string=query))
                    self.assertEqual(mine, theirs, str(query))
        # exception/issue 稀疏：围绕首个出现位置双向对照（避免 O(全段) 扫描进测试）。
        for mode in ('exception', 'issue'):
            first = {'seq': 0, 'direction': 1, 'mode': mode}
            anchor = _json(self.client.get('/api/browse/%s/navigate' % self.store_sid,
                                           query_string=first))['seq']
            self.assertEqual(anchor, _json(self.client.get(
                '/api/browse/%s/navigate' % self.legacy_sid, query_string=first))['seq'])
            if anchor is not None:
                for seq, direction in ((max(0, anchor - 1), 1), (anchor + 1, -1)):
                    query = {'seq': seq, 'direction': direction, 'mode': mode}
                    self.assertEqual(
                        _json(self.client.get('/api/browse/%s/navigate' % self.store_sid,
                                              query_string=query)),
                        _json(self.client.get('/api/browse/%s/navigate' % self.legacy_sid,
                                              query_string=query)), str(query))
        for pc in (0x08000332, 0x0800A9C):
            query = {'seq': 0, 'direction': 1, 'pc': '0x%x' % pc}
            self.assertEqual(
                _json(self.client.get('/api/browse/%s/navigate' % self.store_sid,
                                      query_string=query)),
                _json(self.client.get('/api/browse/%s/navigate' % self.legacy_sid,
                                      query_string=query)), 'pc=0x%x' % pc)

    def test_window_and_timeline_match(self):
        end = self.total if self.sample == 'etm4-10ms' else 200000
        for endpoint in ('window', 'timeline'):
            query = {'fromSeq': 0, 'toSeq': end}
            mine = _json(self.client.get('/api/browse/%s/%s' % (self.store_sid, endpoint),
                                         query_string=query))
            theirs = _json(self.client.get('/api/browse/%s/%s' % (self.legacy_sid, endpoint),
                                           query_string=query))
            self.assertEqual(mine, theirs, endpoint)
        # 旧后端的 JSONL 窗口回退路径（ETM_PYTHON_WINDOW=1）也应一致。
        end = min(end, 20000)
        os.environ['ETM_PYTHON_WINDOW'] = '1'
        try:
            mine = _json(self.client.get('/api/browse/%s/window' % self.store_sid,
                                         query_string={'fromSeq': 0, 'toSeq': end}))
            theirs = _json(self.client.get('/api/browse/%s/window' % self.legacy_sid,
                                           query_string={'fromSeq': 0, 'toSeq': end}))
        finally:
            os.environ.pop('ETM_PYTHON_WINDOW', None)
        self.assertEqual(mine, theirs)

    def test_issues_and_source_match(self):
        mine = _json(self.client.get('/api/browse/%s/issues' % self.store_sid,
                                     query_string={'limit': 100}))
        theirs = _json(self.client.get('/api/browse/%s/issues' % self.legacy_sid,
                                       query_string={'limit': 100}))
        self.assertEqual(mine, theirs)
        files = sorted(self.overview_old.get('sourceFiles') or [])
        self.assertTrue(files)
        query = {'file': files[0]}
        self.assertEqual(
            _json(self.client.get('/api/browse/%s/source' % self.store_sid,
                                  query_string=query)),
            _json(self.client.get('/api/browse/%s/source' % self.legacy_sid,
                                  query_string=query)))


class TenMsServerStore(_ServerStoreCase):
    sample = 'etm4-10ms'
    fused = False


class SteadyServerStore(_ServerStoreCase):
    sample = 'steady'
    fused = True


class CapabilityAndExportTests(_SandboxCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = build_fixture('etm4-10ms')
        super().setUpClass()
        cls.store_sid = 'etm4-10ms-server-store'

    def test_capabilities_keep_default_200ms_and_declare_trace_store(self):
        payload = _json(self.client.get('/api/capabilities'))
        self.assertEqual(payload['captureMaxSeconds'], 0.2)
        self.assertEqual(payload['captureMaxSamples'], 100_000_000)
        self.assertEqual(payload['captureProtocol'], 2)
        store = payload['traceStore']
        self.assertTrue(store['supported'])
        self.assertEqual(store['format'], 'trace-store')
        self.assertEqual(store['formatName'], 'trace-store v1')
        self.assertEqual(store['schemaVersion'], 1)
        self.assertEqual(store['manifest'], 'store/manifest.json')
        self.assertFalse(store['packetText'])
        # 默认上限没有因为新格式放开
        self.assertEqual(server.CAPTURE_MAX_SECONDS, 0.2)
        self.assertEqual(server.CAPTURE_MAX_SAMPLES, 100_000_000)

    def test_status_and_health_keep_old_fields(self):
        health = _json(self.client.get('/api/health'))
        self.assertTrue(health['ok'])
        self.assertEqual(health['captureProtocol'], 2)
        self.assertIn('decoder', health)
        self.assertIn('nativeIndexer', health)

    def test_export_package_contains_store_and_import_browses_without_decode(self):
        before = _json(self.client.get('/api/browse/%s/overview' % self.store_sid))
        page_before = _json(self.client.get('/api/browse/%s/page' % self.store_sid))
        package = self.client.get('/api/download/%s/record' % self.store_sid)
        self.assertEqual(package.status_code, 200, package.data[:200])
        with zipfile.ZipFile(io.BytesIO(package.data)) as archive:
            names = set(archive.namelist())
            self.assertIn('store/manifest.json', names)
            self.assertIn('decoded.json', names)
            manifest = json.loads(archive.read('store/manifest.json'))
            generation = manifest['generation']
            self.assertIn('store/generations/%s/store.bin' % generation, names)
            self.assertEqual(manifest['state'], 'complete')
        imported = self.client.post(
            '/api/import',
            data={'capture': (io.BytesIO(package.data), 'p4-store.etmrecord')},
            content_type='multipart/form-data')
        self.assertEqual(imported.status_code, 200, imported.get_json())
        sid = imported.get_json()['record']
        try:
            folder = server.directory(sid)
            self.assertTrue((folder / 'store' / 'manifest.json').is_file())
            self.assertFalse((folder / 'events.jsonl').exists())
            self.assertIsInstance(server.browse_store(sid),
                                  trace_store_adapter.TraceStoreAdapter)
            after = _json(self.client.get('/api/browse/%s/overview' % sid))
            self.assertEqual(_normalize_overview(after, False),
                             _normalize_overview(before, False))
            page_after = _json(self.client.get('/api/browse/%s/page' % sid))
            self.assertEqual(page_after, page_before)
            total = int(after['eventCount']) - 1
            last = _json(self.client.get('/api/browse/%s/page' % sid,
                                         query_string={'offset': (total // 150) * 150}))
            self.assertEqual(last['total'], page_before['total'])
        finally:
            trace_store_adapter.forget(server.directory(sid))

    def test_simplified_and_csv_export_stream_from_store(self):
        response = self.client.get('/api/download/%s/simplified' % self.store_sid)
        if any(text.strip() for text in server.browse_store(self.store_sid).sources().values()):
            self.assertEqual(response.status_code, 200)
            self.assertIn('简化源码执行流', response.data.decode('utf-8'))
        else:
            # 老采集夹具可能只有 DWARF 文件路径而没有正文，不能再把注释空壳视作成功。
            self.assertEqual(response.status_code, 400)
            self.assertIn('配套源码文本', response.get_json()['error'])
        response.close()
        response = self.client.get('/api/download/%s/csv' % self.store_sid,
                                   query_string={'fromSeq': 0, 'toSeq': 20})
        self.assertEqual(response.status_code, 200)
        self.assertIn('seq,kind', response.data.decode('utf-8-sig'))
        response = self.client.get('/api/download/%s/json' % self.store_sid)
        self.assertEqual(response.status_code, 200)
        self.assertIn('traceStore', response.get_json())


class CorruptStoreTests(_SandboxCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = build_fixture('etm4-10ms')
        super().setUpClass()

    def _copy_store_record(self, name):
        target = SANDBOX / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(self.fixture['server_store'], target, copy_function=shutil.copy2)
        return target

    def test_declared_but_missing_store_is_rejected(self):
        broken = SANDBOX / 'p4-broken'
        if broken.exists():
            shutil.rmtree(broken)
        broken.mkdir(parents=True)
        (broken / 'decoded.json').write_text(
            json.dumps(dict(version=3, protocol='ETMv4',
                            traceStore={'format': 'trace-store v1'})), encoding='utf-8')
        response = self.client.get('/api/browse/p4-broken/overview')
        self.assertEqual(response.status_code, 400)
        self.assertIn('缺少已发布的 store', response.get_json()['error'])

    def test_truncated_store_is_rejected_not_patched(self):
        target = self._copy_store_record('p4-truncated')
        manifest = json.loads((target / 'store' / 'manifest.json').read_text(encoding='utf-8'))
        store_bin = target / 'store' / 'generations' / manifest['generation'] / 'store.bin'
        store_bin.write_bytes(store_bin.read_bytes()[:-64])
        response = self.client.get('/api/browse/p4-truncated/overview')
        self.assertEqual(response.status_code, 400)
        message = response.get_json()['error']
        self.assertTrue('trace-store 不可用' in message or '长度' in message
                        or '校验' in message, message)

    def test_stale_store_vs_changed_input_is_rejected(self):
        target = self._copy_store_record('p4-stale')
        etm = target / 'etm.bin'
        original = etm.read_bytes()
        etm.unlink()
        etm.write_bytes(original + b'\x00')
        try:
            response = self.client.get('/api/browse/p4-stale/overview')
            self.assertEqual(response.status_code, 400)
            self.assertIn('输入不一致', response.get_json()['error'])
        finally:
            etm.write_bytes(original)
        self.assertEqual(self.client.get('/api/browse/p4-stale/overview').status_code, 200)


if __name__ == '__main__':
    unittest.main()
