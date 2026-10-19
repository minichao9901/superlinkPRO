"""执行状态过滤回归：只有 executed 指令算确定访问，not-taken/unknown 单独计数。

审查发现 2：correlate() 之前只要源码行匹配就计入 first/count/loads/stores，
not-taken/unknown 指令会被当成已执行的外设访问。这里用最小事件流覆盖：
  · 新 schema（executionState）与旧 schema（executed 布尔）两种口径；
  · not-taken / unknown 不进入 seq/etm_instructions/loads/stores，只进新增列；
  · CSV 既有 12 列顺序不变，新列追加在末尾；
  · 端到端 main() 输出可解析。

跑：python -X utf8 -m unittest discover -s etm/tools/tests -p "test_usb_init_reg_access.py" -q
"""
import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import usb_init_reg_access as tool  # noqa: E402

SOURCE = '''\
#include "usb_dwc2_param.h"
void usb_dc_init(void) {
    USB_LOG_INFO("CID:%08x\\r\\n", (unsigned int)USB_OTG_GLB->CID);
    USB_OTG_GLB->GUSBCFG |= USB_OTG_GUSBCFG_PHYSEL;
}
void dwc2_core_init(void) {
    USB_OTG_GLB->GINTMSK = 0;
}
'''
HEADER = '''\
void dwc2_get_hwparams(void) {
    snpsid = dwc2_readl(reg_base + GSNPSID_OFFSET);
}
'''


def event(seq, pc, mnemonic, line, name, state=None, executed=None):
    value = dict(kind='instruction', seq=seq, pc=pc, size=2, bytes='00bf',
                 mnemonic=mnemonic, operands='', file='E:/src/' + name, line=line,
                 function='usb_dc_init')
    if state is not None:
        value['executionState'] = state
        value['executed'] = {'executed': True, 'not-taken': False, 'unknown': None}[state]
    else:
        value['executed'] = executed
    return value


class ExecutionStateTests(unittest.TestCase):
    def test_state_normalisation(self):
        self.assertEqual(tool.execution_state({'executionState': 'executed'}), 'executed')
        self.assertEqual(tool.execution_state({'executionState': 'not-taken'}), 'not-taken')
        self.assertEqual(tool.execution_state({'executionState': 'unknown'}), 'unknown')
        # 旧 schema 没有 executionState：按 executed 布尔归一。
        self.assertEqual(tool.execution_state({'executed': True}), 'executed')
        self.assertEqual(tool.execution_state({'executed': False}), 'not-taken')
        self.assertEqual(tool.execution_state({'executed': None}), 'unknown')
        self.assertEqual(tool.execution_state({}), 'unknown')

    def _record(self, temp, events, name='events.jsonl'):
        record = Path(temp) / 'record'
        record.mkdir()
        (record / name).write_text('\n'.join(json.dumps(item) for item in events) + '\n',
                                   encoding='utf8')
        return record

    def test_correlate_counts_only_executed(self):
        rows = [dict(file='usb_dc_dwc2.c', line=3, kind='read'),
                dict(file='usb_dc_dwc2.c', line=4, kind='rw')]
        events = [
            event(2, 0x100, 'ldr.w', 3, 'usb_dc_dwc2.c', state='executed'),
            event(3, 0x104, 'ldr.w', 3, 'usb_dc_dwc2.c', state='not-taken'),
            event(5, 0x108, 'ldr.w', 3, 'usb_dc_dwc2.c', state='unknown'),
            event(7, 0x10c, 'str.w', 3, 'usb_dc_dwc2.c', state='executed'),
            event(9, 0x110, 'ldr.w', 4, 'usb_dc_dwc2.c', state='not-taken'),
        ]
        with tempfile.TemporaryDirectory() as temp:
            record = self._record(temp, events)
            info = tool.correlate(rows, record)
        confirmed = info[('usb_dc_dwc2.c', 3)]
        self.assertEqual(confirmed['count'], 2)                 # 两个 executed
        self.assertEqual(confirmed['first'], 2)
        self.assertEqual(confirmed['loads'], [0x100])           # not-taken/unknown 的 ldr 不算
        self.assertEqual(confirmed['stores'], [0x10c])
        self.assertEqual(confirmed['not_taken'], 1)
        self.assertEqual(confirmed['unknown'], 1)
        self.assertEqual(confirmed['total'], 4)
        self.assertEqual(confirmed['candidate_first'], 2)
        candidate = info[('usb_dc_dwc2.c', 4)]
        self.assertIsNone(candidate['first'])                   # 只有 not-taken → 无确定访问
        self.assertEqual(candidate['count'], 0)
        self.assertEqual(candidate['loads'], [])
        self.assertEqual(candidate['not_taken'], 1)
        self.assertEqual(candidate['candidate_first'], 9)

    def test_correlate_old_schema_boolean(self):
        rows = [dict(file='usb_dc_dwc2.c', line=3, kind='read')]
        events = [event(1, 0x200, 'ldr', 3, 'usb_dc_dwc2.c', executed=True),
                  event(2, 0x204, 'ldr', 3, 'usb_dc_dwc2.c', executed=False),
                  event(3, 0x208, 'ldr', 3, 'usb_dc_dwc2.c', executed=None)]
        with tempfile.TemporaryDirectory() as temp:
            record = self._record(temp, [])
            (record / 'events.jsonl').unlink()
            (record / 'decoded.json').write_text(json.dumps({'events': events}), encoding='utf8')
            info = tool.correlate(rows, record)
        item = info[('usb_dc_dwc2.c', 3)]
        self.assertEqual((item['count'], item['first'], item['loads']), (1, 1, [0x200]))
        self.assertEqual((item['not_taken'], item['unknown'], item['total']), (1, 1, 3))


