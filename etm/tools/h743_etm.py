#!/usr/bin/env python3
"""STM32H743（阿波罗板）ETMv4 + TPIU 并行 trace 编排器。

把 web/app/target.js 里已经验收过的 H7 配置路径（CoreSight 解锁 / ETM / CSTF funnel /
ETF / TPIU / PE2-PE6 复用）移植到 pyOCD，并串起 Kingst LA 采集与 OpenCSD 解码，
供命令行"一气呵成"地跑通。

寄存器地址依据 RM0433（H743，与 RM0455 的 H7B0 同布局，已核对）：
  ETM 0xE0041000 / funnel 0x5C013000 / ETF 0x5C014000 / TPIU 0x5C015000
  TRACECLK 由 PLL1R 派生：TRACECLK = VCO / R / 2

用法：
  python -X utf8 tools/h743_etm.py info
  python -X utf8 tools/h743_etm.py etm-on  [--resume]
  python -X utf8 tools/h743_etm.py etm-off
  python -X utf8 tools/h743_etm.py capture --record DIR --time 0.002 [--at-entry]
  python -X utf8 tools/h743_etm.py decode  --record DIR
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from pyocd.core.helpers import ConnectHelper
from pyocd.core.target import Target

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

ETM = 0xE0041000
FUNNEL = 0x5C013000
ETF = 0x5C014000
TPIU = 0x5C015000
DBGMCU_CR = 0x5C001004
DEMCR = 0xE000EDFC
DHCSR = 0xE000EDF0
AIRCR = 0xE000ED0C
FPB_CTRL = 0xE0002000
GPIOE = 0x58021000
RCC_AHB4ENR = 0x580244E0

CS_UNLOCK = 0xC5ACCE55
FLASH_LO, FLASH_HI = 0x08000000, 0x08200000


# --------------------------------------------------------------------------- #
# 基础读写
# --------------------------------------------------------------------------- #
class T:
    def __init__(self, target):
        self.t = target
        self.saved = {}          # addr -> {'before': v, 'mask': m}   仅掩码位
        self.locks = {}          # base -> 原本是否锁定
        self.raw_saved = {}      # addr -> 原值（整字）

    def r(self, addr):
        return self.t.read32(addr & 0xFFFFFFFF)

    def w(self, addr, value, verify=True):
        self.t.write32(addr & 0xFFFFFFFF, value & 0xFFFFFFFF)
        self.t.flush()
        if verify:
            actual = self.r(addr)
            if actual != (value & 0xFFFFFFFF):
                raise RuntimeError(f'寄存器回读不匹配 {addr:#010x}: {actual:#010x} != {value:#010x}')

    def set(self, addr, mask, value, verify=True):
        cur = self.r(addr)
        if addr in self.saved:
            s = self.saved[addr]
            s['before'] = (s['before'] | (cur & mask & ~s['mask'])) & 0xFFFFFFFF
            s['mask'] = (s['mask'] | mask) & 0xFFFFFFFF
        else:
            self.saved[addr] = {'before': cur & mask, 'mask': mask & 0xFFFFFFFF}
        nxt = ((cur & ~mask) | (value & mask)) & 0xFFFFFFFF
        self.w(addr, nxt, False)
        if verify and (((self.r(addr) ^ nxt) & mask) != 0):
            raise RuntimeError(f'配置回读不匹配 {addr:#010x}')

    def managed(self, addr, value, verify=True):
        return self.set(addr, 0xFFFFFFFF, value, verify)

    def save_raw(self, addr):
        if addr not in self.raw_saved:
            self.raw_saved[addr] = self.r(addr)

    def unlock(self, base):
        if base not in self.locks:
            self.locks[base] = bool(self.r(base + 0xFB4) & 2)
        self.w(base + 0xFB0, CS_UNLOCK, False)
        if self.r(base + 0xFB4) & 2:
            raise RuntimeError(f'CoreSight 解锁失败 {base:#x}')

    def wait(self, addr, mask, value, what='寄存器', tries=200, delay=0.005):
        for _ in range(tries):
            v = self.r(addr)
            if (v & mask) == value:
                return v
            time.sleep(delay)
        raise RuntimeError(f'等待 {what} 超时：{addr:#010x} 当前 {v:#010x}（要 & {mask:#x} == {value:#x}）')


# --------------------------------------------------------------------------- #
# 连接
# --------------------------------------------------------------------------- #
def connect():
    session = ConnectHelper.session_with_chosen_probe(
        target_override='cortex_m',
        options={'frequency': 1000000, 'connect_mode': 'attach',
                 'auto_unlock': False, 'resume_on_disconnect': True})
    if session is None:
        raise RuntimeError('找不到 CMSIS-DAP 探针')
    session.open()
    return session


def elf_symbol(elf, name):
    from elftools.elf.elffile import ELFFile
    with open(elf, 'rb') as fh:
        symtab = ELFFile(fh).get_section_by_name('.symtab')
        for s in symtab.iter_symbols():
            if s.name == name:
                return s['st_value'] & ~1
    raise RuntimeError(f'ELF 里没有符号 {name}')


# --------------------------------------------------------------------------- #
# ETM / TPIU 配置（对齐 target.js configure()）
# --------------------------------------------------------------------------- #
def stop_fifo(t):
    if t.r(ETF + 0x20) & 1:
        t.managed(TPIU + 0x304, 3)
        t.managed(ETF + 0x304, t.r(ETF + 0x304) | 0x1000)
        t.managed(ETF + 0x304, t.r(ETF + 0x304) | 0x40, False)
        t.wait(ETF + 0x304, 0x40, 0, 'ETF 清 FIFO')
        t.wait(ETF + 0xC, 4, 4, 'ETF ready')
    t.managed(ETF + 0x20, 0)
    t.wait(ETF + 0xC, 4, 4, 'ETF ready')


def configure_trace(t, traceid=2, pin_speed=3):
    """返回 (解码器配置 dict, 快照 dict)。调用前目标必须已 halt。"""
    # ① trace 引脚：PE2..PE6 = AF0(TRACE)
    t.unlock(ETM)
    t.unlock(FUNNEL)
    t.unlock(ETF)
    t.unlock(TPIU)
    t.set(RCC_AHB4ENR, 1 << 4, 1 << 4)          # GPIOE 时钟
    mask = 0x3FF0                                # PE2..PE6 每脚 2/4 位
    t.set(GPIOE, mask, 0x2AA0)                   # MODER = AF
    t.set(GPIOE + 4, 0x7C, 0)                    # OTYPER 推挽
    t.set(GPIOE + 8, mask, pin_speed * 0x1550)   # OSPEEDR：3 -> very high
    t.set(GPIOE + 12, mask, 0)                   # PUPDR 无上下拉
    t.set(GPIOE + 0x20, 0x0FFFFF00, 0)           # AFR[0] = AF0
    # ② DBGMCU 门控 + DEMCR TRCENA
    t.set(DBGMCU_CR, 0x700000, 0x700000)
    t.set(DEMCR, 1 << 24, 1 << 24)
    # ③ ETM 上电并复位
    t.w(ETM + 0x310, 0, False)
    t.wait(ETM + 0x314, 2, 0, 'ETM 上电')
    t.managed(ETM + 4, 0)
    t.wait(ETM + 0xC, 1, 1, 'ETM idle')
    t.managed(FUNNEL, 0x301)                     # ETM 走 S0
    stop_fifo(t)
    # ④ ETF：硬件 FIFO 直通 TPIU
    t.managed(ETF + 0x28, 2)
    t.managed(ETF + 0x304, 3)
    t.managed(ETF + 0x20, 1)
    # ⑤ TPIU：4 位并行
    if not (t.r(TPIU + 0x300) & 2):
        t.managed(TPIU + 0x304, t.r(TPIU + 0x304) | 0x1040, False)
        t.wait(TPIU + 0x300, 2, 2, 'TPIU formatter stopped')
    t.managed(TPIU + 0x204, 0)                   # 关测试图案
    t.managed(TPIU + 4, 8)                       # CSPSR：one-hot bit3 = 4 位
    t.managed(TPIU + 0x400, 256, False)          # 同步间隔
    t.managed(TPIU + 0x304, 3)                   # 连续格式化输出
    # ⑥ ETM 配置（ETMv4 / Cortex-M7）
    fixed = t.r(ETM + 0x10) & ~0x1018
    t.managed(ETM + 0x10, fixed)                 # CONFIGR：不要 branch broadcast/cycle count
    t.managed(ETM + 0x20, 0)                     # TRCSTALLCTLR
    t.managed(ETM + 0x24, 0)                     # TRCTSCTLR
    t.managed(ETM + 0x2C, 0)                     # TRCSYNCPR? (对齐 target.js 的 0x2c)
    t.managed(ETM + 0x40, traceid)               # TRCTRACEIDR
    t.managed(ETM + 0x88, 0)                     # TRCVIIECTLR
    t.managed(ETM + 0x8C, 0)                     # TRCVISSCTLR
    t.managed(ETM + 0x80, 0x201)                 # TRCVICTLR：always true + started
    t.managed(ETM + 4, 1)                        # TRCPRGCTLR：开 trace
    info = {}
    for name, off in (('idr0', 0x1E0), ('idr1', 0x1E4), ('idr2', 0x1E8), ('idr8', 0x180),
                      ('idr9', 0x184), ('idr10', 0x188), ('idr11', 0x18C), ('idr12', 0x190),
                      ('idr13', 0x194), ('config', 0x10), ('traceid', 0x40)):
        info[name] = t.r(ETM + off)
    if ((info['idr1'] >> 8) & 0xF) != 4:
        raise RuntimeError(f"ETM 架构不是 v4：IDR1={info['idr1']:#010x}")
    t.set(DEMCR, 1 << 24, 1 << 24)
    return info, dict(t.saved), dict(t.locks)


def restore_trace(t, snapshot=None):
    saved = snapshot or t.saved
    for base, locked in t.locks.items():
        t.w(base + 0xFB0, CS_UNLOCK, False)
    # ETM 先停
    if ETM + 4 in saved:
        t.w(ETM + 0x310, 0, False)
        t.managed(ETM + 4, 0)
        t.wait(ETM + 0xC, 1, 1, 'ETM idle')
    if ETF + 0x20 in saved:
        stop_fifo(t)
    for addr in reversed(list(saved)):
        s = saved[addr]
        t.w(addr, ((t.r(addr) & ~s['mask']) | (s['before'] & s['mask'])) & 0xFFFFFFFF, False)
    for base, locked in t.locks.items():
        if locked:
            t.w(base + 0xFB0, 0, False)


# --------------------------------------------------------------------------- #
# 复位后停在某个符号（用 FPB 硬件断点，目标.js reset() 的同款做法）
# --------------------------------------------------------------------------- #
def halt_at_symbol(session, address, timeout=8.0):
    """复位并停在某个符号。

    注意顺序：pyOCD 的 reset() 会让核**保持运行**，而系统复位会清掉 FPB 比较器，
    所以必须 reset_and_halt() → 重新装断点 → 再放行，否则断点根本不会命中。
    """
    target = session.target
    target.reset_and_halt()
    try:
        target.remove_all_breakpoints()
    except Exception:
        pass
    target.set_breakpoint(address)
    target.resume()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if target.get_state() == Target.State.HALTED:
            pc = target.read_core_register('pc') & ~1
            # 必须撤掉比较器：PC 正停在断点那条指令上，FPB 是"取指命中就停"，
            # 不摘掉直接放行会立刻再命中，目标永远卡在入口（表现为 USB 不枚举）。
            try:
                target.remove_breakpoint(address)
            except Exception:
                target.remove_all_breakpoints()
            if pc != (address & ~1):
                raise RuntimeError(f'复位后停在 {pc:#010x}，不是 {address:#010x}')
            return pc
        time.sleep(0.005)
    raise RuntimeError(f'复位后没有命中 {address:#010x}（超时）')


# --------------------------------------------------------------------------- #
# LA 采集
# --------------------------------------------------------------------------- #
def start_la(csv_path, rate, seconds, channels=(0, 1, 2, 3, 4), threshold=1.65, trigger=None,
             export_span=None):
    # 清掉上一次可能遗留的采集状态（VIS 对 "sampling in progress" 会直接 NAK 导出）
    subprocess.run([sys.executable, '-u', '-X', 'utf8', str(ROOT / 'tools/la/kingst_la.py'), 'stop'],
                   cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    # -u 必须加：子进程 stdout 是管道，块缓冲会让"开始采集"这行迟迟不吐出来
    cmd = [sys.executable, '-u', '-X', 'utf8', str(ROOT / 'tools/la/kingst_la.py'), 'capture',
           '--rate', str(rate), '--time', str(seconds),
           '--threshold', str(threshold), '--channels', *[str(c) for c in channels],
           '--out', str(csv_path), '--stats']
    if trigger:
        cmd += ['--trigger', trigger]
    if export_span:
        # 只导这段窗口：长采集（秒级、上亿样本）整段导 CSV 会是 GB 级；
        # LA buffer 与 .kvdat 仍然保留完整数据，其它窗口事后用 'span' 再导。
        cmd += ['--time-span', str(export_span[0]), str(export_span[1])]
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
    # 等 LA 真正开始采样（"开始采集" 那行之后 start 已经发出）
    deadline = time.time() + 60
    lines = []
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.02)
            continue
        lines.append(line.rstrip())
        print('  [LA] ' + line.rstrip())
        if '开始采集' in line:
            return proc, lines
    proc.kill()
    raise RuntimeError('LA 采集没有进入采样状态：' + ' | '.join(lines[-5:]))


def target_ports(vid=0xF055, pid=0x7431):
    from serial.tools import list_ports
    return [p.device for p in list_ports.comports() if p.vid == vid and p.pid == pid]


def echo_when_ready(names, text='hello world!\n', budget=8.0, repeat=1, interval=0.005):
    """放行 CPU 之后把 hello world!\\n 写进 CDC 口；返回 (首次耗时, 端口, 回读, 写入相对时刻表)。

    目标会重新枚举：先拿复位前记下的端口名直连（打不开会立刻失败，很快），
    每隔一会儿再扫一次端口列表兜底 —— 只靠 list_ports 轮询会慢几百毫秒。

    repeat > 1 时在同一个已打开的句柄上连续写（DTR 不再抖动、不会重新枚举）：
    echo 的 CPU 活动只有十几微秒，用毫秒级的时间窗去"扫"它是几乎必定落空的，
    只有让 echo 密集重复，任意窗里才会正好包住一次。
    """
    import serial
    t0 = time.time()
    last = ''
    names = list(names or [])
    n = 0
    while time.time() - t0 < budget:
        n += 1
        if n % 25 == 0 or not names:
            for p in target_ports():
                if p not in names:
                    names.insert(0, p)
        for name in names:
            try:
                with serial.Serial(name, 115200, timeout=0.15, write_timeout=0.2) as sp:
                    sp.dtr = True
                    time.sleep(0.015)
                    sp.reset_input_buffer()
                    first_delay, readback, writes = None, '', []
                    for i in range(max(1, repeat)):
                        sp.write(text.encode())
                        writes.append(round(time.time() - t0, 6))
                        if i == 0:
                            time.sleep(0.05)
                            readback = sp.read(512).decode('utf-8', 'replace')
                            first_delay = time.time() - t0
                        else:
                            time.sleep(interval)
                return first_delay, name, readback, writes
            except Exception as exc:                     # noqa: BLE001
                last = str(exc)
        time.sleep(0.002)
    return None, last, '', []



# --------------------------------------------------------------------------- #
def cmd_info(args):
    session = connect()
    try:
        target = session.target
        target.halt()
        t = T(target)
        cpuid = t.r(0xE000ED00)
        devid = t.r(0x5C001000)
        print(f'CPUID     = {cpuid:#010x}  (part {(cpuid>>4)&0xFFF:#05x})')
        print(f'DBGMCU ID = {devid:#010x}  DEV_ID {devid&0xFFF:#05x} REV {(devid>>16)&0xFFFF:#06x}')
        for name, addr in (('RCC_CR', 0x58024400), ('RCC_PLLCFGR', 0x5802442C),
                           ('RCC_PLL1DIVR', 0x58024430), ('RCC_D2CCIP2R', 0x58024454),
                           ('RCC_D1CFGR', 0x58024418), ('DBGMCU_CR', DBGMCU_CR),
                           ('PWR_CR3', 0x5802480C)):
            print(f'{name:14s} = {t.r(addr):#010x}')
        divr = t.r(0x58024430)
        q = (divr >> 16) & 0x7F
        r = (divr >> 24) & 0x7F
        pllcfgr = t.r(0x5802442C)
        print(f'PLL1 Q={q+1} R={r+1}  DIVQ1EN={1 if pllcfgr & (1<<17) else 0} '
              f'DIVR1EN={1 if pllcfgr & (1<<18) else 0}')
        t.unlock(ETM)
        idr1 = t.r(ETM + 0x1E4)
        print(f'ETM IDR1  = {idr1:#010x}  arch v{(idr1>>8)&0xF}')
        t.w(ETM + 0x310, 0, False)
        t.wait(ETM + 0x314, 2, 0, 'ETM 上电')
        idr0 = t.r(ETM + 0x1E0)
        print(f'ETM IDR0  = {idr0:#010x}  TRCPDSR={t.r(ETM+0x314):#010x}')
        print(f'TPIU CSPSR= {t.r(TPIU+4):#010x}  TPIU DEVID={t.r(TPIU+0xFC8):#010x}')
        t.w(ETM + 0x310, 1, False)
    finally:
        session.close()


def cmd_etm_on(args):
    session = connect()
    try:
        target = session.target
        target.halt()
        t = T(target)
        info, saved, locks = configure_trace(t, traceid=args.traceid)
        print(json.dumps({'etm': info, 'saved': {hex(k): v for k, v in saved.items()}},
                         indent=2, ensure_ascii=False))
        if args.resume:
            target.resume()
    finally:
        session.close()


def cmd_capture(args):
    record = Path(args.record).resolve()
    record.mkdir(parents=True, exist_ok=True)
    csv_path = record / 'capture.csv'
    if csv_path.exists() and not args.overwrite:
        raise SystemExit(f'{csv_path} 已存在（要覆盖加 --overwrite）')
    elf = Path(args.elf).resolve()
    session = connect()
    meta = {'rate': args.rate, 'seconds': args.time, 'elf': str(elf),
            'at_entry': args.at_entry, 'echo': args.echo}
    try:
        target = session.target
        target.halt()
        t = T(target)
        # 复位前先记下当前 CDC 口名：复位后 USB 会掉，之后靠它快速重连
        known_ports = target_ports()
        print('复位前已知目标 CDC 口:', known_ports)
        meta['known_ports'] = known_ports
        if args.at_entry:
            sym = elf_symbol(elf, args.at_entry)
            pc = halt_at_symbol(session, sym)
            print(f'复位后停在 {args.at_entry} = {pc:#010x}')
            meta['entry_pc'] = pc
        info, saved, _ = configure_trace(t, traceid=args.traceid)
        print('ETM 已配置（目标保持暂停）:', json.dumps(info, ensure_ascii=False))
        meta['etm'] = info
        meta['saved'] = {hex(k): v for k, v in saved.items()}
        shutil.copyfile(elf, record / 'firmware.elf')
        (record / 'meta.json').write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                                          encoding='utf-8')
        if args.trigger:
            # 有触发：先武装 LA（等触发）再放行 CPU，t=0 精确落在"CPU 开始产生 trace"
            proc, _ = start_la(csv_path, args.rate, args.time, trigger=args.trigger,
                               export_span=args.export_span)
            time.sleep(0.05)
            target.resume()
        else:
            # 无触发：必须先放行再武装 —— LA 的 start 会立刻开始消耗采样窗，
            # 反过来做的话整个窗口都落在"CPU 还停着"的空档里（短窗尤其致命）。
            target.resume()
            time.sleep(0.02)
            proc, _ = start_la(csv_path, args.rate, args.time, export_span=args.export_span)
        run_t0 = time.time()
        print(f'CPU 已放行（{run_t0:.3f}）')
        if args.echo:
            delay, port, readback, writes = echo_when_ready(
                known_ports, 'hello world!\n', budget=args.echo_budget,
                repeat=args.echo_repeat, interval=args.echo_interval)
            meta['echo'] = {'delay': delay, 'port': port, 'read': readback,
                            'repeat': args.echo_repeat, 'interval': args.echo_interval,
                            'writes': writes}
            print(f'  回显注入 delay={delay} 端口={port} 回读={readback!r} '
                  f'连续写 {len(writes)} 次，间隔 {args.echo_interval}s')
        out = proc.communicate(timeout=args.time + 180)[0]
        print(out.strip().splitlines()[-1] if out.strip() else '')
        print(f'LA 退出码 {proc.returncode}')
        meta['la_stdout_tail'] = out.strip().splitlines()[-12:]
        target.halt()
        restore_trace(t, saved)
        print('已恢复采集前的 trace 配置')
    finally:
        (record / 'meta.json').write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                                          encoding='utf-8')
        session.close()
    if args.decode:
        cmd_decode(args)


def cmd_restore(args):
    session = connect()
    try:
        recorder = json.loads((Path(args.record) / 'meta.json').read_text(encoding='utf-8'))
        target = session.target
        target.halt()
        t = T(target)
        saved = {int(k, 16): v for k, v in recorder['saved'].items()}
        for base in (ETM, FUNNEL, ETF, TPIU):
            t.unlock(base)
        restore_trace(t, saved)
        print('已恢复')
    finally:
        session.close()


def cmd_decode(args):
    record = Path(args.record).resolve()
    meta = json.loads((record / 'meta.json').read_text(encoding='utf-8'))
    config = {k: int(v) for k, v in meta['etm'].items()}
    if getattr(args, 'stream', False):
        from decode_stream import decode_capture_stream, export_simplified_stream
        result = decode_capture_stream(record / 'capture.csv', record / 'firmware.elf',
                                       record, config=config, fraction=args.fraction,
                                       max_events=args.max_events or None)
        rows = export_simplified_stream(record, record / 'etm-simplified.c')
        print(json.dumps(result['summary'], indent=2, ensure_ascii=False))
        print('framing:', json.dumps({k: v for k, v in result['framing'].items()
                                     if k in ('phase', 'frames')}, ensure_ascii=False))
        print('quality:', json.dumps(result['quality'], ensure_ascii=False))
        print(f'流式产物：decoded.json / events.jsonl / sources.json；简化 C {rows} 行')
        return result
    from decode_trace import decode_capture
    result = decode_capture(record / 'capture.csv', record / 'firmware.elf', record,
                            config=config, fraction=args.fraction,
                            glitch_ns=getattr(args, 'filter_ns', 0.0))
    print(json.dumps(result['summary'], indent=2, ensure_ascii=False))
    print(json.dumps(result['status'], ensure_ascii=False))
    print('framing:', json.dumps({k: v for k, v in result['framing'].items()
                                  if k in ('phase', 'frames', 'bytes', 'sources')},
                                 ensure_ascii=False))


def cmd_span(args):
    """从同一次采集的 LA buffer 里导出 [start, end] 秒的时间窗，再独立解码。

    触发点为 0。长窗会超过解码器的事件上限，所以"一气呵成"的采集按窗口分段看。
    """
    if not getattr(args,'live_buffer',False):
        raise ValueError('span 会导出当前 LA 缓冲，不会自动加载记录中的 KVDAT；请先确认对应波形已加载，再显式传 --live-buffer，避免把不同采集的波形与 ELF 错配')
    from decode_trace import decode_capture, export_source
    record = Path(args.record).resolve()
    meta = json.loads((record / 'meta.json').read_text(encoding='utf-8'))
    tag = f'{args.start:g}-{args.end:g}'.replace('.', 'p')
    out = record / f'span-{tag}.csv'
    subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'tools/la/kingst_la.py'), 'export',
                    '--out', str(out), '--channels', '0', '1', '2', '3', '4',
                    '--time-span', str(args.start), str(args.end)],
                   cwd=str(ROOT), check=True, timeout=900)
    work = record / f'span-{tag}'
    config = {k: int(v) for k, v in meta['etm'].items()}
    result = decode_capture(out, record / 'firmware.elf', work, config=config,
                            fraction=args.fraction,
                            glitch_ns=getattr(args, 'filter_ns', 0.0))
    (work / 'execution.c').write_text(export_source(result), encoding='utf-8')
    (work / 'simplified.c').write_text(export_source(result, True), encoding='utf-8')
    print(json.dumps(result['summary'], indent=2, ensure_ascii=False))
    print(json.dumps(result['status'], ensure_ascii=False))
    print(f'窗口 {args.start}~{args.end}s 记录: {work}')


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='action', required=True)
    sub.add_parser('info').set_defaults(func=cmd_info)
    a = sub.add_parser('etm-on')
    a.add_argument('--traceid', type=int, default=2)
    a.add_argument('--resume', action='store_true')
    a.set_defaults(func=cmd_etm_on)
    a = sub.add_parser('etm-off')
    a.add_argument('--record', default=str(ROOT / 'captures/h743'))
    a.set_defaults(func=cmd_restore)
    a = sub.add_parser('capture')
    a.add_argument('--record', required=True)
    a.add_argument('--elf', default=str(ROOT / 'firmware/stm32h743_cdc_trace/build/fw.elf'))
    a.add_argument('--time', type=float, default=0.002)
    a.add_argument('--rate', type=int, default=500000000)
    a.add_argument('--traceid', type=int, default=2)
    a.add_argument('--fraction', type=float, default=0.25)
    a.add_argument('--filter-ns', type=float, default=0.0,
                   help='解码前软件滤波（ns，0=关闭；有窄脉冲的记录建议 4')
    a.add_argument('--at-entry', default=None, help='复位后停在该 ELF 符号再配 ETM')
    a.add_argument('--trigger', default=None,
                   help='整串传给 LA 的 --trigger，例如 "--reset --neg-edge 1"')
    a.add_argument('--export-span', type=float, nargs=2, default=None, metavar=('T0', 'T1'),
                   help='只导出这段窗口（秒，从采样起点算）；长采集必须给，否则 CSV 会是 GB 级')
    a.add_argument('--echo', action='store_true', help='放行后往 CDC 口写 hello world!\\n')
    a.add_argument('--echo-budget', type=float, default=8.0)
    a.add_argument('--echo-repeat', type=int, default=1,
                   help='连续写多少次（echo 的 CPU 活动只有十几微秒，要扫到必须密集重复）')
    a.add_argument('--echo-interval', type=float, default=0.005, help='重复写的间隔（秒）')
    a.add_argument('--overwrite', action='store_true')
    a.add_argument('--decode', action='store_true')
    a.set_defaults(func=cmd_capture)
    a = sub.add_parser('decode')
    a.add_argument('--record', required=True)
    a.add_argument('--fraction', type=float, default=0.25)
    a.add_argument('--filter-ns', type=float, default=0.0,
                   help='解码前软件滤波（ns，0=关闭；有窄脉冲的记录建议 4')
    a.add_argument('--stream', action='store_true',
                   help='流式展开并分页；CSV/ELF/调用树仍占内存，同时导出简化 C')
    a.add_argument('--max-events', type=int, default=0, help='流式模式下的可选上限，0=无上限')
    a.set_defaults(func=cmd_decode)
    a = sub.add_parser('span')
    a.add_argument('--live-buffer',action='store_true',help='确认当前 LA 波形确属该记录；否则拒绝导出')
    a.add_argument('--record', required=True)
    a.add_argument('--start', type=float, required=True)
    a.add_argument('--end', type=float, required=True)
    a.add_argument('--fraction', type=float, default=0.25)
    a.add_argument('--filter-ns', type=float, default=0.0,
                   help='解码前软件滤波（ns，0=关闭；有窄脉冲的记录建议 4')
    a.set_defaults(func=cmd_span)
    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
