"""Sweep the probe-side RTT bridge's SWD knobs (clock, block size) and report
where the ceiling actually is.

Two independent measurements per configuration:

1. **Pure SWD bench** (HID CMD_RTT action 8): the probe reads a fixed block of
   TARGET SRAM `iters` times through the very same swd_host path the bridge
   uses, and reports bytes + MCHTMR ticks. No CDC, no USB payload, no
   target-side producer - this is the SWD side's own ceiling.
2. **End-to-end drain** (action 3 + COM port): start the bridge and read the
   CDC port. This is what a user gets, so the gap between (1) and (2) is the
   USB/CDC path's share.

Usage: python rtt_bridge_sweep.py [COMxx] [--sec 4] [--addr 0x20000000]
       [--bytes 1024] [--iters 32]
"""
import os
import sys
import threading
import time

import hid
import serial

import rtt_probe_bridge as rb

COM = "COM52"
SEC = 4.0
ADDR = 0x20000000
BYTES = 1024
ITERS = 32

MCHTMR_HZ = 24000000

# 6 MHz (slow blob) up to 100 MHz; Set_Clock_Delay() picks 20/30/36/45/60 MHz
# blobs above their thresholds, so this also tells us what each blob really does.
CLOCKS_MHZ = [20, 36, 45, 60, 80]
CHUNKS = [64, 128, 256, 512, 1024, 2048, 4096]

PATTERN = rb.PATTERN


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT")
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


def raw_cmd(dev, payload, want=1, timeout=2.0):
    """Send a 13+-byte CMD_RTT request payload, return (rc, bytes)."""
    req = [0x01, 0x01, rb.CMD_RTT] + list(payload)
    req += [0] * (64 - len(req))
    dev.write(req)
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = dev.read(64, timeout_ms=200)
        if r and r[0] == 0x02 and r[2] == rb.CMD_RTT:
            return r[3], bytes(r[4:])
    return None, None


def cfg(dev, hz=0, chunk=0, discard=0, delay=0xFF):
    p = [7]                                   # action = CONFIG
    p += list(hz.to_bytes(4, "little"))
    p += list(int(chunk).to_bytes(2, "little"))
    p += [discard, delay]
    return raw_cmd(dev, p)


def bench(dev, addr, nbytes, iters):
    p = [8]                                   # action = BENCH
    p += list(int(addr).to_bytes(4, "little"))
    p += list(int(nbytes).to_bytes(2, "little"))
    p += list(int(iters).to_bytes(2, "little"))
    raw_cmd(dev, p)

    # runs from the main loop (up to ~130 KB of SWD reads), then a result is out
    t0 = time.time()
    while time.time() - t0 < 5.0:
        rc, body = raw_cmd(dev, [9], timeout=1.0)   # action = BENCH_RESULT
        if rc == 1:
            err = int.from_bytes(body[0:4], "little", signed=True)
            got = int.from_bytes(body[4:8], "little")
            ticks = int.from_bytes(body[8:12], "little")
            return err, got, ticks
        time.sleep(0.02)
    return None, 0, 0


def kbs(nbytes, ticks):
    if not ticks:
        return 0.0
    return nbytes / (ticks / float(MCHTMR_HZ)) / 1024.0