class MainTests(unittest.TestCase):
    def test_main_writes_confirmed_and_candidate_columns(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source, header = temp / 'usb_dc_dwc2.c', temp / 'usb_dwc2_param.h'
            source.write_text(SOURCE, encoding='utf8')
            header.write_text(HEADER, encoding='utf8')
            rows = tool.scan_source(source, set(tool.DEFAULT_FUNCTIONS)) + tool.scan_header(header)
            self.assertTrue(rows)
            events = []
            seq = 0
            for row in rows:
                state = 'executed' if row['line'] % 2 else 'not-taken'
                events.append(event(seq, 0x300 + seq, 'ldr' if row['kind'] == 'read' else 'str',
                                    row['line'], row['file'], state=state))
                seq += 2
            record = temp / 'record'
            record.mkdir()
            (record / 'events.jsonl').write_text(
                '\n'.join(json.dumps(item) for item in events) + '\n', encoding='utf8')
            out = temp / 'out.csv'
            argv = sys.argv
            sys.argv = ['usb_init_reg_access.py', '--source', str(source), '--header', str(header),
                        '--record', str(record), '--out', str(out)]
            try:
                with contextlib.redirect_stdout(io.StringIO()) as captured:
                    tool.main()
            finally:
                sys.argv = argv
            self.assertIn('confirmed executed accesses', captured.getvalue())
            self.assertIn('not-taken matches', captured.getvalue())
            with out.open(encoding='utf8', newline='') as stream:
                table = list(csv.DictReader(stream))
            self.assertEqual(len(table), len(rows))
            # 既有 12 列保持原顺序，新列追加在末尾。
            self.assertEqual(list(table[0].keys())[:12],
                             ['seq', 'function', 'file', 'line', 'kind', 'register',
                              'etm_instructions', 'etm_loads', 'etm_stores',
                              'load_pcs', 'store_pcs', 'source'])
            self.assertEqual(list(table[0].keys())[12:], list(tool.EXTRA_COLUMNS))
            executed = [row for row in table if row['seq'].strip()]
            self.assertTrue(executed)
            for row in executed:
                self.assertGreaterEqual(int(row['etm_instructions']), 1)
            for row in table:
                # not-taken/unknown 不混入确定计数：总匹配数 = 确定 + not-taken + unknown。
                self.assertEqual(int(row['etm_instructions_total']),
                                 int(row['etm_instructions']) + int(row['etm_not_taken']) +
                                 int(row['etm_unknown']))
                if not row['seq'].strip():
                    self.assertEqual(int(row['etm_instructions']), 0)
                    self.assertEqual(int(row['etm_loads']), 0)
                    self.assertEqual(int(row['etm_stores']), 0)


if __name__ == '__main__':
    unittest.main()
