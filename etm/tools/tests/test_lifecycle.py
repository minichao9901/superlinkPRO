import concurrent.futures, json, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch, Mock
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import server
import kingst_la

class LifecycleTests(unittest.TestCase):
    def test_interrupted_decode_is_not_shown_as_an_old_success(self):
        sid='interrupted-stream';server.save(sid,dict(input='capture.bin'))
        folder=server.directory(sid);(folder/'decoded.json').write_text('{}')
        (folder/'decode-in-progress.json').write_text('{}')
        client=server.app.test_client()
        response=client.get('/api/browse/'+sid+'/overview')
        self.assertEqual(response.status_code,409);self.assertIn('上次解码未完成',response.json['error'])
        row=next(r for r in client.get('/api/records').json if r['id']==sid)
        self.assertFalse(row['decoded'])
    def test_record_gate_allows_parallel_readers_but_excludes_a_writer(self):
        gate=server.RecordGate()
        self.assertTrue(gate.try_read());self.assertTrue(gate.try_read())
        self.assertFalse(gate.try_write())
        gate.end_read();gate.end_read()
        with gate.write():
            self.assertFalse(gate.try_read());self.assertFalse(gate.try_write())
        self.assertTrue(gate.try_read());gate.end_read()
    def test_decoding_sidecars_are_not_exposed_as_a_mixed_record_and_jobs_remain_readable(self):
        import threading
        ready=threading.Event();release=threading.Event();sid='guarded-record'
        def writer():
            with server.record_guard(sid).write():ready.set();release.wait(3)
        thread=threading.Thread(target=writer);thread.start();self.assertTrue(ready.wait(1))
        jid=server.new_job(sid,'decoding')
        try:
            client=server.app.test_client()
            self.assertEqual(client.get('/api/browse/'+sid+'/overview').status_code,409)
            self.assertEqual(client.get('/api/jobs/'+jid).status_code,200)
        finally:release.set();thread.join()
    def test_capture_is_rejected_while_a_decoding_job_still_holds_the_la_lease(self):
        # 审查 R05：解码期间任务叫 decoding，但仍握着 LA 连接与本次采集缓存。
        # 排他判据必须是"是否持有租约"，否则第二个采集会踢断第一个的 KingstVIS 连接。
        sid='capture-lease';server.save(sid,{'input':'capture.csv'})
        jid=server.new_job(sid,'decoding');server.jobs[jid]['_laLease']=True
        response=self.capture()
        self.assertEqual(response.status_code,400)
        self.assertIn('已有采集任务进行中',response.json['error'])
    def test_stop_during_slice_decoding_defers_instead_of_poking_the_socket(self):
        sid='capture-lease-stop';server.save(sid,{'input':'capture.csv'})
        jid=server.new_job(sid,'decoding');server.jobs[jid]['_laLease']=True
        with patch.object(server,'stop_owned_la') as poked:
            response=server.app.test_client().post('/api/stop/'+jid)
        self.assertEqual(response.status_code,200,response.json)
        self.assertTrue(response.json.get('deferred'))
        self.assertTrue(server.jobs[jid]['_stopped'])
        poked.assert_not_called()   # 不能用第二条连接打断正在跑的导出
    def test_stop_during_saving_is_consumed_before_the_long_decode(self):
        # 审查 F02：单窗路径没有分片边界；导出完成是唯一检查点，停止必须在这里生效。
        sid='capture-stop-saving';server.save(sid,{'input':'capture.csv'});jid=server.new_job(sid,'queued')
        class Socket:
            def settimeout(self,_):pass
            def sendall(self,_):pass
            def recv(self,_):return b'ACK'
        class Vis:
            def __init__(self):self.sock=Socket()
            def cmd(self,command):
                return {'get-supported-sample-rate':'500000000',
                        'get-actual-sample-depth':'100000000',
                        'get-actual-sample-time':'0 0.2'}.get(command,'ACK')
            def close(self):pass
        def stop_after_export(*args,**kwargs):server.jobs[jid]['_stopped']=True
        options=dict(rate=500000000,seconds=.2,threshold=1.65,sync=True,data=[1,2,3,4],clock=0,decode=True)
        with patch.object(server,'Vis',Vis),patch.object(server,'do_export',side_effect=stop_after_export), \
                patch.object(server,'decode_job') as decode,patch.object(server,'stop_owned_la'):
            server.capture_job(jid,sid,options)
        decode.assert_not_called()
        self.assertEqual(server.jobs[jid]['state'],'cancelled')
        m=server.meta(sid)
        self.assertTrue(m.get('stopRequested'))
        self.assertEqual(m.get('captureStatus'),'stopped')
    def test_stop_before_decode_starts_is_not_ignored(self):
        sid='capture-stop-decode';server.save(sid,{'input':'capture.csv'});jid=server.new_job(sid,'queued',kind='decode')
        server.jobs[jid]['_stopped']=True
        with patch.object(server,'_decode_job') as inner:
            server.decode_job(jid,sid)
        inner.assert_not_called()
        self.assertEqual(server.jobs[jid]['state'],'cancelled')
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.previous=server.RECORDS;server.RECORDS=Path(self.tmp.name)
        server.jobs.clear();self.elf='elf-'+'a'*32;server.directory(self.elf).mkdir();(server.directory(self.elf)/'firmware.elf').write_bytes(b'elf')
        self.info=patch.object(server,'elf_info',return_value={'sha256':'same'});self.info.start()
    def tearDown(self):self.info.stop();server.jobs.clear();server.RECORDS=self.previous;self.tmp.cleanup()
    def capture(self,**options):return server.app.test_client().post('/api/capture',json={'elfId':self.elf,'seconds':.001,**options})
    def test_offline_la_constructor_is_a_catchable_error(self):
        with patch.object(kingst_la.socket,'create_connection',side_effect=ConnectionRefusedError('offline')):
            try:kingst_la.Vis()
            except Exception:return
            except SystemExit:self.fail('LA offline exits the worker instead of reporting a task error')
        self.fail('offline LA unexpectedly connected')
    def test_la_worker_exit_cannot_leave_job_queued(self):
        sid='capture-offline';server.save(sid,{'input':'capture.csv'});jid=server.new_job(sid,'queued')
        with patch.object(server,'Vis',side_effect=SystemExit('offline')):
            try:server.capture_job(jid,sid,{})
            except SystemExit:pass
        self.assertEqual(server.jobs[jid]['state'],'error')
    def test_empty_csv_failure_terminates_decode_job(self):
        sid='record-empty';server.save(sid,{'input':'capture.csv','config':{}});jid=server.new_job(sid,'queued')
        with patch.object(server,'decode_capture',side_effect=SystemExit('empty CSV')):
            try:server.decode_job(jid,sid)
            except SystemExit:pass
        self.assertEqual(server.jobs[jid]['state'],'error')
    def test_concurrent_capture_admission_is_atomic(self):
        real=server.new_job
        def delayed(*args,**kwargs):time.sleep(.025);return real(*args,**kwargs)
        with patch.object(server,'threading',SimpleNamespace(Thread=Mock())),patch.object(server,'new_job',side_effect=delayed):
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                codes=sorted(r.status_code for r in pool.map(lambda _:self.capture(),range(2)))
        self.assertEqual(codes,[200,400]);self.assertEqual(len(server.jobs),1)
    def test_duplicate_decode_returns_same_job(self):
        server.save('record-a',{'input':'capture.bin'})
        with patch.object(server.threading,'Thread'):
            a=server.app.test_client().post('/api/decode/record-a').json
            b=server.app.test_client().post('/api/decode/record-a').json
        self.assertEqual(a['job'],b['job']);self.assertEqual(len(server.jobs),1)
    def test_optional_firmware_absence_is_an_actionable_404(self):
        with patch.object(server,'ROOT',Path(self.tmp.name)):
            client=server.app.test_client()
            for path,method in [('/api/default-elf','get'),('/api/firmware','get'),('/api/profile-firmware/spi-280','get'),('/api/profiles/spi-280','post'),('/api/profiles/unknown','post')]:
                response=getattr(client,method)(path);self.assertEqual(response.status_code,404,response.json);self.assertIn('固件',response.json['error'])
    def test_finished_jobs_are_bounded_without_removing_active_or_cleanup_leases(self):
        active=server.new_job('active','armed');failed=server.new_job('failed','error');server.change(failed,cleanupPending=True)
        for i in range(350):
            jid=server.new_job(str(i),'queued');server.change(jid,state='done')
        server.prune_jobs();self.assertLessEqual(len(server.jobs),258);self.assertIn(active,server.jobs);self.assertIn(failed,server.jobs)
        server.prune_jobs(now=time.time()+86401);self.assertEqual(set(server.jobs),{active,failed})
    def test_nonfinite_threshold_is_rejected_before_job(self):
        for value in [float('nan'),float('inf'),-float('inf'),-5,5]:
            with self.subTest(value=value),patch.object(server.threading,'Thread'):
                r=self.capture(threshold=value);self.assertEqual(r.status_code,400,r.json);self.assertEqual(len(server.jobs),0)
    def test_invalid_software_filter_is_rejected_before_job(self):
        for value in [float('nan'),float('inf'),-1,101]:
            with self.subTest(value=value),patch.object(server.threading,'Thread'):
                r=self.capture(glitchNs=value);self.assertEqual(r.status_code,400,r.json);self.assertEqual(len(server.jobs),0)
    def test_queued_capture_can_be_cancelled_without_talking_to_la(self):
        jid=server.new_job('capture-a','queued')
        with patch.object(server,'Vis',side_effect=AssertionError('must not connect')):
            r=server.app.test_client().post('/api/stop/'+jid)
        self.assertEqual(r.status_code,200,r.json);self.assertEqual(server.jobs[jid]['state'],'cancelled')
    def test_thread_start_failure_does_not_leave_busy_job(self):
        with patch.object(server.threading,'Thread') as thread:
            thread.return_value.start.side_effect=RuntimeError('cannot start')
            r=self.capture()
        self.assertEqual(r.status_code,400);self.assertTrue(server.jobs);self.assertEqual(next(iter(server.jobs.values()))['state'],'error')
    def test_closed_socket_and_cancelled_wait_never_export_previous_buffer(self):
        for cancelled in [False,True]:
            with self.subTest(cancelled=cancelled):
                sid='capture-socket-'+str(cancelled);server.save(sid,{'input':'capture.csv'});jid=server.new_job(sid,'queued')
                class Socket:
                    def settimeout(self,_):pass
                    def sendall(self,_):pass
                    def recv(self,_):
                        if cancelled:server.jobs[jid].update(_stopped=True,_stop_confirmed=True)
                        return b''
                class Vis:
                    def __init__(self):self.sock=Socket()
                    def cmd(self,command):return '500000000' if command=='get-supported-sample-rate' else ''
                    def close(self):pass
                with patch.object(server,'Vis',Vis),patch.object(server,'do_export') as export,patch.object(server,'stop_owned_la'):
                    server.capture_job(jid,sid,{'rate':500000000,'seconds':.01,'threshold':1.65,'sync':True,'data':[1,2,3,4],'clock':0,'decode':False})
                export.assert_not_called();self.assertEqual(server.jobs[jid]['state'],'cancelled' if cancelled else 'error')
    def test_incomplete_record_cannot_export_as_complete_package(self):
        server.save('cancelled-record',{'input':'capture.csv'})
        r=server.app.test_client().get('/api/download/cancelled-record/record')
        self.assertEqual(r.status_code,400);self.assertIn('原始波形',r.json['error'])
    def test_bad_config_register_words_never_start_a_job(self):
        for value in [-1,2**32,True,1.5,'0x1']:
            with self.subTest(value=value):
                r=self.capture(config={'config':value});self.assertEqual(r.status_code,400,r.json);self.assertEqual(len(server.jobs),0)
    def test_missing_jobs_are_actionable_and_never_stop_foreign_la(self):
        with patch.object(server,'Vis',side_effect=AssertionError('must not touch LA')):
            for path in ['/api/jobs/missing','/api/stop/missing']:
                r=server.app.test_client().get(path) if '/jobs/' in path else server.app.test_client().post(path)
                self.assertEqual(r.status_code,404);self.assertIn('任务',r.json['error'])
    def test_sources_from_different_elf_are_rejected(self):
        with patch.object(server,'result',return_value={'elf':{'sha256':'different'},'sources':{}}):
            r=server.app.test_client().post('/api/sources/record-a',json={'elfId':self.elf,'sources':{}})
        self.assertEqual(r.status_code,400);self.assertIn('不同 ELF',r.json['error'])
    def test_missing_capture_entry_is_not_disguised_by_zero_packet_errors(self):
        es=[{'kind':'instruction','pc':0x080005a0}]
        marker=server.capture_start_info(es,{'captureEntry':0x080004d0})
        self.assertFalse(marker['entryRecovered']);self.assertFalse(marker['firstMatchesEntry']);self.assertEqual(marker['firstRecoveredPC'],0x080005a0)
        marker=server.capture_start_info(es+[{'kind':'instruction','pc':0x080004d0}],{'captureEntry':0x080004d0})
        self.assertTrue(marker['entryRecovered']);self.assertFalse(marker['firstMatchesEntry'])

if __name__=='__main__':unittest.main()
