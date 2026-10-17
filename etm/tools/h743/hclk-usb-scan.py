#!/usr/bin/env python3
"""不需要 ETM：固定 CPU=120MHz，扫 AHB/HCLK 分频，看 CDC 还能不能枚举+回显。

对每个 (HCLK 分频, TRDT 来源) 组合：构建 -> 烧录 -> 等 COM 口 -> 发 hello world!\\n 看回读。
TRDT 来源 Hclk = 修好的（按 AHB 查表）；Core = 旧行为（按 CPU 频率，A/B 对照）。
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
sys.path.insert(0, str(ROOT / 'tools'))


def ports():
    from serial.tools import list_ports
    return [p.device for p in list_ports.comports() if p.vid == 0xF055 and p.pid == 0x7431]


def try_echo(budget=6.0):
    import serial
    t0 = time.time()
    last = ''
    while time.time() - t0 < budget:
        for name in ports():
            try:
                with serial.Serial(name, 115200, timeout=0.2, write_timeout=0.2) as sp:
                    sp.dtr = True
                    time.sleep(0.02)
                    sp.reset_input_buffer()
                    sp.write(b'hello world!\n')
                    time.sleep(0.08)
                    data = sp.read(512)
                return time.time() - t0, name, data.decode('utf-8', 'replace')
            except Exception as exc:                     # noqa: BLE001
                last = str(exc)
        time.sleep(0.01)
    return None, last, ''


def read_ahb():
    """读回 RCC_D1CFGR.HPRE 与 GUSBCFG.TRDT（OpenOCD）。"""
    ps = ROOT / 'tmp/ocd.ps1'
    out = subprocess.run(['pwsh', '-NoProfile', '-File', str(ps), '-Cmds',
                          'mdw 0x58024418 1;;mdw 0x58024400 1;;mdw 0x4008000C 1'],
                         capture_output=True, text=True, timeout=120).stdout
    vals = [l.split(':')[1].strip().split()[0] for l in out.splitlines() if l.startswith('0x58') or l.startswith('0x4008')]
    d1 = int(vals[0], 16) if len(vals) > 0 else 0
    gusb = int(vals[2], 16) if len(vals) > 2 else 0
    hpre = {0: 1, 8: 2, 9: 4, 10: 8, 11: 16}.get(d1 & 0xF, None)
    trdt = (gusb >> 10) & 0xF
    return hpre, trdt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--core', type=int, default=120)
    ap.add_argument('--trace-r', type=int, default=16)
    ap.add_argument('--divs', default='1,2,4,8,16')
    ap.add_argument('--trdt', default='Core,Hclk')
    ap.add_argument('--out', default=str(ROOT / 'captures/hclk-usb-scan.json'))
    args = ap.parse_args()

    rows = []
    for mode in args.trdt.split(','):
        for div in [int(x) for x in args.divs.split(',')]:
            hclk = args.core / div
            print(f'\n===== AhbDiv={div} (HCLK≈{hclk:g}MHz)  TRDT={mode} =====')
            row = {'ahbDiv': div, 'hclkMHz': hclk, 'trdtMode': mode}
            b = subprocess.run(['pwsh', '-NoProfile', '-File', str(BUILD),
                                '-CoreMHz', str(args.core), '-TraceR', str(args.trace_r),
                                '-AhbDiv', str(div), '-Trdt', mode, '-Gcc', GCC],
                               capture_output=True, text=True, timeout=900,
                               encoding='utf-8', errors='replace')
            row['build'] = b.returncode == 0
            if b.returncode != 0:
                print('  构建失败:', (b.stdout or '')[-400:], (b.stderr or '')[-400:])
                rows.append(row)
                continue
            f = subprocess.run(['pwsh', '-NoProfile', '-File', str(FLASH)],
                               capture_output=True, text=True, timeout=600,
                               encoding='utf-8', errors='replace')
            row['flash'] = 'verified' in (f.stdout or '')
            time.sleep(2.0)
            delay, port, readback = try_echo(6.0)
            row.update(cdc=(delay is not None), delay=delay, port=port, readback=readback)
            try:
                hpre, trdt = read_ahb()
                row.update(hpre=hpre, trdtRead=trdt)
            except Exception as exc:                     # noqa: BLE001
                row['ahbReadError'] = str(exc)[:120]
            ok = ' ✅' if row['cdc'] else ' ❌'
            print(f"  HCLK 分频={row.get('hpre')}  TRDT 读回=0x{row.get('trdtRead', 0):X}"
                  f"  CDC={row['cdc']}{ok}  回读={readback!r}")
            rows.append(row)
            (Path(args.out)).write_text(json.dumps(rows, indent=2, ensure_ascii=False),
                                        encoding='utf-8')
    print('\n===== 汇总 =====')
    for r in rows:
        print(f"div={r['ahbDiv']:>2} HCLK={r['hclkMHz']:>6g}MHz  TRDT={r['trdtMode']:5}"
              f"  trdt=0x{r.get('trdtRead', 0):X}  CDC={'OK' if r.get('cdc') else 'FAIL'}")
    print('结果: ' + args.out)


if __name__ == '__main__':
    main()
