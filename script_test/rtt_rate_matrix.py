"""Per-clock comparison: pure SWD read speed vs the RTT bridge's delivery rate.

For each SWD clock setting this measures, at the SAME target clock:

  1. **SWD read** - HID CMD_RTT action 8 (the probe reads target SRAM through the
     same swd_host path the bridge uses; no CDC, no descriptor, no RdOff write).
     This is the link's own ceiling.
  2. **RTT delivery** - start the bridge and read the CDC port. This is what a
     user gets.

and reports delivery/SWD-read as a percentage plus who is limiting, judged from
the *measured* per-poll statistics rather than from a derived producer rate:

  * 2048 B moved per poll (the RTT_MAX_DRAIN cap) = the target ring stayed full
    -> the link is the limiter;
  * far less than 2048 B per poll with zips == 0 = the ring was neither full nor
    empty -> the target's RTT producer is the limiter (it simply cannot supply
    more), and the link still has headroom.

Usage: python rtt_rate_matrix.py [COMxx] [--sec 5] [--clocks 20,30,36,45,60]
"""
import os
import sys
import threading
import time

import hid
import serial

import rtt_probe_bridge as rb

COM = "COM52"
SEC = 5.0
CLOCKS = [20, 30, 36, 45, 60]
PROBE_MHZ_PER_B_PER_MS = 34.6  # measured producer slope: B/ms per MHz of HCLK


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT")
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


def xfer(dev, pkt, timeout=1.0):
    pkt = pkt + [0] * (64 - len(pkt))
    dev.write(pkt)
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = dev.read(64, timeout_ms=200)
        if r and r[0] == 0x02 and r[2] == rb.CMD_RTT:
            return r
    return None


def cfg(dev, hz=0, chunk=0, discard=0, delay=0xFF):
    xfer(dev, [0x01, 0x01, rb.CMD_RTT, 7] + list(int(hz).to_bytes(4, "little")) +
         list(int(chunk).to_bytes(2, "little")) + [discard, delay])


def status(dev):
    r = xfer(dev, [0x01, 0x01, rb.CMD_RTT, 2])
    return [int.from_bytes(bytes(r[4 + i * 4:8 + i * 4]), "little") for i in range(12)]


def bench(dev, nbytes=1024, iters=32):
    """换档后的第一次基准读偶尔会失败（blob 刚重载），所以重试几次取第一次成功。"""
    last = None
    for _ in range(3):
        xfer(dev, [0x01, 0x01, rb.CMD_RTT, 8] + list((0x20000000).to_bytes(4, "little")) +
             list(nbytes.to_bytes(2, "little")) + list(iters.to_bytes(2, "little")))
        t0 = time.time()
        while time.time() - t0 < 5:
            r = xfer(dev, [0x01, 0x01, rb.CMD_RTT, 9], timeout=1.0)
            if r and r[3] == 1:
                b = bytes(r[4:16])
                err = int.from_bytes(b[0:4], "little", signed=True)
                got = int.from_bytes(b[4:8], "little")
                ticks = int.from_bytes(b[8:12], "little")
                rate = got / (ticks / 24e6) / 1024.0 if ticks else 0.0
                if err == 0:
                    return err, rate
                last = err
                break
            time.sleep(0.02)
        time.sleep(0.05)
    return last, 0.0


