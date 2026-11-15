"""H743 acceptance pipeline and target restoration, with no USB access."""

from collections import deque
from contextlib import contextmanager
import struct
import unittest
from unittest.mock import patch

from tools import board_acceptance_pipeline as pipeline
from tools.board_acceptance_native import NativeFailure
from tools.board_acceptance_target import Pending, SRAM_PIPELINE, Unsafe, pattern
from tools.tests.test_board_acceptance_native import NativeSetup


BASE = 0x24020000
PROFILE = {**SRAM_PIPELINE, 'repeats': 1, 'soakSeconds': 0}
SIZE = PROFILE['windowBytes']


class MockUsb:
    """Decode the transmitted bytes and emulate ordered CMSIS-DAP replies."""

    wMaxPacketSize = 512

    def __init__(self, target, *, corrupt_after_write=False):
        self.target = target
        self.replies = deque()
        self.max_pending = 0
        self.wrote = False
        self.corrupt_after_write = corrupt_after_write
        self.clocks = []
        rcc = 0x58024400
        registers = {pipeline.CPUID: 0x411fc271, pipeline.DBGMCU_IDCODE: 0x450,
                     pipeline.DHCSR: 1 << 17, pipeline.CCR: 0,
                     rcc: (1 << 17) | (1 << 25), rcc + 0x10: 3 << 3, rcc + 0x18: 8,
                     rcc + 0x28: (5 << 4) | 2, rcc + 0x2c: 1 << 16,
                     rcc + 0x30: 191 | (1 << 9)}
        for address, value in registers.items():
            target.poke(address, struct.pack('<I', value))

    def write(self, packet, *, timeout):
        self.replies.append(self.reply(packet))
        self.max_pending = max(self.max_pending, len(self.replies))
        return len(packet)

    def read(self, length, *, timeout):
        response = self.replies.popleft()
        if len(response) > length:
            raise RuntimeError('mock USB response exceeds read buffer')
        return response

    def reply(self, packet):
        if packet[:1] == b'\x7f':
            if packet[:6] != b'\x7f\x02\x05\x00\x01\x05' or packet[10:12] != b'\x06\x00':
                raise ValueError('invalid TAR or TransferBlock request')
            address, words = struct.unpack_from('<I2xH', packet, 6)
            length = words * 4
            if address % 1024 + length > 1024:
                raise ValueError('TransferBlock wrapped TAR')
            header = b'\x7f\x02\x05\x01\x01\x06' + struct.pack('<HB', words, 1)
            if packet[14] == 0x0d:
                if len(packet) != 15 + length:
                    raise ValueError('invalid write payload length')
                self.target.poke(address, packet[15:])
                self.wrote = True
                return header
            if packet[14] != 0x0f or len(packet) != 15:
                raise ValueError('invalid read packet')
            data = self.target.peek(address, length)
            if self.wrote and self.corrupt_after_write:
                data = bytes([data[0] ^ 1]) + data[1:]
            return header + data
        if packet[0] == 5:
            header = b'\x05\x01\x01'
            if len(packet) == 4:
                value = {2: pipeline.DPIDR_H743, 3: 0x23000052, 6: 0xa0000000}[packet[3]]
                return header + struct.pack('<I', value)
            return header
        if packet[0] == 0:
            return {0xfe: b'\x00\x01\x04', 0xff: b'\x00\x02\x00\x04'}[packet[1]]
        if packet[0] == 0x11:
            self.clocks.append(struct.unpack_from('<I', packet, 1)[0])
        return bytes((packet[0], 1 if packet[0] == 2 else 0))

    @contextmanager
    def lease(self, serial):
        dap = pipeline.BulkDap(self, self)
        try:
            yield dap
        finally:
            dap.drain()


class H743PipelineTests(unittest.TestCase):
    def test_packet_geometry_at_superlink_and_smaller_capacities(self):
        self.assertEqual(pipeline.packet_geometry(PROFILE, {'packetSize': 1024, 'packetCount': 4}), (128, 4))
        self.assertEqual(pipeline.packet_geometry(PROFILE, {'packetSize': 64, 'packetCount': 2}), (8, 2))
        self.assertEqual(pipeline.packet_geometry(PROFILE, {'packetSize': 4096, 'packetCount': 32}), (256, 4))

    def test_pipeline_readback_and_restore_after_success_or_wire_corruption(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), NativeSetup(board='h743', clock_hz=60_000_000) as setup:
                original = pattern(SIZE + 128)
                setup.mock.poke(BASE - 64, original)
                usb = MockUsb(setup.mock, corrupt_after_write=corrupt)
                with patch.object(pipeline, 'open_dap', usb.lease):
                    if corrupt:
                        with self.assertRaisesRegex(NativeFailure, '回包不符'):
                            pipeline.run(setup.native, BASE, original, PROFILE)
                    else:
                        report = pipeline.run(setup.native, BASE, original, PROFILE)
                        self.assertEqual(report['targetClock']['hclkHz'], 240_000_000)
                        self.assertTrue(report['verified'] and report['restored'])
                self.assertEqual(usb.max_pending, 4)
                self.assertEqual(usb.clocks, [1_000_000, 60_000_000])
                self.assertEqual(setup.mock.peek(BASE - 64, len(original)), original)
                self.assertEqual(setup.mock.state, 'running')

    def test_cache_enabled_is_pending_without_sram_writes(self):
        with NativeSetup(board='h743') as setup:
            original = setup.mock.peek(BASE - 64, SIZE + 128)
            usb = MockUsb(setup.mock)
            setup.mock.poke(pipeline.CCR, struct.pack('<I', 1 << 16))
            with patch.object(pipeline, 'open_dap', usb.lease):
                with self.assertRaisesRegex(Pending, 'D-cache'):
                    pipeline.run(setup.native, BASE, original, PROFILE)
            self.assertFalse(usb.wrote)
            self.assertEqual(setup.mock.writes, 0)
            self.assertEqual(setup.mock.state, 'running')

    def test_uncertain_drain_stops_before_independent_restore_or_resume(self):
        with NativeSetup(board='h743') as setup:
            original = setup.mock.peek(BASE - 64, SIZE + 128)
            usb = MockUsb(setup.mock)

            @contextmanager
            def failed_drain(serial):
                with usb.lease(serial) as dap:
                    yield dap
                raise Unsafe('未确认收尾')

            with patch.object(pipeline, 'open_dap', failed_drain):
                with self.assertRaises(Unsafe):
                    pipeline.run(setup.native, BASE, original, PROFILE)
            self.assertTrue(usb.wrote)
            self.assertEqual(setup.mock.writes, 0)
            self.assertEqual(setup.mock.resume_calls, 0)

    def test_bad_independent_restore_blocks_following_cases(self):
        with NativeSetup(board='h743', ignore_writes_after=0) as setup:
            original = setup.mock.peek(BASE - 64, SIZE + 128)
            usb = MockUsb(setup.mock)
            with patch.object(pipeline, 'open_dap', usb.lease):
                with self.assertRaisesRegex(Unsafe, '恢复未确认'):
                    pipeline.run(setup.native, BASE, original, PROFILE)
            self.assertTrue((setup.folder / 'pipeline-before.bin').is_file())
            self.assertEqual(setup.mock.resume_calls, 0)
