"""Independent expansion oracle: folding must reconstruct the observed raw path."""
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import trace_store as ts
import native_export
import reading_projection as reading
import reading_reference
from source_reading import CExpression,Integer,MacroData,statements
from test_trace_store import build_events


class ProjectionTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def project(self,spec,**overrides):
        folder=self.root/str(len(list(self.root.iterdir())));folder.mkdir()
        prototype=next(e for e in build_events(cycles=False)[0] if e['kind']=='instruction')
        events=[]
        for item in spec:
            seq=len(events)
            if isinstance(item,str):
                etype={'irq':9,'irq-return':10,'gap':6,'end':3}[item]
                events.append(dict(kind='event',seq=seq,type=etype,offset=0,text=item,payload=1,start=0,end=0,flags=0));continue
            pc,fn,line,flow,state,valid=item
            event=dict(prototype,seq=seq,offset=0,pc=pc,size=2,bytes='00bf',mnemonic='nop',operands='',function=fn,file='main.c' if valid else '',line=line if valid else None,flow=flow,conditional=state is not True,executed=state,executionState=ts.STATE_FROM_EXECUTED[state],executionReason=ts.IT_REASON if state is None else '',callPath=[])
            events.append(event)
        ins=[e for e in events if e['kind']=='instruction']
        summary=dict(instructions=len(ins),executed=sum(e['executed'] is True for e in ins),unknownExecution=sum(e['executed'] is None for e in ins),exceptions=sum(e.get('type')==9 for e in events),gaps=sum(e.get('type') in (1,6,7,8) for e in events))
        (folder/'decoded.json').write_text(json.dumps(dict(events=events,packets=[],elements=[],summary=summary,quality={},sources={'main.c':'A();\nB();\nC();\nD();\n'})),encoding='utf8')
        (folder/'etm.bin').write_bytes(b'fixture');(folder/'session.json').write_text('{}')
        ts.write(folder,folder/'store',verify_image=False,verify_elements=False)
        config=reading.options(overrides)
        with ts.TraceStore(folder/'store') as store:
            entries=[]
            for e in store.static:
                entries.append((e.pc,e.size,int(e.predicated),int(bool(e.file)),e.line or 0,0,{'main':1,'leaf':2,'irq':3}.get(e.function,0),ts.FLOW_CODES[e.flow],900 if e.predicated else 0))
            plan=folder/'plan';output=folder/'nodes';reading.plan(store,entries,plan,0,len(events)-1,config)
            p=subprocess.run([str(native_export.binary_path()),'--project',str(plan),str(store.container.path),str(output)],capture_output=True)
            self.assertEqual(p.returncode,0,p.stderr.decode())
            report=json.loads(p.stdout);reading.validate(output,0,len(events)-1,dict(report,repeatThreshold=config['repeatThreshold']))
            nodes=list(reading.nodes(output));byPC={e.pc:i for i,e in enumerate(store.static)}
            self.assertEqual(nodes,reading_reference.project(events,entries,list(store.strings),config))
        # Reconstruct every code/state slot, including all hidden iterations.
        represented={};boundaries=[]
        def expand(n,shift=0):
            if n['kind']=='repeat':
                for iteration in range(n['totalIterations']):
                    for child in n['pattern']:expand(child,iteration*n['stride'])
            elif n['kind']=='event':boundaries.append(n['seq'])
            elif n['kind']=='library':
                values=events[n['seq']+shift:n['endSeq']+shift+1]
                self.assertTrue(all(e['kind']=='instruction' for e in values))
                self.assertEqual(n['unknown'],sum(e['executed'] is None for e in values))
                for e in values:represented[e['seq']]=byPC[e['pc']]*3+{False:0,True:1,None:2}[e['executed']]
            else:
                for i,code in enumerate(n['path']):
                    self.assertNotIn(n['seq']+shift+i,represented)
                    represented[n['seq']+shift+i]=code
        for n in nodes:expand(n)
        self.assertEqual(boundaries,[e['seq'] for e in events if e['kind']=='event'])
        self.assertEqual(represented,{e['seq']:byPC[e['pc']]*3+{False:0,True:1,None:2}[e['executed']] for e in ins})
        return nodes,report

    def abc(self,n):return [(0x100+i*2,'main',i+1,'linear',True,True) for _ in range(n) for i in range(3)]

    def test_ten_does_not_fold_eleven_keeps_two_and_last(self):
        for iterations in (10,11,100):
            nodes,stats=self.project(self.abc(iterations))
            repeats=[n for n in nodes if n['kind']=='repeat']
            self.assertEqual(len(repeats),int(iterations>10))
            if repeats:
                n=repeats[0];self.assertEqual(n['totalIterations'],iterations)
                self.assertEqual(n['hiddenIterations'],iterations-3);self.assertEqual(n['hiddenInstructionCount'],3*(iterations-3))

    def test_irq_gap_unknown_and_incomplete_suffix_are_preserved(self):
        unknown=(0x300,'main',4,'linear',None,True)
        spec=self.abc(12)+['irq',(0x400,'irq',1,'linear',True,True),'irq-return']+self.abc(12)+['gap',unknown,unknown]+self.abc(11)+self.abc(1)[:2]
        nodes,stats=self.project(spec)
        self.assertEqual(stats['unknown'],2);self.assertTrue(any(n['kind']=='unknown' and n['unknown']==2 for n in nodes))
        self.assertEqual(nodes[-1]['endSeq'],len(spec)-1)

    def test_call_return_context_closes_across_functions(self):
        period=[(0x100,'main',1,'call',True,True),(0x200,'leaf',2,'return',True,True),(0x102,'main',3,'branch',True,True)]
        nodes,_=self.project(period*20)
        self.assertTrue(any(n['kind']=='repeat' and n['totalIterations']==20 for n in nodes))
        # Starting inside a child must not fold a return/new-call interval as
        # though it were one closed dynamic invocation.
        self.project(period[1:]+period*15)

    def test_recursive_frames_and_unmodelled_function_changes_do_not_merge(self):
        nodes,_=self.project([(0x100,'main',1,'call',True,True)]*15)
        self.assertFalse(any(n['kind']=='repeat' for n in nodes))

    def test_observed_tail_transfer_preserves_callers_frame_and_return(self):
        period=[(0x100,'main',1,'call',True,True),(0x200,'leaf',2,'branch',True,True),
                (0x300,'irq',3,'return',True,True),(0x102,'main',4,'branch',True,True)]
        nodes,_=self.project(period*20)
        self.assertTrue(any(n['kind']=='repeat' and n['totalIterations']==20 for n in nodes))

    def test_outer_repeat_survives_short_inner_repeat_and_closed_library(self):
        period=[(0x100,'main',1,'call',True,True),(0x200,'leaf',0,'return',True,False),
                (0x102,'main',2,'linear',True,True),(0x104,'main',3,'linear',True,True),
                (0x104,'main',3,'linear',True,True),(0x106,'main',4,'branch',True,True)]
        nodes,_=self.project(period*20)
        self.assertTrue(any(n['kind']=='repeat' and n['totalIterations']==20 for n in nodes))
        nodes,_=self.project([(0x100,'main',1,'linear',True,True),(0x200,'leaf',1,'linear',True,True)]*15)
        self.assertFalse(any(n['kind']=='repeat' for n in nodes))

    def test_library_complete_partial_nested_unknown_and_interrupt(self):
        call=(0x100,'main',1,'call',True,True);library=(0x200,'leaf',0,'return',True,False);resume=(0x102,'main',2,'linear',True,True)
        nodes,_=self.project([call,library,resume]);lib=next(n for n in nodes if n['kind']=='library')
        self.assertFalse(lib['partial']);self.assertTrue(lib['returnObserved'])
        nodes,_=self.project([library,resume]);self.assertTrue(nodes[0]['partial']);self.assertFalse(nodes[0]['returnObserved'])
        nodes,stats=self.project([call,(0x202,'leaf',0,'linear',None,False),'irq',(0x300,'irq',1,'linear',True,True),'irq-return',library,resume])
        self.assertEqual(stats['unknown'],1);self.assertEqual(sum(n['kind']=='event' for n in nodes),2)

    def test_switches_and_not_taken_are_auditable(self):
        spec=self.abc(15);spec.insert(6,(0x300,'main',4,'branch',False,True))
        nodes,stats=self.project(spec,foldRepeats=False)
        self.assertFalse(any(n['kind']=='repeat' for n in nodes));self.assertEqual(stats['notTaken'],1)
        nodes,_=self.project([(0x100,'main',1,'unknown',True,True)]*15)
        self.assertFalse(any(n['kind']=='repeat' for n in nodes))

    def test_modified_reading_plan_is_rejected_before_output_is_created(self):
        self.project(self.abc(11))
        plan=next(self.root.glob('*/plan'));bad=plan.parent/'bad-plan';raw=bytearray(plan.read_bytes());raw[-1]^=1;bad.write_bytes(raw)
        store=next((plan.parent/'store/generations').glob('*/store.bin'));out=plan.parent/'rejected-output'
        p=subprocess.run([str(native_export.binary_path()),'--project',str(bad),str(store),str(out)],capture_output=True)
        self.assertNotEqual(p.returncode,0);self.assertIn(b'SHA256 mismatch',p.stderr);self.assertFalse(out.exists())


