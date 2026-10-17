import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from validate_flow import validate_flow

class FlowValidationTests(unittest.TestCase):
    def ins(self,seq,pc,flow='linear',mnemonic='nop',operands='',executed=True):
        return dict(kind='instruction',seq=seq,pc=pc,size=2,flow=flow,mnemonic=mnemonic,operands=operands,executed=executed)
    def test_direct_transfers_and_real_discontinuities(self):
        events=[self.ins(0,0x100,'call','bl','#0x200'),self.ins(1,0x200),self.ins(2,0x202),self.ins(3,0x206)]
        result=validate_flow(events);self.assertEqual(result['checks'],3);self.assertEqual(result['violations'],1)
    def test_indirect_load_offsets_and_unknown_predicates_are_not_targets(self):
        events=[self.ins(0,0x100,'branch','ldr','pc, [sp], #4'),self.ins(1,0x200,executed=None),
                self.ins(2,0x300),dict(kind='event',seq=3),self.ins(4,0x400)]
        self.assertEqual(validate_flow(events)['checks'],0)
