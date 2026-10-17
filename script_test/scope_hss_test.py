"""J-Scope 探针侧 HSS 采样 —— 端到端验收脚本（不等网页，先在命令行跑通）。

控制面走 HID 0x32（与网页 app/scope/protocol.js 逐字节一致），数据面用 pyusb 直接
读 interface 0 上的 bulk IN **0x83**（网页走 WebUSB，同一根管子）。
CDC/串口桥开关走 HID **0x34**（`--bridge`），也可以让固件在采样期间自己关
（`--flags 0x20` = SCOPE_FLAG_CDC_OFF）。

用法:
  python scope_hss_test.py status [--bridge on|off]
  python scope_hss_test.py bench  [--set pack|cross|one] [--clock 45000000] [--iters 2000]
  python scope_hss_test.py discard --set one --period 2.5 --secs 5 --flags 0xA1
  python scope_hss_test.py run    [--set pack|cross|one] [--period 100] [--secs 3] [--flags 0x20]

  --flags 位: 0x01 允许 60 MHz / 0x02 丢弃(只采样不推 USB) / 0x08 不让路
              0x10 SWD 空闲拍压 0 / 0x20 采样期间自动关 CDC 桥 / 0x80 单字短批次
  常用组合: 0x20 = 最快端到端；0x22 = 只量探针本体（丢包就全是探针 CPU 的账）

靶子固件: web-serial-rtt-tools/tools/target-firmware/stm32f103_scope（10 kHz 契约波形）
"""
import sys

# ⚠️ 必须在任何中文 print 之前：GBK 控制台编不出 ⚠️/★ 之类的字符会抛 UnicodeEncodeError，
#    直接把脚本自己打崩（曾经因此把一次正常的 cross 采样误判成固件故障）。
#    原来这段写在 `import sys` **之前**，NameError 被 except 吞掉 → 一直是失效的。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import argparse
import os
import struct
import threading
import time

import math

# Hardware dependencies are loaded in main, so protocol/window tests need no device.
hid = usb = None

VID, PID = 0x0D28, 0x0204
EP_IN = 0x83
HID_CMD = 0x32
CMD_BRIDGE = 0x34

ACT = {'STOP': 0, 'START': 1, 'STATUS': 2, 'CLOCK': 3, 'TRIGGER': 4,
       'CONFIG': 7, 'BENCH': 8, 'BENCH_RESULT': 9, 'CONFIG_TICKS': 10, 'METRICS': 11}

BRIDGE = {'STATUS': 0, 'SET': 1}

KIND = {1: 'DEF', 2: 'DATA', 3: 'STAT', 4: 'EVT'}
MAGIC = 0x4A53
PACKET, HEADER, PAYLOAD = 512, 16, 496

TYPES = {0: ('B', 1), 1: ('b', 1), 2: ('H', 2), 3: ('h', 2),
         4: ('I', 4), 5: ('i', 4), 6: ('f', 4), 7: ('d', 8)}

# 靶子固件 stm32f103_scope 的变量契约（tools/target-firmware/stm32f103_scope/README.md）
V_PACK = [  # g_pack 那 8 个字段 → 一个 span（24 B，中间有 2 B 填充）
    ("f_sin",  0x20001014, 4, 6), ("f_tri",  0x20001018, 4, 6),
    ("i_tick", 0x2000101c, 4, 5), ("u_ramp", 0x20001020, 2, 2),
    ("i_sq1k", 0x20001022, 2, 3), ("u_cnt",  0x20001024, 1, 0),
    ("i_saw",  0x20001025, 1, 1), ("u_hi",   0x20001028, 4, 4),
]
V_ONE = [   # 单个 u32（g_tick，10 kHz 斜坡）→ 1 字 span，测"最少传输"下的上限
    ("g_tick", 0x20001044, 4, 4),
]
V_CROSS = [  # span A(0x20000000,14B) + span B(0x20001010,56B)：跨 span 的慢路径
    ("g_lfsr",     0x20000000, 4, 4), ("g_far_cnt",  0x20000008, 4, 4),
    ("g_far_sq100", 0x2000000c, 2, 3), ("g_isr_count", 0x20001010, 4, 4),
    ("i_tick",     0x2000101c, 4, 5), ("g_pair_a",   0x2000102c, 2, 2),
    ("g_pair_b",   0x2000102e, 2, 2), ("g_tick",     0x20001044, 4, 4),
]
V_MIXED = [  # 🚨 混合：一个 3 字 span（自增块读）+ 一个单字 span。
             # 单字快路径（AddrInc=0 + 抱住 TAR）**必须不能启用** —— 一启用就会让 CSW
             # 在自增/不自增之间来回切，切一次赔 2 次传输。这个用例就是守那条守卫的：
             # 采样结果照旧要对（g_tick 斜率），M0 也应该跟"全是单字"明显不同。
    ("g_lfsr",     0x20000000, 4, 4),   # + g_far_cnt @+8 → 并成 0x20000000..0x2000000b 的 3 字 span
    ("g_far_cnt",  0x20000008, 4, 4),
    ("g_tick",     0x20001044, 4, 4),   # 远处孤零零一个字
]
V_TWO = [   # 🚨 两个**远离**的单字 span：都是"4 字节直读"，但地址不同 ⇒ 每拍都得重写 TAR，
            # "抱住 TAR"的前提不成立。守卫必须是 **s_nspans == 1**，不能只是"所有 span
            # 都是单字"：那种情况下 swd_read_word_held 的 TAR 写带一次 RDBUFF 收尾
            # （2 次传输），比 swd_read_block 的裸 TAR 写（1 次）还贵 ⇒ 反而更慢。
    ("g_lfsr",     0x20000000, 4, 4),
    ("g_tick",     0x20001044, 4, 4),
]

