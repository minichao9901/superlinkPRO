"""Anchored row materialization must preserve complete events without replaying CallTracker."""
import concurrent.futures
import json
import os
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import flow_analysis
import trace_store
import trace_store_index
from test_trace_store import build_fixture


class IndexedRowsTests(unittest.TestCase):
    def test_full_fields_ranges_pages_and_events_match_frozen_fixture_for_both_containers(self):
        for cycles, container in [(True, 'v1'), (False, 'v2')]:
            with self.subTest(cycles=cycles, container=container), tempfile.TemporaryDirectory() as directory:
                folder, expected, _, _ = build_fixture(Path(directory)/'record', cycles=cycles)
                trace_store.write(folder, folder/'store', verify_image=False)
                trace_store_index.build_store_index(folder/'store', container=container)
                with trace_store.TraceStore(folder/'store') as store:
                    with patch.object(store, '_expand', side_effect=AssertionError('state replay forbidden')):
                        with patch.object(flow_analysis.CallTracker, 'feed_instruction', side_effect=AssertionError('tracker forbidden')):
                            for lo in range(len(expected)):
                                for hi in [lo, min(lo+3,len(expected)-1), len(expected)-1]:
                                    self.assertEqual(list(store.events(lo,hi)),expected[lo:hi+1])
                                self.assertEqual(store.event(lo)['event'],expected[lo])
                            self.assertEqual(store.page(offset=10**9,limit=7)['rows'],expected[(len(expected)-1)//7*7:])
                            self.assertEqual(store.page(start=3,end=13,offset=3,limit=4)['rows'],expected[6:10])
                            self.assertEqual(list(store.events(-99,1)),expected[:2])
                            self.assertEqual(list(store.events(len(expected)+10)),[])
                            self.assertEqual(store.page(start=7,end=6)['total'],0)
                            with self.assertRaises(trace_store.TraceStoreError):list(store.events(7,6))

    def test_old_store_and_forced_slow_keep_reference_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            folder, expected, _, _=build_fixture(Path(directory)/'record')
            trace_store.write(folder,folder/'store',verify_image=False)
            with trace_store.TraceStore(folder/'store') as old:
                with patch.object(old,'_expand',wraps=old._expand) as expand:
                    self.assertEqual(old.event(len(expected)-1)['event'],expected[-1]);expand.assert_called_once()
            trace_store_index.build_store_index(folder/'store')
            with trace_store.TraceStore(folder/'store') as store:
                with patch.dict(os.environ,{'TRACE_STORE_SLOW':'1'}):
                    with patch.object(store,'_expand',wraps=store._expand) as expand:
                        self.assertEqual(list(store.events(2,8)),expected[2:9]);expand.assert_called_once()

    def test_invalid_prepared_path_falls_back_before_yield_and_bad_anchor_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            folder, expected, _, _=build_fixture(Path(directory)/'record')
            trace_store.write(folder,folder/'store',verify_image=False)
            trace_store_index.build_store_index(folder/'store')
            with trace_store.TraceStore(folder/'store') as store:
                index=store.query_index;arrays=index.arrays;replacement={name:arrays[name] for name in ('cell_anchor_block','cell_anchor_offset','cell_anchor_skip','cell_first_seq','ps_start','ps_count','ps_leaf','node_parent','node_label')}
                replacement['ps_leaf']=np.full_like(replacement['ps_leaf'],10**6)
                with patch.object(index,'arrays',replacement):
                    self.assertEqual(store.event(4)['event'],expected[4]);self.assertIn('路径索引',store._row_index_error)
                    self.assertIsNone(store.query_index)
                self.assertEqual(store.event(4)['event'],expected[4])
            with trace_store.TraceStore(folder/'store') as store:
                index=store.query_index
                self.assertEqual(store.event(4)['event'],expected[4])
                tables, paths=index._row_state
                damaged=dict(tables);damaged['cell_anchor_offset']=np.full_like(tables['cell_anchor_offset'],10**8)
                with patch.object(index,'_row_state',(damaged,paths)):
                    with self.assertRaisesRegex(trace_store_index.QueryIndexError,'偏移越界'):list(store.events(4,8))

    def test_anchor_inside_max_length_run_keeps_tail_cycles_and_consecutive_end_events(self):
        with tempfile.TemporaryDirectory() as directory:
            folder, small, _, _=build_fixture(Path(directory)/'record',with_packets=False)
            expected=[dict(e) for e in small[:2]]
            template=next(e for e in small if e['kind']=='instruction')
            for i in range(2*4096+130):
                cycles=(2**53+17) if i==2*4096+129 else 0
                expected.append(dict(template,seq=len(expected),offset=4 if i<17 else 10,
                                     cycleCount=cycles,cycleSpan=1 if cycles else 0,cycles=cycles))
            for _ in range(2):expected.append(dict(small[-1],seq=len(expected)))
            flow_analysis.analyze_calls(expected)
            (folder/'events.jsonl').write_text(''.join(json.dumps(e,ensure_ascii=False)+'\n' for e in expected),encoding='utf8')
            (folder/'elements.jsonl').unlink()
            decoded=json.loads((folder/'decoded.json').read_text(encoding='utf8'))
            decoded['streamed']['events']=len(expected)
            decoded['summary'].update(instructions=len(expected)-4,executed=len(expected)-4,unknownExecution=0,exceptions=0,gaps=1)
            decoded['summary'].pop('ranges',None);decoded['quality']['unknownExecution']=0
            decoded['quality']['firstPathOffset']=4
            (folder/'decoded.json').write_text(json.dumps(decoded),encoding='utf8')
            trace_store.write(folder,folder/'store',verify_image=False,checkpoint_instructions=10**9)
            trace_store_index.build_store_index(folder/'store',container='v2')
            with trace_store.TraceStore(folder/'store') as store:
                self.assertGreater(int(store.query_index.arrays['cell_anchor_skip'][1]),0)
                with patch.object(store,'_expand',side_effect=AssertionError('replay forbidden')):
                    for lo,hi in [(4095,4102),(8188,8200),(len(expected)-8,len(expected)-1)]:
                        self.assertEqual(list(store.events(lo,hi)),expected[lo:hi+1])

    def test_long_run_cells_event_boundaries_and_concurrent_cold_readers(self):
        with tempfile.TemporaryDirectory() as directory:
            folder, small, _, _=build_fixture(Path(directory)/'record',with_packets=False)
            # Reset markers in each copy make every call path independently
            # defined. Recompute sequence and offset only, leaving all semantic
            # event fields as the original fixture's expected values.
            expected=[]
            for repeat in range(2200):
                for item in small:
                    expected.append(dict(item,seq=len(expected),offset=item['offset']+repeat*64))
            (folder/'events.jsonl').write_text(''.join(json.dumps(e,ensure_ascii=False)+'\n' for e in expected),encoding='utf8')
            decoded=json.loads((folder/'decoded.json').read_text(encoding='utf8'));decoded['streamed']['events']=len(expected)
            decoded['summary'].update(instructions=sum(e['kind']=='instruction' for e in expected),executed=sum(e.get('executed') is True for e in expected),unknownExecution=sum(e.get('kind')=='instruction' and e.get('executed') is None for e in expected),sourceMapped=2200*15,exceptions=2200,gaps=2200)
            decoded['summary'].pop('ranges',None)
            decoded['quality']['unknownExecution']=2200
            (folder/'elements.jsonl').unlink()
            (folder/'decoded.json').write_text(json.dumps(decoded,ensure_ascii=False),encoding='utf8')
            trace_store.write(folder,folder/'store',verify_image=False,checkpoint_instructions=10**9)
            trace_store_index.build_store_index(folder/'store')
            with trace_store.TraceStore(folder/'store') as store:
                randomizer=random.Random(195729);seqs=[0,1,len(expected)-1]+[randomizer.randrange(len(expected)) for _ in range(80)]
                with patch.object(store,'_expand',side_effect=AssertionError('replay forbidden')):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                        rows=list(pool.map(store.event,seqs))
                    self.assertEqual([r['event'] for r in rows],[expected[i] for i in seqs])
                    for lo in [len(expected)-500,4090,8190,12280]:
                        self.assertEqual(list(store.events(lo,lo+333)),expected[lo:lo+334])
                        self.assertLessEqual(store._row_last_stats['skippedInstructions'],store.query_index.cell_size)


if __name__=='__main__':unittest.main()
