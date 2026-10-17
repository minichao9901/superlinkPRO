import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROWS = [
    ('CPU=trace=30 (5ms)', 'captures/h743-sweep/30mhz'),
    ('CPU=60 / trace=30 (5ms)', 'captures/h743/cpu60-trace30'),
    ('CPU=120 / trace=30 (5ms)', 'captures/h743/cpu120-trace30'),
    ('CPU=480 / trace=30 (2ms)', 'captures/h743/t2'),
]
print(f"{'config':26} {'instr':>8} {'exec':>8} {'ranges':>7} {'pkts':>6} "
      f"{'gaps':>5} {'issues':>6} {'funcs':>6} {'mapped':>7}")
for name, rel in ROWS:
    d = json.loads((ROOT / rel / 'decoded.json').read_text(encoding='utf-8'))
    s = d['summary']
    print(f"{name:26} {s['instructions']:>8} {s['executed']:>8} {s['ranges']:>7} "
          f"{s['packets']:>6} {s['gaps']:>5} {s['packetIssues']:>6} {len(s['functions']):>6} "
          f"{s['sourceMapped']:>7}")
print()
for name, rel in ROWS[1:]:
    d = json.loads((ROOT / rel / 'decoded.json').read_text(encoding='utf-8'))
    fn = d['summary']['functions']
    usb = {k: v for k, v in fn.items() if any(t in k.lower() for t in ('usb', 'cdc', 'descriptor'))}
    print(f"{name}: USB/CDС 相关函数 {len(usb)} 个 -> {usb}")
