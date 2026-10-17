#!/usr/bin/env python3
"""stm32f103_scope 测试固件的**客观验收脚本**（不依赖 scope 页面，也不依赖探针固件）。

做两件事：
  1) 用 OpenOCD 把目标 halt 住、dump 一段 RAM，按"变量契约"逐项核对
     —— 每个量的应有值都由 g_tick 唯一确定（契约见 src/main.c 文件头的表）。
  2) 两次 halt 之间让目标跑 3 s，用 g_tick 的差值反测 **10 kHz 时基**准不准。

为什么要它：以后 scope 页面采到的数据可以拿这份"目标侧真值"对账
（同一时刻取到的 g_tick → 其余通道的应有值），而不是靠肉眼看波形。

用法：
    python check.py                       # 烧录后直接跑
    python check.py --hold                # 跑完保持 halt（交给调试器/排障）
    python check.py --openocd <exe> --scripts <dir>
退出码：0 = 通过；1 = 有断言失败；2 = 环境/连接问题。
"""
import argparse
import glob
import math
import os
import struct
import subprocess
import sys
import time

# Windows 控制台默认 GBK，emoji/部分字符会直接抛 UnicodeEncodeError（本轮踩过）
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from elftools.elf.elffile import ELFFile

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ELF = os.path.join(HERE, 'build', 'fw.elf')
DUMP_BASE = 0x20000000
DUMP_LEN = 0x2000                 # 8 KB（F103C8 只有 20 KB RAM）
TICK_HZ = 10000.0
SLEEP_MS = 3000

# sin(2πi/100)×1000 —— 与 src/main.c 里的 SIN100 表同一份定义
SIN100 = [int(round(1000 * math.sin(2 * math.pi * i / 100))) for i in range(100)]

# g_pack 内部字段偏移（与 src/main.c 的结构体逐一对应；整块大小由 ELF 校验 = 24）
PACK_OFF = {'f_sin': 0, 'f_tri': 4, 'i_tick': 8, 'u_ramp': 12, 'i_sq1k': 14,
            'u_cnt': 16, 'i_saw': 17, 'u_hi': 20}
TOL = 2e-6                        # f32 的比较容差（几个 ulp）


def find_tool():
    cands = sorted(glob.glob(r'E:\Share\env-windows\xpack-openocd-*\bin\openocd.exe'))
    cands += sorted(glob.glob(os.path.expanduser(
        r'~\.espressif\tools\openocd-esp32\*\openocd-esp32\bin\openocd.exe')))
    for exe in cands:
        root = os.path.dirname(os.path.dirname(exe))
        for sub in ('openocd/scripts', 'share/openocd/scripts'):
            scr = os.path.join(root, *sub.split('/'))
            if os.path.isfile(os.path.join(scr, 'interface', 'cmsis-dap.cfg')):
                return exe, scr
    return None, None


def symbols(elf_path):
    """从 .symtab 取 {名字: (地址, 大小)}"""
    out = {}
    with open(elf_path, 'rb') as f:
        elf = ELFFile(f)
        sym = elf.get_section_by_name('.symtab')
        if sym is None:
            raise SystemExit('这个 ELF 没有符号表（strip 过了？）')
        for s in sym.iter_symbols():
            if s['st_info']['type'] == 'STT_OBJECT' and s['st_size']:
                out[s.name] = (s['st_value'], s['st_size'])
    return out


def run_openocd(exe, scripts, cmds, timeout=90):
    argv = [exe, '-s', scripts,
            '-f', os.path.join(scripts, 'interface', 'cmsis-dap.cfg'),
            '-c', 'cmsis-dap backend usb_bulk',
            '-f', os.path.join(scripts, 'target', 'stm32f1x.cfg')]
    for c in cmds:
        argv += ['-c', c]
    t0 = time.time()
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    dt = time.time() - t0
    if r.returncode != 0:
        print('\n'.join(r.stdout.splitlines()[-25:]))
        print('\n'.join(r.stderr.splitlines()[-25:]), file=sys.stderr)
        raise SystemExit(f'OpenOCD 失败 (exit {r.returncode}) —— 探针是不是被别的程序占着？')
    return dt, r.stdout


class Ram:
    def __init__(self, buf):
        self.b = buf

    def _o(self, addr):
        return addr - DUMP_BASE

    def u8(self, addr):
        return self.b[self._o(addr)]

    def i8(self, addr):
        return struct.unpack_from('<b', self.b, self._o(addr))[0]

    def u16(self, addr):
        return struct.unpack_from('<H', self.b, self._o(addr))[0]

    def i16(self, addr):
        return struct.unpack_from('<h', self.b, self._o(addr))[0]

    def u32(self, addr):
        return struct.unpack_from('<I', self.b, self._o(addr))[0]

    def i32(self, addr):
        return struct.unpack_from('<i', self.b, self._o(addr))[0]

    def f32(self, addr):
        return struct.unpack_from('<f', self.b, self._o(addr))[0]

    def f64(self, addr):
        return struct.unpack_from('<d', self.b, self._o(addr))[0]


