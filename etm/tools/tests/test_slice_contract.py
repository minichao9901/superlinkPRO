"""长窗（自动分窗）记录的**读取契约**回归：不需要 LA、不需要解码器。

审查 R01 现场：`export_slices()` 只合并了 events.jsonl，顶层没有 capture.csv、没有 packets/
elements，`decoded.json` 仍沿用第一片的指纹与流数量。结果是：记录合成"完成"了，但
`result()`/browse 立刻 400，事件数也对不上（JSONL 计数 2 vs 实际 4）。

这里用两片假数据驱动生产的 `finalize_slices()`（合并 + 摘要 + 建索引），断言：
  · `streamed.schema=3` 且事件数与合并后的行数一致；
  · `/api/records`、`/api/browse/*/overview|page`、`/api/download/*/json` 都可读；
  · 周期按每片 `analysis.timing.totalCycles` 求和，不再出现 912 那种"被片数放大"的数；
  · 长窗的原始波形按片下载（`?slice=K`），整包下载给出明确契约而不是莫名 400。

跑：python -X utf8 -m unittest discover -s etm/tools/tests -p "test_slice_contract.py"
"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402
from slice_merge import merge_slice_events  # noqa: E402

ELF = ROOT / 'firmware/stm32h743_cdc_trace/fw.elf'


def event(seq, pc, function, cycles=0):
    return dict(kind='instruction', seq=seq, pc=pc, size=2, bytes='00bf', mnemonic='nop',
                operands='', executed=True, function=function, file='main.c', line=seq + 1,
                cycleCount=cycles, cycleSpan=1, flow='linear', conditional=False,
                executionState='executed', executionReason='', callPath=['main'], cycles=cycles)


class SliceRecordContract(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.original = server.RECORDS
        server.RECORDS = Path(self.folder.name)
        self.client = server.app.test_client()
        self.sid = 'capture-sliced-contract'
        self.root = server.directory(self.sid)
        self.root.mkdir(parents=True)
        (self.root / 'firmware.elf').write_bytes(ELF.read_bytes())
        (self.root / 'sources.json').write_text(json.dumps({'main.c': 'int main(void){return 0;}'}),
                                                encoding='utf8')
        self.elf_sha = hashlib.sha256(ELF.read_bytes()).hexdigest()
        self.config = dict(idr0=134219489, idr1=1090581505, idr2=4, idr8=1, idr9=0, idr10=0,
                           idr11=0, idr12=1, idr13=0, config=4113, traceid=2)
        server.save(self.sid, dict(name='长窗契约测试', created=1.0, input='capture.csv', rate=500000000,
                                   seconds=1.0, config=self.config, clock=0, data=[1, 2, 3, 4],
                                   sliceSeconds=0.2, longWindow=True, targetVerified=False,
                                   fraction=.25, glitchNs=0, captureStatus='saved',
                                   libraryOrigin='validation'))

    def tearDown(self):
        server.RECORDS = self.original
        self.folder.cleanup()

    def _slice(self, index, rows, total_cycles, packets, functions):
        folder = self.root / 'slices' / f'{index:02d}'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'events.jsonl').write_text(
            '\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n', encoding='utf8')
        (folder / 'packets.jsonl').write_text('\n'.join(
            json.dumps(dict(kind='packet', offset=i * 4, size=4, text='ATOM')) for i in range(packets)) + '\n',
            encoding='utf8')
        (folder / 'elements.jsonl').write_text('', encoding='utf8')
        (folder / 'capture.csv').write_bytes(b'slice-%d' % index)
        summary = dict(packets=packets, ranges=len(rows), instructions=len(rows), executed=len(rows),
                       sourceMapped=len(rows), exceptions=0, gaps=0, packetIssues=0,
                       functions={f: n for f, n in functions})
        decoded = dict(version=3, protocol='ETMv4', config=self.config, inputBytes=len(rows) * 4,
                       elf=dict(name='fw.elf', sha256=self.elf_sha),
                       summary=summary,
                       status=dict(lastErrorCode=0, processed=len(rows) * 4),
                       timeBasis='execution order', streamed=dict(schema=2, events=len(rows),
                                                                  packets=packets, elements=0),
                       quality=dict(inputBytes=len(rows) * 4, processedBytes=len(rows) * 4,
                                    unprocessedBytes=0, packetIssues=0, unknownExecution=0),
                       analysis=dict(timing=dict(totalCycles=total_cycles, cycleRanges=len(rows),
                                                 executedInstructions=len(rows),
                                                 instructions=len(rows), functions=[]),
                                     functions=[dict(function=f, instructions=n, knownExecuted=n,
                                                     unknownExecution=0, notTaken=0, calls=0,
                                                     closedCalls=0, selfCycles=total_cycles // 2,
                                                     totalCycles=total_cycles // 2,
                                                     cycleRanges=1) for f, n in functions],
                                     hotspots=[], flame=dict(name='已恢复执行区间', self=0, value=0,
                                                             selfCycles=0, valueCycles=0, children=[]),
                                     edges=[], observedCalls=0, unknownStackRestarts=0),
                       capture=dict(name='capture.csv', sha256='per-slice'))
        (folder / 'decoded.json').write_text(json.dumps(decoded, ensure_ascii=False), encoding='utf8')
        (folder / 'sources.json').write_text(json.dumps({'main.c': 'int main(void){return 0;}'}),
                                             encoding='utf8')
        return decoded

    def _finalize(self):
        parts, slices, seq_off = [], [], 0
        plan = [(0, 3, 11_970_786, 40, [('main', 2), ('cdc_write', 1)]),
                (1, 2, 12_000_000, 30, [('main', 2)])]
        for index, count, cycles, packets, functions in plan:
            rows = [event(i, 0x08000000 + i * 2, 'main', cycles // count) for i in range(count)]
            parts.append(self._slice(index, rows, cycles, packets, functions))
            seq_off = merge_slice_events(self.root / 'events.jsonl', self.root / 'slices' / f'{index:02d}',
                                         index, seq_off)
            slices.append(dict(index=index, t0=index * .2, t1=(index + 1) * .2,
                               csvBytes=(self.root / 'slices' / f'{index:02d}' / 'capture.csv').stat().st_size,
                               instructions=count, packets=packets, cycles=cycles, packetIssues=0))
        m = server.meta(self.sid)
        server.finalize_slices(self.sid, self.root, m, slices, .2, 1.0, seq_off, parts)
        return slices, seq_off

    def test_slice_record_is_readable_and_totals_are_whole_window(self):
        slices, events = self._finalize()
        m = server.meta(self.sid)
        self.assertEqual(m['decodeStatus'], 'done')
        self.assertTrue(m['longWindow'])
        decoded = json.loads((self.root / 'decoded.json').read_text(encoding='utf8'))
        self.assertEqual(decoded['streamed']['schema'], 3)
        self.assertEqual(decoded['streamed']['events'], events)
        # 周期 = 两片的 analysis.timing 之和（不是每片最大值、不是 912）
        self.assertEqual(decoded['summary']['totalCycles'], 11_970_786 + 12_000_000)
        self.assertEqual(decoded['analysis']['timing']['totalCycles'], 11_970_786 + 12_000_000)
        # 没有顶层 capture.csv 也不该被当成"指纹不一致"
        self.assertNotIn('capture', decoded)
        rows = [json.loads(line) for line in (self.root / 'events.jsonl').read_text(encoding='utf8').splitlines() if line]
        self.assertEqual([r['seq'] for r in rows], list(range(events)))
        self.assertEqual([r['slice'] for r in rows], [0, 0, 0, 1, 1])

        record = self.client.get('/api/records/' + self.sid)
        self.assertEqual(record.status_code, 200, record.json)
        self.assertEqual(record.json['summary']['instructions'], 5)
        self.assertEqual(record.json['summary']['totalCycles'], 11_970_786 + 12_000_000)

        overview = self.client.get('/api/browse/' + self.sid + '/overview')
        self.assertEqual(overview.status_code, 200, overview.json)
        self.assertEqual(overview.json['eventCount'], events)
        page = self.client.get('/api/browse/' + self.sid + '/page')
        self.assertEqual(page.status_code, 200, page.json)

        exported = self.client.get('/api/download/' + self.sid + '/json')
        self.assertEqual(exported.status_code, 200, exported.json)
        raw = self.client.get('/api/download/' + self.sid + '/raw?slice=1')
        self.assertEqual(raw.status_code, 200)
        self.assertEqual(raw.data, b'slice-1')
        whole = self.client.get('/api/download/' + self.sid + '/raw')
        self.assertEqual(whole.status_code, 400)
        self.assertIn('逐片下载', whole.json['error'])
        package = self.client.get('/api/download/' + self.sid + '/record')
        self.assertEqual(package.status_code, 400)
        self.assertIn('长窗', package.json['error'])

    def test_broken_slice_count_is_reported_instead_of_passing_silently(self):
        self._finalize()
        events = (self.root / 'events.jsonl').read_text(encoding='utf8')
        (self.root / 'events.jsonl').write_text(events + json.dumps(event(99, 0x08000000, 'main')) + '\n',
                                                encoding='utf8')
        server.browse_store(self.sid).path.unlink()   # 丢掉索引，逼它重建
        response = self.client.get('/api/browse/' + self.sid + '/overview')
        self.assertEqual(response.status_code, 400)
        self.assertIn('记录数不一致', response.json['error'])

    def test_refinalize_rebuilds_an_existing_record_without_redecoding(self):
        slices, events = self._finalize()
        import refinalize_slices
        refinalize_slices.rebuild(self.root)
        decoded = json.loads((self.root / 'decoded.json').read_text(encoding='utf8'))
        self.assertEqual(decoded['streamed'], dict(schema=3, events=events, slices=len(slices),
                                                   packets=70))
        self.assertEqual(decoded['summary']['totalCycles'], 11_970_786 + 12_000_000)
        rows = [json.loads(line) for line in (self.root / 'events.jsonl').read_text(encoding='utf8').splitlines() if line]
        self.assertEqual([r['seq'] for r in rows], list(range(events)))
        self.assertEqual(server.meta(self.sid)['decodeStatus'], 'done')


if __name__ == '__main__':
    unittest.main()
