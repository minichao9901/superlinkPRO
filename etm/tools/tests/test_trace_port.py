"""用已知字节流合成 DDR 波形，检查初始电平、采集相位和采样偏移。"""
import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trace_port import extract, filter_pulses


def waveform(path, payload, initial_clock, prefix=None, delays=(7,)):
    nibbles = [n for byte in payload for n in (byte & 15, byte >> 4)]
    if prefix is not None:
        nibbles.insert(0, prefix)
    events = {}
    clock = initial_clock
    for i in range(len(nibbles) + 1):
        clock ^= 1
        events.setdefault(i * 125, {})[0] = clock
        if i < len(nibbles):
            events.setdefault(i * 125 + delays[i % len(delays)], {}).update(
                {bit + 1: (nibbles[i] >> bit) & 1 for bit in range(4)})
    levels = [initial_clock, 1, 1, 1, 1]
    with path.open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.writer(stream)
        writer.writerow(['Time [s]', 'Channel 0', 'Channel 1', 'Channel 2', 'Channel 3', 'Channel 4'])
        writer.writerow([-100e-9] + levels)
        for time_ns, changes in sorted(events.items()):
            for channel, level in changes.items():
                levels[channel] = level
            writer.writerow([time_ns * 1e-9] + levels)


class TracePortTests(unittest.TestCase):
    def test_utf16_bom_exports_keep_the_same_ddr_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'utf16.csv';payload=b'\xff\xff\xff\x7f'+bytes(range(16))
            waveform(path,payload,0);text=path.read_text(encoding='utf-8-sig')
            for encoding,bom in [('utf-16-le',b'\xff\xfe'),('utf-16-be',b'\xfe\xff')]:
                with self.subTest(encoding=encoding):
                    path.write_bytes(bom+text.encode(encoding));_,candidates=extract(path,0,[1,2,3,4]);self.assertEqual(candidates[0],payload)
    def test_payload_from_both_initial_clock_levels_and_byte_phases(self):
        payload = b'\xff\xff\xff\x7f' + bytes(range(256)) + b'\xff\xff\xff\x7f'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wave.csv'
            for clock in (0, 1):
                for phase in (0, 1):
                    with self.subTest(clock=clock, phase=phase):
                        waveform(path, payload, clock, prefix=0xB if phase else None)
                        report, candidates = extract(path, 0, [1, 2, 3, 4])
                        self.assertEqual(candidates[phase], payload)
                        self.assertAlmostEqual(report['clock_hz_median'], 4000000, places=3)
                        self.assertEqual(report['irregular_half_periods'], 0)
                        self.assertEqual(report['candidates'][phase]['frame_sync_matches'], 2)

    def test_constant_high_data_uses_initial_level(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'high.csv'
            waveform(path, b'\xff' * 12, 1)
            report, candidates = extract(path, 0, [1, 2, 3, 4])
            self.assertEqual(candidates[0], b'\xff' * 12)
            self.assertTrue(all(v == 0 for v in report['data_transitions'].values()))

    def test_distinct_channels_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wave.csv'
            waveform(path, b'\xa5' * 12, 0)
            with self.assertRaises(ValueError):
                extract(path, 0, [0, 1, 2, 3])

    def test_chinese_kingst_gbk_header(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wave.csv'
            waveform(path, b'\xa5' * 12, 0)
            text = path.read_text(encoding='utf-8-sig')
            path.write_bytes(text.replace('Channel', '通道').encode('gb18030'))
            _, candidates = extract(path, 0, [1, 2, 3, 4])
            self.assertEqual(candidates[0], b'\xa5' * 12)

    def test_real_board_mid_half_cycle_transitions(self):
        payload = bytes(range(32))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wave.csv'
            waveform(path, payload, 1, delays=(59, 65))
            report, before = extract(path, 0, [1, 2, 3, 4], 0.25)
            _, after = extract(path, 0, [1, 2, 3, 4], 0.75)
            _, unsafe = extract(path, 0, [1, 2, 3, 4], 0.5)
            self.assertEqual(before[1], payload[:-1])
            self.assertEqual(after[0], payload)
            self.assertEqual(before[1], after[0][:-1])
            self.assertNotEqual(unsafe[0], payload)
            self.assertGreater(report['min_data_transition_distance_ns'], 20)

    def test_clock_gap_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wave.csv'
            waveform(path, bytes(range(32)), 0)
            with path.open(encoding='utf-8-sig') as stream:
                rows = list(csv.reader(stream))
            for row in rows[2:]:
                if float(row[0]) >= 2000e-9:
                    row[0] = str(float(row[0]) + 1000e-9)
            with path.open('w', newline='', encoding='utf-8-sig') as stream:
                csv.writer(stream).writerows(rows)
            report, _ = extract(path, 0, [1, 2, 3, 4])
            self.assertGreater(report['irregular_half_periods'], 0)

    def test_clock_switch_keeps_ddr_payload_and_reports_two_rates(self):
        payload=bytes(range(256))*4
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'switch.csv'
            waveform(path,payload,0)
            with path.open(encoding='utf-8-sig') as stream:rows=list(csv.reader(stream))
            for row in rows[1:]:
                t=float(row[0])
                if t>=64e-6:row[0]=str(64e-6+(t-64e-6)/4)
            with path.open('w',newline='',encoding='utf-8-sig') as stream:csv.writer(stream).writerows(rows)
            report,candidates=extract(path,0,[1,2,3,4])
            self.assertEqual(candidates[0],payload)
            self.assertAlmostEqual(report['clock_runs'][0]['clock_hz_average'],4e6,places=2)
            self.assertAlmostEqual(report['clock_runs'][-1]['clock_hz_average'],16e6,places=2)


class PulseFilterTests(unittest.TestCase):
    def test_injected_clock_glitch_is_removed_and_original_bytes_are_restored(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'wave.csv';payload=bytes.fromhex('ff ff ff 7f 05 ab 10 22 24 44')
            waveform(p,payload,0)
            _,expected=extract(p,0,[1,2,3,4])
            with p.open(encoding='utf-8-sig',newline='') as stream:rows=list(csv.reader(stream))
            first=next(row for row in rows[1:] if abs(float(row[0])-7e-9)<1e-15)
            glitch=list(first);glitch[0]='5e-8';glitch[1]=str(1-int(first[1]))
            recovered=list(first);recovered[0]='5.2e-8'
            data=sorted(rows[1:]+[glitch,recovered],key=lambda r:float(r[0]))
            with p.open('w',encoding='utf-8-sig',newline='') as stream:csv.writer(stream).writerows([rows[0]]+data)
            _,broken=extract(p,0,[1,2,3,4])
            report,fixed=extract(p,0,[1,2,3,4],glitch_ns=4)
            self.assertNotEqual(broken,expected)
            self.assertEqual(fixed,expected)
            self.assertEqual(report['software_filter']['removed_transitions']['0'],2)
    def test_removes_complete_short_pairs_without_moving_surviving_edges(self):
        edges=[(10e-9,1),(12e-9,0),(20e-9,1),(36e-9,0),(38e-9,1),(52e-9,0)]
        self.assertEqual(filter_pulses(edges,4),[(20e-9,1),(52e-9,0)])
        self.assertEqual(filter_pulses(edges,0),edges)
    def test_filter_is_opt_in_keeps_original_file_and_rejects_destructive_limit(self):
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'wave.csv';waveform(p,bytes.fromhex('ff ff ff 7f 05 20'),0)
            before=p.read_bytes();raw,_=extract(p,0,[1,2,3,4],.25)
            filtered,_=extract(p,0,[1,2,3,4],.25,4)
            self.assertEqual(p.read_bytes(),before)
            self.assertFalse(raw['software_filter']['enabled'])
            self.assertTrue(filtered['software_filter']['enabled'])
            self.assertEqual(filtered['input_sha256'],hashlib.sha256(before).hexdigest())
            with self.assertRaises(ValueError):extract(p,0,[1,2,3,4],.25,100)


if __name__ == '__main__':
    unittest.main()