# HPM6800EVK（HPM6880，RISC-V/JTAG）靶子 script_test/hpm6800evk_scope：
# 8 × u32 = 32 B，**偏移**在这里，基址用 --base 给（构建后用 nm 查 g_v）。
# 靶子侧波形全部由 g_tick 算出来，所以能逐字段精确反算核对（见 verify_rv()）。
V_RV = [
    ("g_tick",  0, 4, 4),   # 10 kHz 计数（100 µs 一拍）
    ("u_hi",    4, 4, 4),   # 0x10000000 | (t & 0xFFFF)
    ("f_sin",   8, 4, 6),   # 500 Hz 正弦（20 点表）
    ("f_tri",  12, 4, 6),   # 500 Hz 三角
    ("i_sq1k", 16, 4, 5),   # 1 kHz 方波 ±1000
    ("i_sq5k", 20, 4, 5),   # 5 kHz 方波 ±1000（NYQUIST 陷阱）
    ("u_ramp", 24, 4, 4),   # t % 1000
    ("lfsr",   28, 4, 4),   # 32 位 LFSR，每拍一步
]

# 与靶子 src/main.c 里的 kSin20 同一张表（契约的一部分）
RV_SIN20 = [0.0, 0.309017, 0.587785, 0.809017, 0.951057,
            1.0, 0.951057, 0.809017, 0.587785, 0.309017,
            0.0, -0.309017, -0.587785, -0.809017, -0.951057,
            -1.0, -0.951057, -0.809017, -0.587785, -0.309017]


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT (%ss)" % sec)
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


def open_hid():
    for _ in range(30):
        cand = [i for i in hid.enumerate(VID, PID) if i.get("usage_page") == 0xFF00]
        if cand:
            d = hid.device()
            d.open_path(cand[0]["path"])
            d.set_nonblocking(1)
            time.sleep(0.2)
            return d
        time.sleep(0.3)
    raise RuntimeError("no HID")


def hid_xfer(dev, data, tmo=3.0):
    """data = [action, ...]；返回**已剥掉 Report ID** 的响应（与网页 xfer() 同语义：
    res[1] === cmd，res[2] 是 payload 第 0 字节）。"""
    req = [0x01, 2 + len(data), HID_CMD] + list(data)
    req += [0] * (64 - len(req))
    dev.write(req)
    t0 = time.time()
    while time.time() - t0 < tmo:
        r = dev.read(64, timeout_ms=200)
        if r and r[0] == 0x02 and r[2] == HID_CMD:
            return list(r)[1:]
    return None


def hid_xfer_raw(dev, cmd, data, tmo=3.0):
    """任意命令号的 HID 收发；返回**已剥掉 Report ID** 的响应。"""
    req = [0x01, 2 + len(data), cmd] + list(data)
    req += [0] * (64 - len(req))
    dev.write(req)
    t0 = time.time()
    while time.time() - t0 < tmo:
        r = dev.read(64, timeout_ms=200)
        if r and r[0] == 0x02 and r[2] == cmd:
            return list(r)[1:]
    return None


def bridge_set(dev, enable):
    """HID 0x34：主循环级 CDC/串口桥开关。返回设置后的实际状态，None = 无响应。"""
    r = hid_xfer_raw(dev, CMD_BRIDGE, [BRIDGE['SET'], 1 if enable else 0])
    if not r:
        return None
    return bool(r[3] & 1)


def bridge_get(dev):
    r = hid_xfer_raw(dev, CMD_BRIDGE, [BRIDGE['STATUS']])
    return None if not r else bool(r[3] & 1)


def s8(v):
    v &= 0xFF
    return v - 256 if v > 127 else v


def status(dev):
    r = hid_xfer(dev, [ACT['STATUS']])
    if not r:
        return None
    w = [int.from_bytes(bytes(r[3 + i * 4:7 + i * 4]), "little") for i in range(12)]
    return {
        'startRc': s8(r[2]), 'running': w[0] & 1, 'riscv': (w[0] >> 1) & 1,
        'supportsTicks': bool(w[0] & 4), 'supportsBatch': bool(w[0] & 8),
        'supportsMetrics': bool(w[0] & 16), 'spans': (w[0] >> 8) & 0xFF,
        'swdReady': (w[0] >> 16) & 1, 'nvars': (w[0] >> 24) & 0xFF,
        'swdHz': w[1], 'produced': w[2], 'dropped': w[3],
        'swdErr': w[5] & 0xFFFF, 'yield': w[5] >> 16, 'seq': w[6],
        'planHash': w[8], 'periodUs': (w[11] & 0xFFFF) / (24.0 if w[11] & (1 << 17) else 1), 'discard': bool(w[11] & (1 << 16)),
        'swdMhz': (w[11] >> 24) & 0xFF, 'raw': w,
    }


