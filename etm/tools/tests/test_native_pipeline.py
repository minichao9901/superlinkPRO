"""Complete native publication, independent reference and failure atomicity."""
import json
import struct
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import native_storebuild
import native_binary
import trace_store
import trace_store_write
from test_trace_store_write import SyntheticCase, ELF_F103, V3_CONFIG


@unittest.skipUnless(native_storebuild.binary_path().is_file(), 'native builder required')
class NativePipelineTests(SyntheticCase):
    def test_published_store_matches_reference_and_all_queries(self):
        result, decoded = self.write_store(engine='native', write_decoded=True)
        self.assertTrue(result['ok'])
        self.assertFalse(result['engine']['fallback'])
        for key in ('summary', 'quality', 'analysis', 'status'):
            self.assertEqual(decoded[key], self.oracle[key], key)
        with trace_store.TraceStore(self.store_dir) as store:
            expected = [json.loads(line) for line in (self.oracle_dir/'events.jsonl').read_text(encoding='utf8').splitlines()]
            self.assertEqual(list(store.events()), expected)
            self.assertIsNotNone(store.query_index)
            for lo, hi in [(None,None),(0,1),(2,7),(4,12)]:
                self.assertEqual(store.window(lo,hi),store._window(lo,hi))
            self.assertTrue(store.manifest['queryIndex']['defaultViews'])
        self.assertFalse(any(self.store_dir.glob('generations/*/work')))

    def test_index_failure_does_not_replace_complete_generation(self):
        self.write_store(engine='native', write_decoded=True)
        path=self.store_dir/'manifest.json'
        before=path.read_bytes()
        with patch('native_facts_index.build_full_index',side_effect=ValueError('index failed')):
            with self.assertRaisesRegex(ValueError,'index failed'):
                self.write_store(engine='native',write_decoded=True)
        self.assertEqual(path.read_bytes(),before)

    def test_packets_preserve_every_raw_byte(self):
        self.write_store(engine='native')
        with trace_store.TraceStore(self.store_dir) as store:
            actual=store.page('packets',0,150)['rows']
        expected=[dict(offset=r[1],size=r[2],hex=r[4].hex())
                  for r in native_binary.NativeFile(self.native).records(with_payload=True)
                  if r[0]==native_binary.RECORD_PACKET]
        self.assertEqual([(p['offset'],p['size'],p['hex']) for p in actual],
                         [(p['offset'],p['size'],p['hex']) for p in expected])

    def test_manifest_failure_restores_metadata_and_previous_store(self):
        self.write_store(engine='native', write_decoded=True)
        paths = [self.store_dir/'manifest.json', self.record/'decoded.json', self.record/'sources.json']
        before = [path.read_bytes() for path in paths]
        with patch('trace_store._publish_manifest', side_effect=OSError('publish failed')):
            with self.assertRaisesRegex(OSError, 'publish failed'):
                self.write_store(engine='native', write_decoded=True)
        self.assertEqual([path.read_bytes() for path in paths], before)
        with trace_store.TraceStore(self.store_dir) as store:
            self.assertIsNotNone(store.query_index)

    def test_folded_cycle_count_keeps_more_than_32_bits(self):
        blob=bytearray(self.native.read_bytes());pos=native_binary.HEADER_BYTES
        while pos<len(blob):
            kind,length=native_binary.RECORD_HEADER.unpack_from(blob,pos)
            if kind==native_binary.RECORD_ELEMENT and blob[pos+5]==5:
                cycles=struct.unpack_from('<I',blob,pos+15)[0]
                if cycles:
                    struct.pack_into('<I',blob,pos+15,0xffffffff)
                    break
            pos+=length
        self.native.write_bytes(blob)
        _,decoded=self.write_store(engine='native')
        with trace_store.TraceStore(self.store_dir) as store:
            values=[e['cycleCount'] for e in store.events() if e['kind']=='instruction']
        self.assertIn(0xffffffff+5,values)
        self.assertEqual(sum(values),decoded['analysis']['timing']['totalCycles'])


class NativePipelineV3Tests(NativePipelineTests):
    protocol='ETMv3'
    elf=ELF_F103
