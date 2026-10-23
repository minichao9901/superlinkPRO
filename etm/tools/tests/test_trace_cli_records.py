import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import trace_cli as cli


class RecordTests(unittest.TestCase):
    def test_legacy_inline_query_builds_only_derived_index_and_keeps_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            event=dict(seq=0,kind='instruction',pc=0x08000100,function='main',file='main.c',line=1,
                       flow='linear',size=2,executed=True,executionState='executed',offset=0,mnemonic='nop',operands='',bytes='00bf')
            cli.save(folder/'session.json',dict(input='capture.bin',config={'traceid':2}))
            cli.save(folder/'decoded.json',dict(version=3,events=[event],packets=[],elements=[],sources={'main.c':'nop'},summary={},quality={},analysis={}))
            before=(folder/'decoded.json').read_bytes()
            view=cli.stored_view(folder)
            try:
                self.assertEqual(view.page('events',0,2)['rows'][0]['pc'],0x08000100)
                self.assertEqual(list(view.iter_events())[0]['seq'],0)
                self.assertFalse(view.input_verification()['verified'])
            finally:view.close()
            self.assertEqual((folder/'decoded.json').read_bytes(),before)

    def test_legacy_config_mismatch_is_not_served_as_current_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            cli.save(folder/'session.json',dict(input='capture.bin',config={'traceid':2}))
            cli.save(folder/'decoded.json',dict(version=3,config={'traceid':3}))
            with self.assertRaisesRegex(ValueError,'配置'):cli.stored_view(folder)

    def test_elf_export_copies_original_bytes_and_refuses_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);(folder/'firmware.elf').write_bytes(b'original-ELF')
            args=cli.build_parser().parse_args(['export','--record',str(folder),'--format','elf','--out',str(folder/'export.elf')])
            cli.export_command(args);self.assertEqual((folder/'export.elf').read_bytes(),b'original-ELF')
            args.start=1;args.end=2
            with self.assertRaises(cli.CliError):cli.export_command(args)

    def test_attaching_sources_preserves_a_backup_and_does_not_change_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);cli.save(folder/'session.json',dict(input='capture.bin'))
            cli.save(folder/'sources.json',{'a.c':'old'});cli.save(folder/'new.json',{'a.c':'new','b.c':'added'})
            (folder/'capture.bin').write_bytes(b'raw')
            args=cli.build_parser().parse_args(['record','sources','--record',str(folder),'--sources-json',str(folder/'new.json')])
            with patch.object(cli,'elf_info',return_value={'sourceFiles':[]}):value=cli.record_command(args)
            self.assertEqual(cli.load(value['backup']),{'a.c':'old'})
            self.assertEqual(cli.load(folder/'sources.json'),{'a.c':'new','b.c':'added'})
            self.assertEqual((folder/'capture.bin').read_bytes(),b'raw')


if __name__=='__main__':unittest.main()