class SourceTests(unittest.TestCase):
    def test_inline_macro_values_preserve_strings_comments_and_other_identifiers(self):
        from source_reading import inline_constants
        text='f(MASK, MASK2, "MASK", \'M\'); /* MASK */ // MASK\nMASK | MASK;'
        self.assertEqual(inline_constants(text,{'MASK':'0x32'}),'f(MASK/*0x32*/, MASK2, "MASK", \'M\'); /* MASK */ // MASK\nMASK/*0x32*/ | MASK/*0x32*/;')

    def test_repeat_comment_covers_only_hidden_iterations_and_retains_seek_template(self):
        from unittest.mock import patch
        meta=dict(static=[dict(text='use(MASK);',inlineConstants={'MASK':'0x32'},file='main.c',firstLine=1,pc=256,function='main')],events=[],functions=['main'])
        p=dict(kind='code',seq=10,endSeq=11,entry=0,instructions=2)
        n=dict(kind='repeat',seq=10,endSeq=49,totalIterations=20,keepFirst=2,keepLast=1,hiddenIterations=17,stride=2,pattern=[p])
        with patch.object(reading,'nodes',return_value=iter([n])):rows=list(reading.visible_rows(None,meta))
        self.assertEqual([(r['seq'],r['endSeq']) for r in rows],[(10,11),(12,13),(14,47),(48,49)])
        self.assertEqual(rows[0]['text'],'use(MASK/*0x32*/);')
        self.assertEqual(rows[2]['text'],'/* 此执行块共重复 20 次；省略中间 17 次 */')
        self.assertEqual(rows[2]['repeat']['pattern'][0]['seq'],10)

    def test_multiline_calls_strings_comments_for_headers_and_ambiguous_lines(self):
        text='MODIFY_REG(\n  a, /* ; } */\n  b, "semicolon;"\n);\nfor (int i=0;i<2;i++) {\n  f(); g();\n}\n'
        spans=statements(text)
        self.assertEqual(spans[2][0][:2],(1,4));self.assertEqual(spans[2],spans[3])
        self.assertEqual(spans[5][0][2],'for (int i=0;i<2;i++)');self.assertGreater(len(spans[6]),1)

    def test_c_target_integer_semantics_and_refusal(self):
        def value(text):return CExpression(text,lambda n:Integer(5)).value()
        self.assertEqual(value('0xffffffffU + 1').value,0)
        self.assertEqual(value('1U << 31').value,2147483648)
        self.assertEqual(value('~0U').value,4294967295)
        self.assertEqual(value('(uint8_t) 257').value,1)
        self.assertEqual(value('-7 / 3').value,-2);self.assertEqual(value('-7 % 3').value,-1)
        self.assertEqual(value('4294967296U'),Integer(4294967296,64,True))
        self.assertEqual(value('0x100000000U'),Integer(4294967296,64,True))
        for text in ['2147483647+1','1<<32','1/0','(-1)>>1','function(1)','__import__("os")']:
            with self.assertRaises(ValueError,msg=text):value(text)
        for text in ['18446744073709551616ULL','0x10000000000000000ULL',
                     '(-2147483647-1) % -1','(-2147483647-1) / -1']:
            with self.assertRaises(ValueError,msg=text):value(text)

    def test_describe_checks_outer_macro_and_does_not_annotate_enum_members(self):
        from types import SimpleNamespace
        from source_reading import SourceReading
        text='f(FLAG, obj.FLAG, ptr->FLAG, BAD(x), JOIN(a,b), VAR(x), GOOD(runtime));'
        r=SourceReading.__new__(SourceReading)
        r.lines={'main.c':[text]};r.spans={};r.cache={};r.annotationCount=r.unresolved=0
        r.macro=SimpleNamespace(unit=lambda pc:0,enums={0:{'FLAG':3}},
            enumAmbiguity={},knownNames=set(),results={(0,'main.c',1):{
                'BAD':('BAD(x) #x','h',1),'JOIN':('JOIN(a,b) a ## b','h',2),
                'VAR':('VAR(...) __VA_ARGS__','h',3),'GOOD':('GOOD(x) ((x)+1)','h',4)}})
        row=r.describe(SimpleNamespace(file='main.c',line=1,pc=256))
        self.assertEqual(len(row['expansions']),1)
        self.assertIn('runtime',row['expansions'][0]);self.assertEqual(r.unresolved,3)
        # A row-level name->value map cannot distinguish a member token from
        # an enumerator token, so ambiguous occurrences must stay unannotated.
        self.assertNotIn('FLAG',row['inlineConstants'])

    def test_active_macro_wins_over_same_named_enumerator_including_aliases(self):
        from types import SimpleNamespace
        from source_reading import SourceReading
        r=SourceReading.__new__(SourceReading)
        r.lines={'main.c':['use(VALUE, ALIAS);']};r.spans={};r.cache={};r.annotationCount=r.unresolved=0
        r.macro=SimpleNamespace(unit=lambda pc:0,enums={0:{'VALUE':1}},enumAmbiguity={},knownNames=set(),
            results={(0,'main.c',1):{'VALUE':('VALUE 2U','h',1),'ALIAS':('ALIAS VALUE','h',2)}})
        row=r.describe(SimpleNamespace(file='main.c',line=1,pc=256))
        self.assertEqual(row['inlineConstants'],{'VALUE':'0x2','ALIAS':'0x2'})

    def test_macro_scopes_define_undef_import_and_conflicts(self):
        def uv(n):
            out=bytearray()
            while n>=128:out.append((n&127)|128);n>>=7
            out.append(n);return bytes(out)
        def define(line,text):return b'\x01'+uv(line)+text.encode()+b'\0'
        header=struct.pack('<HBI',4,2,0)
        raw=header+b'\x03\0\x01'+define(1,'A 1')+define(3,'A 2')+b'\x02\x05A\0'+b'\x04\0'
        m=MacroData.__new__(MacroData);m.raw=raw;m.strings=b'';m.units={};m.expandedUnits={};m.needed={'A'};m.results={};m.lineFiles={0:{1:'/main.c'}}
        m.replay(0,0,{1:'/main.c'},{('/main.c',2):{'A'},('/main.c',4):{'A'},('/main.c',6):{'A'}})
        self.assertEqual(m.results[(0,'/main.c',2)]['A'][0],'A 1')
        self.assertEqual(m.results[(0,'/main.c',4)]['A'][0],'A 2');self.assertNotIn('A',m.results[(0,'/main.c',6)])

    def test_imported_macro_unit_uses_its_own_line_table(self):
        root=struct.pack('<HBI',4,2,0)+b'\x03\0\x01\x07'+struct.pack('<I',128)+b'\x04\0'
        child=struct.pack('<HBI',4,2,100)+b'\x03\0\x01\x01\x03X 7\0\x04\0'
        m=MacroData.__new__(MacroData);m.raw=root.ljust(128,b'\0')+child;m.strings=b'';m.units={};m.expandedUnits={};m.needed={'X'};m.results={};m.lineFiles={0:{1:'/main.c'},100:{1:'/included.h'}}
        m.replay(1,0,{1:'/main.c'},{('/included.h',4):{'X'}})
        self.assertEqual(m.results[(1,'/included.h',4)]['X'],('X 7','/included.h',3))

    def test_nested_function_macros_expand_without_evaluating_runtime_names(self):
        from source_reading import SourceReading
        r=SourceReading.__new__(SourceReading)
        scope={'READ':('READ(R) (*(R))','header.h',1),'WRITE':('WRITE(R,V) ((*(R))=(V))','header.h',2),
               'MODIFY':('MODIFY(R,C,S) WRITE(R,READ(R)&~(C)|(S))','header.h',3),'MASK':('MASK 3U','header.h',4)}
        value=r.expand('MODIFY(reg, MASK, runtime_value)',scope)
        self.assertIn('runtime_value',value);self.assertIn('3U',value)
        self.assertNotIn('WRITE',value);self.assertNotIn('READ',value)
        with self.assertRaises(ValueError):r.expand('BAD(x)',dict(scope,BAD=('BAD(x) #x','h',1)))


if __name__=='__main__':unittest.main()
