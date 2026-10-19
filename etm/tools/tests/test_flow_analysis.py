import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flow_analysis import mark_execution, predicate_addresses, possible_predicates, analyze_calls, byte_accounting


def instruction(pc, fn, code='00bf', executed=True):
    return dict(kind='instruction',pc=pc,size=len(bytes.fromhex(code)),bytes=code,
                executed=executed,function=fn,file=fn+'.c',line=1,seq=pc,offset=0)


class AnalysisTests(unittest.TestCase):
    def test_linear_batch_preserves_calls_unknowns_and_interrupt_context(self):
        from flow_analysis import CallTracker
        for state in (True,False,None):
            for interrupted in (False,True):
                a,b=CallTracker(),CallTracker()
                for tracker in (a,b):
                    tracker.feed_instruction('main','main.c',1,True,'call',0x100,2,3)
                    if interrupted:tracker.feed_event(9,64,15,0x200)
                for pc in range(0x200,0x20a,2):a.feed_instruction('leaf','leaf.c',2,state,'linear',pc,2,0)
                b.feed_linear_many('leaf','leaf.c',2,state,0x200,2,0x208,2,5)
                for tracker in (a,b):
                    tracker.feed_instruction('leaf','leaf.c',3,True,'return',0x20a,2,7)
                    if interrupted:tracker.feed_event(10)
                    tracker.feed_instruction('main','main.c',4,True,'linear',0x102,2,2)
                self.assertEqual(a.finish(),b.finish())

    def test_it_predicate_is_unknown_even_if_generic_range_marked_true(self):
        class Image:
            functions=[(0x1000,8,'fn')]
            def read(self,a,size):return bytes.fromhex('002808bf01217047')[a-0x1000:a-0x1000+size]
        predicates=predicate_addresses(Image())
        self.assertEqual(predicates,{0x1004})
        events=[instruction(0x1002,'fn','08bf'),instruction(0x1004,'fn','0121')]
        mark_execution(events,predicates)
        self.assertTrue(events[0]['executed'])
        self.assertIsNone(events[1]['executed'])
        self.assertEqual(events[1]['executionState'],'unknown')

    def test_calls_are_bl_or_blx_not_function_switches_or_ble(self):
        events=[instruction(0x1000,'A','00f000f8'),instruction(0x1004,'B','7047'),
                instruction(0x1004,'A'),instruction(0x2000,'C','00dd'),instruction(0x2002,'C')]
        mark_execution(events)
        self.assertEqual(events[0]['flow'],'call')
        self.assertNotEqual(events[3]['flow'],'call')
        r=analyze_calls(events)
        self.assertEqual(r['observedCalls'],1)
        self.assertEqual(next(f for f in r['functions'] if f['function']=='B')['closedCalls'],1)

    def test_stripped_image_starting_inside_it_remains_unknown(self):
        class Image:
            functions=[]
            def read(self,a,size):
                data=bytes.fromhex('002808bf01217047')
                return data[a-0x1000:a-0x1000+size] if 0x1000<=a<0x1008 else b''
        e=instruction(0x1004,'fn','0121')
        mark_execution([e],possible_predicates(Image(),[e]))
        self.assertIsNone(e['executed'])

    def test_exception_return_to_other_context_is_not_a_tail_call(self):
        events=[instruction(0x1000,'TaskA','00e0'),dict(kind='event',type=9,payload=14,flags=64,end=0x1004),
                instruction(0x3000,'ISR'),dict(kind='event',type=10,payload=0),instruction(0x4000,'TaskB')]
        mark_execution(events)
        r=analyze_calls(events)
        self.assertFalse(r['edges'])
        self.assertEqual(events[-1]['callPath'],['采集前调用栈未知','TaskB'])

    def test_recursion_and_exception_restore_parent_stack(self):
        a=instruction(0x1000,'A','00f000f8');b=instruction(0x2000,'A','00f000f8')
        events=[a,b,dict(kind='event',type=9,payload=15),instruction(0x3000,'ISR','00f000f8'),
                instruction(0x4000,'H','7047'),instruction(0x3004,'ISR'),
                dict(kind='event',type=10,payload=0),instruction(0x5000,'B','7047'),
                instruction(0x2004,'A','7047'),instruction(0x1004,'A')]
        mark_execution(events)
        r=analyze_calls(events)
        self.assertEqual(r['observedCalls'],3)
        self.assertEqual(events[-1]['callPath'],['采集前调用栈未知','A'])
        self.assertIn('中断 15',events[4]['callPath'])
        self.assertNotIn('中断 15',events[7]['callPath'])

    def test_gap_breaks_call_stack_and_marks_unclosed_call(self):
        events=[instruction(0x1000,'A','00f000f8'),instruction(0x2000,'B'),
                dict(kind='event',type=1,payload=3),instruction(0x1004,'A')]
        mark_execution(events)
        r=analyze_calls(events)
        self.assertEqual(next(f for f in r['functions'] if f['function']=='B')['openCalls'],1)
        self.assertEqual(events[-1]['callPath'],['采集前调用栈未知','A'])
        self.assertIsNone(r['callsPerSecond'])

    def test_tail_transfer_does_not_inflate_normal_call_counts(self):
        events=[instruction(0x1000,'A','00f000f8'),instruction(0x2000,'B','00e0'),
                instruction(0x3000,'C','7047'),instruction(0x1004,'A')]
        mark_execution(events)
        r=analyze_calls(events)
        self.assertEqual(r['observedCalls'],1)
        c=next(f for f in r['functions'] if f['function']=='C')
        self.assertEqual(c['calls'],0)
        self.assertEqual(c['tailTransfers'],1)
        self.assertEqual(next(f for f in r['functions'] if f['function']=='B')['closedCalls'],1)

    def test_blx_register_call_is_counted(self):
        events=[instruction(0x1000,'A','9847'),instruction(0x2000,'B','7047'),instruction(0x1002,'A')]
        mark_execution(events)
        r=analyze_calls(events)
        self.assertEqual(r['observedCalls'],1)
        self.assertEqual(next(f for f in r['functions'] if f['function']=='B')['closedCalls'],1)

    def test_byte_accounting_detects_holes_overlap_and_unsynced_data(self):
        r=dict(status={'processed':10,'lastErrorCode':0},packets=[
            dict(offset=0,size=3,text='I_NOT_SYNC : unknown'),
            dict(offset=2,size=3,text='I_ATOM_F1 : E'),
            dict(offset=7,size=3,text='I_OVERFLOW : overflow')],events=[],elements=[],framing={})
        q=byte_accounting(r)
        self.assertEqual(q['accountedBytes'],8)
        self.assertEqual(q['overlapBytes'],1)
        self.assertEqual(q['unaccountedBytes'],2)
        self.assertEqual(q['unsynchronizedBytes'],3)
        self.assertEqual(q['overflowPackets'],1)
    def test_partial_decoder_progress_cannot_look_like_complete_input(self):
        r=dict(inputBytes=10,status={'processed':4,'lastErrorCode':1},packets=[dict(offset=0,size=4,text='ATOM')],events=[],elements=[],framing={})
        q=byte_accounting(r);self.assertEqual(q['inputBytes'],10);self.assertEqual(q['unprocessedBytes'],6);self.assertEqual(q['unaccountedBytes'],6)


if __name__=='__main__':unittest.main()
