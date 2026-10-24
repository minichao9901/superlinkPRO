"""Managed lifetime and refusal paths. All process creation is mocked."""
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import trace_cli_runtime as runtime


class RuntimeTests(unittest.TestCase):
    def socket(self):return SimpleNamespace(__enter__=lambda s:s,__exit__=lambda *a:None)

    def test_remote_service_is_rejected_without_spawning(self):
        with patch.object(runtime.subprocess,'Popen') as spawn,self.assertRaises(ValueError):
            runtime.start('unused','http://192.0.2.2:8891')
        spawn.assert_not_called()

    def test_url_with_credentials_or_query_is_rejected_without_spawning(self):
        with patch.object(runtime.subprocess,'Popen') as spawn:
            for url in ('http://user@127.0.0.1:8891','http://127.0.0.1:8891/?token=x'):
                with self.assertRaises(ValueError):runtime.start('unused',url)
            spawn.assert_not_called()

    def test_process_creation_failure_is_recorded_instead_of_left_starting(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(runtime.socket,'socket'),patch.object(runtime.subprocess,'Popen',side_effect=OSError('fixture denied')):
            folder=Path(tmp)/'runtime'
            with self.assertRaisesRegex(OSError,'fixture denied'):runtime.start(folder,'http://127.0.0.1:8891')
            state=runtime.read(folder/'runtime.json')
            self.assertEqual(state['status'],'failed');self.assertIn('fixture denied',state['error'])
            with self.assertRaisesRegex(RuntimeError,'fixture denied'):runtime.stop(folder)

    def test_an_existing_runtime_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(runtime.socket,'socket') as sock,patch.object(runtime.subprocess,'Popen') as spawn:
            folder=Path(tmp)/'runtime';folder.mkdir();(folder/'user').write_bytes(b'keep')
            with self.assertRaises(FileExistsError):runtime.start(folder,'http://127.0.0.1:8891')
            self.assertEqual((folder/'user').read_bytes(),b'keep');spawn.assert_not_called()

    def test_busy_listener_is_not_stopped_or_replaced(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(runtime.socket,'socket') as sock,patch.object(runtime.subprocess,'Popen') as spawn:
            sock.return_value.__enter__.return_value.bind.side_effect=OSError('busy')
            folder=Path(tmp)/'runtime'
            with self.assertRaises(OSError):runtime.start(folder,'http://127.0.0.1:8891')
            self.assertFalse(folder.exists());spawn.assert_not_called()

    def test_start_persists_private_token_and_waits_for_ready(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(runtime.socket,'socket'),patch.object(runtime.subprocess,'Popen') as spawn:
            folder=Path(tmp)/'runtime'
            def launch(*args,**kwargs):
                state=runtime.read(folder/'runtime.json');state['status']='ready';runtime.write(folder/'runtime.json',state)
                return SimpleNamespace(poll=lambda:None)
            spawn.side_effect=launch
            value=runtime.start(folder,'http://127.0.0.1:8891','exact-probe')
            self.assertEqual(value['status'],'ready');self.assertEqual(value['probe'],'exact-probe')
            self.assertEqual(len((folder/'cli-token').read_text()),64)
            self.assertNotIn((folder/'cli-token').read_text(),json.dumps(value))

    def test_stop_uses_own_nonce_and_does_not_kill_an_arbitrary_pid(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(runtime.subprocess,'Popen') as spawn:
            folder=Path(tmp);runtime.write(folder/'runtime.json',dict(status='ready',nonce='owned',hostPid=123))
            def clean(_):runtime.write(folder/'runtime.json',dict(status='stopped',nonce='owned'))
            with patch.object(runtime.time,'sleep',side_effect=clean):self.assertEqual(runtime.stop(folder)['status'],'stopped')
            self.assertEqual(runtime.read(folder/'stop-request.json')['nonce'],'owned');spawn.assert_not_called()

    def test_failed_worker_handshake_requests_cleanup_before_closing_server(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            runtime.write(folder/'runtime.json',dict(url='http://127.0.0.1:8891',probe=None,imageRoot=None,nonce='owned'))
            worker=SimpleNamespace(pid=123,stdin=io.BytesIO(),returncode=None,poll=lambda:None)
            def finish(**kwargs):worker.returncode=0
            worker.wait=Mock(side_effect=finish)
            server=Mock()
            service=SimpleNamespace(create_application=Mock(return_value=object()))
            loader=SimpleNamespace(exec_module=lambda module:None)
            with patch('importlib.util.spec_from_file_location',return_value=SimpleNamespace(loader=loader)),patch('importlib.util.module_from_spec',return_value=service),patch('werkzeug.serving.make_server',return_value=server),patch.object(runtime.threading,'Thread'),patch.object(runtime.subprocess,'Popen',return_value=worker),patch.object(runtime,'request',side_effect=RuntimeError('handshake failed')),patch.object(runtime.time,'monotonic',side_effect=[0,13]),patch.dict(sys.modules,{'decode_trace':SimpleNamespace(ROOT=None)}):
                with self.assertRaisesRegex(RuntimeError,'handshake failed'):runtime.host(folder)
            self.assertEqual(worker.stdin.getvalue(),b'stop\n')
            worker.wait.assert_called_once_with(timeout=960)
            server.shutdown.assert_called_once();server.server_close.assert_called_once()
            state=runtime.read(folder/'runtime.json');self.assertEqual(state['status'],'failed')
            self.assertNotIn('cleanupPending',state)


if __name__=='__main__':unittest.main()