def config_data(period_us, vars_, flags=0):
    period_us = float(period_us)
    if not math.isfinite(period_us) or not 2 <= period_us <= 1000000:
        raise ValueError('period must be finite, 2..1000000 us')
    ticks = not period_us.is_integer()
    period = int(math.floor(period_us * 24 + 0.5)) if ticks else int(period_us)
    d = [ACT['CONFIG_TICKS'] if ticks else ACT['CONFIG']]
    d += list(struct.pack('<I', period)) + [flags & 0xFF, len(vars_)]
    for _, addr, size, typ in vars_:
        d += list(struct.pack('<I', addr)) + [size, typ]
    if len(d) > 61:
        raise ValueError('configuration exceeds HID report')
    return d


def do_config(dev, period_us, vars_, flags=0):
    d = config_data(period_us, vars_, flags)
    if d[0] == ACT['CONFIG_TICKS'] or flags & 0x80:
        st = status(dev)
        if not st or (d[0] == ACT['CONFIG_TICKS'] and not st['supportsTicks']):
            raise RuntimeError('firmware does not support tick periods')
        if flags & 0x80 and not st['supportsBatch']:
            raise RuntimeError('firmware does not support FAST_BATCH; upgrade or clear flags bit7')
    r = hid_xfer(dev, d)
    if not r or s8(r[2]) < 0:
        raise RuntimeError('configuration rejected: %s' % (None if not r else s8(r[2])))
    return True


def u32_delta(after, before):
    return (after - before) & 0xffffffff


def snapshot(dev, modern):
    before = time.perf_counter()
    if modern:
        r = hid_xfer(dev, [ACT['METRICS']])
        after = time.perf_counter()
        if not r or len(r) < 51:
            raise RuntimeError('metrics response missing/truncated')
        w = struct.unpack_from('<12I', bytes(r), 3)
        if w[0] != 0x31535348 or w[2] != 24000000:
            raise RuntimeError('invalid metrics magic/frequency')
        return dict(tick=w[1], hz=w[2], produced=w[3], skipped=w[4], usb=w[5],
                    swd=w[6], yield_=w[7], tx=w[8], bytes=w[9], period_ticks=w[10],
                    host=(before+after)/2, uncertainty=(after-before)/2)
    st = status(dev)
    after = time.perf_counter()
    if not st:
        raise RuntimeError('status response missing')
    return dict(produced=st['produced'], dropped=st['dropped'], usb=st['raw'][4] >> 16,
                host=(before+after)/2, uncertainty=(after-before)/2)


def snapshot_delta(begin, end):
    if 'tick' in begin:
        dt = u32_delta(end['tick'], begin['tick']) / begin['hz']
        return dict(dt=dt, produced=u32_delta(end['produced'], begin['produced']),
                    skipped=u32_delta(end['skipped'], begin['skipped']),
                    usb=u32_delta(end['usb'], begin['usb']),
                    swd=u32_delta(end['swd'], begin['swd']),
                    yield_=u32_delta(end['yield_'], begin['yield_']))
    return dict(dt=end['host']-begin['host'],
                produced=u32_delta(end['produced'], begin['produced']),
                dropped=u32_delta(end['dropped'], begin['dropped']),
                usb=(end['usb']-begin['usb']) & 0xffff)


def print_probe_window(delta):
    drop = delta.get('dropped', delta.get('skipped', 0) + delta['usb'])
    dt, produced = delta['dt'], delta['produced']
    rate = produced / dt / 1000 if dt > 0 else 0
    print('探针快照窗口 %.6f s：产 %d 拍 = %.1f kHz；丢 %d 拍 = %.2f%%；USB 缓冲耗尽 %d 拍'
          % (dt, produced, rate, drop, 100*drop/max(produced+drop, 1), delta['usb']))
    if 'skipped' in delta:
        print('  scheduler skip=%d SWD errors=%d DAP yields=%d'
              % (delta['skipped'], delta['swd'], delta['yield_']))
    else:
        print('  旧固件：HID 往返中点估计窗口；USB 计数仅16位，超过65535拍无法精确还原。')


def arrival_window(raw, begin, end):
    return b''.join(b for arrived, b in raw if begin <= arrived < end)


def wait_started(dev):
    hid_xfer(dev, [ACT['START']])
    for _ in range(20):
        time.sleep(0.12)
        st = status(dev)
        if st and st['startRc'] != -100:
            if st['startRc'] != 0:
                raise RuntimeError('START failed rc=%d' % st['startRc'])
            return st
    raise RuntimeError('START timeout')


def run_discard(dev, secs, modern):
    try:
        wait_started(dev)
        begin = snapshot(dev, modern)
        time.sleep(secs)
        end = snapshot(dev, modern)
    finally:
        hid_xfer(dev, [ACT['STOP']])
    print_probe_window(snapshot_delta(begin, end))
    print('DISCARD：未认领 USB 数据接口；只测完整采样器，不是 M0 紧循环。')
    return 0


