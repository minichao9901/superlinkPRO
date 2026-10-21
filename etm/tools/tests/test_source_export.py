import json
import re
import sys
import unittest
import tempfile
import csv
import io
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tools'))
from source_export import simplified_rows,simplified_c,simplified_html,write_execution_export
from decode_trace import export_source
import server

def fixture():
    def event(seq,line,executed=True):return dict(kind='instruction',seq=seq,pc=0x08000000+seq*2,function='main',file='/build/Src/main.c',line=line,executed=executed,executionState='executed' if executed else 'not-taken')
    unknown=event(4,2);unknown.update(executed=None,executionState='unknown')
    return dict(events=[event(0,1),event(1,1),event(2,2),event(3,1),unknown,dict(kind='event',seq=5,type=9,text='EXCEPTION'),event(6,3,False)],sources={'/build/Src/main.c':'while (x) {\n  work();\n  skipped();'},quality={})

class SourceExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name)
        self.folder_patch=patch.object(server,'directory',return_value=self.folder);self.folder_patch.start()
    def tearDown(self):
        self.folder_patch.stop();self.temp.cleanup()
    def test_c_uses_block_comments_preserves_loop_order_and_unknown_boundaries(self):
        text=simplified_c(fixture())
        self.assertEqual(text.count('/build/Src/main.c'),1)
        self.assertEqual(text.count('while (x)'),2)
        self.assertEqual(text.count('/* main · main.c */'),1)
        self.assertIn('条件执行状态未知',text);self.assertIn('EXCEPTION',text);self.assertNotIn('skipped();',text)
        rows=simplified_rows(fixture());self.assertEqual(rows[0]['records'],2);self.assertEqual(rows[0]['endSeq'],1)

    def test_offline_html_escapes_source_script_tags_and_contains_exact_positions(self):
        data=fixture();data['sources']['/build/Src/main.c']='<script>alert(1)</script>\n  work();'
        document=simplified_html(data)
        self.assertNotIn('<script>alert(1)',document)
        embedded=json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>',document,re.S)[1])
        self.assertEqual(embedded['rows'][0]['line'],1);self.assertEqual(embedded['rows'][0]['pc'],0x08000000)
        self.assertIn('<script>alert(1)',embedded['sources']['/build/Src/main.c'])
        self.assertNotIn('<script src=',document)

    def test_download_scopes_only_selected_export_sequence(self):
        with patch.object(server,'result',return_value=fixture()):
            response=server.app.test_client().get('/api/download/export-test/simplified-html?fromSeq=2&toSeq=4')
            self.assertEqual(response.status_code,200)
            embedded=json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>',response.data.decode(),re.S)[1])
            self.assertEqual([r['seq'] for r in embedded['rows']],[2,3,4]);self.assertTrue(response.content_type.startswith('text/html'))
            response.close()

    def test_streamed_writers_match_existing_small_export_semantics(self):
        data=fixture();data['sources']['/build/Src/main.c']+='\n</script><script>bad()</script>'
        for kind,expected in [('simplified',simplified_c(data)),('source',export_source(data)),
                              ('simplified-html',simplified_html(data))]:
            with self.subTest(kind=kind):
                path=self.folder/kind;write_execution_export(data,iter(data['events']),path,kind)
                self.assertEqual(path.read_text(encoding='utf8'),expected)

    def test_store_exports_all_formats_without_legacy_events_and_cleanup(self):
        data=fixture()
        class Store:
            def sources(self):return data['sources']
            def iter_events(self,start,end):return (e for e in data['events'] if (start is None or start<=e['seq']) and (end is None or e['seq']<=end))
        metadata={key:value for key,value in data.items() if key!='events'}
        metadata['traceStore']={'format':'trace-store v1'}
        with patch.object(server,'result',return_value=metadata),patch.object(server,'browse_store',return_value=Store()):
            for kind in ('source','simplified','simplified-html','csv'):
                with self.subTest(kind=kind):
                    response=server.app.test_client().get('/api/download/export-test/'+kind)
                    self.assertEqual(response.status_code,200,response.data[:200])
                    self.assertIn('attachment;',response.headers['Content-Disposition'])
                    if kind=='source':self.assertEqual(response.data.decode(),export_source(data))
                    elif kind=='simplified-html':self.assertEqual(response.data.decode(),simplified_html(data))
                    elif kind=='csv':self.assertEqual(len(list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))),8)
                    self.assertTrue(list(self.folder.glob('export-*')))
                    response.close();self.assertEqual(list(self.folder.glob('export-*')),[])

    def test_empty_source_text_rejected_before_event_expansion_but_csv_still_works(self):
        data=fixture();data['sources']={'/build/Src/main.c':''}
        for kind in ('source','simplified','simplified-html'):
            with self.subTest(kind=kind),patch.object(server,'result',return_value=data):
                response=server.app.test_client().get('/api/download/export-test/'+kind)
                self.assertEqual(response.status_code,400)
                self.assertIn('尚未载入配套源码文本',response.get_json()['error']);response.close()
                self.assertEqual(list(self.folder.glob('export-*')),[])
        def unreadable():raise AssertionError('must not expand events');yield
        with self.assertRaisesRegex(ValueError,'配套源码'):
            write_execution_export(data,unreadable(),self.folder/'missing.c','source')
        with patch.object(server,'result',return_value=data):
            response=server.app.test_client().get('/api/download/export-test/csv')
            self.assertEqual(response.status_code,200);response.close()

    def test_large_selected_html_is_single_pass_and_coalesces_across_old_limit(self):
        data=fixture();total=200001;consumed=[]
        class Store:
            def sources(self):return data['sources']
            def overview(self):return dict(eventCount=total)
            def iter_events(self,start,end):
                for seq in range(start,end+1):
                    consumed.append(seq) if seq in (start,end) else None
                    yield dict(data['events'][0],seq=seq)
        metadata=dict(traceStore={'format':'trace-store v1'},sources=data['sources'])
        with patch.object(server,'result',return_value=metadata),patch.object(server,'browse_store',return_value=Store()):
            response=server.app.test_client().get('/api/download/export-test/simplified-html?fromSeq=0&toSeq=200000')
            self.assertEqual(response.status_code,200,response.data[:200])
            payload=json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>',response.data.decode(),re.S)[1])
            self.assertEqual(len(payload['rows']),1);self.assertEqual(payload['rows'][0]['records'],total)
            self.assertEqual(payload['rows'][0]['endSeq'],200000);self.assertEqual(consumed,[0,200000]);response.close()

    def test_mid_export_failure_removes_partial_file(self):
        data=fixture()
        def broken():yield data['events'][0];raise ValueError('broken event stream')
        with patch.object(server,'result',return_value=dict(data,events=broken())):
            response=server.app.test_client().get('/api/download/export-test/source')
            self.assertEqual(response.status_code,400);self.assertIn('broken event stream',response.get_json()['error'])
            self.assertEqual(list(self.folder.glob('export-*')),[]);response.close()