def expect(t):
    """tick = t 时，各变量的应有值（{名字: 值}；g_pack 用 'pk.xxx'）
       注意 i_saw 等按 C 的整型回绕语义算。"""
    ph = t % 100
    tri = (ph * 40 - 1000) if ph < 50 else (3000 - ph * 40)
    saw = (t * 3) & 0xFF
    return {
        'pk.f_sin':  SIN100[ph] / 1000.0,
        'pk.f_tri':  tri / 1000.0,
        'pk.i_tick': t,
        'pk.u_ramp': t % 1000,
        'pk.i_sq1k': 1000 if (t % 10) < 5 else -1000,
        'pk.u_cnt':  t & 0xFF,
        'pk.i_saw':  saw - 256 if saw > 127 else saw,
        'pk.u_hi':   0x10000000 | (t & 0xFFFF),
        'tick':      t,
        'isr':       t,
        'far_cnt':   t,
        'sq5k':      1000 if (t & 1) else -1000,
        'far_sq100': 1000 if (t % 100) < 50 else -1000,
        'pulse':     1 if (t % 2000) < 100 else 0,
        'pair_a':    t & 0xFFFF,
        'pair_b':    (~t) & 0xFFFF,
        'ramp64':    1.0 + t * 1e-6,
    }


def read_all(ram, a):
    """把契约里的每个量都读出来"""
    base = a['g_pack']
    return {
        'pk.f_sin':  ram.f32(base + PACK_OFF['f_sin']),
        'pk.f_tri':  ram.f32(base + PACK_OFF['f_tri']),
        'pk.i_tick': ram.i32(base + PACK_OFF['i_tick']),
        'pk.u_ramp': ram.u16(base + PACK_OFF['u_ramp']),
        'pk.i_sq1k': ram.i16(base + PACK_OFF['i_sq1k']),
        'pk.u_cnt':  ram.u8(base + PACK_OFF['u_cnt']),
        'pk.i_saw':  ram.i8(base + PACK_OFF['i_saw']),
        'pk.u_hi':   ram.u32(base + PACK_OFF['u_hi']),
        'tick':      ram.u32(a['g_tick']),
        'isr':       ram.u32(a['g_isr_count']),
        'far_cnt':   ram.u32(a['g_far_cnt']),
        'sq5k':      ram.i16(a['g_sq5k']),
        'far_sq100': ram.i16(a['g_far_sq100']),
        'pulse':     ram.u8(a['g_pulse']),
        'pair_a':    ram.u16(a['g_pair_a']),
        'pair_b':    ram.u16(a['g_pair_b']),
        'ramp64':    ram.f64(a['g_ramp64']),
    }


def near(x, y):
    return abs(x - y) < TOL if isinstance(y, float) else x == y


