"""P5a 有界分块物理前端测试。

覆盖四类：

1. 与参考实现（``trace_port.extract`` + ``tpiu_demux.demux``）的逐字节/逐字段等价，
   分块尺寸覆盖 1 字节、奇数、4096、整段，以及跨 FSYNC/HSYNC/帧/边沿/毛刺边界的构造；
2. 跨块状态：待定滤波边沿、半周期/整周期、nibble 进位、partial frame、current trace id、
   未知源/尾部残片在块边界不丢；
3. 明确失败：相位不唯一（两个候选都有效或都无效）、损坏/截断 KVDAT、非法分块、生命周期；
4. 会话复位（reset）不继承上一次会话的任何残片。
"""
import csv
import hashlib
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import trace_port
from tpiu_demux import FSYNC, HSYNC, demux
from trace_frontend import (Frontend, FrontendError, GlitchFilter, IntervalHistogram,
                            PhaseSelectionError, TpuFraming, select_phase)

RATE = 500_000_000
HALF_TICKS = 125          # 半周期 250 ns → 2 MHz 时钟，便于构造小样本
DATA_DELAY_TICKS = 7      # 数据边沿相对时钟边沿延后 14 ns
TRIGGER = 1000
TRACEID = 2
CONFIG = dict(clock=0, data=[1, 2, 3, 4], fraction=0.25, traceid=TRACEID)


# ── 合成波形 ─────────────────────────────────────────────────────────────────


def trace_frame(payload_bytes):
    """构造一个帧：槽 0 切换 trace id 到 2，其余槽输出 ``payload_bytes``。"""
    frame = bytearray(16)
    frame[0] = (TRACEID << 1) | 1
    flags = 0
    offset = 0
    for position in range(2, 15, 2):
        value = payload_bytes[offset] if offset < len(payload_bytes) else 0x10
        offset += 1
        frame[position] = value & 0xFE          # 偶数 = 数据字节
        if position < 14:
            frame[position + 1] = (value ^ 0x5A) & 0xFE
    frame[15] = flags
    return bytes(frame)


def build_stream():
    """一条能唯一确定相位 0 的 TPIU 字节流：FSYNC/HSYNC/两帧/尾部半截 FSYNC。"""
    return (FSYNC + HSYNC + trace_frame(b'ETM-P5A') + HSYNC + trace_frame(b'0123456789')
            + FSYNC + trace_frame(b'tail') + FSYNC[:2])


def phase_isolation_stream(phase=0):
    # An accidental FSYNC in the wrong byte phase occurs before the genuine
    # sync in the selected phase. The wrong phase then hits a malformed frame.
    raw = bytes([0xf0, 0xff, 0xff, 0xff, 0x07]) + bytes(8)
    raw += FSYNC + trace_frame(b'isolation') + FSYNC + trace_frame(b'tail')
    if phase:
        nibbles = [0] + [n for byte in raw for n in (byte & 15, byte >> 4)] + [0]
        raw = bytes(nibbles[i] | (nibbles[i+1] << 4) for i in range(0,len(nibbles),2))
    return raw


def build_tracks(payload, initial_clock=0, data_glitches=(), half_ticks=HALF_TICKS):
    """把字节流摊成 DDR nibble，再生成各通道的“电平跳变”列表（采样位置 = tick）。

    返回 ``(tracks, initial)``；``tracks[ch]`` 是严格递增的 ``[(position, level)]``，
    电平从 ``initial[ch]`` 开始交替，与 KVDAT 边沿记录/CSV 跳变行的语义一致。
    """
    nibbles = []
    for byte in payload:
        nibbles.extend((byte & 15, byte >> 4))
    tracks = {channel: [] for channel in range(5)}
    initial_levels = initial_levels_for(initial_clock)
    levels = dict(initial_levels)
    clock = initial_clock
    for index in range(len(nibbles) + 1):
        position = index * half_ticks
        clock ^= 1
        tracks[0].append((position, clock))
        if index < len(nibbles):
            for bit in range(4):
                level = (nibbles[index] >> bit) & 1
                channel = bit + 1
                if level != levels[channel]:
                    tracks[channel].append((position + DATA_DELAY_TICKS, level))
                    levels[channel] = level
    for channel, position in data_glitches:
        # 在某数据通道上插入一对 4 ns 的窄脉冲：电平先在 position 翻转、再到 position+2
        # 翻回（positions 相差 2 tick = 4 ns），glitch_ns=4 时正好成对消去。
        track = sorted(tracks[channel])
        level = initial_levels[channel]
        cut = 0
        for index, (where, value) in enumerate(track):
            if where <= position:
                level = value
                cut = index + 1
            else:
                break
        if cut and track[cut - 1][0] == position:
            raise AssertionError('毛刺位置与既有跳变冲突，请换一个位置')
        track[cut:cut] = [(position, level ^ 1), (position + 2, level)]
        tracks[channel] = track
    for track in tracks.values():
        track.sort()
    return tracks, dict(initial_levels)


