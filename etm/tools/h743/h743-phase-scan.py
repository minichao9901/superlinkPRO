"""扫描取样相位：找出能让 TPIU 解复用出 trace ID 流的位置。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
from trace_port import extract          # noqa: E402
from tpiu_demux import demux            # noqa: E402

csv = Path(sys.argv[1])
traceid = int(sys.argv[2]) if len(sys.argv) > 2 else 2
print(f'{csv.name}: 扫描 fraction 0.05..0.95')
best = []
for step in range(5, 100, 5):
    frac = step / 100
    try:
        report, cands = extract(csv, 0, [1, 2, 3, 4], frac)
    except Exception as exc:                     # noqa: BLE001
        print(f'  {frac:.2f}  extract 失败: {exc}')
        continue
    row = [f'f={frac:.2f}', f"margin={report['min_data_transition_distance_ns']:.2f}ns",
           f"irreg={report['irregular_half_periods']}"]
    for i, raw in enumerate(cands):
        try:
            frames, streams = demux(raw)
            hit = traceid in streams
            n = len(streams.get(traceid, b''))
            row.append(f'ph{i}:frames={frames["frames"]},id{traceid}={n}B,ok={int(hit and frames["frames"]>0)}')
            if hit and frames['frames'] > 0:
                best.append((frac, i, frames['frames'], n))
        except Exception as exc:                 # noqa: BLE001
            row.append(f'ph{i}:demux失败({str(exc)[:40]})')
    print('  ' + '  '.join(row))
print('可用相位:', best)
