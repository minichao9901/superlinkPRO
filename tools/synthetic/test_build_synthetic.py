#!/usr/bin/env python3
"""tools/synthetic 的单元测试（拼接校验/复位标记/非法参数/来源缺失）。

运行时只依赖标准库 + numpy；KVDAT/CSV 前端等价用 ``etm/tools`` 的现前端（只读引用）。
"""
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import build_synthetic as bs          # noqa: E402
import synthetic_events as se         # noqa: E402

KVDAT_RATE = 500000000
KVDAT_DEPTH = 1000


# ---------------------------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------------------------

def kvdat_bytes(depth, rate, trigger, channels):
    """按 kingst_record.layout 的布局写一个合法 KVDAT。channels: {通道: (首电平, 真实边沿位置)}"""
    header = (b'<?xml version="1.0" encoding="utf-8"?><settings><global><version>3.6.6'
              b'</version></global></settings>\r\n')
    out = bytearray(header)
    out += b'kvdat\0\0\0'
    out += struct.pack('<4Q', depth, rate, trigger, len(channels))
    for channel in sorted(channels):
        initial, positions = channels[channel]
        rows = len(positions) + 1
        out += struct.pack('<IBBHQ', 0x442323, channel, initial, 0, rows)
        for position in positions:
            out += struct.pack('<IB', position, 0)
        out += struct.pack('<IB', depth, 0)
    return bytes(out)


def tiny_kvdat_channels(depth=KVDAT_DEPTH):
    """时钟 0 每 8 个采样一个边沿；CH1 奇数边沿、CH2 首边沿在 0、CH3 无边沿、CH4 单边沿。"""
    clock = list(range(9, depth - 4, 8))
    return {
        0: (0, clock),
        1: (1, [20, 200, 601]),          # 3 条真实边沿 → 奇数，段 1 需要补边
        2: (1, [0, 400]),                # 首边沿在 0 → 补边落在段首前 1 个采样
        3: (0, []),                      # 常数通道：只有结束哨兵
        4: (1, [300]),
    }


