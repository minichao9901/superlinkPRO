"""1s request boundary and real LA early-completion behavior; never touches hardware."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('extent_backend',ROOT/'bridge/etm/backend/server.py')
server=importlib.util.module_from_spec(spec);spec.loader.exec_module(server)


class CaptureExtentTests(unittest.TestCase):
    def test_500m_full_window_and_one_sample_rounding(self):
        self.assertTrue(server.capture_extent('500000000',500000000,1)['complete'])
        self.assertTrue(server.capture_extent('499999999',500000000,1)['complete'])

    def test_device_early_completion_is_not_reported_as_one_second(self):
        value=server.capture_extent('463382775',500000000,1)
        self.assertFalse(value['complete'])
        self.assertAlmostEqual(value['actualSeconds'],.92676555)
        self.assertEqual(value['requestedSamples'],500000000)

    def test_invalid_device_depth_is_rejected(self):
        for value in ('', 'unknown', '0', '-1'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                server.capture_extent(value,500000000,1)

    def test_early_completion_keeps_raw_and_decodes_actual_window_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(server,'RECORDS',Path(tmp)):
            sid='capture-extent';server.save(sid,dict(input='capture.kvdat'))
            jid=server.new_job(sid,'queued',kind='capture')
            v=Mock();v.sock.recv.return_value=b'ACK'
            v.cmd.side_effect=lambda command,**kw:{'get-supported-sample-rate':'500000000',
                'get-actual-sample-depth':'463382775','get-actual-sample-time':'0 .92676555'}.get(command,'')
            def export(_v,path,*_):Path(path).write_bytes(b'original-waveform')
            try:
                with patch.object(server,'Vis',return_value=v),patch.object(server,'do_export',side_effect=export),\
                     patch.object(server,'read_glitch_settings',return_value={}),patch.object(server,'decode_job') as decode:
                    server.capture_job(jid,sid,dict(rate=500000000,seconds=1,threshold=1.65,sync=False,
                        decode=True,clock=0,data=[1,2,3,4],rawFormat='kvdat'))
                decode.assert_called_once_with(jid,sid)
                metadata=server.meta(sid)
                self.assertFalse(metadata['captureExtent']['complete'])
                self.assertIn('926.766',metadata['captureWarning'])
                self.assertIn('实际窗口',server.jobs[jid]['captureWarning'])
                self.assertEqual((Path(tmp)/sid/'capture.kvdat').read_bytes(),b'original-waveform')
            finally:server.jobs.pop(jid,None)


if __name__=='__main__':unittest.main()
