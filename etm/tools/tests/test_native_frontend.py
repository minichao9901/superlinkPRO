"""P5b 原生物理前端测试。

覆盖三类：

1. **版本握手与显式失败**：二进制缺失 / 旧残留（协议不符）都明确报错，不静默回退；
2. **与 P5a 参考实现逐字节/逐字段对照**：10 ms USB 记录（1/7/13/4096/1 MiB+1/整段）、
   steady/boundary 200 ms（含 glitch_ns=4.0 的毛刺滤波）、t3 合成 1 s 物理输入的逐段；
3. **明确拒绝**：CSV、缺文件、KVDAT 截断、非法分块、合成 1 s 整段（段间非连续 trace）。

参考实现（oracle）是 `etm/tools/trace_frontend.py`；它已被 P5a 证据证明与
`trace_port.extract` + `tpiu_demux.demux` 逐字节一致。原生二进制由
`pwsh -NoProfile -File etm/build-decoder.ps1` 构建到 `etm/build-native/etm_frontend.exe`。
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import native_frontend as nf

ETM_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[3]
RECORD_10MS = ETM_ROOT / 'records' / 'capture-4395612c313f47ce97af1e1c17e0a0d5'
RECORD_STEADY = (REPO_ROOT / 'docs/validation/20261018-ds-review-bb2f8ee-6ff02745/data/records'
                 / 'capture-be1e78730b7f486a9e27d5911cd4a434')
RECORD_BOUNDARY = (REPO_ROOT / 'docs/validation/20261018-ds-recheck-0cd4229-a7b4f912/live-data/records'
                   / 'capture-36431f7c7567446e8bcd6229094d1b28')
SYNTHETIC_DIR = REPO_ROOT / 'tmp' / 'p6a-1s' / 'physical'

CONFIG_10MS = dict(clock=0, data=[1, 2, 3, 4], fraction=0.25, glitch_ns=0.0, traceid=2)
CONFIG_200MS = dict(clock=0, data=[1, 2, 3, 4], fraction=0.25, glitch_ns=4.0, traceid=2)


def mixed_plan(size=1 << 20):
    plan = []
    for index in range(8):
        plan.extend([size if index % 4 else size // 8, 1, 3, 5, 7, 13])
    plan.append(size)
    return plan


def require_native(testcase):
    try:
        nf.probe()
    except nf.NativeFrontendError as error:
        testcase.skipTest(f'原生前端不可用：{error}')


class VersionHandshakeTests(unittest.TestCase):
    def test_probe_reports_protocol_and_provenance(self):
        info = nf.probe()
        self.assertEqual(info['protocol'], nf.PROTOCOL)
        self.assertEqual(info['schema'], nf.SCHEMA)
        self.assertIn('kvdat', info.get('inputFormats', []))
        provenance = nf.provenance()
        self.assertTrue(Path(provenance['path']).exists())
        self.assertEqual(len(provenance['sha256']), 64)
        self.assertGreater(provenance['bytes'], 0)

    def test_missing_binary_is_explicit(self):
        with self.assertRaises(nf.NativeFrontendUnavailable) as caught:
            nf.probe(ETM_ROOT / 'build-native' / 'definitely-not-here.exe')
        self.assertIn('build-decoder.ps1', str(caught.exception))

    def test_stale_binary_is_rejected(self):
        # 用 python 解释器冒充：--version 输出不是本协议的 JSON → 必须明确拒绝
        with self.assertRaises(nf.NativeFrontendMismatch):
            nf.probe(sys.executable)
        self.assertFalse(nf.available(sys.executable))


class RecordEquivalenceTests(unittest.TestCase):
    def test_one_malformed_candidate_does_not_abort_the_valid_phase(self):
        from test_trace_frontend import (phase_isolation_stream, build_tracks,
                                         write_kvdat, reference)
        with tempfile.TemporaryDirectory() as directory:
            for phase in (0, 1):
                tracks, initial = build_tracks(phase_isolation_stream(phase))
                path = write_kvdat(Path(directory)/('phase-%d.kvdat'%phase), tracks, initial)
                expected = reference(path)
                self.assertEqual([row[0] for row in expected['valid']], [phase])
                for chunk in (1, 13, 4096):
                    prefix=Path(directory)/('out-%d-%d'%(phase,chunk))
                    actual=nf.run(path, CONFIG_10MS,chunk_bytes=chunk,out_prefix=prefix)
                    self.assertEqual(actual['phase'],phase)
                    self.assertEqual(Path(str(prefix)+'.tpiu.bin').read_bytes(),expected['candidates'][phase])
                    self.assertEqual(Path(str(prefix)+'.id2.bin').read_bytes(),expected['valid'][0][2][2])

    def setUp(self):
        require_native(self)

    def _check(self, path, config, chunks=None, plan=None):
        reference = nf.run_reference(path, config, chunk_bytes=1 << 20)
        specs = []
        if chunks:
            specs.extend([dict(chunk_bytes=chunk) for chunk in chunks])
        if plan:
            specs.append(dict(chunk_plan=plan))
        for spec in specs:
            result = nf.run(path, config, **spec)
            report = nf.compare(result, reference)
            self.assertTrue(report['passed'],
                            f'{path.name} {spec} 不一致 {report["diffs"]}: '
                            f'{json.dumps(report["detail"], ensure_ascii=False)[:600]}')
            self.assertEqual(result['phase'], reference['phase'])

    def test_10ms_record_every_chunk(self):
        path = RECORD_10MS / 'capture.kvdat'
        if not path.exists():
            self.skipTest('缺少 10 ms 记录')
        size = path.stat().st_size
        self._check(path, CONFIG_10MS, chunks=[1, 7, 13, 4096, (1 << 20) + 1, size])

    def test_steady_record_with_glitch_filter(self):
        path = RECORD_STEADY / 'capture.kvdat'
        if not path.exists():
            self.skipTest('缺少 steady 200 ms 记录')
        size = path.stat().st_size
        self._check(path, CONFIG_200MS, chunks=[4096, (1 << 20) + 1, size],
                    plan=mixed_plan())

    def test_boundary_record_with_glitch_filter(self):
        path = RECORD_BOUNDARY / 'capture.kvdat'
        if not path.exists():
            self.skipTest('缺少 boundary 200 ms 记录')
        self._check(path, CONFIG_200MS, chunks=[13, 8191, (1 << 20) + 1])

    def test_synthetic_1s_segments(self):
        segment_dir = SYNTHETIC_DIR / 'segments'
        files = sorted(segment_dir.glob('segment-*.kvdat')) if segment_dir.exists() else []
        if not files:
            self.skipTest('缺少 t3 合成 1 s 段文件（tmp/p6a-1s/physical/segments）')
        config = dict(CONFIG_10MS)
        for path in (files[0], files[len(files) // 2], files[-1]):
            reference = nf.run_reference(path, config, chunk_bytes=1 << 20)
            for chunk in (4096, path.stat().st_size):
                result = nf.run(path, config, chunk_bytes=chunk)
                report = nf.compare(result, reference)
                self.assertTrue(report['passed'],
                                f'{path.name} chunk={chunk} {report["diffs"]}: '
                                f'{json.dumps(report["detail"], ensure_ascii=False)[:600]}')

    def test_whole_synthetic_1s_is_rejected(self):
        whole = SYNTHETIC_DIR / 'capture-synthetic-1s.kvdat'
        if not whole.exists():
            self.skipTest('缺少 t3 合成 1 s 整段文件')
        from trace_frontend import PhaseSelectionError
        with self.assertRaises(PhaseSelectionError) as reference_error:
            nf.run_reference(whole, CONFIG_200MS)
        self.assertEqual(reference_error.exception.valid_count, 0)
        self.assertEqual(len(reference_error.exception.failures), 2)
        with self.assertRaises(nf.NativeFrontendError) as caught:
            nf.run(whole, CONFIG_200MS, chunk_bytes=1 << 20)
        self.assertIn(str(reference_error.exception), str(caught.exception))


class RejectionTests(unittest.TestCase):
    def setUp(self):
        require_native(self)
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_csv_input_is_rejected(self):
        path = self.root / 'capture.csv'
        path.write_text('Time [s],Channel 0\n0,0\n', encoding='utf-8')
        with self.assertRaises(nf.NativeFrontendError) as caught:
            nf.run(path, CONFIG_10MS)
        self.assertIn('只支持 .kvdat', str(caught.exception))

    def test_missing_input_is_rejected(self):
        with self.assertRaises(nf.NativeFrontendError):
            nf.run(self.root / 'nope.kvdat', CONFIG_10MS)

    def test_truncated_kvdat_is_rejected(self):
        source = RECORD_10MS / 'capture.kvdat'
        if not source.exists():
            self.skipTest('缺少 10 ms 记录')
        broken = self.root / 'truncated.kvdat'
        data = source.read_bytes()
        broken.write_bytes(data[:-200])
        with self.assertRaises(nf.NativeFrontendError) as caught:
            nf.run(broken, CONFIG_10MS, chunk_bytes=4096)
        self.assertTrue('KVDAT' in str(caught.exception))

    def test_invalid_chunk_size_is_rejected(self):
        source = RECORD_10MS / 'capture.kvdat'
        if not source.exists():
            self.skipTest('缺少 10 ms 记录')
        completed = subprocess.run([str(nf.binary_path()), str(source),
                                    '--chunk-bytes', '0', '--quiet'],
                                   capture_output=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn('分块大小必须是正整数', completed.stderr.decode('utf-8', 'replace'))

    def test_duplicate_channels_are_rejected(self):
        source = RECORD_10MS / 'capture.kvdat'
        if not source.exists():
            self.skipTest('缺少 10 ms 记录')
        completed = subprocess.run([str(nf.binary_path()), str(source),
                                    '--data', '0,1,2,3', '--quiet'], capture_output=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn('五个不同通道', completed.stderr.decode('utf-8', 'replace'))

    def test_no_demux_mode(self):
        source = RECORD_10MS / 'capture.kvdat'
        if not source.exists():
            self.skipTest('缺少 10 ms 记录')
        out = self.root / 'physical.json'
        completed = subprocess.run([str(nf.binary_path()), str(source), '--no-demux',
                                    '--chunk-bytes', '4096', '--json', str(out), '--quiet'],
                                   capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode('utf-8', 'replace'))
        data = json.loads(out.read_text(encoding='utf-8'))
        self.assertIsNone(data['framing'])
        self.assertEqual(data['physical']['clock_edges'], 600015)
        self.assertEqual(data['physical']['candidates'][0]['bytes'], 300007)


class AdapterContractTests(unittest.TestCase):
    def setUp(self):
        require_native(self)

    def test_out_prefix_writes_artifacts(self):
        source = RECORD_10MS / 'capture.kvdat'
        if not source.exists():
            self.skipTest('缺少 10 ms 记录')
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / 'capture'
            result = nf.run(source, CONFIG_10MS, chunk_bytes=1 << 20, out_prefix=prefix)
            tpiu = Path(str(prefix) + '.tpiu.bin')
            stream = Path(str(prefix) + '.id2.bin')
            self.assertTrue(tpiu.exists() and stream.exists())
            self.assertTrue(Path(str(prefix) + '.physical.json').exists())
            self.assertTrue(Path(str(prefix) + '.framing.json').exists())
            import hashlib
            self.assertEqual(hashlib.sha256(tpiu.read_bytes()).hexdigest(), result['tpiuSha256'])
            self.assertEqual(result['tpiuBytes'], tpiu.stat().st_size)

    def test_compare_reports_specific_differences(self):
        source = RECORD_10MS / 'capture.kvdat'
        if not source.exists():
            self.skipTest('缺少 10 ms 记录')
        reference = nf.run_reference(source, CONFIG_10MS, chunk_bytes=1 << 20)
        native = nf.run(source, CONFIG_10MS, chunk_bytes=1 << 20)
        tampered = dict(reference)
        tampered['physical'] = dict(reference['physical'], clock_edges=-1)
        report = nf.compare(native, tampered)
        self.assertFalse(report['passed'])
        self.assertIn('physical', report['diffs'])
        self.assertIn('clock_edges', report['detail']['physical'])


if __name__ == '__main__':
    unittest.main()
