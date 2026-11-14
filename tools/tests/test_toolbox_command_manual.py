from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from tools import command_manual


ROOT = Path(__file__).resolve().parents[2]


class CommandManualTests(unittest.TestCase):
    def test_manual_requires_same_origin_authentication_and_confirmed_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / 'cli-token'
            token.write_text('a' * 64)
            app = Flask(__name__)
            command_manual.install(app, token, ROOT/'docs/toolbox-cli-first-run.md', [sys.executable], ROOT)
            client = app.test_client()
            headers = {'X-Trace-CLI': 'a' * 64, 'Origin': 'http://localhost'}
            self.assertEqual(client.get('/command-manual').status_code, 403)
            self.assertTrue(client.get('/command-manual/status', headers=headers).json['available'])
            rows = client.get('/command-manual', headers=headers).json['rows']
            self.assertEqual({row['id'] for row in rows if 'id' in row}, set(command_manual.BY_NAME))
            self.assertEqual(client.post('/command-manual/run', headers={**headers, 'Origin': 'http://other'},
                                         json={'id': 'catalog'}).status_code, 403)
            self.assertEqual(client.post('/command-manual/run', headers=headers,
                                         json={'id': 'python -c anything'}).status_code, 404)
            self.assertEqual(client.post('/command-manual/run', headers=headers,
                                         json={'id': 'program'}).status_code, 400)
            reply = client.post('/command-manual/run', headers=headers,
                                json={'id': 'chip', 'values': {'chip': 'stm32h7'}}).json
            result = client.get('/command-manual/job/' + reply['job'], headers=headers).json
            self.assertEqual(result['exitCode'], 0)
            self.assertIn('stm32h7', result['output'])

    def test_frozen_cli_uses_current_runtime_and_one_literal_argument_for_firmware_path(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            firmware = folder / 'fw & with spaces.elf'
            firmware.touch()
            prefix = ['superlinkPRO.exe', '--workbench-cli']
            with (folder / 'log').open('wb') as output, patch.object(command_manual.subprocess, 'Popen') as launch:
                command_manual.start_command(command_manual.BY_NAME['program'],
                                             {'chip': 'hpm6800evk', 'firmware': str(firmware)},
                                             folder, prefix, ROOT, output)
            argv, = launch.call_args.args
            self.assertEqual(argv[:2], prefix)
            self.assertEqual(argv[argv.index('--runtime') + 1], str(folder))
            self.assertEqual(argv[argv.index('--file') + 1], str(firmware.resolve()))
            self.assertFalse(launch.call_args.kwargs.get('shell', False))

    def test_probe_check_and_upgrade_use_the_same_literal_file_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            firmware = folder / 'probe APP.bin'
            firmware.touch()
            prefix = ['superlinkPRO.exe', '--workbench-cli']
            for step, tail in (('probe-check', ['--dry-run']),
                               ('probe-firmware', ['--backup-dir', str(folder/'artifacts')])):
                with (folder/'output.log').open('wb') as output, patch.object(command_manual.subprocess, 'Popen') as launch:
                    command_manual.start_command(command_manual.BY_NAME[step], {'probe-check':str(firmware)},
                                                 folder, prefix, ROOT, output)
                argv, = launch.call_args.args
                self.assertEqual(argv, [*prefix, 'probe-flash', '--file', str(firmware.resolve()), *tail])
