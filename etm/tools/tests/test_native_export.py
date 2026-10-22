"""Native exports preserve the Python format without per-instruction Python calls."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import native_export
import source_export
import trace_store as ts
from test_trace_store import build_fixture


@unittest.skipUnless(native_export.binary_path().is_file(),'native exporter has not been built')
class NativeExportTests(unittest.TestCase):
    def setUp(self):
        self.engine_env=patch.dict(os.environ,ETM_EXPORT_ENGINE='native');self.engine_env.start()
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.folder,self.events,_,sources=build_fixture(self.root/'记录 目录')
        self.sources={name:'\n'.join('行 %d /* */ </script> "comma,"' % line for line in range(1,60)) for name in sources}
        self.sources['extra.c']='unused source\n'
        (self.folder/'sources.json').write_text(json.dumps(self.sources),encoding='utf8')
        ts.write(self.folder,self.folder/'store',verify_image=False)
        self.metadata=json.loads((self.folder/'decoded.json').read_text(encoding='utf8'))
        self.metadata['sources']=self.sources
    def tearDown(self):self.engine_env.stop();self.temp.cleanup()

    def compare(self,store,start=None,end=None):
        result=dict(self.metadata)
        if start is not None:result['exportRange']=dict(start=start,end=store.last_seq if end is None else end)
        events=[e for e in self.events if (start is None or e['seq']>=start) and (end is None or e['seq']<=end)]
        for kind in native_export.FORMATS:
            with self.subTest(kind=kind,start=start,end=end):
                actual=self.root/'原生 导出';expected=self.root/'Python 导出'
                self.assertTrue(native_export.write(store,result,actual,kind,start,end))
                source_export.write_execution_export(result,iter(events),expected,kind)
                self.assertEqual(actual.read_bytes(),expected.read_bytes())
        self.assertFalse(list(self.root.glob('.etm-export-*')))

    def test_all_formats_and_ranges_match_reference_without_python_event_expansion(self):
        with ts.TraceStore(self.folder/'store') as store,patch.object(ts.TraceStore,'events',side_effect=AssertionError('per-instruction Python forbidden')):
            for start,end in [(None,None),(0,0),(2,10),(10,20),(len(self.events)-2,None),(len(self.events)+3,None)]:
                self.compare(store,start,end)

    def test_compressed_sequence_blocks_match_reference(self):
        with ts.TraceStore(self.folder/'store') as store:
            file=store.container.path;tag=store.container.generation_tag
            sink=ts._BlockSink(self.root/'compressed-payload')
            try:
                for i,block in enumerate(store.container.blocks):
                    if block.block_type==ts.BLK_SEQUENCE:
                        raw=store.container.block(i);sink.add_raw(ts.BLK_SEQUENCE,zlib.compress(raw),len(raw),1)
                    else:sink.add_raw(block.block_type,store.container.raw_block(i),block.count,block.flags)
                sink.assemble(self.root/'compressed.bin',tag,optional_features=4)
            finally:sink.close()
        os.replace(self.root/'compressed.bin',file)
        manifest_path=self.folder/'store/manifest.json';manifest=json.loads(manifest_path.read_text(encoding='utf8'))
        manifest['files'][0].update(bytes=file.stat().st_size,sha256=hashlib.sha256(file.read_bytes()).hexdigest())
        manifest_path.write_text(json.dumps(manifest),encoding='utf8')
        with ts.TraceStore(self.folder/'store') as store:self.compare(store,3,len(self.events)-1)

    def test_partial_missing_sources_keep_diagnostics(self):
        self.metadata['sources']={'main.c':'first\nsecond\n','leaf.c':''}
        with ts.TraceStore(self.folder/'store') as store:self.compare(store)

    def test_legacy_instruction_fields_without_cycles_match(self):
        folder,self.events,_,_=build_fixture(self.root/'legacy-no-cycles',cycles=False)
        ts.write(folder,folder/'store',verify_image=False)
        with ts.TraceStore(folder/'store') as store:self.compare(store,2,len(self.events)-1)

    def test_missing_helper_only_allows_source_fallback_and_invalid_engine_is_rejected(self):
        with ts.TraceStore(self.folder/'store') as store:
            target=self.root/'output.c'
            with patch.object(native_export,'binary_path',return_value=self.root/'missing.exe'):
                self.assertFalse(native_export.write(store,self.metadata,target,'source'))
                with patch.object(sys,'frozen',True,create=True):
                    with self.assertRaisesRegex(ValueError,'缺少原生导出程序'):
                        native_export.write(store,self.metadata,target,'source')
            with patch.dict(os.environ,ETM_EXPORT_ENGINE='invalid'):
                with self.assertRaisesRegex(ValueError,'ETM_EXPORT_ENGINE'):
                    native_export.write(store,self.metadata,target,'source')

    def test_plan_hash_and_store_mutation_are_rejected(self):
        with ts.TraceStore(self.folder/'store') as store:
            plan=self.root/'plan.bin';native_export.prepare_plan(store,self.metadata,'source',plan)
            payload=bytearray(plan.read_bytes());payload[20]^=1;plan.write_bytes(payload)
            result=subprocess.run([str(native_export.binary_path()),str(plan),str(store.container.path),str(self.root/'bad.out')],capture_output=True)
            self.assertNotEqual(result.returncode,0);self.assertIn(b'plan SHA256',result.stderr)
            native_export.prepare_plan(store,self.metadata,'source',plan)
            target=store.container.path;original=target.read_bytes();changed=bytearray(original);changed[-1]^=1
            try:
                target.write_bytes(changed)
                result=subprocess.run([str(native_export.binary_path()),str(plan),str(target),str(self.root/'bad.out')],capture_output=True)
                self.assertNotEqual(result.returncode,0);self.assertIn(b'SHA256 mismatch',result.stderr)
            finally:target.write_bytes(original)

    def test_sequence_crc_is_checked_even_when_plan_hash_matches_changed_file(self):
        with ts.TraceStore(self.folder/'store') as store:
            target=store.container.path;original=target.read_bytes()
            sequence=next(block for block in store.container.blocks if block.block_type==ts.BLK_SEQUENCE)
            changed=bytearray(original);changed[sequence.offset]^=1
            old_hash=store.manifest['files'][0]['sha256']
            try:
                target.write_bytes(changed);store.manifest['files'][0]['sha256']=hashlib.sha256(changed).hexdigest()
                plan=self.root/'plan.bin';native_export.prepare_plan(store,self.metadata,'source',plan)
                result=subprocess.run([str(native_export.binary_path()),str(plan),str(target),str(self.root/'bad.out')],capture_output=True)
                self.assertNotEqual(result.returncode,0);self.assertIn(b'sequence CRC',result.stderr)
            finally:target.write_bytes(original);store.manifest['files'][0]['sha256']=old_hash

    def test_failures_preserve_previous_output_and_explicit_python_bypasses_native(self):
        with ts.TraceStore(self.folder/'store') as store:
            target=self.root/'output.c';target.write_text('previous result')
            with patch.dict(os.environ,ETM_EXPORT_ENGINE='python'):
                self.assertFalse(native_export.write(store,self.metadata,target,'source'))
            self.assertEqual(target.read_text(),'previous result')
            result=dict(self.metadata,sources={name:'' for name in self.sources})
            with self.assertRaisesRegex(ValueError,'配套源码文本'):native_export.write(store,result,target,'simplified')
            self.assertEqual(target.read_text(),'previous result')
            self.assertFalse(list(self.root.glob('.etm-export-*')))

    def test_cli_refuses_output_hardlink_alias_of_input(self):
        with ts.TraceStore(self.folder/'store') as store:
            plan=self.root/'plan.bin';native_export.prepare_plan(store,self.metadata,'csv',plan)
            alias=self.root/'store-alias';os.link(store.container.path,alias);before=hashlib.sha256(alias.read_bytes()).hexdigest()
            completed=subprocess.run([str(native_export.binary_path()),str(plan),str(store.container.path),str(alias)],capture_output=True)
            self.assertNotEqual(completed.returncode,0);self.assertIn(b'aliases',completed.stderr)
            self.assertEqual(hashlib.sha256(alias.read_bytes()).hexdigest(),before)


if __name__=='__main__':unittest.main()