def main():
    global COM, SEC, ADDR, BYTES, ITERS

    clk = 45
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = [a for a in sys.argv[1:] if a.startswith("--")]
    if args:
        COM = args[0]
    for o in opts:
        k, _, v = o.partition("=")
        if k == "--sec":
            SEC = float(v)
        elif k == "--addr":
            ADDR = int(v, 0)
        elif k == "--bytes":
            BYTES = int(v, 0)
        elif k == "--iters":
            ITERS = int(v, 0)
        elif k == "--clk":
            clk = int(v)
    # Try the requested clock for the bridge phases, then walk down.
    bridge_clocks = [c for c in [clk, 45, 36, 20, 10, 6] if c <= clk]

    print("=== RTT bridge sweep: SWD 侧天花板 + 端到端 ===")
    infos = hid.enumerate(0x0D28, 0x0204)
    cand = [i for i in infos if i.get("usage_page") == 0xFF00]
    if not cand:
        print("no custom HID interface (device in APP mode?)")
        return 1
    dev = hid.device()
    dev.open_path(cand[0]["path"])
    dev.set_nonblocking(1)

    print("1. boosting the target via OpenOCD")
    rb.boost_target()
    time.sleep(0.3)

    print("\n2. 纯 SWD 基准（读目标 SRAM %d B x %d 次，不含 CDC/USB/生产者）" % (BYTES, ITERS))
    cfg(dev, chunk=512, discard=1)
    print("   时钟 x clock_delay 扫描（块 = 512 B）")
    print("   | SWD 设定 | delay | 实测吞吐 | 错误 |")
    print("   | --- | --- | --- | --- |")
    best_hz, best_delay, best_rate = 0, 0xFF, 0.0
    for mhz in CLOCKS_MHZ:
        for delay in (None, 0):     # None = 用档位原生值
            cfg(dev, hz=mhz * 1000000, delay=(0xFF if delay is None else delay))
            rc, body = rb.rtt_cmd(dev, rb.ACT_STATUS)
            native = (body[0] >> 24) & 0xFF if body else -1
            shown = native if delay is None else delay
            err, got, ticks = bench(dev, ADDR, BYTES, ITERS)
            if err is None:
                print("   | %4d MHz | %d | 超时无结果 | - |" % (mhz, shown))
                continue
            rate = kbs(got, ticks)
            print("   | %4d MHz | %d | %8.1f KB/s | %s |" %
                  (mhz, shown, rate, "0" if err == 0 else str(err)))
            if err == 0 and rate > best_rate:
                best_rate, best_hz = rate, mhz
                best_delay = native if delay is None else delay

    print("   -> 最快 %.1f KB/s @ %d MHz (delay=%d)" % (best_rate, best_hz, best_delay))
    cfg(dev, hz=best_hz * 1000000)

    print("\n   块大小扫描（时钟 = %d MHz）" % best_hz)
    print("   | 块大小 | 实测吞吐 | 错误 |")
    print("   | --- | --- | --- |")
    best_chunk, best_chunk_rate = 512, 0.0
    for chunk in CHUNKS:
        cfg(dev, chunk=chunk)
        b = min(chunk, 2048)
        err, got, ticks = bench(dev, ADDR, b, ITERS)
        if err is None:
            print("   | %5d B | 超时无结果 | - |" % chunk)
            continue
        rate = kbs(got, ticks)
        print("   | %5d B | %8.1f KB/s | %s |" % (chunk, rate, "0" if err == 0 else str(err)))
        if err == 0 and rate > best_chunk_rate:
            best_chunk_rate, best_chunk = rate, chunk

    print("   -> 最快 %.1f KB/s，块 %d B" % (best_chunk_rate, best_chunk))

    print("\n3. 桥的搬运速率（丢弃模式：只搬不送 CDC）")
    # The target's own SWD logic is clocked by HCLK: at the reset-default 8 MHz
    # it cannot keep up with a fast SWD clock, so a target that got reset during
    # the bench phase would fail to answer at 36/45 MHz. Re-boost before the
    # bridge phases so "which clock works" is about the SWD link, not about the
    # target having fallen back to 8 MHz.
    print("   重新 boost 目标（保证 HCLK=64 MHz，SWD 采样余量才够）")
    rb.boost_target()
    time.sleep(0.3)

    # The bench above is a tight run of 32-bit block reads; the bridge's control
    # block scan and RdOff writes are short/byte accesses with less timing
    # margin, so the fastest blob can be marginal there. Walk down from the
    # requested clock until a start actually succeeds.
    start_rc = None
    for mhz in bridge_clocks:
        cfg(dev, hz=mhz * 1000000, chunk=best_chunk, discard=1, delay=best_delay)
        rb.rtt_cmd(dev, rb.ACT_AUTOSTART)
        for _ in range(40):
            time.sleep(0.1)
            rc, words = rb.rtt_cmd(dev, rb.ACT_STATUS)
            if words and words[10] != 0xFFFFFF9C:
                break
        start_rc = words[10] & 0xFF if words else 0xFF
        if start_rc > 127:
            start_rc -= 256
        if start_rc == 0:
            print("   启动成功 @ %d MHz, 块 %d B" % (mhz, best_chunk))
            break
        print("   %d MHz 启动失败 (rc=%d)，降档重试" % (mhz, start_rc))
    if start_rc != 0:
        print("   bridge refused to start at any clock")
        return 2
    bridge_hz = mhz

    d0 = words[3]
    t0 = time.perf_counter()
    time.sleep(SEC)
    dur = time.perf_counter() - t0
    _, words = rb.rtt_cmd(dev, rb.ACT_STATUS)
    discard_rate = (words[3] - d0) / dur / 1024.0
    print("   丢弃模式: %.1f KB/s（polls=%d drains=%d zips=%d rd_err=%d wr_err=%d）" %
          (discard_rate, words[4] & 0xFFFF, words[4] >> 16, (words[6] >> 16) & 0xFFFF,
           words[5] & 0xFFFF, (words[5] >> 16) & 0xFFFF))

    print("\n4. 端到端（同配置，接 CDC 读走）")
    ser = serial.Serial(COM, 115200, timeout=0.2)
    try:
        ser.reset_input_buffer()
        rb.rtt_cmd(dev, rb.ACT_STOP)
        t_end = time.perf_counter() + 0.5
        while time.perf_counter() < t_end:
            ser.read(65536)
        ser.reset_input_buffer()

        cfg(dev, hz=bridge_hz * 1000000, chunk=best_chunk, discard=0)
        rb.rtt_cmd(dev, rb.ACT_AUTOSTART)
        for _ in range(40):
            time.sleep(0.1)
            rc, words = rb.rtt_cmd(dev, rb.ACT_STATUS)
            if words and words[10] != 0xFFFFFF9C:
                break
        t_end = time.perf_counter() + 0.3
        while time.perf_counter() < t_end:
            ser.read(65536)
        ser.reset_input_buffer()
        _, w0 = rb.rtt_cmd(dev, rb.ACT_STATUS)
        d0 = w0[3]

        total = 0
        stream = bytearray()
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < SEC:
            c = ser.read(65536)
            if c:
                total += len(c)
                stream += c
        dur = time.perf_counter() - t0
        rb.rtt_cmd(dev, rb.ACT_STOP)
        t1 = time.perf_counter()
        while time.perf_counter() - t1 < 1.0:
            c = ser.read(65536)
            if c:
                total += len(c)
                stream += c
                t1 = time.perf_counter()
    finally:
        ser.close()

    _, words = rb.rtt_cmd(dev, rb.ACT_STATUS)
    moved = words[3] - d0

    n = len(PATTERN)
    off = stream.find(PATTERN)
    gaps = []
    if off >= 0:
        pos = off
        while True:
            i = stream.find(PATTERN, pos)
            if i < 0:
                break
            if i != pos:
                gaps.append((pos, i - pos))
            pos = i + n
        tail = stream[pos:]
        tail_ok = tail == PATTERN[:len(tail)]
    else:
        tail_ok = False

    print("   CDC: %d B in %.2fs -> %.1f KB/s" % (total, dur, total / dur / 1024.0))
    print("   桥搬运 %.1f KB/s；流校验：%d 处间隙（%s），尾段完整=%s" %
          (moved / dur / 1024.0, len(gaps),
           "无" if not gaps else "首个在 %d，%+d 字节" % gaps[0], tail_ok))

    print("\n=== 结论 ===")
    print("   SWD 侧天花板   : %8.1f KB/s @ %d MHz, 块 %d B" % (best_chunk_rate, best_hz, best_chunk))
    print("   桥搬运(丢弃)   : %8.1f KB/s @ %d MHz" % (discard_rate, bridge_hz))
    print("   端到端(CDC)    : %8.1f KB/s" % (total / dur / 1024.0))
    return 0


if __name__ == "__main__":
    watchdog(240)
    sys.exit(main())
