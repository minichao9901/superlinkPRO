import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stream_store import attach_streams, JsonLines
from record_store import RecordStore
from flow_analysis import byte_accounting
from decode_stream import export_simplified_stream


class StreamStoreTests(unittest.TestCase):
    def test_valid_jsonl_prefix_is_not_accepted_as_a_complete_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'events.jsonl';p.write_text('{}\n')
            with self.assertRaisesRegex(ValueError,'记录数不一致'):list(JsonLines(p,2))
    def test_native_path_follows_portable_runtime_selection_after_import(self):
        from unittest.mock import patch
        import decode_trace
        from decode_stream import _native
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);native=root/'build-native/etm_decode.exe'
            native.parent.mkdir();native.write_bytes(b'not executed')
            with patch.object(decode_trace,'ROOT',root):self.assertEqual(_native(),native)
    def test_replay_index_navigation_and_unknown_export(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            events=[dict(kind='instruction',seq=i,pc=0x100+i*2,size=2,
                         function='main',file='main.c',line=1,flow='linear',
                         executed=None if i==1 else True,
                         executionState='unknown' if i==1 else 'executed',
                         bytes='00bf',mnemonic='nop',operands='',offset=i,
                         callPath=['采集前调用栈未知','main']) for i in range(350)]
            packets=[dict(offset=0,size=2,text='I_TRACE_INFO'),
                     dict(offset=1,size=2,text='I_OVERFLOW'),
                     dict(offset=5,size=1,text='I_NOT_SYNC')]
            metadata=dict(version=3,summary={},quality={},analysis={},inputBytes=8,
                          status=dict(processed=6),
                          streamed=dict(schema=2,events=len(events),packets=len(packets),elements=0))
            for name,rows in [('events',events),('packets',packets),('elements',[])]:
                (folder/(name+'.jsonl')).write_text(''.join(json.dumps(e)+'\n' for e in rows),encoding='utf8')
            (folder/'sources.json').write_text(json.dumps({'main.c':'work();'}),encoding='utf8')
            (folder/'decoded.json').write_text(json.dumps(metadata),encoding='utf8')
            r=attach_streams(dict(metadata),folder)
            self.assertIsInstance(r['events'],JsonLines)
            self.assertEqual(list(r['events']),list(r['events']))
            q=byte_accounting(r)
            self.assertEqual((q['overlapBytes'],q['accountedBytes'],q['unaccountedBytes']),(1,4,4))
            self.assertEqual((q['overflowPackets'],q['unknownExecution']),(1,1))
            store=RecordStore(folder,lambda:r)
            self.assertEqual(store.overview()['eventCount'],350)
            self.assertEqual(store.page(offset=150)['rows'][0]['seq'],150)
            self.assertEqual(store.event(1)['event']['executed'],None)
            output=folder/'simplified.c';export_simplified_stream(folder,output)
            self.assertIn('条件执行状态未知',output.read_text(encoding='utf8'))
            self.assertNotIn('events',json.loads((folder/'decoded.json').read_text()))
            (folder/'sources.json').write_text(json.dumps({'main.c':'changed();'}))
            self.assertFalse(store.ready())

    def test_malformed_jsonl_is_not_silently_truncated(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'events.jsonl';p.write_text('{broken}\n')
            with self.assertRaises(ValueError):list(JsonLines(p,1))