def initial_levels_for(initial_clock):
    return {0: initial_clock, 1: 1, 2: 1, 3: 1, 4: 1}


def write_csv(path, tracks, initial, trigger=TRIGGER):
    """把跳变列表写成 Kingst 风格跳变 CSV（时间 = (position - trigger) / rate）。"""
    events = {}
    for channel, track in tracks.items():
        for position, level in track:
            events.setdefault(position, {})[channel] = level
    levels = dict(initial)
    with path.open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.writer(stream)
        writer.writerow(['Time [s]', 'Channel 0', 'Channel 1', 'Channel 2',
                         'Channel 3', 'Channel 4'])
        writer.writerow([repr((0 - trigger) / RATE)] + [levels[ch] for ch in range(5)])
        for position in sorted(events):
            levels.update(events[position])
            writer.writerow([repr((position - trigger) / RATE)]
                            + [levels[ch] for ch in range(5)])
    return path


KVDAT_SETTINGS = ('<?xml version="1.0" encoding="utf-8"?>\n'
                  '<settings><global><version>3.6.2</version></global></settings>')


def write_kvdat(path, tracks, initial, trigger=TRIGGER, depth=None, half_ticks=None):
    """写一个布局合规的 KVDAT（3.6.x 设置头 + kvdat 标记 + 文件头 + 通道块 + 范围哨兵）。"""
    depth = depth if depth is not None else max(
        [position for track in tracks.values() for position, _ in track] + [0]) + 100
    blob = bytearray(KVDAT_SETTINGS.encode('utf-8'))
    blob += b'kvdat\0\0\0'
    blob += struct.pack('<4Q', depth, RATE, trigger, len(tracks))
    for channel in sorted(tracks):
        edges = tracks[channel]
        blob += struct.pack('<IBBHQ', 0x442323, channel, initial[channel], 0, len(edges) + 1)
        for position, _level in edges:
            blob += struct.pack('<IB', position, 0)
        blob += struct.pack('<IB', depth, 0)      # 范围哨兵：position == depth、flag == 0
    path.write_bytes(bytes(blob))
    return path


def reference(path, glitch_ns=0.0, traceid=TRACEID, fraction=0.25):
    """参考链路：trace_port.extract + tpiu_demux.demux + decode_capture_stream 的相位判定。"""
    physical, candidates = trace_port.extract(path, 0, [1, 2, 3, 4], fraction, glitch_ns)
    valid, failures = [], []
    for phase, raw in enumerate(candidates):
        try:
            framing, streams = demux(raw)
        except ValueError as error:
            failures.append({'phase': phase, 'error': str(error)})
            continue
        if traceid in streams and framing['frames']:
            valid.append((phase, framing, streams))
    return dict(physical=physical, candidates=candidates, valid=valid, failures=failures)


def record_dir():
    return Path(__file__).resolve().parents[2] / 'records'


# ── 统计与滤波等价 ───────────────────────────────────────────────────────────


