import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import native_export
import native_selection
import trace_store as ts
import trace_store_index
from test_trace_store import build_fixture


@unittest.skipUnless(native_export.binary_path().is_file(),'native worker required')
class NativeQueryTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,ETM_QUERY_ENGINE='native');self.env.start()
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.folder,self.events,_,_=build_fixture(self.root/'查询 记录')
        ts.write(self.folder,self.folder/'store',verify_image=False)
        trace_store_index.build_store_index(self.folder/'store',container='v2')
    def tearDown(self):self.env.stop();self.temp.cleanup()

    def reference(self,store,method,**kwargs):
        with patch.dict(os.environ,ETM_QUERY_ENGINE='python'):
            return getattr(store,method)(**kwargs)

    def test_pages_match_reference_for_search_states_bounds_and_clamped_offsets(self):
        with ts.TraceStore(self.folder/'store') as store:
            pc=next(e['pc'] for e in self.events if e['kind']=='instruction')
            for search in ['', 'MaIn','0x%08x'%pc,str(pc),'中断','\t','not-in-any-text-19']:
                for hide in (False,True):
                    if not search and not hide:continue
                    for lo,hi in [(None,None),(2,16),(-8,3),(100,120),(7,3)]:
                        for offset in (0,5,10**30):
                            kwargs=dict(search=search,hide_skipped=hide,start=lo,end=hi,offset=offset,limit=3)
                            try:expected=self.reference(store,'page',**kwargs)
                            except ts.TraceStoreError:
                                with self.assertRaises(ts.TraceStoreError):store.page(**kwargs)
                                continue
                            with self.subTest(**kwargs):self.assertEqual(store.page(**kwargs),expected)

    def test_timelines_match_reference_without_python_events(self):
        with ts.TraceStore(self.folder/'store') as store:
            for search in ['', 'MAIN','0x','中断','    ','missing-query']:
                for hide in (False,True):
                    if not search and not hide:continue
                    for lo,hi in [(None,None),(2,16),(0,0),(100,120)]:
                        for bins in (100,257):
                            kwargs=dict(search=search,hide_skipped=hide,start=lo,end=hi,bins=bins,max_functions=1)
                            expected=self.reference(store,'function_timeline',**kwargs)
                            with self.subTest(**kwargs),patch.object(store,'events',side_effect=AssertionError('whole-record Python forbidden')):
                                self.assertEqual(store.function_timeline(**kwargs),expected)

    def test_page_materializes_only_bounded_groups(self):
        with ts.TraceStore(self.folder/'store') as store:
            expected=self.reference(store,'page',search='main',limit=2,offset=3,hide_skipped=2)
            original=store.events;ranges=[]
            def events(lo,hi):
                self.assertLessEqual(hi-lo,8192);ranges.append((lo,hi));return original(lo,hi)
            with patch.object(store,'events',events),patch.object(store,'_expand',side_effect=AssertionError('stateful replay forbidden')):
                self.assertEqual(store.page(search='main',limit=2,offset=3,hide_skipped=2),expected)
            self.assertTrue(ranges)

    def test_missing_or_old_worker_fallback_and_explicit_reference(self):
        with ts.TraceStore(self.folder/'store') as store:
            with patch.object(native_export,'binary_path',return_value=self.root/'missing.exe'):
                self.assertIsNone(native_selection._run(store,0,0,5,'main',False))
                with patch.object(sys,'frozen',True,create=True),self.assertRaisesRegex(ValueError,'缺少原生查询'):
                    native_selection._run(store,0,0,5,'main',False)
            with patch.dict(os.environ,ETM_QUERY_ENGINE='python'),patch.object(subprocess,'run',side_effect=AssertionError('started worker')):
                self.assertIsNone(native_selection._run(store,0,0,5,'main',False))
            with patch.dict(os.environ,ETM_QUERY_ENGINE='invalid'),self.assertRaisesRegex(ValueError,'ETM_QUERY_ENGINE'):
                native_selection._run(store,0,0,5,'main',False)
            old=subprocess.CompletedProcess([],0,json.dumps(dict(protocol='etm-export/1',abi=1)).encode(),b'')
            with patch.object(subprocess,'run',return_value=old):
                self.assertIsNone(native_selection._run(store,0,0,5,'main',False))

    def test_plan_digest_and_changed_store_are_rejected(self):
        original=subprocess.run
        def mutate(command,**kwargs):
            if '--select' in command:
                path=Path(command[2]);raw=bytearray(path.read_bytes());raw[40]^=1;path.write_bytes(raw)
            return original(command,**kwargs)
        with ts.TraceStore(self.folder/'store') as store:
            with patch.object(subprocess,'run',mutate),self.assertRaisesRegex(ValueError,'plan SHA256'):
                store.page(search='main')
            path=store.container.path;raw=bytearray(path.read_bytes());raw[-1]^=1;path.write_bytes(raw)
            with self.assertRaisesRegex(ValueError,'store SHA256'):store.page(search='main')

    def test_parallel_plans_remain_private(self):
        with ts.TraceStore(self.folder/'store') as store:
            cases=[dict(search=s,hide_skipped=h) for s,h in [('main',False),('0x',True),('中断',False),('missing',True)]]
            expected=[self.reference(store,'page',**kwargs) for kwargs in cases]
            with ThreadPoolExecutor(max_workers=4) as pool:
                actual=list(pool.map(lambda kwargs:store.page(**kwargs),cases))
            self.assertEqual(actual,expected)

if __name__=='__main__':unittest.main()
