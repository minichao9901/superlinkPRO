"""CLI contracts and Web ZIP lifetime; no target or browser connection."""
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

sys.path[:0]=[str(Path(__file__).resolve().parents[2]),str(Path(__file__).resolve().parents[1])]
import server
import trace_cli as cli
import reading_projection
from reading_view import document


class InterfaceTests(unittest.TestCase):
    def test_capabilities_and_dotted_fields_keep_stdout_single_json(self):
        out=io.StringIO()
        with patch.object(cli,'fingerprint',return_value=dict(bytes=1,sha256='fixture')),patch.object(cli.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=b'{"features":["reading-v1"]}')),__import__('contextlib').redirect_stdout(out):
            code=cli.main(['capabilities'])
        value=json.loads(out.getvalue());self.assertEqual(code,0);self.assertTrue(value['result']['softwareCapabilities']['readingProjection']['available'])
        args=SimpleNamespace(fields='summary.instructions,quality.unknownExecution',compact=False)
        self.assertEqual(cli.query_selection(dict(summary=dict(instructions=123,functions={'main':100}),quality=dict(unknownExecution=2)),args),dict(summary=dict(instructions=123),quality=dict(unknownExecution=2)))
        with self.assertRaises(cli.CliError):cli.query_selection({},args)

    def test_reading_options_reject_invalid_limits(self):
        for config in [dict(repeatThreshold=2),dict(keepFirst=10,keepLast=2),dict(maxBlock=0),dict(maxBlock=513)]:
            with self.assertRaises(ValueError):reading_projection.options(config)
        self.assertEqual(reading_projection.options()['repeatThreshold'],10)

    def test_zip_download_contains_all_three_files_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary);calls=[]
            class Store:
                def overview(self):return dict(eventCount=50)
                def sources(self):return {'main.c':'work();'}
                def export_reading(self,result,out,mapping,manifest,kind,start,end):
                    calls.append((kind,start,end));out.write_text('work();');mapping.write_text('{"kind":"header"}\n');manifest.write_text('{"schema":"etm-reading/1"}')
            with patch.object(server,'directory',return_value=folder),patch.object(server,'browse_store',return_value=Store()):
                for format in ('reading-c','reading-html'):
                    response=server.app.test_client().get('/api/download/fixture/'+format+'?fromSeq=10&toSeq=20')
                    self.assertEqual(response.status_code,200,response.data[:100])
                    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:self.assertEqual(len(archive.namelist()),3)
                    self.assertTrue(list(folder.glob('export-*.zip')))
                    response.close();self.assertFalse(list(folder.iterdir()))
                self.assertEqual(calls,[('simplified',10,20),('simplified-html',10,20)])
                response=server.app.test_client().get('/api/download/fixture/reading-c?fromSeq=10');self.assertEqual(response.status_code,400);response.close()

    def test_failed_reading_download_does_not_leave_files_or_publish_error_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            class Store:
                def overview(self):return {}
                def sources(self):return {}
                def export_reading(self,*args):raise ValueError('fixture export failed')
            with patch.object(server,'directory',return_value=folder),patch.object(server,'browse_store',return_value=Store()):
                response=server.app.test_client().get('/api/download/fixture/reading-html')
                self.assertEqual(response.status_code,400);self.assertIn('fixture export failed',response.get_json()['error']);response.close()
                self.assertFalse(list(folder.iterdir()))

    def test_reading_html_keeps_original_compact_layout_and_safe_text_nodes(self):
        from source_export import _html_document
        html=document();original=_html_document('fixture',None)
        self.assertEqual(html.split('<style>')[1].split('</style>')[0],original.split('<style>')[1].split('</style>')[0])
        self.assertIn('完整映射与质量见配套清单',html)
        self.assertNotIn('innerHTML',html);self.assertIn('textContent',html)
        self.assertNotIn('<details',html);self.assertNotIn('展开代表片段',html)
        self.assertIn('choose(i,seq)',html);self.assertIn('未知',html)
        self.assertNotIn('</script>',reading_projection.json_text({'source':'</script><script>bad()</script>'}))


if __name__=='__main__':unittest.main()
