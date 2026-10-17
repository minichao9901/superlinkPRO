"""PXLogic 后端离线测试：位打包解析、跳变表生成、与 Kingst 链路的 CSV 一致性。

不需要硬件：用已知字节流合成 5 路位打包 channel_N.bin，
再经 pxlogic_la.write_transition_csv 生成跳变 CSV，
最后交给 tools/trace_port.py 的 DDR 提取，要求还原出同一份载荷。
"""
import csv
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'la'))
import pxlogic_la as px                     # noqa: E402
from trace_port import extract, read_tracks  # noqa: E402

CHANNELS = [0, 1, 2, 3, 4]
RATE = 1_000_000_000          # 1 GSa/s，1 样本 = 1 ns
HALF = 125                    # TRACECLK 半周期 125 样本
DELAY = 7                     # 数据在时钟沿后 7 样本建立
DELAY_CHANNEL = 3             # 数据通道相对时钟的额外延时（样本）


def pack(levels):
    """电平序列 → 位打包字节（8 样本/字节、LSB-first）。"""
    out = bytearray((len(levels) + 7) // 8)
    for index, level in enumerate(levels):
        if level:
            out[index >> 3] |= 1 << (index & 7)
    return bytes(out)


def synth(directory, payload, prefix=0, initial_clock=0):
    """按 DDR 时序合成 5 路位打包文件，返回样本数。

    时钟在每 HALF 个样本翻转；第 k 个 nibble 在第 k 个半周期内保持稳定。
    """
    nibbles = [prefix] + [n for byte in payload for n in (byte & 15, byte >> 4)]
    total = (len(nibbles) + 2) * HALF
    tracks = {channel: [1] * total for channel in CHANNELS}     # 初值：数据线为 1
    tracks[0] = [initial_clock] * total
    for index in range(total):
        tracks[0][index] = ((index // HALF) + initial_clock) & 1
        interval = index // HALF
        if interval < len(nibbles):
            start = interval * HALF + DELAY
            if start <= index < start + HALF:
                value = nibbles[interval]
                for bit in range(4):
                    tracks[bit + 1][index] = (value >> bit) & 1
    for channel in CHANNELS:
        (directory / f'channel_{channel}.bin').write_bytes(pack(tracks[channel]))
    return total, tracks


class ParseTests(unittest.TestCase):
    def test_rate_and_duration_suffixes(self):
        self.assertEqual(px.parse_rate('500M'), 500_000_000)
        self.assertEqual(px.parse_rate('10k'), 10_000)
        self.assertEqual(px.parse_rate('1000000'), 1_000_000)
        self.assertEqual(px.parse_rate(2_000_000), 2_000_000)
        self.assertAlmostEqual(px.parse_seconds('2ms'), 0.002)
        self.assertAlmostEqual(px.parse_seconds('0.01'), 0.01)
        self.assertAlmostEqual(px.parse_seconds('1.5s'), 1.5)

    def test_channel_lists(self):
        self.assertEqual(px.parse_channels('0,1,2'), [0, 1, 2])
        self.assertEqual(px.parse_channels('0-4'), [0, 1, 2, 3, 4])
        self.assertEqual(px.parse_channels('0,2-4'), [0, 2, 3, 4])

    def test_invalid_input_is_rejected(self):
        for bad in ('', 'abc', '5x'):
            with self.assertRaises(SystemExit):
                px.parse_rate(bad)


class BitPackingTests(unittest.TestCase):
    def test_chunk_boundary_preserves_every_transition(self):
        import random
        rng = random.Random(103)
        levels = [rng.randrange(2) for _ in range(8192 * 3 + 13)]
        expected = [i for i in range(1, len(levels)) if levels[i] != levels[i - 1]]
        self.assertEqual(px.transitions(pack(levels), len(levels)), expected)

    def test_transitions_are_one_based_sample_indices(self):
        # 样本 0..15：0 0 1 1 1 0 0 0 1 0 1 1 0 0 0 0
        levels = [0, 0, 1, 1, 1, 0, 0, 0, 1, 0, 1, 1, 0, 0, 0, 0]
        data = pack(levels)
        self.assertEqual(px.transitions(data, len(levels)), [2, 5, 8, 9, 10, 12])

    def test_level_at_matches_bit_order(self):
        data = pack([1, 0, 1, 1, 0, 0, 0, 1, 1])
        self.assertEqual([px.level_at(data, i) for i in range(9)],
                         [1, 0, 1, 1, 0, 0, 0, 1, 1])

    def test_no_transition_on_constant_signal(self):
        self.assertEqual(px.transitions(pack([1] * 40), 40), [])
        self.assertEqual(px.transitions(pack([0] * 40), 40), [])


class TransitionCsvTests(unittest.TestCase):
    def test_csv_roundtrip_reproduces_payload(self):
        payload = b'\xff\xff\xff\x7f' + bytes(range(64)) + b'\xff\xff\xff\x7f'
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            total, _ = synth(directory, payload)
            csv_path = directory / 'capture.csv'
            summary = px.write_transition_csv(directory, csv_path, CHANNELS, RATE, samples=total)
            report, candidates = extract(csv_path, 0, [1, 2, 3, 4], fraction=0.25)
            self.assertIn(payload, candidates)
            self.assertGreater(summary['transitions']['1'], 0)    # 数据线在时钟沿之后翻转
            self.assertGreater(summary['transitions']['0'], len(payload) * 2)   # 每字节两个 DDR nibble

    def test_csv_levels_match_bit_packed_source(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            total, tracks = synth(directory, bytes(range(32)))
            csv_path = directory / 'capture.csv'
            px.write_transition_csv(directory, csv_path, CHANNELS, RATE, samples=total)
            initial, transitions, start, end = read_tracks(csv_path)
            self.assertEqual(start, 0.0)
            # 末行是最后一次跳变，必然落在窗口内；不能要求它等于最后一个样本的时刻。
            self.assertLessEqual(end, (total - 1) / RATE + 1e-9)
            self.assertGreater(end, 0.0)
            for channel in CHANNELS:
                levels = tracks[channel].copy()
                expected = [i for i in range(1, total) if levels[i] != levels[i - 1]]
                got = [round(time * RATE) for time, _ in transitions.get(channel, [])]
                self.assertEqual(got, expected, f'通道 {channel} 跳变时刻不一致')
                self.assertEqual(initial[channel], levels[0])

    def test_generated_csv_has_no_comment_line(self):
        """tools/trace_port.py 把第一个非空行当表头，注释行会让它把 Time[s] 当时间列。"""
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            total, _ = synth(directory, bytes(range(8)))
            csv_path = directory / 'capture.csv'
            px.write_transition_csv(directory, csv_path, CHANNELS, RATE, samples=total)
            first = csv_path.read_text(encoding='utf-8-sig').splitlines()[0]
            self.assertTrue(first.startswith('Time[s]'), first)
            self.assertFalse(any(line.startswith('#') for line in
                                 csv_path.read_text(encoding='utf-8-sig').splitlines()))
            read_tracks(csv_path)            # 既有消费者必须能直接读

    def test_padding_bits_do_not_create_phantom_tail_transition(self):
        """样本数不是 8 的倍数时，末字节补齐 0 不能被当成一次下跌。"""
        levels = [1] * 13                     # 13 个样本 → 2 字节，末字节有 3 个补齐 0
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / 'channel_0.bin').write_bytes(pack(levels))
            trimmed = px.write_transition_csv(directory, directory / 'trim.csv', [0], RATE, samples=13)
            padded = px.write_transition_csv(directory, directory / 'pad.csv', [0], RATE)
            self.assertEqual(trimmed['transitions']['0'], 0)
            self.assertEqual(trimmed['padding_samples'], 3)
            self.assertEqual(padded['transitions']['0'], 1)       # 不传真实样本数就会多出假跳变

    def test_missing_channel_file_is_reported(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / 'channel_0.bin').write_bytes(pack([0] * 16))
            with self.assertRaises(SystemExit):
                px.write_transition_csv(directory, directory / 'x.csv', CHANNELS, RATE)


class RawDirTests(unittest.TestCase):
    def test_export_missing_empty_old_or_unequal_is_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            with self.assertRaises(SystemExit):
                px.validate_export(directory, [0], time.time())
            p = directory / 'channel_0.bin'
            p.write_bytes(b'')
            with self.assertRaises(SystemExit):
                px.validate_export(directory, [0], time.time())
            p.write_bytes(b'\x00')
            os.utime(p, (1, 1))
            with self.assertRaises(SystemExit):
                px.validate_export(directory, [0], time.time())
            p.write_bytes(b'\x00')
            (directory / 'channel_1.bin').write_bytes(b'\x00\x00')
            with self.assertRaises(SystemExit):
                px.validate_export(directory, [0, 1], time.time())

    """Codex review 2026-10-13 P1：导出目录残留会被当成本次采集的结果。"""

    def test_old_bitstreams_are_refused_without_reuse(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / 'channel_0.bin').write_bytes(pack([0] * 16))
            with self.assertRaises(SystemExit):
                px.prepare_raw_dir(directory, reuse=False)

    def test_old_bitstreams_are_removed_with_reuse(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / 'channel_0.bin').write_bytes(pack([0] * 16))
            (directory / 'channel_3.bin').write_bytes(pack([1] * 16))
            (directory / 'keep.txt').write_text('x', encoding='utf-8')
            removed = px.prepare_raw_dir(directory, reuse=True)
            self.assertEqual(removed, ['channel_0.bin', 'channel_3.bin'])
            self.assertEqual(list(directory.glob('channel_*.bin')), [])
            self.assertTrue((directory / 'keep.txt').exists())      # 只清位流，不动别的文件


class ReconcileTests(unittest.TestCase):
    """Codex review 2026-10-13 P1：容量与请求不符时不能默默改用别的时间轴。"""

    def test_byte_padding_within_one_byte_is_accepted(self):
        for padding in range(8):
            self.assertEqual(px.reconcile_samples(1000, 1000 + padding, force=False), 1000)

    def test_bigger_mismatch_is_rejected_by_default(self):
        with self.assertRaises(SystemExit):
            px.reconcile_samples(1000, 2000, force=False)

    def test_force_accepts_but_reports_exported_capacity(self):
        self.assertEqual(px.reconcile_samples(1000, 2000, force=True), 2000)


class StatsTests(unittest.TestCase):
    def test_reference_rate_does_not_assume_half_duty(self):
        levels = [int(i % 8 < 2) for i in range(80)]
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / 'channel_0.bin').write_bytes(pack(levels))
            rate, error = px.verify_rate_from_reference(directory, 0, 1000, 8000, 80, .01)
            self.assertEqual(rate, 8000)
            self.assertEqual(error, 0)

    def test_reference_requires_multiple_complete_cycles(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / 'channel_0.bin').write_bytes(pack([0] * 10 + [1] * 10 + [0] * 10))
            with self.assertRaises(SystemExit):
                px.verify_rate_from_reference(directory, 0, 1000, 20000, 30, .01)

    def test_square_wave_frequency_and_duty(self):
        half = 5000
        levels = [1 if (i // half) % 2 else 0 for i in range(half * 40)]
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / 'channel_0.bin').write_bytes(pack(levels))
            item = px.channel_stats(directory, 0, 10_000_000)
        self.assertEqual(item['edges'], 39)
        self.assertEqual(item['median_half_period_samples'], half)
        self.assertAlmostEqual(item['frequency_hz'], 1000.0, places=3)
        self.assertAlmostEqual(item['duty_percent'], 50.0, places=3)


class CaptureProvenanceTests(unittest.TestCase):
    def test_same_second_captures_keep_raw_and_use_verified_rate(self):
        with tempfile.TemporaryDirectory() as name:
            out = Path(name) / 'capture.csv'
            args = px.build_parser().parse_args([
                'capture', '--out', str(out), '--channels', '0', '--rate', '1000',
                '--samples', '16', '--verify-hz', '100', '--verify-tolerance', '.2'])

            def cli(_cli, command, *_args, **_kwargs):
                if command[0] == 'export':
                    raw = Path(command[command.index('--dir') + 1])
                    (raw / 'channel_0.bin').write_bytes(b'\x0f\xf0')
                return '', {'state': 'completed'}

            with patch.object(px, 'find_cli', return_value='mock'), \
                 patch.object(px, 'pick_device', return_value={'id': 0, 'is_hardware': True}), \
                 patch.object(px, 'status', return_value={'state': 'completed', 'have_hardware_data': True}), \
                 patch.object(px, 'run_cli', side_effect=cli), \
                 patch.object(px, 'verify_rate_from_reference', return_value=(900, .1)), \
                 contextlib.redirect_stdout(io.StringIO()):
                px.cmd_capture(args)
                px.cmd_capture(args)

            manifests = list(Path(name).glob('capture.pxlogic/*/capture.json'))
            self.assertEqual(len(manifests), 2)
            for path in manifests:
                m = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(m['stale_removed'], [])
                self.assertEqual(m['capture']['requested']['samples'], 16)
                self.assertEqual(m['capture']['time_axis_rate_hz'], 900)
                self.assertEqual(m['capture']['actual']['sample_rate_hz'], 900)
                self.assertTrue(m['capture']['actual']['verified'])
                self.assertAlmostEqual(m['transition_csv']['window_ms'], 16 / 900 * 1000)
            _, tracks, _, _ = read_tracks(out)
            self.assertAlmostEqual(tracks[0][0][0], 4 / 900, places=8)

    def test_selftest_refuses_success_response_without_fresh_export(self):
        with tempfile.TemporaryDirectory() as name:
            (Path(name) / 'channel_0.bin').write_bytes(pack([i % 2 for i in range(100)]))
            args = px.build_parser().parse_args(['selftest', '--dir', name])
            with patch.object(px, 'find_cli', return_value='mock'), \
                 patch.object(px, 'pick_device', return_value={'id': 0}), \
                 patch.object(px, 'status', return_value={'state': 'completed', 'have_hardware_data': True}), \
                 patch.object(px, 'run_cli', return_value=('', {})), \
                 self.assertRaises(SystemExit):
                px.cmd_selftest(args)


if __name__ == '__main__':
    unittest.main()
