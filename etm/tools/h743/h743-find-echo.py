"""在同一次 LA 采集里扫时间窗，找出含 CDC 回显路径的那一段。"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
from decode_trace import decode_capture          # noqa: E402

if '--live-buffer' not in sys.argv:
    raise SystemExit('此工具导出当前 LA 缓冲；确认已经加载对应 KVDAT 后，显式传 --live-buffer，避免采集错配')
args=[a for a in sys.argv[1:] if a!='--live-buffer']
rec = Path(args[0])
span = float(args[1]) if len(args) > 1 else 0.004
starts = [float(x) for x in (args[2].split(',') if len(args) > 2
                             else '0.48,0.50,0.52,0.54,0.56,0.58'.split(','))]
meta = json.loads((rec / 'meta.json').read_text(encoding='utf-8'))
config = {k: int(v) for k, v in meta['etm'].items()}
markers = ('usbd_cdc_acm_bulk_out', 'usbd_cdc_acm_bulk_in', 'cdc_write', 'handle_command',
           'trace_log_printf', 'SEGGER_RTT_Write')
for t0 in starts:
    tag = f'{t0:g}-{t0 + span:g}'.replace('.', 'p')
    csv = rec / f'span-{tag}.csv'
    if not csv.exists():
        subprocess.run([sys.executable, '-u', '-X', 'utf8', str(ROOT / 'tools/la/kingst_la.py'),
                        'export', '--out', str(csv), '--channels', '0', '1', '2', '3', '4',
                        '--time-span', str(t0), str(t0 + span), '--no-kvdat'],
                       cwd=str(ROOT), check=True, timeout=900)
    try:
        r = decode_capture(csv, rec / 'firmware.elf', rec / f'span-{tag}', config=config)
    except Exception as exc:                       # noqa: BLE001
        print(f'{t0:.3f}-{t0+span:.3f}  解码失败: {str(exc)[:90]}')
        continue
    hits = {k: v for k, v in r['summary']['functions'].items() if k in markers}
    print(f"{t0:.3f}-{t0+span:.3f}  instr={r['summary']['instructions']:>7}  "
          f"issues={r['summary']['packetIssues']:>3}  命中={hits}")
    if any(k.startswith('usbd_cdc_acm_bulk') or k == 'cdc_write' for k in hits):
        print('  ^^ 这一段就是回显路径')
        break