class HistogramTests(unittest.TestCase):
    def test_median_matches_numpy_for_odd_and_even_counts(self):
        rng = np.random.default_rng(20261020)
        for count in (1, 2, 3, 4, 5, 17, 1000, 4097):
            values = np.round(rng.normal(0.0, 1e-8, count), 12)
            histogram = IntervalHistogram()
            histogram.add(values)
            self.assertEqual(histogram.median(), float(np.median(values)), f'count={count}')

    def test_median_with_duplicates_and_single_value(self):
        values = np.asarray([2.5e-9] * 7 + [3.5e-9] * 4, dtype=np.float64)
        histogram = IntervalHistogram()
        histogram.add(values)
        self.assertEqual(histogram.median(), float(np.median(values)))
        single = IntervalHistogram()
        single.add(np.asarray([1.25e-8, 1.25e-8], dtype=np.float64))
        self.assertEqual(single.median(), float(np.median(np.asarray([1.25e-8, 1.25e-8]))))

    def test_distinct_limit_fails_explicitly(self):
        histogram = IntervalHistogram(max_distinct=3)
        with self.assertRaises(FrontendError):
            histogram.add(np.arange(8, dtype=np.float64))


class GlitchFilterTests(unittest.TestCase):
    def test_matches_reference_filter_pulses_across_chunk_splits(self):
        rng = np.random.default_rng(4242)
        for trial in range(20):
            positions = np.cumsum(rng.integers(1, 6, 120)).astype(np.float64) * 2e-9
            reference_edges = trace_port.filter_pulses(
                [(float(time), index & 1) for index, time in enumerate(positions)], 4.0)
            expected = [time for time, _ in reference_edges]
            for chunk in (1, 2, 3, 5, 7, 64):
                glitch = GlitchFilter(4.0)
                produced = []
                for start in range(0, len(positions), chunk):
                    batch = positions[start:start + chunk]
                    survivors, _ = glitch.push_batch(batch)
                    produced.extend(survivors.tolist())
                edge = glitch.flush()
                if edge is not None:
                    produced.append(edge[0])
                self.assertEqual(produced, expected,
                                 f'trial={trial} chunk={chunk}')
                self.assertEqual(glitch.removed_count(),
                                 len(positions) - len(expected),
                                 f'trial={trial} chunk={chunk} 移除计数')

    def test_disabled_filter_keeps_every_edge(self):
        glitch = GlitchFilter(0)
        times = np.asarray([1e-9, 1e-9, 2e-9], dtype=np.float64)
        survivors, _ = glitch.push_batch(times)
        self.assertEqual(survivors.tolist(), times.tolist())
        self.assertEqual(glitch.removed_count(), 0)
        self.assertIsNone(glitch.flush())


# ── TPIU 解复用分块等价 ─────────────────────────────────────────────────────


def crafted_streams():
    """构造覆盖边界情况的原始字节流：初始跳过、FSYNC/HSYNC、unknown 源、id 切换、尾部残片。"""
    frame_unknown = bytearray(16)
    frame_unknown[15] = 0
    frame_unknown[0] = 0x20                  # 偶数 → 用 current id（初始为 None → unknown）
    frame_unknown[2] = 0x30
    frame_switch = bytearray(16)
    frame_switch[0] = (3 << 1) | 1           # id 切到 3
    frame_switch[2] = (5 << 1) | 1           # id 再切到 5
    frame_switch[4] = 0x40
    frame_switch[15] = 0b00000001            # 槽 0 的 flag：id 切换后输出旧 id
    return {
        'skip-then-frames': b'\x11\x22\x33' + FSYNC + bytes(frame_switch) + FSYNC
                            + bytes(frame_unknown) + HSYNC + b'\x00\x00',
        'trailing-sync-1': FSYNC + bytes(frame_unknown) + FSYNC[:1],
        'trailing-sync-3': FSYNC + bytes(frame_unknown) + FSYNC[:3],
        'trailing-frame': FSYNC + bytes(frame_unknown) + b'\x0a\x0b\x0c\x0d',
        'trailing-raw-odd': FSYNC + bytes(frame_unknown) + b'\x0e',
        'only-syncs': FSYNC * 4 + HSYNC * 3,
        'no-sync': b'\x11' * 17,
        'empty': b'',
    }


