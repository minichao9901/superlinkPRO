"""CLI invariants: no clobbering, no fabricated metadata, strict completion and safe imports."""
import contextlib
import io
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import trace_cli as cli
import trace_cli_hardware as hw


class CliTests(unittest.TestCase):
    def test_embedded_main_restores_environment_on_success_and_failure(self):
        for fail in (False,True):
            with self.subTest(fail=fail),patch.dict(os.environ,{'ETM_STORE_ENGINE':'python','TRACE_CLI_WEB':'original'}):
                os.environ.pop('ETM_EXPORT_ENGINE',None)
                before=dict(os.environ)
                def operation(_):
                    self.assertEqual(os.environ['ETM_STORE_ENGINE'],'native')
                    self.assertEqual(os.environ['ETM_EXPORT_ENGINE'],'native')
                    if fail:raise ValueError('fixture error')
                    return {'passed':True}
                args=SimpleNamespace(json_out=None,command='test',func=operation)
                with patch.object(cli,'build_parser',return_value=SimpleNamespace(parse_args=lambda _:args)),contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main([]),2 if fail else 0)
                self.assertEqual(dict(os.environ),before)

    def test_argument_errors_are_one_machine_readable_result(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):code=cli.main(['capture','--elf','x','--out','y','--rate','nope'])
        result=json.loads(output.getvalue());self.assertEqual(code,2);self.assertFalse(result['ok'])
        self.assertEqual(result['protocol'],'trace-cli/1')

    def test_export_reservation_preserves_existing_file_and_partial_failure_is_not_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'out';path.write_bytes(b'user-data')
            with self.assertRaises(FileExistsError):
                with cli.destination(path):pass
            self.assertEqual(path.read_bytes(),b'user-data')
            fresh=Path(tmp)/'fresh'
            with self.assertRaises(ValueError):
                with cli.destination(fresh) as stage:stage.write_bytes(b'partial');raise ValueError('failure')
            self.assertFalse(fresh.exists());self.assertEqual(list(Path(tmp).iterdir()),[path])

    def test_lease_does_not_steal_an_existing_owner_and_releases_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(hw,'lock_path',return_value=Path(tmp)/'lease'):
            with self.assertRaisesRegex(ValueError,'failure'):
                with hw.HardwareLease('test'):
                    with self.assertRaises(RuntimeError):
                        with hw.HardwareLease('other'):pass
                    raise ValueError('failure')
            self.assertFalse((Path(tmp)/'lease').exists())

    def test_complete_configuration_is_required_instead_of_default_registers(self):
        for value in ({},{'traceid':2},{'config':{'traceid':2}}):
            with self.assertRaises(cli.CliError):cli.validated_config(value)

    def test_actual_927ms_cannot_pass_a_one_second_cli_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'capture.kvdat';path.write_bytes(b'layout mocked')
            with patch('kingst_record.layout',return_value=dict(rate=500000000,depth=463382775)):
                value=cli.extent(Path(tmp),dict(input=path.name,seconds=1,rate=500000000))
            self.assertFalse(value['complete']);self.assertAlmostEqual(value['actualSeconds'],.92676555)

    def test_quality_only_accepts_explicit_end_of_input_truncation(self):
        overview={'quality':{'packetIssues':1,'inputBytes':100}}
        class View:
            def issues(self,*_):return dict(total=1,rows=[dict(category='packet',reason='包问题：I_INCOMPLETE_EOT',offset=99,bytes=1)])
        self.assertTrue(cli.quality_check(View(),overview)['passed'])
        overview['quality']['inputBytes']=101
        self.assertFalse(cli.quality_check(View(),overview)['passed'])
        overview['quality']['packetIssues']=0;overview['quality']['overflowPackets']=1
        self.assertFalse(cli.quality_check(View(),overview)['passed'])

    def test_malicious_and_duplicate_zip_members_are_rejected_before_creating_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for number,names in enumerate([['../escape'],['a','a'],['C:/escape'],['a\\escape']]):
                archive=root/('%d.etmrecord'%number)
                with zipfile.ZipFile(archive,'w') as z:
                    for name in names:z.writestr(name,b'x')
                args=cli.build_parser().parse_args(['import-record','--input',str(archive),'--out',str(root/'out')])
                with self.assertRaises(cli.CliError):cli.import_command(args)
                self.assertFalse((root/'out').exists())

    def test_metadata_fingerprint_change_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);(folder/'capture.bin').write_bytes(b'raw');(folder/'firmware.elf').write_bytes(b'elf')
            cli.save(folder/'session.json',dict(input='capture.bin',config={'traceid':2}))
            cli.input_bindings(folder)
            cli.save(folder/'session.json',dict(input='capture.bin',config={'traceid':3}))
            with self.assertRaisesRegex(cli.CliError,'配置指纹'):cli.verify_record(folder)

    def test_record_metadata_cannot_select_a_file_outside_the_record(self):
        for value in ('../capture.bin','C:/capture.bin','other.bin','../firmware.elf'):
            with self.subTest(value=value),self.assertRaises(cli.CliError):cli.record_input({'input':value})

    def test_cli_hardware_does_not_import_pyocd(self):
        source=Path(hw.__file__).read_text(encoding='utf8')
        self.assertNotIn('from pyocd',source);self.assertNotIn('import pyocd',source)

    def test_interrupted_reply_cannot_be_mistaken_for_cleanup_result(self):
        import queue
        pipe=hw.WebPipe();pipe.process=SimpleNamespace(stdin=io.StringIO());pipe.messages=queue.Queue()
        pipe.messages.put(dict(id=0,ok=False,error='old command'))
        pipe.messages.put(dict(id=1,ok=True,value={'restored':True}))
        self.assertEqual(pipe.call('close'),{'restored':True})

    def test_invalid_timeout_is_rejected_before_hardware(self):
        with patch.object(hw,'web_request',side_effect=AssertionError('must not connect')),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['web','wait-halt','--timeout','nan']),2)

    def test_pc_navigation_uses_the_existing_instruction_and_pc_query_contract(self):
        seen=[]
        view=SimpleNamespace(navigate=lambda *a:seen.append(a) or {'seq':2},close=lambda:None)
        args=cli.build_parser().parse_args(['query','--record','unused','--view','navigate','--mode','pc','--pc','0x08000100'])
        with patch.object(cli,'stored_view',return_value=view):self.assertEqual(cli.query_command(args),{'seq':2})
        self.assertEqual(seen[0][2:4],('instruction',0x08000100))
        args.pc=None
        with self.assertRaises(cli.CliError):cli.query_command(args)

    def test_decoder_error_or_midstream_gap_is_not_hidden_by_clean_byte_accounting(self):
        self.assertFalse(cli.quality_check(None,dict(quality={},status={'lastErrorCode':1}))['passed'])
        self.assertFalse(cli.quality_check(None,dict(quality={},summary={'gaps':2}))['passed'])

    def test_json_report_is_reserved_before_the_command_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'result.json'
            def operation(_):
                self.assertTrue(path.exists());self.assertEqual(path.stat().st_size,0)
                return {'passed':True}
            args=SimpleNamespace(json_out=path,command='test',func=operation)
            with patch.object(cli,'build_parser',return_value=SimpleNamespace(parse_args=lambda _:args)),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main([]),0)
                original=path.read_bytes();self.assertEqual(cli.main([]),2)
                self.assertEqual(path.read_bytes(),original)


if __name__=='__main__':unittest.main()
