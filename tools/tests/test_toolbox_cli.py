"""Unified entry, argument encoding, queue transport and failure evidence; no hardware."""
import base64
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('toolbox_under_test', ROOT / 'toolbox.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
sys.path.insert(0, str(ROOT / 'etm/tools'))


class EntryTests(unittest.TestCase):
    def test_all_cli_arguments_have_current_explanations(self):
        spec = importlib.util.spec_from_file_location('cli_options_under_test', ROOT / 'tools/build_cli_options.py')
        reference = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reference)
        text, count = reference.render()
        self.assertEqual(count, 63)
        self.assertEqual(text, (ROOT / 'docs/toolbox-cli-options.md').read_text(encoding='utf-8'))
        self.assertIn('必须且只能选一项', text)

    def test_catalog_and_reference_cover_all_pages(self):
        reference = cli.load_json(ROOT / 'docs/toolbox-cli-reference.json')
        self.assertEqual({p['id'] for p in reference['pages']}, set(cli.PAGES))
        self.assertEqual(len(cli.PAGES), 15)
        self.assertGreater(sum(len(p['controls']) for p in reference['pages']), 700)

    def test_trace_is_forwarded_without_reinterpreting_arguments(self):
        with patch.object(cli.subprocess, 'call', return_value=7) as run:
            self.assertEqual(cli.main(['trace', '--runtime', 'E:/x y', 'web', 'command', '-e', 'p x']), 7)
        self.assertEqual(run.call_args.args[0], [sys.executable, '-X', 'utf8', str(ROOT / 'etm/trace.py'), '--runtime', 'E:/x y', 'web', 'command', '-e', 'p x'])

    def test_json_file_bytes_and_output_are_not_shell_interpreted(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'input.bin'
            source.write_bytes(bytes([0, 128, 255]))
            v = cli.expand_arguments([{'$file': str(source), 'kind': 'buffer'}])[0]
            self.assertEqual(base64.b64decode(v['$base64']), source.read_bytes())
            self.assertEqual(v['kind'], 'buffer')
            out = Path(folder) / 'result.json'
            cli.emit({'text': '`$()中文'}, out)
            with self.assertRaises(FileExistsError):
                cli.emit({}, out)

    def test_invalid_args_and_existing_out_fail_before_side_effects(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / 'exists.json'
            out.write_text('{}')
            with patch.object(cli, 'Client') as client, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['--runtime', folder, '--out', str(out), 'ping']), 1)
                client.assert_not_called()

    def test_timeout_preserves_job_id_and_never_resubmits(self):
        client = object.__new__(cli.Client)
        client.timeout = -1
        calls = []
        def http(path, data=None):
            calls.append(path)
            return {'id': 'a' * 32} if path == 'request' else {'state': 'running'}
        client.http = http
        with self.assertRaisesRegex(TimeoutError, 'job ' + 'a' * 32):
            client.request({'action': 'workbench'})
        self.assertEqual(calls.count('request'), 1)

    def test_poll_connection_failure_also_preserves_job_id(self):
        client = object.__new__(cli.Client)
        client.timeout = 1
        client.http = lambda *a: {'id': 'b' * 32}
        client.result = lambda jid: (_ for _ in ()).throw(ConnectionError('lost'))
        with self.assertRaisesRegex(RuntimeError, 'job ' + 'b' * 32):
            client.request({'action': 'workbench'})

    def test_browser_runtime_rejects_remote_urls(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'toolbox-runtime.json').write_text(json.dumps({'url': 'https://example.com'}))
            with self.assertRaisesRegex(ValueError, '本机'):
                cli.Client(folder)


class TransportTests(unittest.TestCase):
    def test_real_http_queue_and_client_roundtrip(self):
        from flask import Flask
        from werkzeug.serving import make_server
        from web_cli_queue import install_web_cli
        with tempfile.TemporaryDirectory() as folder:
            app = Flask(__name__)
            install_web_cli(app, Path(folder) / 'cli-token')
            server = make_server('127.0.0.1', 0, app, threaded=True)
            host = threading.Thread(target=server.serve_forever, daemon=True)
            host.start()
            Path(folder, 'toolbox-runtime.json').write_text(json.dumps({'url': 'http://127.0.0.1:' + str(server.server_port)}))
            client = cli.Client(folder, 5)
            wid = 'c' * 32
            ready = threading.Event()
            failures = []
            def worker():
                try:
                    ready.set()
                    job = client.http('poll', {'worker': wid})['job']
                    if job is None:
                        job = client.http('poll', {'worker': wid})['job']
                    self.assertEqual(job['request'], {'action': 'workbench', 'page': 'analog', 'op': 'describe'})
                    client.http('complete/' + job['id'], {'worker': wid, 'result': {'ok': True, 'value': {'controls': ['real']}}})
                except BaseException as error:
                    failures.append(error)
            thread = threading.Thread(target=worker)
            thread.start()
            ready.wait(1)
            try:
                # Wait for registration rather than retry a mutating command.
                import time
                time.sleep(.05)
                result = client.request({'action': 'workbench', 'page': 'analog', 'op': 'describe'})
                self.assertTrue(result['ok'])
                self.assertEqual(result['value']['controls'], ['real'])
                self.assertEqual(client.result(result['job'])['state'], 'done')
                self.assertTrue(client.http('ack/' + result['job'], {})['removed'])
            finally:
                thread.join(5)
                server.shutdown()
                server.server_close()
            self.assertFalse(failures)


if __name__ == '__main__':
    unittest.main()
