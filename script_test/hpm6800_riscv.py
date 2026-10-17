"""Probe-side RISC-V engine control (HID CMD_RISCV = 0x33).

The JTAG bit-bang only runs from the probe's main loop, so every action is
queued and the status block is read back with follow-up polls (the reply to a
request describes the state at the moment it was queued).

Usage:
  python hpm6800_riscv.py open
  python hpm6800_riscv.py status
  python hpm6800_riscv.py rbench <addr> <bytes> <iters>
  python hpm6800_riscv.py wbench <addr> <bytes> <iters>
  python hpm6800_riscv.py sbench <addr> <iters>
  python hpm6800_riscv.py rcheck <addr> <words>
  python hpm6800_riscv.py sbastat
  python hpm6800_riscv.py stop
  python hpm6800_riscv.py delay <n>
"""
import os
import sys
import threading
import time

import hid

VID, PID = 0x0D28, 0x0204
CMD_RISCV = 0x33   # 0x32 让给了网页「J-Scope 波形」页的 SCOPE

ACT_STOP, ACT_OPEN, ACT_RBENCH, ACT_WBENCH = 0, 1, 2, 3
ACT_SBENCH, ACT_RCHECK, ACT_STATUS, ACT_CONFIG = 4, 5, 6, 7
ACT_DMIPROBE = 8
ACT_SBASTAT = 9

MCHTMR_HZ = 24000000


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT (%ss)" % sec)
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


