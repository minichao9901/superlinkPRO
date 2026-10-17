"""RTT delivery forensics for the HPM6800EVK JTAG backend.

Runs the probe-side bridge, drains COM5, and snapshots the probe's own RAM with
the HID PEEK action at three points (before start / mid-run / after) so the
exact failure point shows up as a number instead of a guess.

Symbols come from build_dfu_evklite/output/akaLinkPro_App.asm (# <sym> comments).

Usage:  python hpm6800_rtt_diag.py [COM5] [seconds]
"""
import os
import sys
import threading
import time

import hid

VID, PID = 0x0D28, 0x0204
CMD_RTT = 0x31
ACT_STOP, ACT_START, ACT_STATUS, ACT_PEEK, ACT_CONFIG, ACT_TARGET = 0, 1, 2, 5, 7, 10

CB_ADDR = 0x1240000
CB_SIZE = 0x2000

# uint32 scalars (one PEEK word each)
SYM = [
    ("uartrx.in",     0x82010),
    ("uartrx.out",    0x82014),
    ("uartrx.mask",   0x82018),
    ("uartrx.pool",   0x8201c),
    ("tx_idle",       0x8185f),
    ("cfg_uart_tr",   0x81861),
    ("cfg_uart",      0x81862),
    ("cdc_src_rtt",   0x8188c),
    ("uart2_com",     0x8188d),
    ("s_running",     0x8193d),
    ("s_open",        0x81958),
    ("s_cb_addr",     0x81938),
    ("s_up_addr",     0x8192c),
    ("s_polls",       0x81914),
    ("s_drained",     0x81918),
    ("s_read_err",    0x8190c),
    ("s_write_err",   0x81908),
    ("s_zips",        0x818fc),
    ("s_gate_hits",   0x81900),
    ("s_rescans",     0x81904),
    ("s_last_move",   0x818f8),
    ("s_rd_pend_v",   0x8191f),
    ("s_rd_pend",     0x81920),
    ("s_last_sbcs",   0x81948),
    ("s_idcode",      0x81954),
    ("s_dbg_n",       0x81940),
    ("s_last_cmd",    0x818f4),
    ("s_last_rsp",    0x818f0),
]

# uint64_t s_dbg[4]: raw 41-bit DMI responses (op[1:0] | data[33:2] | addr[40:34])
S_DBG = 0x80F40


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT (%ss)" % sec)
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


def open_hid(timeout=8.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        cand = [i for i in hid.enumerate(VID, PID) if i.get("usage_page") == 0xFF00]
        if cand:
            d = hid.device()
            d.open_path(cand[0]["path"])
            d.set_nonblocking(1)
            time.sleep(0.15)
            return d
        time.sleep(0.3)
    raise RuntimeError("custom HID interface not found")


def cmd(dev, action, args=None, tmo=2.0):
    args = list(args or [])
    req = [0x01, 1 + 1 + len(args), CMD_RTT, action] + args
    req += [0] * (64 - len(req))
    dev.write(req)
    t0 = time.time()
    while time.time() - t0 < tmo:
        d = dev.read(64, timeout_ms=200)
        if d and d[0] == 0x02 and d[2] == CMD_RTT:
            return d
    return None


def words(resp):
    if resp is None:
        return None
    return [int.from_bytes(bytes(resp[4 + i * 4:8 + i * 4]), "little") for i in range(12)]


def u32(v):
    return [v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF]


def peek(dev, addr, n=1):
    w = words(cmd(dev, ACT_PEEK, u32(addr) + [n] + [0, 0, 0]))
    return w[:n] if w else None


def snapshot(dev, tag):
    print("--- %s ---" % tag)
    vals = {}
    for name, addr in SYM:
        r = peek(dev, addr, 1)
        vals[name] = r[0] if r else None
    for name, addr in SYM:
        print("   %-14s = %s" % (name, ("0x%08X" % vals[name]) if vals[name] is not None else "??"))
    used = None
    if vals["uartrx.mask"] is not None:
        used = (vals["uartrx.in"] - vals["uartrx.out"]) & vals["uartrx.mask"]
        print("   ring used      = %d  (in=0x%X out=0x%X mask=0x%X)"
              % (used, vals["uartrx.in"], vals["uartrx.out"], vals["uartrx.mask"]))
    dbg = peek(dev, S_DBG, 8)
    if dbg:
        s = []
        for i in range(4):
            v = dbg[2 * i] | (dbg[2 * i + 1] << 32)
            op = v & 3
            data = (v >> 2) & 0xFFFFFFFF
            a = (v >> 34) & 0x7F
            s.append("[%d] op=%d data=0x%08X addr=0x%02X" % (i, op, data, a))
        print("   s_dbg: " + " | ".join(s))
    return vals, used


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    port = argv[0] if argv else "COM5"
    secs = float(argv[1]) if len(argv) > 1 else 3.0

    import serial

    dev = open_hid()
    print("target = RISC-V (JTAG)")
    cmd(dev, ACT_TARGET, [1])
    cmd(dev, ACT_CONFIG, u32(0) + [0x00, 0x04, 0, 0xFF])   # chunk 1024 B
    snapshot(dev, "before start (bridge stopped)")

    ser = serial.Serial(port, 115200, timeout=0.1)
    ser.reset_input_buffer()
    time.sleep(0.3)
    snapshot(dev, "COM5 open, bridge still stopped")

    print("starting bridge [0x%08X, +0x%X) ch0" % (CB_ADDR, CB_SIZE))
    r = cmd(dev, ACT_START, u32(CB_ADDR) + u32(CB_SIZE) + [0])
    w = words(r)
    rc = (w[0] >> 16) & 0xFF if w else -1
    print("   rc=%d (0=ok,1=init,3=cb not found)" % rc)

    got = 0
    half = secs / 2.0
    for phase in ("mid", "end"):
        t0 = time.time()
        while time.time() - t0 < half:
            got += len(ser.read(65536))
        snapshot(dev, "%s run: host read %d bytes" % (phase, got))
    ser.close()

    print("stopping bridge")
    cmd(dev, ACT_STOP)
    return 0


if __name__ == "__main__":
    watchdog(90)
    sys.exit(main())
