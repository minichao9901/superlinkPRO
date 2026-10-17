#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""akaLinkPro 探针 HID 直驱诊断（rc=-4 归因 / RTT 字节级完整性）

绕开页面，直接用 HID 说探针的私有协议（0x31 RTT 桥 / 0x32 scope 采样器）——
页面在这一层之上，所以这里量到的是探针本身的行为。
源码级分析、全部实测数据与修法见 docs/probe-rc4-and-rtt-loss.md。

模式（--mode=）：
  bridge   只反复起 RTT 桥：桥的 -4 只能来自 rtt_swd_init（CB 扫描失败回 -3）
  scope    只反复 scope start/stop：① init + ② AP 验收读，两级串联
  disc     scope + **PEEK** 探针内存判定失败在哪一级
           （0x81EAD = rtt_bridge.c 的 s_swd_ready：init 成功才置 1）
  bench    scope start 之后紧跟 BENCH(4 B,1 拍)：BENCH 走同一个 rtt_read_bytes
           但自带一次清错重试 → 量"补一次重试能不能救"
  recover  scope 失败后按 0/2/5/10/20/50/100 ms 连探，量瞬态持续多久
  rc4      复刻现场：RTT 转发跑满 → 停桥 → scope start（循环）
  loss     丢字节分层：探针 drained 计数 vs 主机实际收到；--seq 时用带序号靶子
           （tools/target-firmware/stm32f103_rtt_seq）算**真丢字节数**

几条口径（踩过才知道）：
  · 探针符号地址从 akaLinkPro 的 .map/.elf 取（如 `riscv32-unknown-elf-nm -S`），
    改固件后地址会变，PEEK 前先核对；
  · 启动/收尾的字节会把"探针 drained vs 主机收到"打偏 ≤2 KB（在飞缓冲），
    所以要**从桥 start 起就计数**（含预热排空），不能只算窗口；
  · loss 模式必须有一个**后台读线程**从会话一开始就排空：驱动 rx 缓冲只有 4 KB，
    2.6 MB/s 下 1.6 ms 就满，一边 HID 轮询一边不读，OS 会静默丢字节（量出来的"丢"
    全是主机自己的）；
  · 固定图案的 gap/lost 是**相位**口径（1..24），不能当字节数；要字节数就 --seq。

⚠️ 跑完 scope 类模式（scope/disc/bench/recover/rc4）建议**重烧探针**：
  这些模式会让采样器往 USB 0x83 推 DEF/DATA 包，而本工具不读那条端点 —— 8 个包缓冲
  会一直"在飞"回不来，之后点页面上的 scope「标定真实速率」会稳定 err=-5（没有空闲
  包缓冲）。重烧（RAM 重来）或让页面正常读一次就恢复。

