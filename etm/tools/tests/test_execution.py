import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
from decode_trace import export_source


class ExecutionTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / 'decoded/500m/decoded.json').exists() and (ROOT / 'decoded/100m/decoded.json').exists(), '缺少封存的 decoded/500m|100m 样本（gitignored，干净克隆里没有）')
    def test_real_samples_branch_targets_workload_calls_and_exceptions(self):
        for sample in ('500m', '100m'):
            r = json.loads((ROOT / f'decoded/{sample}/decoded.json').read_text(encoding='utf-8'))
            self.assertEqual(r['status']['lastErrorCode'], 0)
            self.assertGreater(r['summary']['instructions'], 8000)
            self.assertEqual(r['summary']['instructions'], r['summary']['sourceMapped'])
            events = r['events']
            direct = conditional = complete_rounds = 0
            calls = None
            for index, e in enumerate(events[:-1]):
                if e['kind'] != 'instruction':
                    continue
                if e['pc'] == 0x08000050:
                    if calls is not None:
                        self.assertEqual(len(calls), 8)
                        self.assertTrue(all(a != b for a, b in zip(calls, calls[1:])))
                        complete_rounds += 1
                    calls = []
                if e['mnemonic'] == 'bl' and e['operands'] in ('#0x8000040', '#0x8000048') and calls is not None:
                    calls.append(e['operands'])
                following = events[index+1]
                if following['kind'] != 'instruction':
                    continue
                branch = re.fullmatch(r'#(0x[0-9a-f]+)', e['operands'])
                if e['mnemonic'].startswith('b') and branch:
                    expected = int(branch[1], 16) if e['executed'] else e['pc'] + e['size']
                    self.assertEqual(following['pc'], expected, (sample, e, following))
                    direct += 1
                    conditional += not e['executed']
            exceptions = [e for e in r['elements'] if e['type'] == 9]
            self.assertTrue(all(e['payload'] == 15 for e in exceptions))
            self.assertEqual(len(exceptions), sum(e['type'] == 10 for e in r['elements']))
            self.assertGreater(direct, 2000)
            self.assertGreater(conditional, 500)
            self.assertGreater(complete_rounds, 5)
            if 'analysis' in r:
                by_name={f['function']:f for f in r['analysis']['functions']}
                self.assertEqual(by_name['trace_even']['calls'],sum(e['mnemonic']=='bl' and e['operands']=='#0x8000040' for e in events if e['kind']=='instruction'))
                self.assertEqual(by_name['trace_odd']['calls'],sum(e['mnemonic']=='bl' and e['operands']=='#0x8000048' for e in events if e['kind']=='instruction'))
                self.assertEqual(by_name['SysTick_Handler']['interruptEntries'],len(exceptions))
                self.assertEqual(r['analysis']['flame']['value'],r['summary']['instructions'])

    def test_export_preserves_nonconsecutive_returns_and_gaps(self):
        e = lambda seq, line:dict(kind='instruction', seq=seq, pc=0x8000000+seq*2,
                                  executed=True, function='fn', file='main.c', line=line)
        r = dict(sources={'main.c':'one();\ntwo();\n'},
                 events=[e(0,1), e(1,1), e(2,2), e(3,1),
                         dict(kind='event',type=1,text='NO_SYNC'),e(5,1)])
        text = export_source(r, True)
        self.assertEqual(text.count('one();'), 3)
        self.assertIn('NO_SYNC',text)
        self.assertIn('two();',text)


if __name__ == '__main__':
    unittest.main()
