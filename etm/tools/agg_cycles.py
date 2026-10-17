"""跨切片聚合周期计数：把 N 份 10 ms 记录的周期合成"长时间分布"（任务1）。

用法：
    python -X utf8 tmp/agg-cycles.py [记录目录, 默认 etm/records] [--limit N]

判定口径（与页面一致）：
  · 每条记录取 decoded.json 的 analysis.timing（totalCycles / cycleRanges / cpi / functions[*].selfCycles）
  · 只有 timeFlameAvailable=true 的记录参与聚合（没周期数据的记录单独列出来，绝不混算）
  · 每切片单独一行（时间轴），合计给出总周期、总覆盖时长、按自身周期排序的函数表
"""
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith('--') else 'etm/records')
limit = None
if '--limit' in sys.argv:
    limit = int(sys.argv[sys.argv.index('--limit') + 1])

rows = []
skipped = []
for d in sorted(root.glob('capture-*'), key=lambda p: p.stat().st_mtime):
    sess = d / 'session.json'
    dec = d / 'decoded.json'
    if not sess.exists():
        continue
    m = json.loads(sess.read_text(encoding='utf8'))
    cfg = int((m.get('config') or {}).get('config') or 0)
    if not (cfg & 0x10):
        continue                                  # 没开周期计数的记录不参与
    if not dec.exists():
        skipped.append((d.name, '未解码'))
        continue
    j = json.loads(dec.read_text(encoding='utf8'))
    t = (j.get('analysis') or {}).get('timing') or {}
    if not t.get('timeFlameAvailable'):
        skipped.append((d.name, '无周期数据'))
        continue
    clocks = (m.get('targetSnapshot') or {}).get('clocks') or {}
    rows.append(dict(name=d.name, mtime=d.stat().st_mtime,
                     cycles=int(t['totalCycles']), ranges=int(t.get('cycleRanges') or 0),
                     cpi=t.get('cpi'), instructions=int(t.get('executedInstructions') or 0),
                     coreHz=clocks.get('coreHz'), traceHz=clocks.get('physicalTraceHz'),
                     seconds=float(m.get('seconds') or 0), functions=t.get('functions') or []))
if limit:
    rows = rows[-limit:]
if not rows:
    print('没有可聚合的周期记录（先按 CC-only 采集几份）')
    raise SystemExit(1)

total = sum(r['cycles'] for r in rows)
core = next((r['coreHz'] for r in rows if r['coreHz']), None)
per_fn, fn_slices = {}, {}
for r in rows:
    for f in r['functions']:
        per_fn[f['function']] = per_fn.get(f['function'], 0) + int(f['selfCycles'])
        fn_slices.setdefault(f['function'], set()).add(r['name'])

print(f'聚合 {len(rows)} 份切片记录（每份 {rows[0]["seconds"]*1000:.0f} ms 窗口，CC-only）')
print(f'总周期 {total:,} · 平均 CPI {total/sum(r["instructions"] for r in rows if r["instructions"]):.2f}'
      + (f' · 总覆盖 {total/core*1e6:.1f} µs（coreHz={core/1e6:.0f} MHz）' if core else ''))
print('\n每切片（时间轴）：')
print(f'  {"记录":<10}{"窗口":>7}{"周期":>10}{"CPI":>7}{"指令":>9}{"周期占比":>9}')
for r in rows:
    print(f'  {r["name"][8:16]:<10}{r["seconds"]*1000:>6.0f}ms{r["cycles"]:>10,}'
          f'{(r["cpi"] or 0):>7.2f}{r["instructions"]:>9,}{100*r["cycles"]/total:>8.1f}%')
print('\n按自身周期排序（跨切片合并）：')
print(f'  {"函数":<28}{"自身周期":>10}{"占比":>8}{"出现在":>8}')
for fn, cyc in sorted(per_fn.items(), key=lambda kv: -kv[1])[:15]:
    print(f'  {fn:<28}{cyc:>10,}{100*cyc/total:>7.1f}%{len(fn_slices[fn]):>7}/' + str(len(rows)))
if skipped:
    print('\n未参与（明确列出，不混算）：')
    for name, why in skipped[:10]:
        print(f'  {name[8:16]}  {why}')