class DemuxChunkTests(unittest.TestCase):
    def test_crafted_streams_match_reference_for_every_chunk(self):
        for name, raw in crafted_streams().items():
            try:
                expected = demux(raw)
            except ValueError:
                expected = None
            for chunk in (1, 2, 3, 5, 7, 16, 64, len(raw) or 1):
                framing = TpuFraming()
                for start in range(0, len(raw), chunk):
                    framing.feed(raw[start:start + chunk])
                try:
                    stats, streams = framing.finish()
                except FrontendError as error:
                    self.assertIsNone(expected, f'{name} chunk={chunk} 应成功却报错 {error}')
                    self.assertEqual(str(error), '没有 FSYNC，无法确认 TPIU 帧起点。')
                    continue
                self.assertIsNotNone(expected, f'{name} chunk={chunk} 应失败却成功')
                self.assertEqual(stats, expected[0], f'{name} chunk={chunk} 统计')
                self.assertEqual(streams, expected[1], f'{name} chunk={chunk} 流')

    def test_mid_frame_fsync_rejected_identically(self):
        raw = FSYNC + b'\x02\x00' + FSYNC + b'\x02\x00'
        with self.assertRaises(ValueError) as expected:
            demux(raw)
        framing = TpuFraming()
        with self.assertRaises(FrontendError) as actual:
            for index in range(len(raw)):
                framing.feed(raw[index:index + 1])
            framing.finish()
        self.assertEqual(str(actual.exception), str(expected.exception))

    def test_record_tpiu_stream_chunk_equivalence(self):
        path = record_dir() / 'capture-4395612c313f47ce97af1e1c17e0a0d5' / 'tpiu.bin'
        if not path.exists():
            self.skipTest('缺少 10 ms 记录的 tpiu.bin')
        raw = path.read_bytes()
        expected = demux(raw)
        for chunk in (1, 7, 4096, len(raw)):
            framing = TpuFraming()
            for start in range(0, len(raw), chunk):
                framing.feed(raw[start:start + chunk])
            stats, streams = framing.finish()
            self.assertEqual(stats, expected[0], f'chunk={chunk} 统计')
            self.assertEqual(streams, expected[1], f'chunk={chunk} 流')


class PhaseSelectionTests(unittest.TestCase):
    def test_bad_frame_diagnostic_uses_absolute_offset_after_sync_search(self):
        for prefix_length in (1, 2, 13, 4097):
            raw = bytes(prefix_length) + FSYNC + b'\x00\x00\xff\xff'
            with self.assertRaises(ValueError) as expected:
                demux(raw)
            self.assertIn('物理偏移 %d' % (prefix_length + 6), str(expected.exception))
            for chunk in (1, 13, 4096):
                framing = TpuFraming()
                with self.assertRaises(FrontendError) as actual:
                    for start in range(0, len(raw), chunk):
                        framing.feed(raw[start:start+chunk])
                    framing.finish()
                self.assertEqual(str(actual.exception), str(expected.exception))

    def test_malformed_other_phase_does_not_reject_a_valid_phase(self):
        with tempfile.TemporaryDirectory() as directory:
            for phase in (0, 1):
                tracks, initial = build_tracks(phase_isolation_stream(phase))
                path = write_kvdat(Path(directory)/('phase-%d.kvdat'%phase), tracks, initial)
                expected = reference(path)
                self.assertEqual([row[0] for row in expected['valid']], [phase])
                self.assertEqual(len(expected['failures']), 1)
                for chunk in (1, 13, 4096):
                    frontend = Frontend(path, CONFIG, chunk_bytes=chunk)
                    try:
                        actual = frontend.run()
                        self.assertEqual(actual['phase'], phase)
                        self.assertEqual(actual['tpiu'], expected['candidates'][phase])
                        self.assertEqual(actual['streams'], expected['valid'][0][2])
                    finally:
                        frontend.close()

    def test_single_valid_candidate_selected(self):
        raw = build_stream()
        demuxers = [TpuFraming(), TpuFraming()]
        demuxers[0].feed(raw)
        demuxers[1].feed(b'\x11' * 64)
        phase, framing, streams, failures = select_phase(demuxers, TRACEID)
        self.assertEqual(failures, [{'phase': 1, 'error': '没有 FSYNC，无法确认 TPIU 帧起点。'}])
        self.assertEqual(phase, 0)
        self.assertEqual(framing['phase'], 0)
        self.assertIn(TRACEID, streams)

    def test_both_candidates_valid_is_rejected(self):
        raw = build_stream()
        demuxers = [TpuFraming(), TpuFraming()]
        for demuxer in demuxers:
            demuxer.feed(raw)
        with self.assertRaises(PhaseSelectionError) as caught:
            select_phase(demuxers, TRACEID)
        self.assertEqual(caught.exception.valid_count, 2)
        self.assertIn('不能唯一确定 TPIU 字节相位：2 个候选', str(caught.exception))

    def test_no_valid_candidate_is_rejected(self):
        demuxers = [TpuFraming(), TpuFraming()]
        for demuxer in demuxers:
            demuxer.feed(b'\x11' * 128)
        with self.assertRaises(PhaseSelectionError) as caught:
            select_phase(demuxers, TRACEID)
        self.assertEqual(caught.exception.valid_count, 0)
        self.assertEqual([item['phase'] for item in caught.exception.failures], [0, 1])
        self.assertIn('没有 FSYNC', str(caught.exception))