def do_bench(dev, iters=2000):
    hid_xfer(dev, [ACT['BENCH']] + list(struct.pack('<I', iters)))
    time.sleep(0.35 + iters / 40000.0)
    r = hid_xfer(dev, [ACT['BENCH_RESULT']])
    if not r:
        return None
    ticks = int.from_bytes(bytes(r[3:7]), "little")
    it = int.from_bytes(bytes(r[7:11]), "little")
    err = int.from_bytes(bytes(r[11:15]), "little", signed=True)
    # 字 3/4：**实际装载**的 SWD blob 偏移（见 Custom HID Protocol.md 第 16 条）。
    # 没这个数就分不出"时钟命令被忽略"和"生效了但没差别" —— 这个坑踩过：
    # 1 MHz 与 60 MHz 的读数一模一样，状态字却报着新频率。
    blob = int.from_bytes(bytes(r[15:19]), "little")
    return it, ticks, err, blob


# swd_ops 里的读函数偏移 → 档位（swd_blob_evklite.h 的 SWD_READ_OFFSET_*）
BLOB_TIER = {0x53C: '60M(6 指令/bit)', 0x60C: '45M(8)', 0x6E0: '36M(10)',
             0x7C4: '30M(12)', 0xA54: '20M(18)', 0x620: 'SLOW', 0xFFFFFFFF: '还没装载'}


def find_ep83():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        raise RuntimeError("探针没找到（pyusb）")
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass
    for intf in dev.get_active_configuration():
        for ep in intf:
            if ep.bEndpointAddress == EP_IN:
                return dev, intf.bInterfaceNumber, ep
    raise RuntimeError("没找到 bulk IN 0x83")


class PktStream:
    """512 B 定长自描述包；按 magic 重同步，坏包只丢自己。"""

    def __init__(self):
        self.buf = bytearray()
        self.pkts = 0
        self.resync = 0

    def push(self, chunk):
        self.buf += chunk
        out = []
        while True:
            if len(self.buf) < PACKET:
                break
            m = int.from_bytes(self.buf[0:2], "little")
            if m != MAGIC:
                i = self.buf.find(struct.pack('<H', MAGIC), 1)
                if i < 0:
                    self.buf = self.buf[-1:]
                    self.resync += 1
                    break
                self.buf = self.buf[i:]
                self.resync += 1
                continue
            p = bytes(self.buf[:PACKET])
            del self.buf[:PACKET]
            self.pkts += 1
            out.append({
                'version': p[2],
                'kind': p[3],
                'seq': int.from_bytes(p[4:8], "little"),
                't_raw': int.from_bytes(p[8:12], "little"),
                't_us': int.from_bytes(p[8:12], "little") / (24.0 if p[2] == 2 else 1),
                'n': int.from_bytes(p[12:14], "little"),
                'aux': int.from_bytes(p[14:16], "little"),
                'payload': p[HEADER:],
            })
        return out


def rv_expect(t):
    """靶子契约：给定一拍 g_tick，算出这一拍除 LFSR 外的字段该是什么。"""
    p20 = t % 20
    return {
        'u_hi':   0x10000000 | (t & 0xFFFF),
        'f_sin':  RV_SIN20[p20],
        'f_tri':  ((p20 if p20 < 10 else 20 - p20) / 10.0) - 1.0,
        'i_sq1k': 1000 if (t % 10) < 5 else -1000,
        'i_sq5k': 1000 if (t & 1) else -1000,
        'u_ramp': t % 1000,
    }


RV_ORDER = ('u_hi', 'f_sin', 'f_tri', 'i_sq1k', 'i_sq5k', 'u_ramp')


def rv_same(name, got, want):
    if isinstance(want, float):
        return abs(got - want) <= 1e-6
    return got == want


def lfsr_next(v):
    """靶子里的那个 32 位 LFSR（x^32 + x^22 + x^2 + x + 1 的右移形式）。"""
    return ((v >> 1) ^ ((0xFFFFFFFF if (v & 1) else 0) & 0x80200003)) & 0xFFFFFFFF


