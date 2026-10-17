"""检查原文件字节保留、真实档案快照及运行中保护；不修改本机 VIS 配置。"""
import sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'la'))
from kingst_la import glitch_preset_bytes,read_glitch_settings,cmd_glitch_preset
ROOT=Path(__file__).resolve().parents[2]
def settings():
    return b'<settings><global><other>\xd6\xd0\xce\xc4</other><glitchFilterMode>0</glitchFilterMode><glitchFilterSettings>'+b'0,1,'*16+b'</glitchFilterSettings></global></settings>'
class FilterTests(unittest.TestCase):
    def test_only_selected_channels_change_and_local_encoding_survives(self):
        modified=glitch_preset_bytes(settings(),[0,4],2)
        self.assertIn(b'<other>\xd6\xd0\xce\xc4</other>',modified)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'vis.config';path.write_bytes(modified)
            result=read_glitch_settings(path,500000000)
        self.assertTrue(result['keepOnRestart'])
        self.assertEqual([x['channel'] for x in result['channels'] if x['enabled']],[0,4])
        self.assertEqual(result['channels'][0]['widthNs'],4)
    def test_real_snapshot_and_rate_mismatch(self):
        path=ROOT/'records/capture-3ca5db88fbc5428c8a2ab7d8d485d0d8/capture.kvdat'
        result=read_glitch_settings(path)
        self.assertEqual(result['sampleRate'],500000000)
        self.assertEqual([x['channel'] for x in result['channels'] if x['enabled']],list(range(5)))
        self.assertEqual(result['channels'][0]['widthNs'],2)
        with self.assertRaises(ValueError):read_glitch_settings(path,100000000)
    def test_live_application_rejected_without_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'vis.config';path.write_bytes(settings())
            args=SimpleNamespace(config=str(path),channels=[0],samples=2,disable=False,reset_on_restart=False,apply=True)
            with patch('kingst_la.kingst_running',return_value=True):
                with self.assertRaises(SystemExit):cmd_glitch_preset(args)
            self.assertEqual(path.read_bytes(),settings())
            self.assertEqual(list(Path(temp).iterdir()),[path])
    def test_bad_channel_or_duplicate_field_refused(self):
        with self.assertRaises(ValueError):glitch_preset_bytes(settings(),[16])
        with self.assertRaises(ValueError):glitch_preset_bytes(settings(),[0,0])
        with self.assertRaises(ValueError):glitch_preset_bytes(settings()+settings(),[0])
    def test_closed_application_preserves_backup_and_can_disable(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'vis.config';original=glitch_preset_bytes(settings(),[0,4],2);path.write_bytes(original)
            args=SimpleNamespace(config=str(path),channels=[4],samples=1,disable=True,reset_on_restart=False,apply=True)
            with patch('kingst_la.kingst_running',return_value=False):cmd_glitch_preset(args)
            backups=list(Path(temp).glob('*.bak'))
            self.assertEqual(len(backups),1);self.assertEqual(backups[0].read_bytes(),original)
            info=read_glitch_settings(path)
            self.assertEqual([x['channel'] for x in info['channels'] if x['enabled']],[0])
if __name__=='__main__':unittest.main()