# ── 合成输入的完整链路等价 ───────────────────────────────────────────────────


class SyntheticEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def _build(self, glitches=(), payload=None):
        tracks, initial = build_tracks(payload or build_stream(), data_glitches=glitches)
        kvdat = write_kvdat(self.root / 'capture.kvdat', tracks, initial)
        csv_path = write_csv(self.root / 'capture.csv', tracks, initial)
        return tracks, initial, kvdat, csv_path

    def test_chunk_sizes_are_equivalent_to_reference(self):
        _tracks, _initial, kvdat, csv_path = self._build()
        for path in (kvdat, csv_path):
            expected = reference(path)
            self.assertEqual(len(expected['valid']), 1, '参考实现应得到唯一相位')
            phase, framing, streams = expected['valid'][0]
            total = path.stat().st_size
            for chunk in (1, 2, 3, 5, 7, 13, 4096, total):
                frontend = Frontend(path, CONFIG, chunk_bytes=chunk)
                result = frontend.run()
                self.assertEqual(result['phase'], phase, f'{path.name} chunk={chunk} 相位')
                self.assertEqual(frontend.candidate_bytes(0), expected['candidates'][0],
                                 f'{path.name} chunk={chunk} phase0 字节')
                self.assertEqual(frontend.candidate_bytes(1), expected['candidates'][1],
                                 f'{path.name} chunk={chunk} phase1 字节')
                self.assertEqual(result['streams'], streams, f'{path.name} chunk={chunk} ETM 流')
                self.assertEqual(result['framing'], {**framing, 'phase': phase},
                                 f'{path.name} chunk={chunk} framing')
                self.assertEqual(result['physical'], expected['physical'],
                                 f'{path.name} chunk={chunk} physical')
                self.assertEqual(frontend.demux_errors(), expected['failures'])

    def test_kvdat_and_csv_reports_share_the_same_signal_fields(self):
        _tracks, _initial, kvdat, csv_path = self._build()
        kvdat_report = Frontend(kvdat, CONFIG, chunk_bytes=7).run()['physical']
        csv_report = Frontend(csv_path, CONFIG, chunk_bytes=7).run()['physical']
        for field in ('clock_edges', 'clock_hz_median', 'clock_hz_average', 'half_period_ns',
                      'irregular_half_periods', 'clock_runs', 'clock_gaps', 'data_transitions',
                      'nibbles', 'min_data_transition_distance_ns', 'candidates',
                      'software_filter'):
            self.assertEqual(kvdat_report[field], csv_report[field], field)
        self.assertEqual(kvdat_report['input_format'], 'Kingst KVDAT')
        self.assertEqual(kvdat_report['sample_rate'], RATE)
        self.assertNotIn('input_format', csv_report)

    def test_glitch_pairs_removed_like_reference(self):
        # 两对 4 ns 窄脉冲（tick 差 2 = 4 ns），glitch_ns=4 时应被成对消去
        glitches = ((4, 20 * HALF_TICKS + 60), (2, 40 * HALF_TICKS + 60))
        _tracks, _initial, kvdat, csv_path = self._build(glitches=glitches)
        for path in (kvdat, csv_path):
            expected = reference(path, glitch_ns=4.0)
            clean = reference(path, glitch_ns=0.0)
            self.assertEqual(len(expected['valid']), 1, 'glitch 后仍应唯一相位')
            filtered = dict(CONFIG, glitch_ns=4.0)
            for chunk in (1, 3, 7, 4096):
                frontend = Frontend(path, filtered, chunk_bytes=chunk)
                result = frontend.run()
                self.assertEqual(result['physical'], expected['physical'],
                                 f'{path.name} chunk={chunk} physical')
                self.assertEqual(result['framing'],
                                 {**expected['valid'][0][1], 'phase': expected['valid'][0][0]},
                                 f'{path.name} chunk={chunk} framing')
            removed = expected['physical']['software_filter']['removed_transitions']
            self.assertEqual(sum(removed.values()), 4, '两对窄脉冲应正好移除 4 条边沿')
            self.assertEqual(expected['physical']['clock_edges'],
                             clean['physical']['clock_edges'])
            for channel, count in clean['physical']['data_transitions'].items():
                expected_count = count - (2 if channel in ('CH2', 'CH4') else 0)
                self.assertEqual(expected['physical']['data_transitions'][channel],
                                 expected_count, f'{channel} 滤波后跳变数')
            # 消去成对窄脉冲后，取样字节应与完全干净的输入一致
            filtered_result = Frontend(path, dict(CONFIG, glitch_ns=4.0), chunk_bytes=7).run()
            for phase in (0, 1):
                self.assertEqual(filtered_result['physical']['candidates'][phase],
                                 clean['physical']['candidates'][phase])

    def test_glitch_threshold_too_large_is_rejected(self):
        # 半周期 120 ns 时 100 ns 阈值会吃掉正常跳变，参考实现同样明确拒绝
        tracks, initial = build_tracks(build_stream(), half_ticks=60)
        path = write_kvdat(self.root / 'short.kvdat', tracks, initial, half_ticks=60)
        with self.assertRaises(ValueError) as expected:
            reference(path, glitch_ns=100.0)
        frontend = Frontend(path, dict(CONFIG, glitch_ns=100.0), chunk_bytes=4096)
        with self.assertRaises(FrontendError) as caught:
            frontend.run()
        self.assertEqual(str(caught.exception), str(expected.exception))
        self.assertIn('软件滤波阈值须小于典型时钟半周期的一半', str(caught.exception))

    def test_no_sync_stream_fails_with_reference_message(self):
        _tracks, _initial, kvdat, _csv = self._build(payload=b'\x11' * 24)
        expected = reference(kvdat)
        self.assertEqual(expected['valid'], [])
        frontend = Frontend(kvdat, dict(CONFIG), chunk_bytes=4096)
        with self.assertRaises(FrontendError) as caught:
            frontend.run()
        message = str(caught.exception)
        self.assertIn('不能唯一确定 TPIU 字节相位：0 个候选', message)
        self.assertIn(expected['failures'][-1]['error'], message)
        self.assertEqual(frontend.demux_errors(), expected['failures'])

    def test_state_carries_pending_edges_and_nibble_carry(self):
        _tracks, _initial, kvdat, _csv = self._build()
        frontend = Frontend(kvdat, dict(CONFIG, glitch_ns=4.0), chunk_bytes=1,
                            accumulate_edges=2, min_batch_edges=2, batch_edges=8,
                            parse_min_bytes=5)
        seen_pending = False
        seen_carry = False
        seen_partial_frame = False
        seen_confirm = False
        try:
            for _ in range(4000):
                if not frontend.feed():
                    break
                state = frontend.state()
                if any(item['pending_edge'] is not None for item in state['data'].values()):
                    seen_pending = True
                if state['nibbles']['carry']:
                    seen_carry = True
                if any(item['frame_bytes'] for item in state['tpiu']):
                    seen_partial_frame = True
                if state['clock']['confirmed_buffered']:
                    seen_confirm = True
            self.assertTrue(seen_pending, '数据通道的待定滤波边沿应跨块保留')
            self.assertTrue(seen_carry, '未成字节的 nibble 应跨块保留')
            self.assertTrue(seen_partial_frame, 'TPIU partial frame 应跨块保留')
            self.assertTrue(seen_confirm, '已确认时钟边沿缓冲应跨块保留')
            result = frontend.finish()
            expected = reference(kvdat, glitch_ns=4.0)
            self.assertEqual(result['physical'], expected['physical'])
            self.assertEqual(result['framing'],
                             {**expected['valid'][0][1], 'phase': expected['valid'][0][0]})
        finally:
            frontend.close()

    def test_reset_restarts_without_inherited_state(self):
        _tracks, _initial, kvdat, _csv = self._build()
        frontend = Frontend(kvdat, dict(CONFIG), chunk_bytes=3)
        first = frontend.run()
        frontend.reset()
        self.assertEqual(frontend.state()['nibbles']['carry'], [])
        self.assertIsNone(frontend.state()['clock']['pending_edge'])
        second = frontend.run()
        self.assertEqual(first['physical'], second['physical'])
        self.assertEqual(frontend.tpiu(), second['tpiu'])
        self.assertEqual(frontend.state()['source']['bytes_read'],
                         second['counters']['bytes_read'])