def main():
    global COM, SEC, CLOCKS
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        COM = args[0]
    # 同时支持 --sec=5 与 --sec 5 两种写法
    argv = sys.argv[1:]
    for i, o in enumerate(argv):
        if not o.startswith("--"):
            continue
        k, eq, v = o.partition("=")
        if not eq and (i + 1) < len(argv) and not argv[i + 1].startswith("--"):
            v = argv[i + 1]
        if k == "--sec":
            SEC = float(v)
        elif k == "--clocks":
            CLOCKS = [int(x) for x in v.split(",")]

    print("=== SWD 读速 vs RTT 交付率（逐档）===")
    infos = hid.enumerate(0x0D28, 0x0204)
    cand = [i for i in infos if i.get("usage_page") == 0xFF00]
    if not cand:
        print("no custom HID interface")
        return 1
    dev = hid.device()
    dev.open_path(cand[0]["path"])
    dev.set_nonblocking(1)

    rb.boost_target()
    time.sleep(0.3)

    # target clock, for context (the delivery ceiling scales with it)
    tgt_mhz, tgt_desc = 0.0, "?"
    try:
        import subprocess
        proc = subprocess.Popen([rb.OPENOCD, "-s", rb.SCRIPTS, "-f", rb.CFG,
                                 "-c", "gdb port disabled", "-c", "tcl port disabled",
                                 "-c", "telnet port 4453"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        tn = None
        for _ in range(60):
            try:
                tn = rb.Telnet(4453)
                break
            except OSError:
                time.sleep(0.3)
        if tn is not None:
            cfgr = rb.rd(tn, 0x40021004) or 0
            tgt_mhz, tgt_desc = rb.decode_clock(cfgr)
            try:
                tn.cmd("shutdown")
            except Exception:
                pass
        time.sleep(0.5)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    except Exception as e:
        print("  (目标时钟读取失败: %s)" % e)

    print("目标主频: %.0f MHz (%s)；生产者上限 ≈ %.0f KB/s（按 %.1f B/ms/MHz 外推）\n" %
          (tgt_mhz, tgt_desc, tgt_mhz * PROBE_MHZ_PER_B_PER_MS, PROBE_MHZ_PER_B_PER_MS))
    print("| SWD 设定 | SWD 读（纯 SRAM） | RTT 交付 | 交付/读 | 每次搬运 | zips | 谁主导 |")
    print("| --- | --- | --- | --- | --- | --- | --- |")

    for ask in CLOCKS:
        cfg(dev, hz=ask * 1_000_000, chunk=2048)
        err, swd_rate = bench(dev)
        swd_txt = ("%7.1f KB/s" % swd_rate) if err == 0 else ("读失败 err=%s" % err)

        ser = serial.Serial(COM, 115200, timeout=0.2)
        try:
            ser.reset_input_buffer()
            rb.rtt_cmd(dev, rb.ACT_STOP)
            cfg(dev, hz=ask * 1_000_000, chunk=2048)
            t = time.perf_counter() + 0.4
            while time.perf_counter() < t:
                ser.read(65536)
            ser.reset_input_buffer()
            rb.rtt_cmd(dev, rb.ACT_AUTOSTART)
            for _ in range(30):
                time.sleep(0.1)
                w = status(dev)
                if (w[10] & 0xFF) != 0xFFFFFF9C:
                    break
            sr = w[10] & 0xFF
            if sr > 127:
                sr -= 256
            if sr != 0:
                print("| %2d MHz | %s | 启动失败 rc=%d | - | - | - | - |" % (ask, swd_txt, sr))
                continue
            t = time.perf_counter() + 0.3
            while time.perf_counter() < t:
                ser.read(65536)
            ser.reset_input_buffer()
            w0 = status(dev)
            d0, p0 = w0[3], w0[4] >> 16
            total = 0
            rbuf = bytearray(1 << 20)
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < SEC:
                # readinto 复用大缓冲：pyserial 的 read() 在 Windows 上每次都要新分配，
                # 主机侧会被压到 2169 KB/s，readinto 能到 2956 KB/s（差 36%），
                # 用 read() 测会把"主机读得慢"误读成"设备只能跑这么快"。
                n = ser.readinto(rbuf)
                if n:
                    total += n
            dur = time.perf_counter() - t0
            rb.rtt_cmd(dev, rb.ACT_STOP)
        finally:
            ser.close()
        w = status(dev)
        used = (w[11] >> 24) & 0xFF
        nd = max((w[4] >> 16) - p0, 1)
        per_poll = (w[3] - d0) / nd
        zips = (w[6] >> 16) & 0xFFFF
        deliv = total / dur / 1024.0
        pct = (deliv / swd_rate * 100.0) if swd_rate else 0.0

        if per_poll >= 2000 and zips == 0:
            who = "**SWD 链路**（环一直满）"
        elif per_poll < 1600 and zips == 0:
            who = "**目标生产者**（环不空不满）"
        elif zips > 0:
            who = "目标生产者（环常空）"
        else:
            who = "接近均衡"
        print("| %s | %s | %7.1f KB/s | %4.0f%% | %4.0f B | %d | %s |" %
              (("%d MHz" % ask) if used == ask else ("%d→%d MHz" % (ask, used)),
               swd_txt, deliv, pct, per_poll, zips, who))

    return 0


if __name__ == "__main__":
    watchdog(240)
    sys.exit(main())