def verify_rv(samples):
    """逐字段精确核对 HPM6800EVK scope 靶子的契约。

    靶子的每个字段都由同一拍 `g_tick` 算出（见 hpm6800evk_scope/src/main.c），所以能
    反算核对，而不是"看着像波形"。

    🚨 但**靶子的 8 个字不是原子更新的**：探针读一整个 32 B span 要 ~30 µs，而靶子
    每 100 µs 更新一次，所以偶尔会在读的过程中推进到下一拍。此时帧内会出现
    "前 k 个字是 tick 的值、后面是 tick+1 的值"——这是**目标侧**的固有竞态
    （任何调试器都一样），不是探针读错。所以判据是：

      * 每个字都必须等于 `tick` 或 `tick+1` 推出来的值（**不允许**别的取值）；
      * 这些取值必须随字偏移**单调**（先 tick 后 tick+1）—— 顺带证明确实按地址
        顺序读、没有重排/错位；
      * LFSR 必须是那条唯一的 32 位序列（逐拍步进核对）—— 错位、串值、重排都过不去。

    任何一条不满足即为 FAIL。撕裂率单独报出来（它是采样率/总线争用的函数）。"""
    print("—— 靶子契约核对（逐字段由同一拍 g_tick 反算；允许读期间靶子推进一拍）——")
    n = len(samples)
    if n == 0:
        print("  没有样本")
        return False

    # 🚨 先确认靶子真的在动：全 0 的帧会让"f_sin == 表[0] == 0.0"和"u_ramp == 0 % 1000"
    #    这两项**假通过**（实测踩过：探针读回全 0 时它们照样报 100% 正确）。
    ticks = [s['g_tick'] for s in samples]
    if max(ticks) == 0 or len(set(ticks)) == 1:
        print("  靶子数据没在动（g_tick 恒为 %d）—— 先查靶子在不在跑 / 探针读路径（SBCS 配置）"
              % ticks[0])
        return False

    matched = dict.fromkeys(RV_ORDER, 0)     # 与 tick 或 tick+1 之一相符
    exact = 0                                 # 整帧同拍
    torn = 0                                  # 读期间靶子推进了一拍
    torn_boundary = {}                        # 撕裂点分布（第几个字开始是新的一拍）
    bad = []                                  # 无法用契约解释的样本
    gens = {}                                 # 每个样本选定/允许的"字段世代"

    for idx, s in enumerate(samples):
        t = s['g_tick']
        e0, e1 = rv_expect(t), rv_expect(t + 1)
        allow = {}
        for f in RV_ORDER:
            m0 = rv_same(f, s[f], e0[f])
            m1 = rv_same(f, s[f], e1[f])
            if m0 or m1:
                matched[f] += 1
            allow[f] = {g for g, m in ((0, m0), (1, m1)) if m}
        # 单调赋值：能选出"先 0 后 1"的序列才算合法
        prev, gen, why = 0, [], None
        for i, f in enumerate(RV_ORDER):
            pick = next((g for g in sorted(allow[f]) if g >= prev), None)
            if pick is None:
                why = "第 %d 个字 %s=0x%X 既不是 tick 也不是 tick+1 的值" % (
                    i + 1, f, s[f] if isinstance(s[f], int) else 0)
                break
            prev = pick
            gen.append(pick)
        if why is None:
            if 1 in gen:
                torn += 1
                torn_boundary[gen.index(1) + 1] = torn_boundary.get(gen.index(1) + 1, 0) + 1
            else:
                exact += 1
        else:
            bad.append((s, why))
        gens[idx] = None if why else gen

    # LFSR：只能是那条唯一的 32 位序列，逐拍步进核对（错位/串值/重排都过不去）。
    # 🚨 lfsr 是第 8 个字，撕裂同样可能"只发生在它身上"，所以它也允许推进一拍：
    #    期望值由参考点推进 (tick - tick_ref) 或 (tick + 1 - tick_ref) 步得到。
    lfsr_ok = lfsr_bad = 0
    ref = None                                # (世代, lfsr)
    for idx, s in enumerate(samples):
        gen = gens.get(idx)
        if gen is None:
            continue
        prev_gen = (gen[-1] if gen else 0)
        t = s['g_tick']
        if ref is None:
            if prev_gen != 0:
                continue                      # 撕裂帧不当参考，等一个同拍帧定基准
            ref = (t, s['lfsr'])
            continue
        cand = []
        for g in (t, t + 1):
            if g < ref[0] or (g - ref[0]) > 64:
                continue                      # 倒退/跳太远：不当候选
            v = ref[1]
            for _ in range(g - ref[0]):
                v = lfsr_next(v)
            cand.append((g, v))
        # 单调性：字段已经推进到下一拍时，lfsr 不能反而停在上一拍
        ok_c = [g for g, v in cand if v == s['lfsr'] and g >= t + prev_gen]
        if ok_c:
            lfsr_ok += 1
            ref = (max(ok_c), s['lfsr'])
        else:
            lfsr_bad += 1
            if cand:
                ref = (cand[0][0], s['lfsr'])  # 重新对齐，避免一处错报成一片

    ok = (not bad)
    for f in RV_ORDER:
        print("  %-7s %6d/%d 与 tick 或 tick+1 相符%s" % (
            f, matched[f], n, "" if matched[f] == n else "   ← %d 个不符" % (n - matched[f])))
    print("  %-7s %6d/%d 逐拍步进正确%s" % (
        "lfsr", lfsr_ok, max(lfsr_ok + lfsr_bad, 1),
        "" if lfsr_bad == 0 else "   ← %d 处断链" % lfsr_bad))
    print("  同拍帧 %d (%.1f%%)；读期间推进一拍（目标侧非原子更新）%d (%.1f%%)%s" % (
        exact, 100.0 * exact / n, torn, 100.0 * torn / n,
        "" if not torn_boundary else "，撕裂点分布 %s" %
        ", ".join("第%d字×%d" % (k, v) for k, v in sorted(torn_boundary.items()))))
    if bad:
        s, why = bad[0]
        print("  ❌ 无法解释的样本：%s" % why)
        print("     实际 tick=%d u_hi=0x%X f_sin=%r f_tri=%r i_sq1k=%d i_sq5k=%d u_ramp=%d lfsr=0x%X"
              % (s['g_tick'], s['u_hi'], s['f_sin'], s['f_tri'], s['i_sq1k'], s['i_sq5k'],
                 s['u_ramp'], s['lfsr']))
    ok = ok and (lfsr_bad == 0)
    print("  契约核对：%s" % ("PASS" if ok else "FAIL"))
    return ok


