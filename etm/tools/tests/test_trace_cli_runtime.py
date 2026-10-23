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


if __name__=='__main__':unittest.main()
