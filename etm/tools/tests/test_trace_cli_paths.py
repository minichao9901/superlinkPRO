"""Portable CLI cannot depend on developer PATH or the repository layout."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import trace_cli_paths as paths


class PathTests(unittest.TestCase):
    def test_portable_uses_only_its_bundled_node_and_frozen_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            home=Path(temporary).resolve()/'中文 运行包';node=home/'cli/node/node.exe'
            node.parent.mkdir(parents=True);node.write_bytes(b'fixture')
            with patch.object(sys,'frozen',True,create=True),patch.object(sys,'executable',str(home/'etm-bridge.exe')),patch.object(sys,'_MEIPASS',str(home/'runtime'),create=True),patch.object(paths.shutil,'which',return_value='unrelated-node') as which:
                self.assertEqual(paths.node_executable(),str(node))
                self.assertEqual(paths.backend_root(),home/'runtime/backend')
                self.assertEqual(paths.host_command('run'),[str(home/'etm-bridge.exe'),'--cli-host','run'])
                self.assertEqual(paths.hardware_lock(),home/'data/trace-cli-hardware.lock')
                node.unlink()
                with self.assertRaises(RuntimeError):paths.node_executable()
                which.assert_not_called()

    def test_source_host_remains_the_same_python_interpreter(self):
        with patch.object(sys,'frozen',False,create=True):
            argv=paths.host_command('run')
            self.assertEqual(argv[0],sys.executable)
            self.assertEqual(argv[1:3],['-X','utf8'])
            self.assertEqual(argv[-2:],['host','run'])
            self.assertTrue(paths.backend_root().joinpath('server.py').is_file())


if __name__=='__main__':unittest.main()