def write_tiny_kvdat(path, *, depth=KVDAT_DEPTH, channels=None):
    channels = channels or tiny_kvdat_channels(depth)
    Path(path).write_bytes(kvdat_bytes(depth, KVDAT_RATE, depth // 2, channels))
    return channels


def write_csv_fixture(path, *, rows=400, rate=KVDAT_RATE):
    """写一个跳变表 CSV：CH0 每 8 个采样翻转，CH1/CH3 少量跳变，CH2 周期跳变。"""
    lines = ['Time[s], CH0, CH1, CH2, CH3, CH4\n']
    levels = [0, 1, 1, 1, 1]
    for position in range(rows):
        if position % 8 == 0:
            levels[0] ^= 1
        if position == 100:
            levels[1] ^= 1
        if position % 64 == 0:
            levels[2] ^= 1
        if position == 250:
            levels[3] ^= 1
        if position % 128 == 0:
            levels[4] ^= 1
        if position == 0 or position % 8 == 0 or position in (100, 250):
            seconds = (position - 0) / rate
            lines.append(', '.join([repr(seconds)] + [str(value) for value in levels]) + '\n')
    Path(path).write_text(''.join(lines), encoding='utf-8')
    return path


def instruction(seq, offset, *, pc=None, cycles=0):
    return dict(kind='instruction', seq=seq, offset=offset,
                pc=0x08000000 + seq * 2 if pc is None else pc, size=2, bytes='00bf',
                mnemonic='nop', operands='', executed=True, function='main', file='main.c',
                line=10, flow='linear', conditional=False, executionState='executed',
                executionReason='', callPath=['main'], cycles=cycles)


def boundary(seq, offset, etype, payload):
    return dict(kind='event', seq=seq, type=etype, offset=offset,
                text='OCSD_GEN_TRC_ELEM_TEST', payload=payload, start=0, end=0, flags=0)


def make_events_record(root):
    """小记录目录：streamed schema 2（events.jsonl）+ decoded.json 摘要。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    events = [boundary(0, 0, 1, 1),
              boundary(1, 2, 2, 0)]
    for index in range(5):
        events.append(instruction(2 + index, 25 + index))
    events.append(boundary(7, 40, 3, 7))
    with (root / 'events.jsonl').open('w', encoding='utf-8') as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n')
    decoded = dict(
        version='test', protocol='ETMv4', config={'traceid': 2}, inputBytes=40,
        summary=dict(packets=0, ranges=2, instructions=5, executed=5, unknownExecution=0,
                     exceptions=0, gaps=1, functions={'main': 5}),
        quality=dict(unknownExecution=0, firstPathOffset=25, inputBytes=40),
        streamed=dict(schema=2, events=len(events), packets=0),
        sources={},
    )
    (root / 'decoded.json').write_text(json.dumps(decoded, ensure_ascii=False), encoding='utf-8')
    return root, events


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix='etm-synthetic-test-')
        self.root = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()


# ---------------------------------------------------------------------------------------------
# 物理：KVDAT
# ---------------------------------------------------------------------------------------------

class KvdatPhysicalTests(TempDirTestCase):
    def build(self, segments=3):
        source = self.root / 'capture.kvdat'
        channels = write_tiny_kvdat(source)
        src = bs.load_kvdat_source(source)
        out = self.root / 'synth' / 'capture-synthetic.kvdat'
        physical = bs.build_physical_kvdat(src, segments, out, fraction=0.25)
        return dict(source=source, channels=channels, src=src, out=out, physical=physical)

    def test_layout_and_rows(self):
        built = self.build(segments=3)
        info = bs._import_kingst_record().layout(built['out'])
        self.assertEqual(info['depth'], KVDAT_DEPTH * 3)
        self.assertEqual(info['rate'], KVDAT_RATE)
        report = bs.verify_kvdat_rows(built['out'], built['src'], 3, fraction=0.25)
        self.assertTrue(report['ok'])
        self.assertEqual(report['sentinel'], KVDAT_DEPTH * 3)
        # CH1 有 3 条真实边沿（奇数）→ 段 1 补 1 条后段 2 又变奇数 → 段 2 再补 1 条
        self.assertEqual(report['padding']['1'], {1: KVDAT_DEPTH, 2: 2 * KVDAT_DEPTH})
        # CH2 首边沿在 0 → 真实边沿 2 条（偶数）→ 不需要补边
        self.assertEqual(report['padding']['2'], {})
        # CH4 只有 1 条真实边沿 → 段 1、段 2 都要补
        self.assertEqual(report['padding']['4'], {1: KVDAT_DEPTH, 2: 2 * KVDAT_DEPTH})
        # 常数通道没有边沿，永远不补
        self.assertEqual(report['padding']['3'], {})

    def test_segments_split_byte_identical(self):
        built = self.build(segments=3)
        plans = bs.channel_padding_plans(built['src'], 3, 0.25)
        info = bs._import_kingst_record().layout(built['out'])
        source_bytes = built['source'].read_bytes()
        for index in range(3):
            path = self.root / ('segment-%d.kvdat' % index)
            meta = bs._write_kvdat_segment(built['src'], built['out'], info, index, path,
                                           padding=plans)
            self.assertEqual(meta['sha256'], built['src']['sha256'],
                             '段 %d 应与来源 KVDAT 逐字节一致' % index)
            self.assertEqual(path.read_bytes(), source_bytes)
            self.assertEqual(meta['sampleOffset'], index * KVDAT_DEPTH)

    def test_frontend_equivalence(self):
        built = self.build(segments=3)
        sampling = dict(fraction=0.25, glitchNs=0.0, clock=0, data=[1, 2, 3, 4])
        report = bs.verify_physical_segments(built['src'], built['out'], 3,
                                             self.root / 'segments', sampling=sampling,
                                             physical=built['physical'],
                                             selection=[0, 1, 2])
        self.assertTrue(report['ok'])
        self.assertEqual(report['verified'], [0, 1, 2])
        for item in report['perSegment']:
            self.assertTrue(item['byteIdenticalToSource'])
            self.assertEqual(item['phaseSha256'], report['referencePhaseSha256'])

    def test_cli_physical_and_metadata(self):
        source_dir = self.root / 'record'
        source_dir.mkdir()
        write_tiny_kvdat(source_dir / 'capture.kvdat')
        out = self.root / 'out'
        code = bs.main(['--mode', 'physical', '--record', str(source_dir), '--kind', 'kvdat',
                        '--segments', '2', '--out', str(out), '--verify', 'all', '--quiet'])
        self.assertEqual(code, 0)
        meta = json.loads((out / 'synthetic.json').read_text(encoding='utf-8'))
        self.assertTrue(meta['declaration']['synthetic'])
        self.assertFalse(meta['declaration']['continuousRealTrace'])
        self.assertTrue(meta['declaration']['notForHardwareAcceptance'])
        self.assertEqual(len(meta['physical']['segments']), 2)
        self.assertTrue(meta['physical']['segments'][1]['reset'])
        self.assertEqual(meta['physical']['segments'][1]['sampleOffset'], KVDAT_DEPTH)
        self.assertIn('sha256', meta['physical'])
        self.assertIn('capture.kvdat', meta['source']['files'])
        self.assertEqual(meta['source']['files']['capture.kvdat']['sha256'],
                         se.sha256_file(source_dir / 'capture.kvdat'))
        verify = json.loads((out / 'verify' / 'physical-verify.json').read_text(encoding='utf-8'))
        self.assertTrue(verify['ok'])
        self.assertTrue((out / 'physical' / 'segments.json').is_file())
        self.assertTrue((out / 'physical' / 'session-synthetic.json').is_file())

    def test_split_mode(self):
        source_dir = self.root / 'record'
        source_dir.mkdir()
        write_tiny_kvdat(source_dir / 'capture.kvdat')
        out = self.root / 'out'
        self.assertEqual(bs.main(['--mode', 'physical', '--record', str(source_dir),
                                  '--segments', '3', '--out', str(out), '--verify', 'none',
                                  '--quiet']), 0)
        split_dir = self.root / 'split'
        code = bs.main(['--mode', 'split', '--meta', str(out / 'synthetic.json'),
                        '--out', str(split_dir)])
        self.assertEqual(code, 0)
        files = sorted(split_dir.glob('segment-*.kvdat'))
        self.assertEqual(len(files), 3)
        self.assertEqual(files[0].read_bytes(), (source_dir / 'capture.kvdat').read_bytes())


class PaddingPlanTests(TempDirTestCase):
    def test_padding_collision_rejected(self):
        channel = dict(initial=1, positions=bs._import_numpy().array([0], dtype='<u4'))
        with self.assertRaises(bs.BuildError):
            bs.padding_plan(channel, 10, 3, max_sample_position=9.0)

    def test_no_padding_for_even_edges(self):
        numpy = bs._import_numpy()
        channel = dict(initial=1, positions=numpy.array([5, 6], dtype='<u4'))
        self.assertEqual(bs.padding_plan(channel, 100, 4, max_sample_position=10.0), {})

    def test_padding_every_other_segment(self):
        numpy = bs._import_numpy()
        channel = dict(initial=1, positions=numpy.array([5], dtype='<u4'))
        plan = bs.padding_plan(channel, 100, 5, max_sample_position=10.0)
        # 单条真实边沿：每段都会把累计奇偶翻成奇数，所以除段 0 外每段都要补 1 条
        self.assertEqual(plan, {1: 100, 2: 200, 3: 300, 4: 400})


# ---------------------------------------------------------------------------------------------
# 物理：CSV
# ---------------------------------------------------------------------------------------------

class CsvPhysicalTests(TempDirTestCase):
    def make_record(self, *, rows=400):
        record = self.root / 'record'
        record.mkdir()
        write_csv_fixture(record / 'capture.csv', rows=rows)
        (record / 'session.json').write_text(json.dumps(
            dict(rate=KVDAT_RATE, seconds=rows / KVDAT_RATE, fraction=0.25, glitchNs=0.0,
                 clock=0, data=[1, 2, 3, 4])), encoding='utf-8')
        return record

    def test_csv_build_and_verify(self):
        record = self.make_record()
        out = self.root / 'out'
        code = bs.main(['--mode', 'physical', '--kind', 'csv', '--record', str(record),
                        '--segments', '3', '--out', str(out), '--verify', 'all', '--quiet'])
        self.assertEqual(code, 0)
        meta = json.loads((out / 'synthetic.json').read_text(encoding='utf-8'))
        self.assertEqual(meta['physical']['format'], 'csv')
        self.assertEqual(len(meta['physical']['segments']), 3)
        self.assertEqual(meta['physical']['layout']['depth'], 400 * 3)
        verify = json.loads((out / 'verify' / 'physical-verify.json').read_text(encoding='utf-8'))
        self.assertTrue(verify['ok'])
        self.assertEqual(verify['sampling']['verified'], [0, 1, 2])

    def test_csv_off_grid_rejected(self):
        record = self.make_record()
        path = record / 'capture.csv'
        lines = path.read_text(encoding='utf-8').splitlines()
        parts = lines[1].split(',')
        parts[0] = repr((1 + 0.4) / KVDAT_RATE)      # 故意偏离采样网格 0.4 个采样（> 0.25 容差）
        lines[1] = ', '.join(parts)
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        self.assertEqual(bs.main(['--mode', 'physical', '--kind', 'csv', '--record', str(record),
                                  '--segments', '2', '--out', str(self.root / 'out2'),
                                  '--verify', 'none', '--quiet']), 1)


# ---------------------------------------------------------------------------------------------
# 事件流
# ---------------------------------------------------------------------------------------------

class EventStreamTests(TempDirTestCase):
    def build_unit(self, segments=3):
        record, events = make_events_record(self.root / 'record')
        unit = se.build_unit_spool(record, self.root / 'unit' / 'unit.events.jsonl')
        return unit, events

    def test_spool_canonical(self):
        unit, events = self.build_unit()
        self.assertEqual(unit['events'], len(events))
        self.assertEqual(unit['instructions'], 5)
        self.assertEqual(unit['boundaryEvents'], 3)
        self.assertEqual(unit['eventTypes'], {'1': 1, '2': 1, '3': 1})
        self.assertEqual(unit['firstInstructionSeq'], 2)
        self.assertEqual(unit['firstInstructionOffset'], 25)
        # 字段顺序固定：kind/seq/offset 在最前
        first = (self.root / 'unit' / 'unit.events.jsonl').read_bytes().splitlines()[0]
        self.assertTrue(first.startswith(b'{"kind":"event","seq":0,"type":1,"offset":0,'))

    def test_stream_remap_and_markers(self):
        unit, _events = self.build_unit()
        stats = se.stream_stats(unit, 3)
        self.assertEqual(stats['totalEvents'], 3 * 8 + 2)
        self.assertEqual(stats['totalInstructions'], 15)
        self.assertEqual(stats['resetCount'], 2)
        lines = list(se.iter_event_lines(unit, 3))
        self.assertEqual(len(lines), stats['totalEvents'])
        parsed = [json.loads(line) for line in lines]
        sequences = [item['seq'] for item in parsed]
        self.assertEqual(sequences, list(range(stats['totalEvents'])))
        markers = [item for item in parsed if item['kind'] == 'event'
                   and item['text'].startswith(se.MARKER_PREFIX)]
        self.assertEqual(len(markers), 2)
        self.assertEqual([item['type'] for item in markers], [2, 2])
        self.assertEqual([item['payload'] for item in markers], [0, 0])
        self.assertEqual([item['offset'] for item in markers], [unit['offsetSpan'],
                                                                2 * unit['offsetSpan']])
        # 段 1 的第一条指令 offset = 单元 offset + 段偏移
        instructions = [item for item in parsed if item['kind'] == 'instruction']
        self.assertEqual(instructions[5]['offset'], 25 + unit['offsetSpan'])
        self.assertEqual(instructions[5]['seq'], 8 + 1 + 2)

    def test_write_and_verify_stream(self):
        unit, _events = self.build_unit()
        out = self.root / 'events' / 'events.jsonl'
        written = se.write_event_stream(unit, 3, out)
        report = se.verify_event_stream(out, unit, 3, sample_every=1)
        self.assertTrue(report['ok'])
        self.assertEqual(report['totalEvents'], written['lines'])
        self.assertEqual(report['sha256'], written['sha256'])
        self.assertEqual(report['resetMarkers'], 2)
        # 抽样比对只覆盖 spool 事件（标记单独逐字段校验），每段 8 条
        self.assertEqual(report['sampledJsonComparisons'], 3 * 8)

    def test_verify_detects_tampering(self):
        unit, _events = self.build_unit()
        out = self.root / 'events' / 'events.jsonl'
        se.write_event_stream(unit, 2, out)
        data = out.read_bytes().replace(b'"offset":25', b'"offset":26', 1)
        out.write_bytes(data)
        with self.assertRaises(se.SyntheticError):
            se.verify_event_stream(out, unit, 2, sample_every=1)

    def test_markers_skip(self):
        unit, _events = self.build_unit()
        stats = se.stream_stats(unit, 3, markers='skip')
        self.assertEqual(stats['resetCount'], 0)
        self.assertEqual(stats['totalEvents'], 24)
        lines = list(se.iter_event_lines(unit, 3, markers='skip'))
        self.assertEqual(len(lines), 24)

    def test_shards(self):
        unit, _events = self.build_unit()
        index = se.write_event_shards(unit, 3, self.root / 'shards', shard_events=10)
        self.assertEqual(index['totalLines'], 3 * 8 + 2)
        self.assertGreater(len(index['shards']), 1)
        report = se.verify_event_stream(index, unit, 3, sample_every=1)
        self.assertTrue(report['ok'])
        self.assertEqual(report['sha256'], index['sha256'])

    def test_iter_event_dicts_native_markers(self):
        unit, _events = self.build_unit()
        items = list(se.iter_event_dicts(unit, 2, marker_style='native'))
        markers = [item for item in items if item['kind'] == 'session-reset']
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]['segmentIndex'], 1)
        self.assertEqual(markers[0]['seq'], unit['events'])

    def test_build_synthetic_decoded_scaling(self):
        unit, _events = self.build_unit()
        decoded = se.build_synthetic_decoded(unit, 3)
        self.assertEqual(decoded['streamed'], {'schema': 2, 'events': 3 * 8 + 2, 'packets': 0})
        self.assertEqual(decoded['summary']['instructions'], 15)
        self.assertEqual(decoded['summary']['executed'], 15)
        self.assertEqual(decoded['summary']['gaps'], 3)
        self.assertEqual(decoded['summary']['functions'], {'main': 15})
        self.assertEqual(decoded['quality']['firstPathOffset'], 25)
        self.assertEqual(decoded['inputBytes'], 120)
        self.assertTrue(decoded['synthetic']['declaration']['notForHardwareAcceptance'])
        self.assertEqual(len(decoded['synthetic']['resetMarkers']), 2)

    def test_events_record_dir(self):
        unit, _events = self.build_unit()
        record_dir = self.root / 'out' / 'events-record'
        record_dir.mkdir(parents=True)
        out = record_dir / 'events.jsonl'
        se.write_event_stream(unit, 3, out)
        meta = se.write_events_record_dir(unit, 3, record_dir, events_path=out)
        self.assertEqual(meta['streamedEvents'], 3 * 8 + 2)
        decoded = json.loads((record_dir / 'decoded.json').read_text(encoding='utf-8'))
        self.assertEqual(decoded['streamed']['schema'], 2)
        self.assertNotIn('elements', decoded)
        self.assertNotIn('packets', decoded)

    def test_cli_events_mode(self):
        record, _events = make_events_record(self.root / 'record')
        out = self.root / 'out'
        code = bs.main(['--mode', 'events', '--record', str(record), '--segments', '3',
                        '--out', str(out), '--event-sink', 'jsonl', '--verify', 'all',
                        '--quiet'])
        self.assertEqual(code, 0)
        meta = json.loads((out / 'synthetic.json').read_text(encoding='utf-8'))
        self.assertEqual(meta['events']['stats']['totalEvents'], 26)
        self.assertEqual(len(meta['events']['resets']), 2)
        self.assertEqual(meta['events']['unit']['instructions'], 5)
        self.assertEqual(meta['events']['record']['streamedEvents'], 26)
        verify = json.loads((out / 'verify' / 'events-verify.json').read_text(encoding='utf-8'))
        self.assertTrue(verify['ok'])
        # 单元 spool 可复用（--force 不删单元，只重建产物）
        code = bs.main(['--mode', 'events', '--record', str(record), '--segments', '2',
                        '--out', str(out), '--event-sink', 'none', '--verify', 'none',
                        '--force', '--quiet'])
        self.assertEqual(code, 0)

    def test_decoded_inline_source(self):
        record = self.root / 'record'
        record.mkdir()
        events = [boundary(0, 0, 1, 1), instruction(1, 25), instruction(2, 26),
                  boundary(3, 30, 3, 7)]
        decoded = dict(summary=dict(instructions=2, executed=2, exceptions=0, gaps=1),
                       quality=dict(firstPathOffset=25), events=events)
        (record / 'decoded.json').write_text(json.dumps(decoded), encoding='utf-8')
        unit = se.build_unit_spool(record, self.root / 'unit' / 'unit.events.jsonl')
        self.assertEqual(unit['eventsSource'], 'decoded')
        self.assertEqual(unit['events'], 4)
        self.assertEqual(unit['instructions'], 2)


# ---------------------------------------------------------------------------------------------
# 参数与失败模式
# ---------------------------------------------------------------------------------------------

class FailureModeTests(TempDirTestCase):
    def test_missing_record(self):
        code = bs.main(['--mode', 'physical', '--record', str(self.root / 'nope'),
                        '--segments', '2', '--out', str(self.root / 'out'), '--quiet'])
        self.assertEqual(code, 1)

    def test_missing_record_argument(self):
        with self.assertRaises(SystemExit):
            bs.main(['--mode', 'physical', '--out', str(self.root / 'out')])

    def test_zero_segments(self):
        with self.assertRaises(SystemExit):
            bs.main(['--mode', 'physical', '--record', str(self.root), '--segments', '0',
                     '--out', str(self.root / 'out')])

    def test_bad_kind_for_file(self):
        path = self.root / 'capture.kvdat'
        write_tiny_kvdat(path)
        code = bs.main(['--mode', 'physical', '--record', str(path), '--kind', 'csv',
                        '--segments', '2', '--out', str(self.root / 'out'), '--quiet'])
        self.assertEqual(code, 1)

    def test_truncated_kvdat(self):
        path = self.root / 'capture.kvdat'
        write_tiny_kvdat(path)
        data = path.read_bytes()
        path.write_bytes(data[:-10])
        with self.assertRaises(ValueError):
            bs.load_kvdat_source(path)

    def test_nonempty_out_requires_force(self):
        record = self.root / 'record'
        record.mkdir()
        write_tiny_kvdat(record / 'capture.kvdat')
        out = self.root / 'out'
        args = ['--mode', 'physical', '--record', str(record), '--segments', '2',
                '--out', str(out), '--verify', 'none', '--quiet']
        self.assertEqual(bs.main(args), 0)
        self.assertEqual(bs.main(args), 1)
        self.assertEqual(bs.main(args + ['--force']), 0)

    def test_events_source_missing(self):
        record = self.root / 'record'
        record.mkdir()
        with self.assertRaises(se.SyntheticError):
            se.build_unit_spool(record, self.root / 'unit' / 'unit.events.jsonl')

    def test_unit_spool_seq_gap_rejected(self):
        record = self.root / 'record'
        record.mkdir()
        events = [boundary(0, 0, 1, 1), instruction(2, 25)]
        with (record / 'events.jsonl').open('w', encoding='utf-8') as handle:
            for event in events:
                handle.write(json.dumps(event) + '\n')
        (record / 'decoded.json').write_text(json.dumps(dict(summary={}, quality={})),
                                             encoding='utf-8')
        with self.assertRaises(se.SyntheticError):
            se.build_unit_spool(record, self.root / 'unit' / 'unit.events.jsonl')

    def test_help_exits_zero(self):
        with self.assertRaises(SystemExit) as caught:
            bs.main(['--help'])
        self.assertEqual(caught.exception.code, 0)


if __name__ == '__main__':
    unittest.main()
