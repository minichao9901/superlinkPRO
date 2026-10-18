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


with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    # 两片：各自 seq 从 1 起、cycles 从 0 起（模拟独立解码）
    a = root / 'slices' / '00'
    a.mkdir(parents=True)
    (a / 'events.jsonl').write_text('\n'.join(json.dumps(e, ensure_ascii=False) for e in events([1, 2, 3], [0, 10, 25])),
                                    encoding='utf8')
    b = root / 'slices' / '01'
    b.mkdir(parents=True)
    (b / 'events.jsonl').write_text('\n'.join(json.dumps(e, ensure_ascii=False) for e in events([1, 2], [0, 7], ['cdc_write', 'main'])),
                                    encoding='utf8')

    merged = root / 'events.jsonl'
    so, co = merge_slice_events(merged, a, 0)
    check('第 1 片后 seq_off = 3', so == 3, so)
    check('第 1 片后 cyc_off = 25', co == 25, co)
    so, co = merge_slice_events(merged, b, 1, so, co)
    check('第 2 片后 seq_off = 5', so == 5, so)
    check('第 2 片后 cyc_off = 32', co == 32, co)

    rows = [json.loads(l) for l in merged.read_text(encoding='utf8').splitlines() if l.startswith('{')]
    check('合并后 5 条事件', len(rows) == 5, len(rows))
    check('seq 连续 1..5', [r['seq'] for r in rows] == [1, 2, 3, 4, 5], [r['seq'] for r in rows])
    check('cycles 累加（第 2 片接着 25）', [r['cycles'] for r in rows] == [0, 10, 25, 25, 32], [r['cycles'] for r in rows])
    check('slice 字段标注片号', [r['slice'] for r in rows] == [0, 0, 0, 1, 1], [r['slice'] for r in rows])
    check('function 原样保留', [r['function'] for r in rows] == ['main', 'main', 'main', 'cdc_write', 'main'],
          [r['function'] for r in rows])

    # 聚合
    slices = [dict(index=0, t0=0, t1=.2, instructions=3, packets=10, cycles=25, packetIssues=0),
              dict(index=1, t0=.2, t1=.4, instructions=2, packets=8, cycles=7, packetIssues=1)]
    base = dict(summary=dict(instructions=999, packets=999), quality=dict(packetIssues=9),
                capture=dict(name='capture.csv'))
    agg = aggregate_slices(base, slices, .2, dict(basis='measured'))
    check('聚合指令数 = 5', agg['summary']['instructions'] == 5, agg['summary'])
    check('聚合包数 = 18', agg['summary']['packets'] == 18, agg['summary'])
    check('聚合周期 = 32', agg['summary']['totalCycles'] == 32, agg['summary'])
    check('聚合包问题 = 1', agg['quality']['packetIssues'] == 1, agg['quality'])
    check('标记 sliced-window', agg['captureScope'] == 'sliced-window', agg['captureScope'])
    check('保留原有的 capture 元信息', agg.get('capture') == dict(name='capture.csv'), agg.get('capture'))
    check('slices 原样带出', agg['slices'] == slices, agg['slices'])

print()
if fails:
    print(f'✗ {len(fails)} 项失败：{fails}')
    sys.exit(1)
print('✓ 全部通过')

