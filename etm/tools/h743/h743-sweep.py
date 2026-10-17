#!/usr/bin/env python3
"""H743 CPU==TRACECLK 同频扫频：每个频点 构建 → 烧录 → LA 采集 → OpenCSD 解码。

结果写到 captures/h743-sweep/<f>mhz/{capture.csv,decoded.json,result.json} 与总表 sweep.json。
用法：python -X utf8 tmp/h743-sweep.py [--freqs 35,40,45,...] [--time 0.002] [--rate 500000000]
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GCC = 'E:/Share/env-windows/tools/gnu_gcc/arm_gcc/mingw/bin/arm-none-eabi-gcc.exe'
BUILD = ROOT / 'firmware/stm32h743_cdc_trace/build.ps1'
FLASH = ROOT / 'tools/h743/h743-flash.ps1'
ELF = ROOT / 'firmware/stm32h743_cdc_trace/build/fw.elf'


def run(cmd, timeout, tag):
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       encoding='utf-8', errors='replace')
    dt = time.time() - t0
    ok = p.returncode == 0
    print(f'[{tag}] rc={p.returncode} {dt:.1f}s')
    if not ok:
        print((p.stdout or '')[-2000:])
        print((p.stderr or '')[-2000:])
    return ok, (p.stdout or '') + (p.stderr or ''), dt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--freqs', default='30,35,40,45,50,60,70,80,90')
    ap.add_argument('--time', type=float, default=0.005)
    ap.add_argument('--rate', type=int, default=500000000)
    ap.add_argument('--fraction', type=float, default=0.25)
    ap.add_argument('--decode', type=int, default=1)
    args = ap.parse_args()

    out = ROOT / 'captures' / 'h743-sweep'
    out.mkdir(parents=True, exist_ok=True)
    table = []
    for f in [int(x) for x in args.freqs.split(',')]:
        rec = out / f'{f}mhz'
        entry = {'freqMHz': f, 'record': str(rec), 'at': time.strftime('%H:%M:%S')}
        print(f'\n===== {f} MHz (CPU == TRACECLK) =====')
        ok, log, dt = run(['pwsh', '-NoProfile', '-File', str(BUILD), '-CoreMHz', str(f),
                           '-Gcc', GCC], 900, 'build')
        entry['build'] = {'ok': ok, 'seconds': round(dt, 1),
                          'line': next((l for l in log.splitlines() if '构建完成' in l), '')}
        if not ok:
            table.append(entry)
            continue
        ok, _, dt = run(['pwsh', '-NoProfile', '-File', str(FLASH)], 600, 'flash')
        entry['flash'] = {'ok': ok, 'seconds': round(dt, 1)}
        if not ok:
            table.append(entry)
            continue
        rec.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, '-u', '-X', 'utf8', str(ROOT / 'tools/h743_etm.py'), 'capture',
               '--record', str(rec), '--time', str(args.time), '--rate', str(args.rate),
               '--fraction', str(args.fraction), '--overwrite']
        if args.decode:
            cmd.append('--decode')
        ok, log, dt = run(cmd, 900, 'capture')
        entry['capture'] = {'ok': ok, 'seconds': round(dt, 1)}
        print(log[-1200:])
        decoded = rec / 'decoded.json'
        if decoded.exists():
            r = json.loads(decoded.read_text(encoding='utf-8'))
            phys = r.get('physical') or {}
            entry['decode'] = {
                'instructions': r['summary']['instructions'],
                'executed': r['summary']['executed'],
                'sourceMapped': r['summary']['sourceMapped'],
                'packets': r['summary']['packets'],
                'ranges': r['summary']['ranges'],
                'exceptions': r['summary']['exceptions'],
                'gaps': r['summary']['gaps'],
                'packetIssues': r['summary']['packetIssues'],
                'functions': r['summary']['functions'],
                'openCsdError': r['status'].get('lastError', ''),
                'traceClockMHz': round((phys.get('clock_hz_average') or 0) / 1e6, 6),
                'irregularHalfPeriods': phys.get('irregular_half_periods'),
                'minDataTransitionNs': phys.get('min_data_transition_distance_ns'),
                'framing': {k: v for k, v in (r.get('framing') or {}).items()
                            if k in ('phase', 'frames', 'bytes')},
            }
        else:
            entry['decode'] = None
            entry['captureLog'] = log[-2500:]
        table.append(entry)
        (out / 'sweep.json').write_text(json.dumps(table, indent=2, ensure_ascii=False),
                                        encoding='utf-8')
    print('\n===== 汇总 =====')
    for e in table:
        d = e.get('decode')
        if d:
            print(f"{e['freqMHz']:>3} MHz  clk={d['traceClockMHz']:>10.4f}  "
                  f"instr={d['instructions']:>7}  packets={d['packets']:>6}  "
                  f"issues={d['packetIssues']}  gaps={d['gaps']}  "
                  f"minDT={d['minDataTransitionNs']}  phase={d['framing'].get('phase')}")
        else:
            print(f"{e['freqMHz']:>3} MHz  失败: build={e.get('build',{}).get('ok')} "
                  f"flash={e.get('flash',{}).get('ok')} capture={e.get('capture',{}).get('ok')}")
    (out / 'sweep.json').write_text(json.dumps(table, indent=2, ensure_ascii=False), encoding='utf-8')
    print('汇总: ' + str(out / 'sweep.json'))


if __name__ == '__main__':
    main()
