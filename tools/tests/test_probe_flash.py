from argparse import Namespace
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from tools import probe_flash


def app_pack(payload):
    header = struct.pack('<4I', 0x48504D21, len(payload), zlib.crc32(payload, 0x0D000721), 1)
    return header.ljust(256, b'\0') + payload


class ProbeFlashTests(unittest.TestCase):
    def test_dry_run_checks_crc_without_usb_or_dfu_util(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'probe.bin'
            valid = app_pack(b'firmware')
            args = Namespace(file=image, dry_run=True)
            image.write_bytes(valid)
            with patch.object(probe_flash, 'run', side_effect=AssertionError('DFU forbidden')):
                self.assertEqual(probe_flash.flash(args)['bytes'], len(valid))
                image.write_bytes(valid[:-1] + b'!')
                with self.assertRaisesRegex(ValueError, 'CRC32'):
                    probe_flash.flash(args)

    def test_exact_device_backup_readback_and_runtime_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            image = folder / 'probe app.bin'
            payload = app_pack(b'new app')
            image.write_bytes(payload)
            tool = folder / 'dfu-util.exe'
            tool.touch()
            old_pack = app_pack(b'old app')
            old = old_pack.ljust(probe_flash.APP_SIZE, b'\xff')
            calls = []
            corrupt = False

            def simulate(exe, arguments, **options):
                calls.append(arguments)
                self.assertEqual(exe, str(tool.resolve()))
                if arguments[:2] == ['-l', '-d'] and arguments[2] == probe_flash.DFU_ID:
                    return 'Found DFU: [0d28:0207] ver=0100, cfg=1, intf=0, path="2-2.4", alt=0, name="@Internal Flash /0x80020000/55*16Kg", serial="TEST1"'
                self.assertEqual(arguments[arguments.index('-S') + 1], 'TEST1')
                self.assertEqual(arguments[arguments.index('-p') + 1], '2-2.4')
                if arguments[0] == '-l':
                    return 'Found Runtime: [0d28:0204] ver=0100, cfg=1, intf=4, path="2-2.4", alt=0, name="Runtime", serial="TEST1"'
                if '-U' in arguments:
                    target = Path(arguments[-1])
                    target.write_bytes(old if target.name.startswith('before') else payload + (b'x' if corrupt else b''))
                if '-D' in arguments:
                    self.assertEqual(Path(arguments[-1]).read_bytes(), payload)
                    self.assertEqual((Path(arguments[-1]).parent/'before-app-region.bin').read_bytes(), old)
                    self.assertNotIn(':leave', ' '.join(arguments))
                return ''

            args = Namespace(file=image, dry_run=False, dfu_util=tool, serial='TEST1', backup_dir=folder/'ok')
            with patch.object(probe_flash, 'run', side_effect=simulate):
                result = probe_flash.flash(args)
                self.assertTrue(result['verified'] and result['runtimeDetected'])
                self.assertEqual((args.backup_dir/'before-app-pack.bin').read_bytes(), old_pack)
                self.assertIn(':leave', calls[-2])
                calls.clear()
                corrupt = True
                args.backup_dir = folder/'mismatch'
                with self.assertRaisesRegex(RuntimeError, '逐字节'):
                    probe_flash.flash(args)
                self.assertFalse(any(':leave' in arguments for arguments in calls))