def check(dump, sym, label):
    """核对一次 dump；返回 (tick, fails, warns)"""
    a = {n: v[0] for n, v in sym.items()}
    need = ['g_pack', 'g_tick', 'g_isr_count', 'g_far_cnt', 'g_sq5k', 'g_far_sq100',
            'g_pulse', 'g_pair_a', 'g_pair_b', 'g_ramp64', 'g_lfsr']
    missing = [n for n in need if n not in a]
    if missing:
        raise SystemExit(f'ELF 里缺符号：{missing}（改了名字？）')
    ram = Ram(dump)
    got = read_all(ram, a)
    t = got['tick']
    fails, warns = [], []

    if sym['g_pack'][1] != 24:
        fails.append(f'g_pack 大小 = {sym["g_pack"][1]}，期望 24（布局变了，请同步 PACK_OFF）')

    # 🚨 每个量**各自**允许落在 t 或 t-1：ISR 是逐字段 store 的，halt 完全可能
    #    停在 ISR 中途 → 于是"早写的字段已是 t、晚写的字段还是 t-1"（实测就是这样：
    #    g_tick 先更，所以它往往是 t，而 g_pack 那 24 B 还停在 t-1）。
    #    只有"两个都不匹配"才算失败；混合命中不是失败，而是**撕裂的现场**，要报出来。
    exp_t, exp_p = expect(t), expect(t - 1)
    bad, lag = [], []
    for k in exp_t:
        if near(got[k], exp_t[k]):
            continue
        if near(got[k], exp_p[k]):
            lag.append(k)
        else:
            bad.append(f'{k}: 读 {got[k]!r}，期望 {exp_t[k]!r} 或 {exp_p[k]!r}')
    if bad:
        fails.append('与 tick 对不上 → ' + '; '.join(bad))
    if lag:
        warns.append(f'halt 落在 ISR 中途：{len(lag)}/{len(exp_t)} 个量还是上一 tick 的'
                     f'（{", ".join(lag)}）—— 这就是"逐字段 store、非原子快照"的现场')

    if (got['pair_a'] ^ got['pair_b']) != 0xFFFF:
        warns.append(f'g_pair_a/b 撕裂（停在两次 store 之间）：'
                     f'a={got["pair_a"]:#06x} b={got["pair_b"]:#06x}')

    print(f'[{label}] g_tick={t} = {t / TICK_HZ:.3f} s   '
          f'i_tick={got["pk.i_tick"]} u_hi={got["pk.u_hi"]:#010x} '
          f'f_sin={got["pk.f_sin"]:+.4f} f_tri={got["pk.f_tri"]:+.4f}')
    print(f'           g_lfsr={ram.u32(a["g_lfsr"]):#010x} '
          f'u_ramp={got["pk.u_ramp"]:4d} u_cnt={got["pk.u_cnt"]:3d} '
          f'i_sq1k={got["pk.i_sq1k"]:+6d} i_saw={got["pk.i_saw"]:+4d} '
          f'g_sq5k={got["sq5k"]:+6d} g_far_sq100={got["far_sq100"]:+6d} '
          f'g_pulse={got["pulse"]} pair={got["pair_a"]:#06x}/{got["pair_b"]:#06x} '
          f'g_ramp64={got["ramp64"]!r}')
    return t, fails, warns


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--elf', default=DEFAULT_ELF)
    ap.add_argument('--openocd')
    ap.add_argument('--scripts')
    ap.add_argument('--hold', action='store_true', help='跑完保持 halt（交给调试器）')
    args = ap.parse_args()

    exe, scripts = args.openocd, args.scripts
    if not exe or not scripts:
        e2, s2 = find_tool()
        exe = exe or e2
        scripts = scripts or s2
    if not exe:
        raise SystemExit('找不到带 cmsis-dap.cfg 的 OpenOCD（用 --openocd/--scripts 指定）')
    if not os.path.isfile(args.elf):
        raise SystemExit(f'找不到 {args.elf}（先跑 build.ps1）')

    sym = symbols(args.elf)
    out = os.path.join(HERE, 'build')
    p0 = os.path.join(out, 'chk0.bin').replace('\\', '/')
    p1 = os.path.join(out, 'chk1.bin').replace('\\', '/')
    dump = f'dump_image "{p0}" 0x{DUMP_BASE:08x} 0x{DUMP_LEN:x}'
    dump2 = f'dump_image "{p1}" 0x{DUMP_BASE:08x} 0x{DUMP_LEN:x}'

    print(f'openocd : {exe}')
    print(f'elf     : {args.elf}')
    if args.hold:
        cmds = ['init', 'halt', dump, 'shutdown']
    else:
        # 两次 halt 之间让目标**跑** 3 s（不夹 dump，避免把 dump 时间算进时基）
        cmds = ['init', 'halt', dump, 'resume', f'sleep {SLEEP_MS}', 'halt', dump2,
                'resume', 'shutdown']

    wall, _ = run_openocd(exe, scripts, cmds)
    with open(os.path.join(out, 'chk0.bin'), 'rb') as f:
        d1 = f.read()
    t1, fails, warns = check(d1, sym, 'halt#1')

    if not args.hold:
        with open(os.path.join(out, 'chk1.bin'), 'rb') as f:
            d2 = f.read()
        t2, f2, w2 = check(d2, sym, 'halt#2')
        fails += f2
        warns += w2
        dt = t2 - t1
        meas = dt / (SLEEP_MS / 1000.0)
        err = (meas - TICK_HZ) / TICK_HZ * 100
        print(f'--- 时基：目标跑 {SLEEP_MS} ms，Δtick = {dt} → {meas:.1f} Hz '
              f'（标称 {TICK_HZ:.0f} Hz，偏差 {err:+.2f}%；openocd 全程 {wall:.1f} s）')
        if abs(err) > 3:
            fails.append(f'时基偏差 {err:+.2f}% 超过 3%')

    print()
    for w in warns:
        print(f'  ⚠️  {w}')
    if fails:
        print('❌ 失败：')
        for x in fails:
            print('   - ' + x)
        return 1
    print('✅ 全部通过：每个变量的值与 10 kHz 时基都符合 src/main.c 的契约')
    return 0


if __name__ == '__main__':
    sys.exit(main())
