import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tools'))
from source_export import simplified_rows,simplified_c,simplified_html
import server

def fixture():
    def event(seq,line,executed=True):return dict(kind='instruction',seq=seq,pc=0x08000000+seq*2,function='main',file='/build/Src/main.c',line=line,executed=executed,executionState='executed' if executed else 'not-taken')
    unknown=event(4,2);unknown.update(executed=None,executionState='unknown')
    return dict(events=[event(0,1),event(1,1),event(2,2),event(3,1),unknown,dict(kind='event',seq=5,type=9,text='EXCEPTION'),event(6,3,False)],sources={'/build/Src/main.c':'while (x) {\n  work();\n  skipped();'},quality={})

class SourceExportTests(unittest.TestCase):
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