短超时 + 轮询 + 看门狗；不改固件、不写磁盘（--seq 只读流做统计）。
"""
import argparse
import os
import sys
import threading
import time

import hid

try:
    import serial
except ImportError:
    serial = None

# 本脚本的提示/日志是中文，而 Windows 控制台默认 936 → 会花屏。
# 能改就改成 UTF-8（改不了也不影响测量本身）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

VID, PID = 0x0D28, 0x0204
CMD_RTT, CMD_SCOPE, CMD_SETCFG = 0x31, 0x32, 0x02

R_ACT = {"STOP": 0, "START": 1, "STATUS": 2, "AUTOSTART": 3, "CONFIG": 7}
S_ACT = {"STOP": 0, "START": 1, "STATUS": 2, "CLOCK": 3, "CONFIG": 7}

PAT = b"hello world!\n"
PENDING = 0x9C          # (int8)-100 = 排队中（固件哨兵）


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT (%ss)" % sec)
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


class Probe:
    def __init__(self):
        infos = hid.enumerate(VID, PID)
        cand = [i for i in infos if i.get("usage_page") == 0xFF00]
        if not cand:
            print("!! 找不到自定义 HID 接口（探针不在 APP 模式？）")
            sys.exit(2)
        self.dev = hid.device()
        try:
            self.dev.open_path(cand[0]["path"])
        except OSError as e:
            print("!! 打开 HID 失败（多半被浏览器占着）：%s" % e)
            sys.exit(2)
        self.dev.set_nonblocking(1)

    def _drain(self):
        while self.dev.read(64, timeout_ms=0):
            pass

    def xfer(self, cmd, payload, wait=1.0):
        """payload 不含 report id / 长度字节，按网页口径 p[0]=1+len(data)"""
        p = bytearray(63)
        p[0] = (1 + len(payload)) & 0xFF
        p[1] = cmd
        p[2:2 + len(payload)] = payload
        self._drain()
        self.dev.write(b"\x01" + bytes(p))
        t0 = time.time()
        while time.time() - t0 < wait:
            r = self.dev.read(64, timeout_ms=100)
            if r and r[0] == 0x02 and r[2] == cmd:
                words = [int.from_bytes(bytes(r[4 + i * 4:8 + i * 4]), "little") for i in range(12)]
                return r[3], words
        return None, None

    def rtt(self, action, addr=0, size=0, channel=0, wait=1.0):
        pl = [action,
              addr & 0xFF, (addr >> 8) & 0xFF, (addr >> 16) & 0xFF, (addr >> 24) & 0xFF,
              size & 0xFF, (size >> 8) & 0xFF, (size >> 16) & 0xFF, (size >> 24) & 0xFF,
              channel]
        return self.xfer(CMD_RTT, pl, wait)

    def scope(self, action, args=b"", wait=1.0):
        return self.xfer(CMD_SCOPE, bytes([action]) + args, wait)


def signed8(v):
    return v - 256 if v is not None and v > 127 else v


def poll_start(probe, cmd, tries=40, gap=0.05):
    """等 start 结果（固件是排队执行的，-100 表示还没轮到主循环）"""
    rc, words = None, None
    for _ in range(tries):
        rc, words = probe.xfer(cmd, [S_ACT["STATUS"]] if cmd == CMD_SCOPE else [R_ACT["STATUS"]])
        if rc is None:
            return None, None
        if rc != PENDING:
            return signed8(rc), words
        time.sleep(gap)
    return signed8(rc), words


def wait_bridge_ready(probe, tries=40, gap=0.05):
    rc, words = None, None
    for _ in range(tries):
        rc, words = probe.rtt(R_ACT["STATUS"])
        if rc is None:
            return None, None
        if rc != PENDING:
            return signed8(rc), words
        time.sleep(gap)
    return signed8(rc), words


def scope_vars(spec):
    out = b""
    for item in spec.split(","):
        a, s, t = item.split(":")
        a, s, t = int(a, 0), int(s), int(t)
        out += a.to_bytes(4, "little") + bytes([s, t])
    return out


def find_probe_com():
    """按 VID/PID 找探针的 CDC 口（换 USB 口/机器都不用手改 COM 号）"""
    if serial is None:
        return None
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    for p in list_ports.comports():
        hwid = (p.hwid or "").upper()
        if "VID_0D28" in hwid and "PID_0204" in hwid and "MI_02" in hwid:
            return p.device
    for p in list_ports.comports():
        if "VID_0D28" in (p.hwid or "").upper():
            return p.device
    return None


def open_ser(com, rxbuf):
    if serial is None:
        return None
    if not com:
        com = find_probe_com()
        print("   自动识别串口: %s" % com)
    if not com:
        print("!! 没找到探针的 CDC 串口")
        return None
    try:
        ser = serial.Serial(com, 115200, timeout=0.05)
    except Exception as e:
        print("!! 串口 %s 打不开：%s" % (com, e))
        return None
    if rxbuf:
        try:
            ser.set_buffer_size(rx_size=rxbuf)
            print("   串口驱动缓冲 rx_size=%d" % rxbuf)
        except Exception as e:
            print("!! set_buffer_size 失败：%s" % e)
    return ser


def drain(ser, secs):
    if ser is None:
        return 0
    rbuf = bytearray(1 << 20)
    got = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < secs:
        n = ser.readinto(rbuf)
        if n:
            got += n
    return got


class _Reader(threading.Thread):
    """后台排空串口。

    必须从**会话一开始**就在读：驱动侧 rx 缓冲只有 4 KB（pyserial 默认），2.6 MB/s
    下 1.6 ms 就满，而握手/状态轮询期间主线程不读 —— OS 会静默丢字节，量出来的
    "丢"全是主机自己的。线程里只做 readinto + extend，收尾 join 之后再取数据。
    """

    def __init__(self, ser):
        super().__init__(daemon=True)
        self.ser = ser
        self.buf = bytearray()
        self.total = 0
        self._stop = False

    def run(self):
        rbuf = bytearray(1 << 20)
        while not self._stop:
            try:
                n = self.ser.readinto(rbuf)
            except Exception:
                return
            if n:
                self.buf.extend(rbuf[:n])
                self.total += n

    def stop(self):
        self._stop = True


def analyze_seq(stream, dump=True):
    """带序号素材（"seq NNNNNNN\\r\\n"，13 B/条）逐条对账 —— 这才是**真丢字节数**。

    固定图案的 gap/lost 只是"相位差"（1..12 之间），丢 5 字节和丢 5000 字节看起来一样；
    序号素材能直接算出跳号：missing = (序号差-1) × 13。
    """
    n = len(stream)
    pos = 0
    prev_val = prev_pos = None
    recs = 0
    missing = extra = bad = 0
    rewound = 0
    jumps = []
    samples = []
    while True:
        i = stream.find(b"seq ", pos)
        if i < 0 or i + 13 > n:
            break
        rec = stream[i:i + 13]
        good = rec[11:13] == b"\r\n" and all(48 <= c <= 57 for c in rec[4:11])
        if not good:
            bad += 1
            pos = i + 1
            continue
        val = int(rec[4:11])
        recs += 1
        if recs % 200000 == 0:
            samples.append((i, val))
        if prev_val is not None:
            d = val - prev_val
            if d < -5000000:
                d += 10000000
            elif d > 5000000:
                d -= 10000000
            dpos = i - prev_pos
            if d != 1 or dpos != 13:
                jumps.append((prev_pos, dpos, d, prev_val, val))
                if d > 1:
                    missing += (d - 1) * 13
                elif d <= 0:
                    rewound += 13 * (-d if d < 0 else 1)
                elif dpos > 13:
                    extra += dpos - 13
        prev_val, prev_pos = val, i
        pos = i + 13
    if dump:
        for at, dpos, d, pv, v in jumps[:5]:
            a = max(0, at - 16)
            b = min(n, at + 48)
            print("     跳变@%d: 序号 %07d→%07d (差 %+d, 字节差 %d)：前值是 %r" %
                  (at, pv, v, d, dpos, bytes(stream[a:at + 13])))
            print("       hex: %s" % bytes(stream[a:b]).hex(" "))
        print("      记录号采样(字节偏移:序号)：%s" %
              " ".join("%d:%07d" % s for s in samples[:10]))
    return {"records": recs, "missing": missing, "extra": extra, "bad": bad,
            "rewound": rewound, "jumps": len(jumps), "first": (jumps[0][0] if jumps else -1)}


def pattern_check(stream, dump=True, ctx=40):
    """照 akaLinkPro/script_tools/rtt_probe_bridge.py 的口径：13 字节图案的相位断裂。
    dump=True 时把断裂点前后的字节打出来 —— 区分"整段丢字节"（两段都是干净相位）
    还是"读到了别的地址/垃圾"（断裂处出现非图案字节）。"""
    off = stream.find(PAT)
    if off < 0:
        return None
    pos, gaps = off, []
    while True:
        i = stream.find(PAT, pos)
        if i < 0:
            break
        if i != pos:
            gaps.append((pos, i - pos))
        pos = i + len(PAT)
    lost = sum(g for _, g in gaps if g > 0)
    dup = -sum(g for _, g in gaps if g < 0)
    if dump:
        for at, g in gaps[:4]:
            a = max(0, at - ctx)
            b = min(len(stream), at + g + ctx)
            seg = bytes(stream[a:b])
            print("     断裂@%d gap=%+d 前后 %d B：" % (at, g, len(seg)))
            print("       hex: %s" % seg.hex(" "))
            print("       asc: %s" % "".join(chr(c) if 32 <= c < 127 else "." for c in seg))
    return {"gaps": len(gaps), "lost": lost, "dup": dup,
            "first": (gaps[0][0] if gaps else -1)}


# ---------------------------------------------------------------- modes ------
def mode_bridge(probe, a):
    """只反复起桥：桥的 -4 只能来自 rtt_swd_init（CB 扫描失败是 -3）"""
    rcs = {}
    for i in range(a.iters):
        t0 = time.perf_counter()
        if a.clk:
            probe.rtt(R_ACT["CONFIG"], a.clk * 1000000, 0xFF000000, 0)
        probe.rtt(R_ACT["AUTOSTART"])
        rc, w = wait_bridge_ready(probe)
        dt = (time.perf_counter() - t0) * 1000
        rcs[rc] = rcs.get(rc, 0) + 1
        clk = (w[11] >> 24) if w else -1
        print("  #%02d rc=%s %6.1fms clk=%dM drained=%d rd_err=%d wr_err=%d rescan=%d" %
              (i + 1, rc, dt, clk, w[3] if w else -1, (w[5] & 0xFFFF) if w else -1,
               (w[5] >> 16) if w else -1, (w[7] >> 16) if w else -1))
        probe.rtt(R_ACT["STOP"])
        time.sleep(0.05)
    print("汇总:", rcs)


def _scope_once(probe, a, vars_bin):
    if a.clk:
        probe.scope(S_ACT["CLOCK"], (a.clk * 1000000).to_bytes(4, "little"))
    pl = (a.period & 0xFFFFFFFF).to_bytes(4, "little") + bytes([0, a.nvars]) + vars_bin
    rc0, w0 = probe.scope(S_ACT["CONFIG"], pl)
    probe.scope(S_ACT["START"])
    t0 = time.perf_counter()
    rc, w = poll_start(probe, CMD_SCOPE)
    dt = (time.perf_counter() - t0) * 1000
    ready = ((w[0] >> 16) & 1) if w else -1
    swderr = (w[5] & 0xFFFF) if w else -1
    clk = (w[11] >> 24) if w else -1
    probe.scope(S_ACT["STOP"])
    time.sleep(0.03)
    return rc, dt, clk, ready, swderr


def mode_scope(probe, a):
    vars_bin = scope_vars(a.vars)
    rcs = {}
    for i in range(a.iters):
        rc, dt, clk, ready, swderr = _scope_once(probe, a, vars_bin)
        rcs[rc] = rcs.get(rc, 0) + 1
        print("  #%02d rc=%s %6.1fms clk=%dM swd_ready=%d swd_err=%d" % (i + 1, rc, dt, clk, ready, swderr))
        if a.verbose and rc != 0:
            probe.scope(S_ACT["CLOCK"], (a.clk * 1000000).to_bytes(4, "little"))
    print("汇总:", rcs)


def mode_rc4(probe, a):
    """复刻现场：转发跑满 → 停桥 → scope start"""
    ser = open_ser(a.com, a.rxbuf)
    vars_bin = scope_vars(a.vars)
    rcs = {}
    for i in range(a.iters):
        if a.clk:
            probe.rtt(R_ACT["CONFIG"], a.clk * 1000000, 0xFF000000, 0)
        probe.rtt(R_ACT["AUTOSTART"])
        brc, w0 = wait_bridge_ready(probe)
        got = 0
        if brc == 0:
            drain(ser, 0.3)
            got = drain(ser, a.heavy)
        _, w1 = probe.rtt(R_ACT["STATUS"])
        probe.rtt(R_ACT["STOP"])
        drain(ser, 0.2)
        drained = (w1[3] - w0[3]) if (w0 and w1) else -1
        rc, dt, clk, ready, swderr = _scope_once(probe, a, vars_bin)
        rcs[rc] = rcs.get(rc, 0) + 1
        print("  #%02d 桥rc=%s drained=%d 主机=%d(差%+d) rd_err=%d | scope rc=%s %6.1fms clk=%dM" %
              (i + 1, brc, drained, got, (drained - got) if drained >= 0 else 0,
               (w1[5] & 0xFFFF) if w1 else -1, rc, dt, clk))
    if ser:
        ser.close()
    print("汇总:", rcs)


def peek(probe, addr, n=1):
    """读探针自己的内存（CMD_RTT action 5 = PEEK；必须 4 字节对齐）"""
    rc, w = probe.rtt(5, addr, n, 0)
    return w


def mode_disc(probe, a):
    """判定 -4 的两级来源：scope start 是 ① init 失败 还是 ② AP 验收读失败

    依据：rtt_swd_init() 成功时会把**桥自己的** s_swd_ready 置 1（rtt_bridge.c:286），
    而验收读失败只会清 scope 自己那份（scope_sampler.c:757）→ 事后 PEEK
    桥的 s_swd_ready（0x81EAD，取对齐字 0x81EAC 的 byte1）即可分辨。
    顺带把邻居字节一起打出来：byte0=s_rescans_since_step、byte2=s_discard、byte3=s_rd_pending_valid。
    （PEEK 不走 DAP 通路，不会自己把标志清掉 —— 只有 RAW_DAP/主机 DAP 才会。）
    """
    vars_bin = scope_vars(a.vars)
    combos = {}
    for i in range(a.iters):
        rc, dt, clk, sready, swderr = _scope_once(probe, a, vars_bin)
        w = peek(probe, a.peek_addr, 1)
        word = w[0] if w else -1
        brd = (word >> 8) & 1
        combos[(rc, brd)] = combos.get((rc, brd), 0) + 1
        print("  #%02d rc=%-3s %5.1fms | 桥s_swd_ready=%d  scan_since=%d discard=%d rd_pending=%d | scope swd_err=%d" %
              (i + 1, rc, dt, brd, word & 0xFF, (word >> 16) & 0xFF, (word >> 24) & 0xFF, swderr))
    print("汇总 (rc, 桥ready):", combos)
    print("判读：rc=-4 且 ready=0 → 失败在 rtt_swd_init（DP/换挡）；rc=-4 且 ready=1 → 失败在 AP 验收读")


def mode_bench(probe, a):
    """验证"补一次重试就能吃掉这个瞬态"（结论要给固件修法用的证据）

    每次：scope start（单次验收读，没有重试）→ scope stop → BENCH 读同一地址 4 B。
    BENCH 走的是**同一个** rtt_read_bytes()（rtt_bridge.c:941），但它内部有一次
    `rtt_clear_link_errors()` + 重试（retried）—— 于是两者的失败率之差就是
    "重试能不能救"的直接量度。前提：scope start 成功后桥的 s_swd_ready=1，
    BENCH 会跳过初始化（rtt_bridge.c:925），所以量的确实只有"读"这一级。
    """
    vars_bin = scope_vars(a.vars)
    sc, bn, both = {}, {}, 0
    for i in range(a.iters):
        rc, dt, clk, sready, swderr = _scope_once(probe, a, vars_bin)
        sc[rc] = sc.get(rc, 0) + 1
        # BENCH: req[4..7]=addr, [8..9]=bytes, [10..11]=iters
        probe.rtt(8, a.bench_addr, (1 << 16) | 4, 0)
        err = None
        for _ in range(20):
            rc2, w = probe.rtt(9)          # BENCH_RESULT
            if rc2 == 1 and w:
                err = w[0]
                break
            time.sleep(0.02)
        bn[err] = bn.get(err, 0) + 1
        if rc != 0 and err == 0:
            both += 1
        print("  #%02d scope rc=%-3s | BENCH(4B,带一次重试) err=%s" % (i + 1, rc, err))
    print("汇总 scope rc:", sc)
    print("汇总 BENCH err（0=两次都成/一次重试救回来了，-4=连重试都失败）:", bn)
    print("scope 失败但 BENCH 成功 = %d 次 → 重试能救的就是这些" % both)


def mode_recover(probe, a):
    """量"瞬态到底持续多久"：失败后按 0/2/5/10/20/50/100 ms 的间隔连探，
    每次 BENCH(4B,1 拍) 自带一次**零延迟**重试 —— 记录第一次成功的延迟。
    （BENCH 会跳过初始化：scope start 成功后桥的 s_swd_ready=1，所以量的是纯"读"。）
    """
    vars_bin = scope_vars(a.vars)
    fails = 0
    hist = {}
    for i in range(a.iters):
        rc, dt, clk, sready, swderr = _scope_once(probe, a, vars_bin)
        if rc == 0:
            continue
        fails += 1
        t0 = time.perf_counter()
        got = None
        for d in (0.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0):
            while (time.perf_counter() - t0) * 1000.0 < d:
                time.sleep(0.0005)
            probe.rtt(8, a.bench_addr, (1 << 16) | 4, 0)
            err = None
            for _ in range(20):
                rc2, w = probe.rtt(9)
                if rc2 == 1 and w:
                    err = w[0]
                    break
                time.sleep(0.02)
            if err == 0:
                got = d
                break
        hist[got] = hist.get(got, 0) + 1
        print("  #%02d scope rc=%s → 首次读成功延迟 = %s ms" % (i + 1, rc, got))
    print("失败 %d 次；恢复延迟直方图（ms）: %s" % (fails, hist))


def mode_loss(probe, a):
    """分层：探针 drained（它读出来并放进 CDC 环的字节） vs 主机实际收到

    口径要点（两条都是踩出来的）：
      ① **必须有一个后台读线程从会话一开始就排空**。驱动侧 rx 缓冲只有 4 KB，
         2.6 MB/s 下 1.6 ms 就满 —— 一边用 HID 轮询握手一边不读，OS 会**静默丢字节**，
         量出来的"丢"全是主机自己的（实测差 = drained-主机 恒为正、且漂到几 KB）。
      ② 计数从"桥刚起来"起算（含预热排空）：探针的 s_drained 也是从桥 start 起清零的，
         两个计数器才覆盖同一段时间。
    于是：差 > 0 = 字节死在探针 CDC 环之后（USB/驱动/主机读）；差 = 0 而流里有跳号
    = 断在探针读目标环这一段（SWD 侧）。
    """
    ser = open_ser(a.com, a.rxbuf)
    if ser is None:
        print("!! 没有串口就没法做主计（需要主机侧读者）")
        return
    probe.scope(S_ACT["STOP"])
    for it in range(a.iters):
        # 先把上一个会话的尾巴读干净（不计入本轮），再起后台读者
        probe.rtt(R_ACT["STOP"])
        drain(ser, 0.3)
        rd = _Reader(ser)
        rd.start()
        if a.clk:
            probe.rtt(R_ACT["CONFIG"], a.clk * 1000000, 0xFF000000, 0)
        probe.rtt(R_ACT["AUTOSTART"])
        t_start = time.perf_counter()
        brc, _ = wait_bridge_ready(probe)
        if brc != 0:
            rd.stop(); rd.join(timeout=2.0)
            print("  #%02d 桥起不来 rc=%s" % (it + 1, brc))
            continue
        time.sleep(a.warm)                 # 预热：排空启动积压（计进 total）
        n0, t0 = rd.total, time.perf_counter()
        time.sleep(a.window)
        n1, t1 = rd.total, time.perf_counter()
        probe.rtt(R_ACT["STOP"])
        time.sleep(0.8)                    # 收尾：生产者已停，剩下的都是真丢
        rd.stop()
        rd.join(timeout=3.0)
        total, stream, dur = rd.total, rd.buf, (t1 - t0)
        _, w1 = probe.rtt(R_ACT["STATUS"])
        drained = w1[3]
        if a.seq:
            chk = analyze_seq(stream)
            desc = ("GARBLED" if not chk["records"] else
                    "记录=%d 真丢=%d B 多=%d B 坏=%d 回退=%d 跳变=%d first@%d" %
                    (chk["records"], chk["missing"], chk["extra"], chk["bad"],
                     chk["rewound"], chk["jumps"], chk["first"]))
        else:
            chk = pattern_check(stream)
            desc = ("GARBLED" if chk is None else "gap=%d lost=%d dup=%d first@%d" %
                    (chk["gaps"], chk["lost"], chk["dup"], chk["first"]))
        print("  #%02d %.2fs 窗口=%.1f KB/s(读线程累计 %d B) 探针drained=%d 差=%+d | rd_err=%d wr_err=%d rescan=%d zips=%d | %s" %
              (it + 1, dur, ((n1 - n0) / dur / 1024.0) if dur > 0 else 0, total,
               drained, drained - total,
               w1[5] & 0xFFFF, w1[5] >> 16, w1[7] >> 16, w1[6] >> 16, desc))
    ser.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="scope",
                    choices=["bridge", "scope", "rc4", "loss", "disc", "bench", "recover"])
    ap.add_argument("--bench-addr", dest="bench_addr", type=lambda v: int(v, 0), default=0x20000000)
    ap.add_argument("--peek-addr", dest="peek_addr", type=lambda v: int(v, 0), default=0x81EAC,
                    help="disc 模式要 PEEK 的探针内存地址（默认 = 桥 s_swd_ready 所在对齐字）")
    ap.add_argument("--iters", type=int, default=20)
    ap.add_argument("--clk", type=int, default=60, help="SWD 档 MHz（0=不动）")
    ap.add_argument("--period", type=int, default=20, help="采样周期 us")
    ap.add_argument("--nvars", type=int, default=1)
    ap.add_argument("--vars", default="0x20000000:4:7", help="addr:size:type[,...]")
    ap.add_argument("--com", default="COM5")
    ap.add_argument("--rxbuf", type=int, default=0, help="串口驱动 rx_size（0=默认）")
    ap.add_argument("--window", type=float, default=10.0)
    ap.add_argument("--warm", type=float, default=0.6, help="loss 模式：桥起来后排空积压的秒数（计入总数）")
    ap.add_argument("--heavy", type=float, default=2.0, help="rc4 模式里转发跑的秒数")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--seq", action="store_true",
                    help="素材是带序号的 13 B 记录（靶子 tools/target-firmware/stm32f103_rtt_seq）")
    ap.add_argument("--wd", type=int, default=900)
    a = ap.parse_args()
    a.nvars = a.vars.count(",") + 1

    watchdog(a.wd)
    probe = Probe()
    print("=== mode=%s iters=%d clk=%dM period=%dus vars=%s ===" % (a.mode, a.iters, a.clk, a.period, a.vars))
    t0 = time.perf_counter()
    if a.mode == "bridge":
        mode_bridge(probe, a)
    elif a.mode == "scope":
        mode_scope(probe, a)
    elif a.mode == "rc4":
        mode_rc4(probe, a)
    elif a.mode == "disc":
        mode_disc(probe, a)
    elif a.mode == "bench":
        mode_bench(probe, a)
    elif a.mode == "recover":
        mode_recover(probe, a)
    else:
        mode_loss(probe, a)
    print("总耗时 %.1fs" % (time.perf_counter() - t0))


if __name__ == "__main__":
    main()