def open_hid(timeout=8.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        infos = hid.enumerate(VID, PID)
        cand = [i for i in infos if i.get("usage_page") == 0xFF00]
        if cand:
            d = hid.device()
            d.open_path(cand[0]["path"])
            d.set_nonblocking(1)
            time.sleep(0.15)
            return d
        time.sleep(0.3)
    raise RuntimeError("custom HID interface not found")


def drain(dev, rounds=32):
    """Flush any response the probe queued for a request we gave up on.

    Every HID request produces exactly one report, so a single timed-out read
    shifts the whole stream by one and every later reply looks plausible while
    actually describing the previous operation."""
    n = 0
    while n < rounds:
        try:
            d = dev.read(64, timeout_ms=30)
        except Exception:
            break
        if not d:
            break
        n += 1
    return n


def xfer(dev, cmd, args=(), tmo=4.0):
    drain(dev)
    req = [0x01, 1 + len(args), cmd] + list(args)
    req += [0] * (64 - len(req))
    dev.write(req)
    t0 = time.time()
    while time.time() - t0 < tmo:
        d = dev.read(64, timeout_ms=200)
        if d and d[0] == 0x02 and d[2] == cmd:
            return d
    return None


def words_of(resp):
    if resp is None:
        return None
    return [int.from_bytes(bytes(resp[4 + i * 4:8 + i * 4]), "little") for i in range(12)]


def status(dev):
    return words_of(xfer(dev, CMD_RISCV, [ACT_STATUS] + [0] * 10))


def request(dev, action, addr=0, a1=0, a2=0, timeout=60.0):
    args = [action,
            addr & 0xFF, (addr >> 8) & 0xFF, (addr >> 16) & 0xFF, (addr >> 24) & 0xFF,
            a1 & 0xFF, (a1 >> 8) & 0xFF, (a1 >> 16) & 0xFF, (a1 >> 24) & 0xFF,
            a2 & 0xFF, (a2 >> 8) & 0xFF]
    w = words_of(xfer(dev, CMD_RISCV, args))
    if w is None:
        raise RuntimeError("no reply to the request")
    t0 = time.time()
    while time.time() - t0 < timeout:
        w = status(dev)
        if w is not None and (w[0] & 0x100) == 0:
            return w
        time.sleep(0.05)
    raise RuntimeError("operation did not finish in %.0fs (status=%s)" % (timeout, w))


def decode_sbcs(v):
    """把 SBCS 拆成人话 —— sticky 错误位是"读到恒定值"的元凶，必须一眼看见。"""
    cfg = "sbaccess=%d" % ((v >> 17) & 7)
    if v & (1 << 16): cfg += "|autoinc"
    if v & (1 << 20): cfg += "|ronaddr"
    if v & (1 << 15): cfg += "|rondata"
    bad = []
    if v & (1 << 21): bad.append("sbbusy")
    if v & (1 << 22): bad.append("SBBUSYERROR")
    if (v >> 12) & 7: bad.append("SBERROR=%d" % ((v >> 12) & 7))
    return cfg + ("  <-- " + ",".join(bad) if bad else "")


def show(w, label=""):
    if w is None:
        print("  no status")
        return
    flags = w[0]
    print("  %sopen=%d pending=%d rc=0x%X action=%d" %
          (label, flags & 0xFF, (flags >> 8) & 0xFF, (flags >> 16) & 0xFFFF, (flags >> 24) & 0xFF))
    print("    idcode=0x%08X dtmcs=0x%08X dmstatus=0x%08X" % (w[1], w[2], w[3]))
    print("    moved=%d ticks=%d (%.1f ms) sbcs=0x%08X [%s] delay=%d iters=%d" %
          (w[4], w[5], w[5] / (MCHTMR_HZ / 1000.0), w[6], decode_sbcs(w[6]), w[7] & 0xFF, (w[7] >> 8) & 0xFFFFFF))
    if w[4] and w[5]:
        print("    rate = %.1f KB/s (%.0f bytes/s)" % (w[8] / 1024.0, w[8]))
    if w[9] or w[10] or w[11]:
        print("    check=0x%08X word0=0x%08X word1=0x%08X" % (w[9], w[10], w[11]))


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    dev = open_hid()

    if cmd == "open":
        show(request(dev, ACT_OPEN), "OPEN: ")
    elif cmd == "stop":
        show(request(dev, ACT_STOP), "STOP: ")
    elif cmd == "status":
        show(status(dev))
    elif cmd == "delay":
        show(request(dev, ACT_CONFIG, addr=int(argv[1], 0)), "DELAY: ")
    elif cmd in ("rbench", "wbench"):
        addr = int(argv[1], 0)
        size = int(argv[2], 0)
        iters = int(argv[3], 0)
        act = ACT_RBENCH if cmd == "rbench" else ACT_WBENCH
        w = request(dev, act, addr=addr, a1=size, a2=iters, timeout=120)
        show(w, "%s %d bytes x%d @0x%08X: " % (cmd.upper(), size, iters, addr))
    elif cmd == "sbench":
        addr = int(argv[1], 0)
        iters = int(argv[2], 0)
        w = request(dev, ACT_SBENCH, addr=addr, a1=iters, timeout=120)
        print("  single-word SBA reads x%d in %.1f ms -> %.1f us/read (4 DMI scans each)" %
              (w[7] >> 8, w[5] / (MCHTMR_HZ / 1000.0),
               (w[5] / (MCHTMR_HZ / 1e6)) / max(1, (w[7] >> 8))))
        show(w, "SBENCH: ")
    elif cmd == "dmiprobe":
        n = int(argv[1], 0) if len(argv) > 1 else 6
        w = request(dev, ACT_DMIPROBE, addr=n)
        print("  raw DMI responses (41-bit, low32 + high9):")
        for i in range(4):
            lo = w[4 + i * 2]
            hi = w[5 + i * 2]
            v = lo | (hi << 32)
            print("    [%d] 0x%09X  op=%d data=0x%08X addr=0x%02X" %
                  (i, v, v & 3, (v >> 2) & 0xFFFFFFFF, (v >> 34) & 0x7F))
    elif cmd == "rcheck":
        addr = int(argv[1], 0)
        words = int(argv[2], 0)
        show(request(dev, ACT_RCHECK, addr=addr, a1=words), "RCHECK: ")
    elif cmd == "sbastat":
        w = request(dev, ACT_SBASTAT)
        show(w, "SBASTAT: ")
        print("    SBCS 硬件回读 = 0x%08X  [%s]" % (w[6], decode_sbcs(w[6])))
        print("    sticky 错误事件 = %d（首次出错时 SBCS = 0x%08X）" % (w[9], w[10]))
        print("    整块重读 = %d 次，单字流水重挂 = %d 次" % (w[11] & 0xFFFF, w[11] >> 16))
        if w[6] & (1 << 21):
            print("    => 🚨 SBA 挂死：sbbusy 一直挂着。通常是访问了没有响应的地址"
                  "（未挂载/未上电的区域 —— 总线事务永远不返回）。清 sticky 位、复位 DM、"
                  "复位 TAP 都解不开，只能复位目标（ndmreset / 断电重上电）。")
        elif w[9] == 0:
            print("    => 干净：没有发生过 SBA 静默失败（读值不会冻结）")
        else:
            print("    => 发生过 SBA sticky 错误（sbbusyerror/sberror），已自动清掉并重读；"
                  "不查这一位就会读出一串恒定值")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    watchdog(300)
    sys.exit(main(sys.argv[1:]))