def main():
    ap = argparse.ArgumentParser()
    global hid, usb
    ap.add_argument('cmd', choices=['status', 'bench', 'run', 'discard'])
    ap.add_argument('--set', dest='vset', default='pack', choices=['pack', 'cross', 'one', 'mixed', 'two', 'rv'])
    ap.add_argument('--clock', type=int, default=0, help='SWD Hz，0=不动')
    ap.add_argument('--period', type=float, default=100, help='采样周期 us')
    ap.add_argument('--iters', type=int, default=2000)
    ap.add_argument('--flags', type=lambda s: int(s, 0), default=0, help='flags 位（0x10 = clock_delay 压 0）')
    ap.add_argument('--secs', type=float, default=3.0)
    ap.add_argument('--readsize', type=int, default=8192,
                    help='每次 ep.read 要多少字节。pyusb 是同步读：一个 URB 满了才返回，'
                         '两次 URB 之间的空档里设备发不出东西，所以小读=空档多、大读=空档少但延迟高。'
                         'pack 这种"每包只装 20 个样本"的高包率场景对这个数很敏感。')
    ap.add_argument('--bridge', choices=['on', 'off', 'keep'], default='keep',
                    help='HID 0x34：主循环 CDC/串口桥开关（off = 采样期间不用服务 COM 口，省几百周期/轮）')
    ap.add_argument('--riscv', action='store_true',
                    help='目标类型切 RISC-V/JTAG（HID CMD_RTT action 10）+ 配置里带 flags bit6。'
                         '用于 HPM6800EVK 这类只有 JTAG 的 RISC-V 目标（见 Custom HID Protocol 第 16 条）')
    ap.add_argument('--swd', action='store_true',
                    help='把全局目标类型切回 SWD/ARM（它是粘的：采过 RISC-V 之后要显式切回来）')
    ap.add_argument('--dump', type=int, default=0,
                    help='打印前 N 个解码后的样本值（拿已知内容的地址采，用来验值的正确性）')
    ap.add_argument('--addr', type=lambda s: int(s, 0), default=0,
                    help='覆盖 --set one 那个变量的地址（RISC-V 冒烟测试：随便给个会变的 RAM 地址）')
    ap.add_argument('--base', type=lambda s: int(s, 0), default=0,
                    help='用 8 个连续 u32（base+0..28）替掉整个变量表 —— 量多通道/一个多字 span 用')
    a = ap.parse_args()
    if not math.isfinite(a.secs) or not 0 < a.secs < 170:
        ap.error('--secs must be >0 and <170 (u32 timer wrap)')
    config_data(a.period, []) # Validate before touching hardware.
    import hid
    if a.cmd == 'discard': a.flags |= 0x02
    if a.cmd == 'run' and not a.flags & 0x02:
        import usb.core
        import usb.util

    vars_ = {'pack': V_PACK, 'cross': V_CROSS, 'one': V_ONE, 'mixed': V_MIXED, 'two': V_TWO,
             'rv': V_RV}[a.vset]
    if a.vset == 'rv':
        if not a.base:
            print("--set rv 需要 --base <g_v 地址>（构建后 nm 查 g_v，见 hpm6800evk_scope/README.md）")
            return 1
        vars_ = [(n, a.base + off, sz, ty) for (n, off, sz, ty) in V_RV]
    if a.addr and a.vset == 'one':
        vars_ = [("probe_addr", a.addr, 4, 4)]
    if a.base and a.vset != 'rv':
        vars_ = [("w%d" % i, a.base + i * 4, 4, 4) for i in range(8)]
    dev = open_hid()

    if a.riscv:
        # CMD_RTT action 10：全局目标类型（RTT 桥与 J-Scope 共用），0=SWD/ARM，1=RISC-V/JTAG
        r = hid_xfer_raw(dev, 0x31, [10, 1])
        time.sleep(0.05)
        if r is None:
            print("⚠️ CMD_RTT action 10 无响应（固件太旧？）")
        a.flags |= 0x40                     # SCOPE_FLAG_RISCV：强制本会话走 JTAG 后端
        print("目标类型: RISC-V/JTAG（flags 加 bit6）")
    elif a.swd:
        # 把全局目标类型切回 SWD/ARM（它是**粘**的，采过 RISC-V 之后必须显式切回来）
        r = hid_xfer_raw(dev, 0x31, [10, 0])
        time.sleep(0.05)
        if r is None:
            print("⚠️ CMD_RTT action 10 无响应（固件太旧？）")
        print("目标类型: SWD/ARM")

    if a.bridge != 'keep':
        got = bridge_set(dev, a.bridge == 'on')
        if got is None:
            print("⚠️ HID 0x34 无响应（固件太旧？）")
        else:
            print("CDC/串口桥 = %s" % ('开' if got else '关'))

    if a.clock:
        hid_xfer(dev, [ACT['CLOCK']] + list(struct.pack('<I', a.clock)))
        time.sleep(0.1)

    if a.cmd == 'status':
        st = status(dev)
        print(st)
        return 0

    do_config(dev, a.period, vars_, a.flags)
    st = status(dev)
    expect = {'pack': 1, 'one': 1, 'cross': 3, 'mixed': 2, 'two': 2, 'rv': 1}[a.vset]
    print("配置: %d 变量, period=%g us, 探针算出 %d 个 span (本地期望 %s)"
          % (len(vars_), a.period, st['spans'], expect))
    if st['spans'] != expect:
        print("⚠️ span 数与本地计划不一致 —— 检查合并规则/地址")

    if a.cmd == 'bench':
        r = do_bench(dev, a.iters)
        if not r:
            print("标定无响应"); return 1
        it, ticks, err, blob = r
        if err != 0 or it == 0:
            print("标定失败 err=%d iters=%d blob=0x%05X（-3 变量表空 / -4 读失败 / 其它=初始化码）"
                  % (err, it, blob))
            return 1
        us = (ticks / 24.0) / it
        print("**M0 标定**: %d 次 × %.3f us/样本 = %.2f ms  → 上限 ≈ %.1f kHz   [blob=0x%05X %s]"
              % (it, us, ticks / 24.0 / 1000.0, 1000.0 / us, blob, BLOB_TIER.get(blob, '?')))
        return 0

    if a.flags & 0x02:
        try:
            return run_discard(dev, a.secs, st['supportsMetrics'])
        finally:
            if a.bridge == 'off': bridge_set(dev, True)

    # ---- run：启动推流 + 读 0x83 ----
    ud, intf, ep = find_ep83()
    try:
        if ud.is_kernel_driver_active(intf):
            ud.detach_kernel_driver(intf)
    except Exception:
        pass
    usb.util.claim_interface(ud, intf)

    st = status(dev)
    stream = PktStream()
    chunks = []
    raw = []
    stop = threading.Event()

    # 🚨 读线程必须在 START **之前**起来，否则启动后那段没人读的时间（下面等 rc 的
    #    轮询至少 120 ms）里探针会把包缓冲填满并开始丢拍 —— 实测 period=40us 时
    #    dropped=3192 ≈ 120 ms × 25 kHz − 8 个缓冲，全是这一段的账，看着却像固件丢数据。
    #    同一个坑在 RTT 交付率脚本里已经踩过一次（见 docs/hpm6800evk-jtag.md §5.3）。
    #
    # 🚨 读线程里**只搬字节、不解析**：早先在读线程里直接 PktStream.push()，每个包都建
    #    dict + 496 B 的 bytes 对象，GIL 上与主线程抢，Python 那边一停顿超过
    #    8×512 B 的缓冲深度（pack 组只有 2.1 ms）就真丢数据 —— 而且丢的账会被算到
    #    固件头上（s_usb_drop）。解析挪到窗口结束之后做。
    def reader():
        while not stop.is_set():
            try:
                b = bytes(ep.read(a.readsize, timeout=200))
            except usb.core.USBTimeoutError:
                continue
            except Exception:
                break
            raw.append((time.perf_counter(), b))

    th = threading.Thread(target=reader, daemon=True)
    th.start()

    try:
        st = wait_started(dev)
    except Exception:
        hid_xfer(dev, [ACT['STOP']])
        stop.set(); th.join(timeout=1.5)
        usb.util.dispose_resources(ud)
        raise
    print('启动 rc=0')

    # Arrival window and counter window are separately bracketed; never include drain.
    try:
        try:
            st_begin = snapshot(dev, st['supportsMetrics'])
            t0 = time.perf_counter()
            time.sleep(a.secs)
            t_end = time.perf_counter()
            st_end = snapshot(dev, st['supportsMetrics'])
            st = status(dev) # diagnostic only; never used in the rate delta
        finally:
            try:
                hid_xfer(dev, [ACT['STOP']])
            finally:
                stop.set(); th.join(timeout=1.5)
        if th.is_alive():
            raise RuntimeError('USB reader failed to settle')
    except BaseException:
        usb.util.dispose_resources(ud)
        if a.bridge == 'off': bridge_set(dev, True)
        raise
    window_bytes = arrival_window(raw, t0, t_end)
    dt = t_end - t0
    total = len(window_bytes)
    # Drain after STOP for endpoint hygiene. It never enters window_bytes or delta.
    t1 = time.perf_counter()
    try:
        while time.perf_counter() - t1 < 0.4:
            try:
                ep.read(16384, timeout=120)
            except usb.core.USBTimeoutError:
                break
    finally:
        usb.util.dispose_resources(ud)
        if a.bridge == 'off': bridge_set(dev, True)
    chunks = stream.push(window_bytes)
    raw.clear()

    if a.bridge == 'off' or (a.flags & 0x20):
        # 采完就把 COM 口还回去，免得下一次跑脚本时"串口怎么不通了"。
        # flags 0x20（SCOPE_FLAG_CDC_OFF）时这一步应该由固件在 STOP 里做完 —— 这里只是对账。
        bs = bridge_get(dev)
        print("（采样结束后 CDC/串口桥 = %s）" % ('开' if bs else '关'))
        if a.bridge == 'off' and not bs:
            print("（手动恢复 CDC/串口桥 = 开：%s）" % bridge_set(dev, True))

    print("主机收到 %d B / %.2f s -> %.1f KB/s；包数 %d，重同步 %d"
          % (total, dt, total / dt / 1024.0, stream.pkts, stream.resync))

    kinds = {}
    defs = [p for p in chunks if p['kind'] == 1]
    datas = [p for p in chunks if p['kind'] == 2]
    stats = [p for p in chunks if p['kind'] == 3]
    for p in chunks:
        kinds[KIND.get(p['kind'], p['kind'])] = kinds.get(KIND.get(p['kind'], p['kind']), 0) + 1
    print("包类型: %s" % kinds)
    if defs:
        d = defs[0]['payload']
        print("DEF: swd=%d Hz period=%g us flags=0x%X nvars=%d spans=%d"
              % (int.from_bytes(d[0:4], 'little'), int.from_bytes(d[4:8], 'little') / (24.0 if defs[0]['version'] == 2 else 1),
                 int.from_bytes(d[8:10], 'little'), d[10], d[11]))
    print("probe: produced=%d dropped=%d swdErr=%d yield=%d seq=%d 后端=%s"
          % (st['produced'], st['dropped'], st['swdErr'], st['yield'], st['seq'],
             'RISC-V/JTAG' if st.get('riscv') else 'SWD/ARM'))

    # seq 缺口
    gaps = 0
    prev = None
    for p in sorted(datas + stats, key=lambda x: x['seq']):
        if prev is not None and p['seq'] != prev + 1:
            gaps += p['seq'] - prev - 1
        prev = p['seq']
    print("seq 缺口合计 = %d 包" % gaps)

    # 解码样本 + 按契约核对
    order = sorted(vars_, key=lambda v: v[0] and v[1])   # 固件按地址排序
    order = sorted(vars_, key=lambda v: v[1])
    fb = sum(v[2] for v in order)
    samples = []
    for p in datas:
        nb = min(p['aux'], p['n'] * fb)
        for i in range(nb // fb):
            off = i * fb
            row = {}
            fo = 0
            for name, addr, size, typ in order:
                fmt = '<' + TYPES[typ][0]
                row[name] = struct.unpack_from(fmt, p['payload'], off + fo)[0]
                fo += size
            samples.append(row)
    print("解出 %d 个样本（frame %d B，期望 %d 个/包）" % (len(samples), fb, PAYLOAD // fb))

    if a.dump and samples:
        # 值的正确性只能靠"拿已知内容的地址采"来验（例如靶子里一段常量）：
        #   scope_hss_test.py run --riscv --set one --addr 0x1240000 --dump 6
        for i, s in enumerate(samples[:a.dump]):
            cells = []
            for k, v in s.items():
                cells.append("%s=%d" % (k, v) if isinstance(v, int) else "%s=%.6f" % (k, v))
            print("  样本[%d] = %s" % (i, " ".join(cells)))

    # ★ 端到端速率：窗口内**主机实收**的样本数 ÷ 窗口时长。探针侧的 produced 增量
    #   用来把「探针自己跳拍」和「USB 没送到」分开 —— 两者看着都是掉数据，成因差很远。
    if dt > 0:
        print('★ 端到端 %.1f kHz（主机到达窗口 %d 样本 / %.6f s）'
              % (len(samples) / dt / 1000.0, len(samples), dt))
    print_probe_window(snapshot_delta(st_begin, st_end))
    print('  主机窗口按 USB 返回时刻；探针窗口按快照 tick，边界有 HID/在飞包偏差。')
    # 校验按"这组里实际有哪些变量"自适应 —— 单选一个 g_tick 时没有 i_tick/u_hi
    if a.vset == 'rv' and samples and not verify_rv(samples):
        return 1
    if len(samples) >= 2:
        tkey = next((k for k in ('i_tick', 'g_tick', 'g_far_cnt') if k in samples[0]), None)
        if tkey is None:
            print("（这组没有 tick 类变量，跳过斜率校验）")
            return 0
        # tick 斜率 = 采样率/目标 tick 率；跳变 > 1 就是丢样本
        ts = [s[tkey] for s in samples]
        d = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
        ones = sum(1 for x in d if x == 1)
        jumps = sum(x - 1 for x in d if x > 1)
        print("tick 斜率: 连续+1 占 %d/%d，跳变丢样本合计 %d（probe dropped+usbDrop=%d）"
              % (ones, len(d), jumps, st['dropped']))
        expect_period = (d and sorted(d)[len(d) // 2]) or 0
        print("目标 tick 步进中位数 = %d（=1 表示采样率与目标 10 kHz 同拍）" % expect_period)
        if "u_hi" in samples[0]:
            bad = [s for s in samples if (s["u_hi"] >> 28) != 1]
            print("u_hi 高位校验: %d/%d 正确" % (len(samples) - len(bad), len(samples)))
    return 0


if __name__ == "__main__":
    watchdog(180)
    sys.exit(main())