# ── 拒绝语义：损坏输入、非法分块、生命周期 ───────────────────────────────────


class RejectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        tracks, initial = build_tracks(build_stream())
        self.kvdat = write_kvdat(self.root / 'capture.kvdat', tracks, initial)
        self.csv = write_csv(self.root / 'capture.csv', tracks, initial)

    def tearDown(self):
        self.directory.cleanup()

    def test_invalid_chunk_sizes_rejected(self):
        for chunk in (0, -1, 1.5, '7', [], [0], [7, -2], True):
            with self.assertRaises(FrontendError, msg=repr(chunk)):
                Frontend(self.kvdat, CONFIG, chunk_bytes=chunk)

    def test_chunk_plan_and_feed_override(self):
        frontend = Frontend(self.kvdat, CONFIG, chunk_bytes=[1, 3, 7, 4096])
        try:
            frontend.feed()
            frontend.feed(5)
            with self.assertRaises(FrontendError):
                frontend.feed(0)
            result = frontend.finish()
        finally:
            frontend.close()
        expected = reference(self.kvdat)
        self.assertEqual(result['physical'], expected['physical'])

    def test_lifecycle_errors(self):
        frontend = Frontend(self.kvdat, CONFIG, chunk_bytes=4096)
        with self.assertRaises(FrontendError):
            frontend.physical()
        frontend.run()
        with self.assertRaises(FrontendError):
            frontend.feed()
        with self.assertRaises(FrontendError):
            frontend.finish()

    def test_truncated_and_corrupt_kvdat_rejected(self):
        data = self.kvdat.read_bytes()
        cases = {
            '尾部截断': data[:-7],
            '多出未解析尾部': data + b'\x00' * 3,
            '设置头损坏': data.replace(b'</settings>', b'</settingz>'),
            '版本不支持': data.replace(b'3.6.2', b'3.7.2'),
            '结束标记无效': data[:-5] + struct.pack('<IB', 12345, 0),
        }
        for name, blob in cases.items():
            path = self.root / f'bad-{abs(hash(name))}.kvdat'
            path.write_bytes(blob)
            with self.assertRaises((FrontendError, ValueError), msg=name):
                Frontend(path, CONFIG, chunk_bytes=4096).run()
            if name == '版本不支持':
                with self.assertRaises(ValueError) as caught:
                    Frontend(path, CONFIG)
                self.assertIn('尚不支持此 KVDAT 版本', str(caught.exception))

    def test_csv_dialect_rejections(self):
        header = 'Time [s],Channel 0,Channel 1,Channel 2,Channel 3,Channel 4'
        cases = {
            '电平非 0/1': [header, '-2e-06,0,1,1,1,1', '-1e-06,1,2,1,1,1'],
            '时间倒序': [header, '-2e-06,0,1,1,1,1', '-3e-06,1,1,1,1,1'],
            '通道数不一致': [header, '-2e-06,0,1,1,1,1', '-1e-06,1,1,1,1'],
            '表头重复通道': [header.replace('Channel 1', 'Channel 0'), '-2e-06,0,1,1,1,1'],
            '表头无法识别': ['Time [s],A,B,C,D,E', '-2e-06,0,1,1,1,1'],
            '时间列非法': [header, 'nonsense,0,1,1,1,1'],
            '空文件': [],
            '只有表头': [header],
        }
        for name, content in cases.items():
            path = self.root / f'bad-{abs(hash(name))}.csv'
            path.write_text('\n'.join(content) + '\n', encoding='utf-8-sig')
            with self.assertRaises((FrontendError, ValueError), msg=name):
                Frontend(path, CONFIG, chunk_bytes=4096).run()
        # 参考实现对这两类同样明确拒绝，错误语义一致
        for name in ('电平非 0/1', '时间倒序'):
            path = self.root / f'ref-{abs(hash(name))}.csv'
            path.write_text('\n'.join(cases[name]) + '\n', encoding='utf-8-sig')
            with self.assertRaises(ValueError, msg=name + ' (参考实现)'):
                reference(path)


