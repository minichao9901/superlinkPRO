import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import decode_trace as dt
import static_dict

ELF=Path(__file__).resolve().parents[2]/'firmware/stm32f103_cdc_trace/fw-ze.elf'

@unittest.skipUnless(ELF.is_file(),'bundled ARM ELF required')
class ImagePreparationTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,ETM_PREPARE_CACHE='1');self.env.start()
        with dt._IMAGE_CACHE_LOCK:dt._IMAGE_CACHE.clear()
        with static_dict._ADDRESS_CACHE_LOCK:static_dict._ADDRESS_CACHE.clear()
    def tearDown(self):self.env.stop()

    def test_content_reuse_preserves_parser_and_source_isolation(self):
        first=dt.Image(ELF,{'main.c':'original'})
        expected=(list(first.functions),dict(first.lines),list(first.segments))
        first.functions.clear();first.lines.clear();first.segments.clear()
        first.source_text['main.c']='changed'
        with patch.object(dt.Image,'_parse',side_effect=AssertionError('cache must avoid DWARF parse')):
            second=dt.Image(ELF,{'main.c':'second'})
        self.assertTrue(second.preparation['cacheHit'])
        self.assertEqual((second.functions,second.lines,second.segments),expected)
        self.assertEqual(second.source_text,{'main.c':'second'})
        self.assertIsNot(first.elf,second.elf)
        with tempfile.TemporaryDirectory() as temporary:
            other=Path(temporary)/'同一 固件.elf';shutil.copyfile(ELF,other)
            self.assertTrue(dt.Image(other).preparation['cacheHit'])

    def test_same_size_same_mtime_changed_bytes_invalidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'firmware.elf';shutil.copyfile(ELF,path)
            first=dt.Image(path);stat=path.stat();raw=bytearray(path.read_bytes())
            segment=next(s for s in first.elf.iter_segments() if s['p_type']=='PT_LOAD' and s['p_filesz']>8)
            raw[int(segment['p_offset'])+8]^=1;path.write_bytes(raw)
            os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
            second=dt.Image(path)
            self.assertFalse(second.preparation['cacheHit'])
            self.assertNotEqual(second.preparation['sha256'],first.preparation['sha256'])
            self.assertNotEqual(second.segments,first.segments)

    def test_single_flight_and_disabled_reference(self):
        original=dt.Image._parse;calls=[]
        def parse(image):calls.append(1);return original(image)
        with patch.object(dt.Image,'_parse',parse),ThreadPoolExecutor(max_workers=4) as pool:
            images=list(pool.map(lambda _:dt.Image(ELF),range(4)))
        self.assertEqual(len(calls),1)
        self.assertEqual(sum(i.preparation['cacheHit'] for i in images),3)
        with patch.dict(os.environ,ETM_PREPARE_CACHE='0'):
            reference=dt.Image(ELF)
        self.assertFalse(reference.preparation['cacheHit'])
        self.assertEqual(reference.lines,images[0].lines)
        self.assertEqual(reference.functions,images[0].functions)

    def test_address_cache_is_immutable_and_observes_changed_function_spans(self):
        image=dt.Image(ELF)
        pred,known,cost=static_dict._address_preparation(image)
        self.assertFalse(cost['addressPreparationCacheHit'])
        with patch.object(static_dict,'predicate_addresses',side_effect=AssertionError('recomputed')):
            a,b,cost=static_dict._address_preparation(dt.Image(ELF))
        self.assertTrue(cost['addressPreparationCacheHit']);self.assertEqual((a,b),(pred,known))
        self.assertIsInstance(a,frozenset);self.assertIsInstance(b,frozenset)
        image.functions=[]
        a,b,cost=static_dict._address_preparation(image)
        self.assertFalse(cost['addressPreparationCacheHit']);self.assertFalse(a);self.assertFalse(b)

    def test_bounded_eviction_does_not_change_values(self):
        with patch.object(dt,'_IMAGE_CACHE_ENTRIES',1),tempfile.TemporaryDirectory() as temporary:
            first=dt.Image(ELF);path=Path(temporary)/'other.elf';raw=bytearray(first.bytes)
            raw[-1]^=1;path.write_bytes(raw)
            dt.Image(path)
            self.assertEqual(len(dt._IMAGE_CACHE),1)
            final=dt.Image(ELF)
            self.assertFalse(final.preparation['cacheHit']);self.assertEqual(final.lines,first.lines)

    def test_address_key_does_not_keep_large_image_segments_and_still_invalidates(self):
        image=dt.Image(ELF);image.functions=[]
        image.segments=[(0,b'\0'*(2<<20))]
        static_dict._address_preparation(image)
        def retained_bytes(value):
            if isinstance(value,bytes):return len(value)
            if isinstance(value,(tuple,list)):return sum(map(retained_bytes,value))
            return 0
        self.assertLess(retained_bytes(tuple(static_dict._ADDRESS_CACHE)),4096)
        image.segments=[(0,b'\1'+image.segments[0][1][1:])]
        _,_,cost=static_dict._address_preparation(image)
        self.assertFalse(cost['addressPreparationCacheHit'])

if __name__=='__main__':unittest.main()
