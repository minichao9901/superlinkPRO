import copy,json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from record_store import RecordStore
from flow_analysis import analyze_calls
def ins(seq,pc,fn,line,flow='linear',state='executed'):
    return dict(seq=seq,kind='instruction',pc=pc,function=fn,file=fn+'.c',line=line,flow=flow,
                size=2,executed=None if state=='unknown' else state=='executed',executionState=state,offset=seq,
                mnemonic='nop',operands='',bytes='00bf')
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.folder=Path(self.tmp.name)
        events=[dict(seq=0,kind='event',type=1,offset=0,text='NO_SYNC'),ins(1,0x100,'main',10,'call'),
                ins(2,0x200,'leaf',1),ins(3,0x202,'leaf',2,'return'),ins(4,0x102,'main',11),
                ins(5,0x104,'main',12,state='unknown'),ins(6,0x106,'main',12,state='unknown'),
                dict(seq=7,kind='event',type=8,offset=7,text='gap'),ins(8,0x200,'leaf',1)]
        analyze_calls(events)
        self.data=dict(events=events,packets=[dict(offset=0,size=2,text='I_TRACE_INFO',hex='0001')],
            elements=[],sources={'main.c':'line1\nline2'},summary={},quality={},analysis={})
        (self.folder/'decoded.json').write_text(json.dumps(self.data),encoding='utf8')
        self.store=RecordStore(self.folder,lambda:copy.deepcopy(self.data))
    def tearDown(self):self.tmp.cleanup()
    def test_rebuild_waits_for_an_existing_windows_reader_to_close(self):
        import threading,sqlite3,time
        self.store.ensure();ready=threading.Event()
        def reader():
            db=sqlite3.connect(self.store.path);db.execute('SELECT * FROM meta').fetchall();ready.set();time.sleep(.3);db.close()
        worker=threading.Thread(target=reader);worker.start();self.assertTrue(ready.wait(2))
        (self.folder/'decoded.json').write_text(json.dumps(self.data)+' ',encoding='utf8')
        try:self.store.ensure();self.assertTrue(self.store.ready())
        finally:worker.join(2)
    def test_navigation_and_unknown_boundary_do_not_stitch_stacks(self):
        self.assertEqual(self.store.navigate(0,1,'call')['seq'],1)
        self.assertEqual(self.store.navigate(2,1,'return')['seq'],3)
        self.assertEqual(self.store.navigate(1,1,'source')['seq'],2)
        self.assertEqual(self.store.event(2)['callPath'],['采集前调用栈未知','main','leaf'])
        self.assertEqual(self.store.event(4)['callPath'],['采集前调用栈未知','main'])
        self.assertEqual(self.store.event(8)['callPath'],['采集前调用栈未知','leaf'])
        self.assertIsNone(self.store.event(5)['event']['executed'])
    def test_pages_filters_and_repeated_pc_wrap(self):
        p=self.store.page(limit=2,search='leaf');self.assertEqual(p['total'],3);self.assertEqual([e['seq'] for e in p['rows']],[2,3])
        self.assertEqual(self.store.navigate(2,pc=0x200)['seq'],8)
        self.assertTrue(self.store.navigate(8,pc=0x200)['wrapped'])
        self.assertEqual(self.store.page(search='%')['total'],0)
        self.assertEqual(self.store.source('main.c')['text'],'line1\nline2')
        self.assertNotIn('events',self.store.overview());self.assertNotIn('sources',self.store.overview())
    def test_unknown_runs_keep_offsets_and_index_is_rebuildable(self):
        p=next(p for p in self.store.issues()['rows'] if p['category']=='predicate')
        self.assertEqual((p['startSeq'],p['endSeq'],p['records'],p['offset']),(5,6,2,5))
        (self.folder/'decoded.json').write_text(json.dumps(self.data)+' ',encoding='utf8')
        self.assertFalse(self.store.ready());self.store.ensure();self.assertTrue(self.store.ready())
    def test_nested_interrupts_resume_correct_context(self):
        es=[ins(0,0x100,'main',1,'call'),ins(1,0x200,'leaf',1),dict(seq=2,kind='event',type=9,payload=15,flags=64,end=0x202),
            ins(3,0x300,'SysTick',1),dict(seq=4,kind='event',type=9,payload=16,flags=64,end=0x302),
            ins(5,0x400,'nested',1),dict(seq=6,kind='event',type=10),ins(7,0x302,'SysTick',2),
            dict(seq=8,kind='event',type=10),ins(9,0x202,'leaf',2)]
        analyze_calls(es)
        self.assertEqual(es[5]['callPath'],['中断 16','nested'])
        self.assertEqual(es[7]['callPath'],['中断 15','SysTick'])
        self.assertEqual(es[9]['callPath'],['采集前调用栈未知','main','leaf'])
    def test_damaged_packet_coverage_keeps_exact_byte_locations(self):
        self.data['quality']['processedBytes']=10
        self.data['packets']=[dict(offset=0,size=3,text='I_NOT_SYNC'),dict(offset=2,size=3,text='ATOM'),dict(offset=7,size=3,text='I_OVERFLOW')]
        rows=self.store.issues()['rows']
        self.assertTrue(any(p['category']=='gap' and p['offset']==5 and p['bytes']==2 for p in rows))
        self.assertTrue(any(p['category']=='overlap' and p['offset']==2 and p['bytes']==1 for p in rows))
        self.assertTrue(any(p['category']=='packet' and p['offset']==7 for p in rows))
        self.assertTrue(all(p['startSeq'] is None for p in rows if p['category'] in ('packet','gap','overlap')))
    def test_raw_input_change_invalidates_cached_index(self):
        raw=self.folder/'capture.csv';raw.write_text('original');self.store.ensure();self.assertTrue(self.store.ready())
        raw.write_text('changed-input');self.assertFalse(self.store.ready())
    def test_local_execution_window_inherits_stack_and_never_invents_time(self):
        view=self.store.window(2,4)
        self.assertEqual(view['analysis']['flame']['value'],3)
        self.assertEqual(view['analysis']['observedCalls'],0)  # actual call is outside this selection
        self.assertFalse(view['timing']['verifiedTime'])
        self.assertEqual(view['timeline'][0]['path'],['采集前调用栈未知','main','leaf'])
        self.assertEqual([e['seq'] for e in self.store.page(start=2,end=4)['rows']],[2,3,4])
        self.assertIsNone(self.store.navigate(3,start=2,end=3)['seq'])
        self.assertEqual(self.store.navigate(3,pc=0x200,start=2,end=4)['seq'],2)
        self.assertEqual(self.store.window(5,6)['analysis']['functions'][0]['unknownExecution'],2)
    def test_function_timeline_aggregates_dense_markers_across_the_whole_range(self):
        self.data['events']=[dict(seq=i,kind='event',type=9 if i%2 else 1,text='marker') for i in range(6001)]
        (self.folder/'decoded.json').write_text(json.dumps(self.data),encoding='utf8')
        self.store.ensure()
        view=self.store.function_timeline(bins=240)
        self.assertEqual(view['markerCount'],6001)
        self.assertLessEqual(len(view['markers']),240)
        self.assertGreater(view['markers'][-1]['seq'],5900)
        self.assertEqual(sum(marker['count'] for marker in view['markers']),6001)
        self.assertEqual(sum(sum(marker['types'].values()) for marker in view['markers']),6001)
    def test_sidecar_fingerprint_rejects_equal_length_rewrite(self):
        # 审查 F03：只比 size/行数时，events.jsonl 被等长改写（甚至保持 mtime）仍会复用旧 sidecar，
        # 查询列来自旧事件、按偏移读回的 JSON 来自新事件 → 自相矛盾。v2 用 sha256 绑定内容。
        import hashlib,os
        folder=Path(self.tmp.name);source=folder/'events.jsonl';sidecar=folder/'browse-columns.tsv'
        body=b'{"seq":0,"kind":"event"}\n{"seq":1,"kind":"event"}\n'
        source.write_bytes(body);stat=source.stat()
        header=('#etm-browse-columns 2 events=%020d rows=%010d mtime=%020d sha256=%064s\n'
                % (stat.st_size,2,stat.st_mtime_ns,hashlib.sha256(body).hexdigest()))
        sidecar.write_text(header+'0\tevent\n1\tevent\n',encoding='utf-8')
        class Events:
            def __len__(self):return 2
        store=RecordStore(folder,lambda:None)
        self.assertTrue(store._sidecar_fresh(sidecar,source,Events()))
        # 等长改写 + 强制保持 mtime：只有 sha256 能发现
        source.write_bytes(body.replace(b'event',b'evxnt'));os.utime(source,ns=(stat.st_atime_ns,stat.st_mtime_ns))
        self.assertFalse(store._sidecar_fresh(sidecar,source,Events()))
        # v1 头部（只有 size/行数）一律视为过期，强制重建
        sidecar.write_text('#etm-browse-columns 1 events=%020d rows=%010d\n0\tevent\n1\tevent\n'
                           % (stat.st_size,2),encoding='utf-8')
        self.assertFalse(store._sidecar_fresh(sidecar,source,Events()))
    def test_window_results_are_cached_until_the_index_is_rebuilt(self):
        # 全记录 window() 实测 3.86 M 事件 119.5 s；同一区间第二次必须立即返回（审查 P2）。
        self.store.ensure()
        first=self.store.window(0,8);second=self.store.window(0,8)
        self.assertEqual(first,second);self.assertTrue(self.store._windows)
        (self.folder/'decoded.json').write_text(json.dumps(self.data)+' ',encoding='utf8')
        self.store.ensure()
        self.assertEqual(self.store._windows,{})
if __name__=='__main__':unittest.main()