# ── 真实 10 ms 记录（存在才跑） ─────────────────────────────────────────────


class RecordEquivalenceTests(unittest.TestCase):
    RECORD = record_dir() / 'capture-4395612c313f47ce97af1e1c17e0a0d5'

    def setUp(self):
        if not (self.RECORD / 'capture.kvdat').exists():
            self.skipTest('缺少 10 ms USB 记录')

    def test_10ms_record_matches_reference_and_recorded_artifacts(self):
        expected = json.loads((self.RECORD / 'physical.json').read_text(encoding='utf-8'))
        tpiu_sha = hashlib.sha256((self.RECORD / 'tpiu.bin').read_bytes()).hexdigest()
        etm_sha = hashlib.sha256((self.RECORD / 'etm.bin').read_bytes()).hexdigest()
        framing_expected = json.loads((self.RECORD / 'framing.json').read_text(encoding='utf-8')) \
            if (self.RECORD / 'framing.json').exists() else None
        for name in ('capture.kvdat', 'capture.csv'):
            path = self.RECORD / name
            reference_result = reference(path)
            self.assertEqual(len(reference_result['valid']), 1)
            phase, framing, streams = reference_result['valid'][0]
            for chunk in (7, 4096, path.stat().st_size):
                frontend = Frontend(path, CONFIG, chunk_bytes=chunk)
                result = frontend.run()
                self.assertEqual(result['physical'], reference_result['physical'],
                                 f'{name} chunk={chunk} physical')
                self.assertEqual(result['framing'], {**framing, 'phase': phase},
                                 f'{name} chunk={chunk} framing')
                self.assertEqual(result['streams'], streams, f'{name} chunk={chunk} ETM 流')
                self.assertEqual(hashlib.sha256(result['tpiu']).hexdigest(), tpiu_sha,
                                 f'{name} chunk={chunk} tpiu.bin')
                self.assertEqual(hashlib.sha256(result['streams'][TRACEID]).hexdigest(), etm_sha,
                                 f'{name} chunk={chunk} etm.bin')
        # physical 与记录内既有报告逐字段一致（该记录由 capture.csv 产出，glitchNs=0）
        csv_result = Frontend(self.RECORD / 'capture.csv', CONFIG, chunk_bytes=1 << 20).run()
        self.assertEqual(csv_result['physical'], expected)
        if framing_expected is not None:
            self.assertEqual(csv_result['framing'], framing_expected)


if __name__ == '__main__':
    unittest.main()
