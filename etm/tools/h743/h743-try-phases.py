"""对同一份采集试不同取样相位，看能不能解出指令（不占硬件）。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
from decode_trace import decode_capture           # noqa: E402

rec = Path(sys.argv[1])
meta = json.loads((rec / 'meta.json').read_text(encoding='utf-8'))
config = {k: int(v) for k, v in meta['etm'].items()}
for frac in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45,
             0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95):
    work = rec / f'phase-{int(frac*100):02d}'
    try:
        r = decode_capture(rec / 'capture.csv', rec / 'firmware.elf', work,
                           config=config, fraction=frac)
        s = r['summary']
        print(f"f={frac:.2f}  instr={s['instructions']:>7} packets={s['packets']:>6} "
              f"issues={s['packetIssues']} gaps={s['gaps']} "
              f"phase={r['framing'].get('phase')} frames={r['framing'].get('frames')}")
    except Exception as exc:                       # noqa: BLE001
        print(f'f={frac:.2f}  失败: {str(exc)[:110]}')
