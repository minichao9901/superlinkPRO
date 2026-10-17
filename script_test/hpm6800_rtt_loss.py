"""Loss check for the probe-side RTT bridge over JTAG (HPM6800EVK).

The flood firmware writes the same 13-byte record "hello world!\\n" in a tight
loop. Any dropped byte shifts the pattern, so a received stream that is an exact
repetition of that record proves the delivery is byte-exact - a stronger claim
than "the counters say so".

Host-side reading matters as much as the probe side: pyserial's read(n) allocates
a fresh buffer per call and caps the host at ~2169 KB/s (measured on this
project), which at the rates this bridge reaches shows up as *host-side* byte
loss while the probe still reports rderr=0/wderr=0. So: readinto() into one
reused 1 MB buffer, and after the measuring window stop the bridge FIRST and only
then drain the tail - with the producer stopped, whatever is still missing is
real loss, not buffering.

Usage: python hpm6800_rtt_loss.py [COM5] [seconds]
"""
import os
import sys
import threading
import time

import hid
import serial

VID, PID = 0x0D28, 0x0204
CMD_RTT = 0x31
ACT_STOP, ACT_START, ACT_STATUS, ACT_CONFIG, ACT_TARGET = 0, 1, 2, 7, 10

CB_ADDR, CB_SIZE = 0x1240000, 0x2000
PAT = b"hello world!\n"


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
            time.sleep(0.15)
            return d
        time.sleep(0.3)
    raise RuntimeError("no HID")


def cmd(dev, action, args=None, tmo=2.0):
    args = list(args or [])
    req = [0x01, 2 + len(args), CMD_RTT, action] + args
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


def stream_report(body):
    """Walk the pattern record by record. A next occurrence that is not exactly
    one record away is a gap (dropped bytes) or an overlap (duplicated bytes)."""
    pos, n, lost, dup = 0, 0, 0, 0
    while True:
        j = body.find(PAT, pos)
        if j < 0:
            break
        d = j - pos
        if d > 0:
            lost += d
        elif d < 0:
            dup += -d
        pos = j + len(PAT)
        n += 1
    return n, lost, dup, len(body) - pos


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    port = argv[0] if argv else "COM5"
    secs = float(argv[1]) if len(argv) > 1 else 10.0

    dev = open_hid()
    cmd(dev, ACT_TARGET, [1])
    cmd(dev, ACT_CONFIG, u32(0) + [0x00, 0x04, 0, 0xFF])   # chunk 1024 B
    cmd(dev, ACT_STOP)

    ser = serial.Serial(port, 115200, timeout=0.2)
    ser.reset_input_buffer()

    r = cmd(dev, ACT_START, u32(CB_ADDR) + u32(CB_SIZE) + [0])
    w = words(r)
    print("bridge rc=%d, draining %s for %.1fs ..."
          % (((w[0] >> 16) & 0xFF) if w else -1, port, secs))

    buf = bytearray()
    rbuf = bytearray(1 << 20)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < secs:
        n = ser.readinto(rbuf)
        if n:
            buf += rbuf[:n]
    dt = time.perf_counter() - t0

    # Stop the producer first, then collect the tail: with the bridge stopped,
    # whatever the host is still missing after this is real loss, not buffering.
    cmd(dev, ACT_STOP)
    t1 = time.perf_counter()
    while time.perf_counter() - t1 < 1.0:
        n = ser.readinto(rbuf)
        if n:
            buf += rbuf[:n]
            t1 = time.perf_counter()
    ser.close()

    w = words(cmd(dev, ACT_STATUS))
    got = len(buf)
    print("host received   %d bytes in %.3fs -> %.1f KB/s" % (got, dt, got / dt / 1024.0))
    if w:
        print("probe drained   %d bytes (polls=%d moves=%d rderr=%d wderr=%d zips=%d)"
              % (w[3], w[4] & 0xFFFF, w[4] >> 16, w[5] & 0xFFFF, w[5] >> 16, w[6] >> 16))
        print("probe-host delta: %d bytes (+ = probe drained more, tail in flight)"
              % (w[3] - got))

    i = buf.find(PAT)
    if i < 0:
        print("FAIL: pattern never found in %d bytes: %r" % (got, bytes(buf[:64])))
        return 1
    body = bytes(buf[i:])
    n, lost, dup, tail = stream_report(body)
    print("records=%d lost=%d dup=%d tail=%d (banner skipped: %d)"
          % (n, lost, dup, tail, i))
    if lost or dup:
        print("FAIL: stream is not a clean repetition (%d bytes lost, %d duplicated)"
              % (lost, dup))
        return 1
    print("stream check OK: pattern exact - %d bytes, 0 lost, 0 duplicated"
          % (n * len(PAT)))
    return 0


if __name__ == "__main__":
    watchdog(180)
    sys.exit(main())
