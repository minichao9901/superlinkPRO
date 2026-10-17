import io
import os
import hashlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import server


class ServerTests(unittest.TestCase):
    def test_library_has_one_demo_and_preserves_user_records_separately_from_experiments(self):
        from unittest.mock import patch
        demo='demo-example'
        self.assertEqual(server.record_kind(demo,{'libraryOrigin':'validation'},demo),'demo')
        self.assertEqual(server.record_kind('manual',{'captureScope':'business-unfiltered-window','captureEntry':0x08001234},demo),'user')
        self.assertEqual(server.record_kind('validation',{'libraryOrigin':'validation','captureScope':'business-unfiltered-window','captureEntry':0x08001234},demo),'experiment')
        self.assertEqual(server.record_kind('early',{'captureScope':'unfiltered-window'},demo),'experiment')
        for sid,data in [(demo,{}),('manual',{'libraryOrigin':'user'}),('early',{})]:
            folder=server.directory(sid);folder.mkdir();server.save(sid,{'name':sid,**data})
        with patch.object(server,'stable_baseline',return_value={'previewRecord':demo}):records=self.client.get('/api/records').json
        self.assertEqual(sum(r['recordKind']=='demo' for r in records),1)
        self.assertEqual({r['id'] for r in records},{demo,'manual','early'})

    def test_etm_serves_its_committed_source_store_copy(self):
        upstream=Path(os.environ.get('SERIAL_ROOT',str(ROOT.parent/'web-serial-rtt-tools')))/'app/dbg/source.js'
        bundled=ROOT/'web/app/shared/dbg/source.js'
        self.assertEqual(bundled.read_bytes(),upstream.read_bytes())
        response=self.client.get('/app/shared/dbg/source.js')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data,bundled.read_bytes())
        response.close()

    def test_capture_requires_an_explicit_uploaded_elf_before_creating_a_job(self):
        response=self.client.post('/api/capture',json={'seconds':.01})
        self.assertEqual(response.status_code,400)
        self.assertIn('明确的 ELF',response.json['error'])
        self.assertEqual(list(Path(self.folder.name).iterdir()),[])

    def test_source_snapshot_batches_are_immutable_and_capture_uses_small_reference(self):
        from unittest.mock import patch
        response=self.client.post('/api/elf',data={'elf':(io.BytesIO((ROOT/'firmware/stm32h7b0_spi_trace/stable-70-35-v1/fw.elf').read_bytes()),'fw.elf')})
        elf=response.json
        self.assertEqual(len(elf['sourceFiles']),22)
        first,second=elf['sourceFiles'][:2]
        upload=self.client.post('/api/source-snapshots',json={'elfId':elf['elfId'],'sources':{first:'exact first source'},'complete':False})
        self.assertEqual(upload.status_code,200,upload.json)
        snap=upload.json['snapshot']
        incomplete=self.client.post('/api/capture',json={'elfId':elf['elfId'],'sourceSnapshot':snap})
        self.assertEqual(incomplete.status_code,400)
        final=self.client.post('/api/source-snapshots',json={'elfId':elf['elfId'],'snapshot':snap,'sources':{second:'exact second source'},'complete':True})
        self.assertEqual(final.status_code,200,final.json)
        self.assertEqual(final.json['files'],2)
        rewritten=self.client.post('/api/source-snapshots',json={'elfId':elf['elfId'],'snapshot':snap,'sources':{first:'changed'}})
        self.assertEqual(rewritten.status_code,400)
        before_jobs=set(server.jobs)
        try:
            with patch.object(server.threading,'Thread'):
                capture=self.client.post('/api/capture',json={'elfId':elf['elfId'],'sourceSnapshot':snap,'seconds':.002})
            self.assertEqual(capture.status_code,200,capture.json)
            stored=json.loads((server.directory(capture.json['record'])/'sources.json').read_text())
            self.assertEqual(stored,{first:'exact first source',second:'exact second source'})
            self.assertNotIn('sources',server.meta(capture.json['record']))
        finally:
            for jid in set(server.jobs)-before_jobs:server.jobs.pop(jid)

    def test_source_snapshot_rejects_unrelated_sdk_file_and_413_is_actionable(self):
        response=self.client.post('/api/elf',data={'elf':(io.BytesIO((ROOT/'firmware/stm32h7b0_spi_trace/stable-70-35-v1/fw.elf').read_bytes()),'fw.elf')})
        uploaded=self.client.post('/api/source-snapshots',json={'elfId':response.json['elfId'],'sources':{'unrelated.c':'not referenced'},'complete':True})
        self.assertEqual(uploaded.status_code,400)
        old=server.app.config['MAX_CONTENT_LENGTH']
        try:
            server.app.config['MAX_CONTENT_LENGTH']=64
            oversized=self.client.post('/api/capture',json={'sources':{'big.c':'x'*100}})
            self.assertEqual(oversized.status_code,413)
            self.assertIn('ELF',oversized.json['error'])
        finally:server.app.config['MAX_CONTENT_LENGTH']=old

    def test_uploaded_elf_resolves_source_breakpoints_without_neighbor_guess(self):
        response = self.client.post('/api/elf', data={'elf': (
            io.BytesIO((ROOT / 'firmware/stm32h7b0_spi_trace/stable-70-35-v1/fw.elf').read_bytes()), 'fw.elf')})
        self.assertEqual(response.status_code, 200, response.json)
        sid = response.json['elfId']
        resolved = self.client.post(f'/api/elf/{sid}/breakpoint', json={'spec': 'b main:255'})
        self.assertEqual(resolved.status_code, 200, resolved.json)
        self.assertEqual(resolved.json['candidates'][0]['address'], 0x08000894)
        missing = self.client.post(f'/api/elf/{sid}/breakpoint', json={'spec': 'b main:199'})
        self.assertEqual(missing.status_code, 400)
        self.assertIn('没有可下断点', missing.json['error'])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.original = server.RECORDS
        server.RECORDS = Path(self.folder.name)
        self.client = server.app.test_client()

    def tearDown(self):
        server.RECORDS = self.original
        self.folder.cleanup()

    def test_record_package_reload_decode_and_export(self):
        elf_path=ROOT/'firmware/stm32h7b0_etm_board_v1/fw.elf'
        image=server.Image(elf_path);sources=[]
        # A worktree differs from the compile-time DWARF root. Supply source
        # snapshots as the web UI does, rather than depending on that old root.
        for file in sorted({file for _,file,_ in image.lines.values() if file}):
            relative=file.replace('\\','/').split('/firmware/',1)
            local=ROOT/'firmware'/relative[-1]
            if len(relative)==2 and local.is_file():sources.append((io.BytesIO(local.read_bytes()),file))
        response = self.client.post('/api/import', data={
            'capture': (io.BytesIO((ROOT / 'captures/trace-20261011-000319.csv').read_bytes()), 'trace.csv'),
            'elf': (io.BytesIO(elf_path.read_bytes()), 'fw.elf'),'sources':sources})
        self.assertEqual(response.status_code, 200, response.json)
        sid = response.json['record']
        jid = self.client.post('/api/decode/' + sid).json['job']
        for _ in range(100):
            job = self.client.get('/api/jobs/' + jid).json
            if job['state'] in ('done', 'error'):
                break
            time.sleep(.05)
        self.assertEqual(job['state'], 'done', job)
        data = self.client.get('/api/records/' + sid).json
        self.assertEqual(data['summary']['instructions'], 10753)
        self.assertEqual(data['status']['lastErrorCode'], 0)
        package = self.client.get('/api/download/' + sid + '/record')
        reloaded = self.client.post('/api/import', data={'capture': (io.BytesIO(package.data), 'saved.etmrecord')})
        self.assertEqual(reloaded.status_code, 200, reloaded.json)
        copied = reloaded.json['record']
        self.assertEqual(server.meta(copied)['config'], server.meta(sid)['config'])
        self.assertEqual((server.directory(copied) / 'sources.json').read_bytes(),
                         (server.directory(sid) / 'sources.json').read_bytes())
        exported = self.client.get('/api/download/' + sid + '/csv').data.decode('utf-8-sig')
        self.assertEqual(len(exported.splitlines()), len(data['events']) + 1)
        source = self.client.get('/api/download/' + sid + '/source').data.decode('utf-8')
        self.assertIn('trace_even', source)
        self.assertIn('trace_odd', source)
        self.assertIn('NO_SYNC', source)

    def test_cross_origin_mutation_and_invalid_capture_rejected(self):
        response = self.client.post('/api/capture', headers={'Origin': 'https://example.com'}, json={})
        self.assertEqual(response.status_code, 403)
        response = self.client.post('/api/capture', json={'seconds': 3})
        self.assertEqual(response.status_code, 400)

    def test_spi_profile_uses_matching_elf_and_known_capture_entry(self):
        for key in ('spi-70','spi-280'):
            response=self.client.post('/api/profiles/'+key,json={})
            self.assertEqual(response.status_code,200,response.json)
            info=response.json
            self.assertTrue(0x08000000<=info['captureEntry']<0x08020000)
            self.assertEqual(server.elf_info(server.directory(info['elfId'])/'firmware.elf')['sha256'],info['sha256'])
            bound=self.client.get('/api/record-elf/'+info['elfId']).json
            self.assertEqual(bound['profile'],key)
            flash_bytes=sum(len(__import__('base64').b64decode(s['data'])) for s in info['segments'])
            self.assertEqual(flash_bytes,len((ROOT/'firmware/stm32h7b0_spi_trace'/('fw-'+key.removeprefix('spi-')+'.bin')).read_bytes()))

    def test_valid_packets_do_not_hide_a_narrow_sampling_window(self):
        folder=server.directory('phase-proof');folder.mkdir()
        server.save('phase-proof',dict(fraction=.25))
        (folder/'etm.bin').write_bytes(b'canonical')
        same=hashlib.sha256(b'canonical').hexdigest()
        other=hashlib.sha256(b'other').hexdigest()
        result=dict(version=3,physical=dict(half_period_ns=14,min_data_transition_distance_ns=2.5))
        (folder/'decoded.json').write_text(json.dumps(result))
        phases=[dict(fraction=f,sha=same) for f in (.1,.15,.2,.25,.3,.35,.4)]+[dict(fraction=.75,sha=other)]
        (folder/'phase-validation.json').write_text(json.dumps(phases))
        q=server.result('phase-proof')['samplingValidation']
        self.assertEqual(q['stableWindow'],[.1,.4]);self.assertFalse(q['sensitive'])
        (folder/'phase-validation.json').write_text(json.dumps([dict(fraction=.25,sha=same),dict(fraction=.3,sha=same),dict(fraction=.1,sha=other)]))
        self.assertTrue(server.result('phase-proof')['samplingValidation']['sensitive'])

    def test_baseline_stays_pinned_when_new_experimental_records_exist(self):
        baseline=json.loads((ROOT/'baselines/spi-70-35-v1/manifest.json').read_text(encoding='utf8'))
        for sid in (baseline['record'],baseline['previewRecord'],'new-experiment'):
            folder=server.directory(sid);folder.mkdir();server.save(sid,dict(created=9999999999))
            (folder/'decoded.json').write_text('{}')
            (folder/'spi-validation.json').write_text('{}')
        self.assertEqual(self.client.get('/api/spi-sample').json['record'],baseline['record'])
        self.assertEqual(self.client.get('/api/stable-baseline').json['previewRecord'],baseline['previewRecord'])
    def test_changed_capture_cannot_be_served_as_previous_decoded_history(self):
        sid='changed-proof';folder=server.directory(sid);folder.mkdir()
        raw=b'original';(folder/'capture.csv').write_bytes(raw)
        server.save(sid,dict(input='capture.csv'))
        (folder/'decoded.json').write_text(json.dumps(dict(version=3,capture=dict(sha256=hashlib.sha256(raw).hexdigest()))))
        (folder/'capture.csv').write_bytes(b'changed')
        response=self.client.get('/api/records/'+sid)
        self.assertEqual(response.status_code,400);self.assertIn('指纹不一致',response.json['error'])
    def test_cached_overview_rechecks_hash_even_if_size_and_time_preserved(self):
        import os
        sid='same-stat-proof';folder=server.directory(sid);folder.mkdir();raw=folder/'capture.csv';raw.write_bytes(b'AAAA')
        server.save(sid,dict(input='capture.csv'))
        (folder/'decoded.json').write_text(json.dumps(dict(version=3,events=[],packets=[],sources={},capture=dict(sha256=hashlib.sha256(b'AAAA').hexdigest()))))
        self.assertEqual(self.client.get('/api/browse/'+sid+'/overview').status_code,200)
        before=raw.stat();raw.write_bytes(b'BBBB');os.utime(raw,ns=(before.st_atime_ns,before.st_mtime_ns))
        self.assertTrue(server.browse_store(sid).ready())
        self.assertEqual(self.client.get('/api/browse/'+sid+'/overview').status_code,400)


if __name__ == '__main__':
    unittest.main()
