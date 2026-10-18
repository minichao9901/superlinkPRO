"""长窗分窗合并的纯逻辑自测（不需要硬件）。

跑：python -X utf8 tools/test_slices.py
"""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from slice_merge import merge_slice_events, aggregate_slices  # noqa: E402

fails = []


def check(name, cond, detail=''):
    if cond:
        print(f'  ✓ {name}')
    else:
        fails.append(name)
        print(f'  ✗ {name} {detail}')


def events(seqs, cycles, funcs=None):
    out = []
    for i, s in enumerate(seqs):
        e = dict(kind='instruction', seq=s, cycles=cycles[i], pc=0x08000000 + i * 2,
                 function=(funcs or ['main'] * len(seqs))[i], executed=True)
        out.append(e)
    return out


def slice_decoded(total_cycles, functions, packets=10):
    """模拟一片 decode_capture_stream 的 decoded.json（analysis.timing 是真的每片周期）。"""
    return dict(summary=dict(instructions=sum(f['instructions'] for f in functions), packets=packets,
                             executed=sum(f['instructions'] for f in functions), gaps=0,
                             exceptions=0, sourceMapped=0, ranges=0, packetIssues=0,
                             functions={f['function']: f['instructions'] for f in functions}),
                quality=dict(packetIssues=0, inputBytes=100, processedBytes=100,
                             unprocessedBytes=0, unknownExecution=0),
                analysis=dict(timing=dict(totalCycles=total_cycles, cycleRanges=2,
                                          executedInstructions=sum(f['instructions'] for f in functions),
                                          instructions=sum(f['instructions'] for f in functions),
                                          functions=[]),
                              functions=functions,
                              hotspots=[dict(function=functions[0]['function'], file='m.c', line=7,
                                             records=2, unknown=0, cycles=total_cycles, pc=0x08000000)],
                              flame=dict(name='已恢复执行区间', self=0, value=0, selfCycles=0,
                                         valueCycles=0, children=[
                                             dict(name=f['function'], self=f['instructions'],
                                                  value=f['instructions'], selfCycles=f['selfCycles'],
                                                  valueCycles=f['selfCycles'], children=[])
                                             for f in functions]),
                              edges=[dict(caller='main', callee='cdc_write', kind='call', count=1)],
                              observedCalls=1, unknownStackRestarts=0))


with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    # 两片：各自 seq 从 0 起、cycles 是**每元素**量（模拟生产事件）
    a = root / 'slices' / '00'
    a.mkdir(parents=True)
    (a / 'events.jsonl').write_text('\n'.join(json.dumps(e, ensure_ascii=False) for e in events([0, 1, 2], [0, 3, 7])),
                                    encoding='utf8')
    b = root / 'slices' / '01'
    b.mkdir(parents=True)
    (b / 'events.jsonl').write_text('\n'.join(json.dumps(e, ensure_ascii=False) for e in events([0, 1], [2, 5], ['cdc_write', 'main'])),
                                    encoding='utf8')

    merged = root / 'events.jsonl'
    so = merge_slice_events(merged, a, 0)
    check('第 1 片后 seq_off = 3', so == 3, so)
    so = merge_slice_events(merged, b, 1, so)
    check('第 2 片后 seq_off = 5', so == 5, so)

    rows = [json.loads(l) for l in merged.read_text(encoding='utf8').splitlines() if l.startswith('{')]
    check('合并后 5 条事件', len(rows) == 5, len(rows))
    check('seq 连续 0..4（片边界不撞号）', [r['seq'] for r in rows] == [0, 1, 2, 3, 4], [r['seq'] for r in rows])
    check('cycles 原样保留（每元素量，不加偏移）', [r['cycles'] for r in rows] == [0, 3, 7, 2, 5],
          [r['cycles'] for r in rows])
    check('slice 字段标注片号', [r['slice'] for r in rows] == [0, 0, 0, 1, 1], [r['slice'] for r in rows])
    check('function 原样保留', [r['function'] for r in rows] == ['main', 'main', 'main', 'cdc_write', 'main'],
          [r['function'] for r in rows])

    # 聚合：每片 2 条/1 条指令，周期 10 / 7（analysis.timing 才可信）
    fn_main = dict(function='main', instructions=2, knownExecuted=2, unknownExecution=0, notTaken=0,
                   calls=0, closedCalls=0, tailTransfers=0, interruptEntries=0,
                   selfCycles=8, totalCycles=8, cycleRanges=1)
    fn_cdc = dict(function='cdc_write', instructions=1, knownExecuted=1, unknownExecution=0, notTaken=0,
                  calls=1, closedCalls=1, tailTransfers=0, interruptEntries=0,
                  selfCycles=2, totalCycles=2, cycleRanges=1)
    fn_main_b = dict(fn_main, instructions=1, knownExecuted=1, selfCycles=5, totalCycles=5)
    slices = [dict(index=0, t0=0, t1=.2, instructions=3, packets=10, cycles=10, packetIssues=0),
              dict(index=1, t0=.2, t1=.4, instructions=1, packets=8, cycles=7, packetIssues=1)]
    parts = [slice_decoded(10, [fn_main, fn_cdc], packets=10), slice_decoded(7, [fn_main_b], packets=8)]
    base = dict(summary=dict(instructions=999, packets=999), quality=dict(packetIssues=9),
                capture=dict(name='capture.csv'))
    agg = aggregate_slices(base, slices, .2, dict(basis='measured'), parts)
    check('聚合指令数 = 4', agg['summary']['instructions'] == 4, agg['summary'])
    check('聚合包数 = 18', agg['summary']['packets'] == 18, agg['summary'])
    check('聚合周期 = 17（按每片 analysis.timing 求和）', agg['summary']['totalCycles'] == 17, agg['summary'])
    check('聚合包问题 = 1', agg['quality']['packetIssues'] == 1, agg['quality'])
    check('标记 sliced-window', agg['captureScope'] == 'sliced-window', agg['captureScope'])
    check('保留原有的 capture 元信息', agg.get('capture') == dict(name='capture.csv'), agg.get('capture'))
    check('slices 原样带出', agg['slices'] == slices, agg['slices'])
    timing = agg['analysis']['timing']
    check('analysis.timing.totalCycles = 17（不再是第一片）', timing['totalCycles'] == 17, timing)
    funcs = {f['function']: f for f in agg['analysis']['functions']}
    check('函数统计跨片相加：main 3 条 / 13 周期',
          funcs['main']['instructions'] == 3 and funcs['main']['selfCycles'] == 13, funcs.get('main'))
    check('周期占比按整段重算：main ≈ 76.5%',
          abs(funcs['main']['cyclePercent'] - 100 * 13 / 17) < 1e-9, funcs['main']['cyclePercent'])
    check('火焰树按名字合并', any(c['name'] == 'main' and c['selfCycles'] == 13
                                  for c in agg['analysis']['flame']['children']), agg['analysis']['flame'])
    check('调用边跨片合并（同边 count 相加）',
          agg['analysis']['edges'] == [dict(caller='main', callee='cdc_write', kind='call', count=2)],
          agg['analysis']['edges'])
    check('质量字节数跨片相加', agg['quality']['inputBytes'] == 200, agg['quality'])

    # 兼容：不传 parts 时退回按 slices[].cycles 求和（离线旧数据）
    agg2 = aggregate_slices({}, slices, .2)
    check('无 parts 时按 slices[].cycles 求和 = 17', agg2['summary']['totalCycles'] == 17, agg2['summary'])

print()
if fails:
    print(f'✗ {len(fails)} 项失败：{fails}')
    sys.exit(1)
print('✓ 全部通过')

