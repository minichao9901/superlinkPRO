import concurrent.futures,itertools,json,sys,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[2]));import server

class MatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old=server.RECORDS;server.RECORDS=Path(self.tmp.name);server.jobs.clear()
        self.elf='elf-'+'b'*32;server.directory(self.elf).mkdir();(server.directory(self.elf)/'firmware.elf').write_bytes(b'fixture')
        self.info=patch.object(server,'elf_info',return_value={});self.info.start()
        self.worker=patch.object(server,'threading',SimpleNamespace(Thread=Mock()));self.worker.start()
    def tearDown(self):self.worker.stop();self.info.stop();server.jobs.clear();server.RECORDS=self.old;self.tmp.cleanup()
    def post(self,**opts):return server.app.test_client().post('/api/capture',json={'elfId':self.elf,**opts})
    def release(self):
        for job in server.jobs.values():job['state']='done'
    def test_numeric_boundaries_336_requests(self):
        count=0
        # 2026-10-18：窗口上限 200 ms → 4 s（e5e1fac，实测 LA 可采 1/2/4 s），
        # 边界口径同步为 0 < seconds <= 4 且 rate*seconds <= 2.1e9（500 MSa/s × 4 s）。
        for rate,seconds,fraction in itertools.product([0,1,50000000,100000000,500000000,500000001,10**10],[-1,0,.001,.01,2,2.001,float('nan'),float('inf')],[-.1,0,.25,1,float('nan'),float('inf')]):
            expected=0<rate<=500000000 and 0<seconds<=4 and rate*seconds<=2100000000 and 0<fraction<1
            r=self.post(rate=rate,seconds=seconds,fraction=fraction)
            self.assertEqual(r.status_code,200 if expected else 400,(rate,seconds,fraction,r.json));self.release();count+=1
        self.assertEqual(count,336)
    def test_all_120_channel_permutations_and_20_duplicate_orders(self):
        for channels in itertools.permutations(range(5)):
            r=self.post(seconds=.001,clock=channels[0],data=list(channels[1:]));self.assertEqual(r.status_code,200,r.json);self.release()
        for a,b in itertools.permutations(range(5),2):
            channels=list(range(5));channels[b]=channels[a]
            self.assertEqual(self.post(clock=channels[0],data=channels[1:]).status_code,400)
    def test_25_simultaneous_capture_pairs_only_one_admitted(self):
        real=server.new_job
        def delayed(*args,**kwargs):time.sleep(.002);return real(*args,**kwargs)
        for _ in range(25):
            with patch.object(server,'new_job',side_effect=delayed),concurrent.futures.ThreadPoolExecutor(2) as pool:
                r=list(pool.map(lambda _:self.post(seconds=.001),range(2)))
            self.assertEqual(sorted(x.status_code for x in r),[200,400]);self.release()
    def test_22_job_phase_kind_cleanup_stop_orders(self):
        count=0
        for phase,kind,pending in itertools.product(['queued','starting','armed','saving','decoding','done','cancelled','error'],['capture','decode'],[False,True]):
            if pending and phase not in ('error','done','cancelled'):continue
            jid=server.new_job('capture-stop',phase,kind=kind);server.jobs[jid]['cleanupPending']=pending
            with patch.object(server,'stop_owned_la') as stop:
                r=server.app.test_client().post('/api/stop/'+jid)
            allowed=kind=='capture' and (phase in ('queued','starting','armed','done','cancelled','error') or pending)
            self.assertEqual(r.status_code,200 if allowed else 400,(phase,kind,pending,r.json))
            if kind=='decode':stop.assert_not_called()
            count+=1
        self.assertEqual(count,22)
    def test_metadata_100_writes_and_1000_reads_are_never_partial_json(self):
        sid='atomic';server.save(sid,{'input':'capture.csv','value':-1});errors=[]
        def writer(index):
            for j in range(25):server.save(sid,{'input':'capture.csv','value':index*25+j,'payload':'中文'*1000})
        def reader():
            for _ in range(1000):
                try:self.assertIsInstance(server.meta(sid)['value'],int)
                except Exception as e:errors.append(str(e))
        with concurrent.futures.ThreadPoolExecutor(5) as pool:
            tasks=[pool.submit(writer,i) for i in range(4)]+[pool.submit(reader)]
            for task in tasks:task.result(timeout=20)
        self.assertEqual(errors,[]);self.assertEqual(list(server.directory(sid).glob('*.tmp')),[])
    def test_untrusted_host_and_foreign_input_paths_are_rejected(self):
        for host in ['attacker.example:8878','localhost.evil:8878','127.0.0.1.evil:8878']:
            self.assertEqual(server.app.test_client().get('/api/records',headers={'Host':host}).status_code,403)
        folder=server.directory('foreign');folder.mkdir();(folder/'session.json').write_text(json.dumps({'input':'../../secret.txt'}))
        self.assertEqual(server.app.test_client().get('/api/download/foreign/raw').status_code,400)

if __name__=='__main__':unittest.main()
