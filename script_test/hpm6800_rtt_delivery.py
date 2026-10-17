"""RTT delivery rate from the HPM6800EVK flood firmware, drained by the probe's
own RTT bridge over **JTAG** (RISC-V backend, src/riscv/).

The probe polls the target's SEGGER RTT control block itself and pushes the
bytes into the CDC ring, so the host only reads a COM port (no round trips).

Host-side reading is part of the measurement: pyserial's read(n) allocates a
fresh buffer per call and caps the host at ~2169 KB/s on this project (measured),
so the loop below uses readinto() into one reused 1 MB buffer - the same pattern
the SWD-side scripts use. Order matters too: open the port and start reading
BEFORE ACT_START, otherwise reset_input_buffer() throws away the first chunk the
bridge pushes.

Usage:
  python hpm6800_rtt_delivery.py [COM5] [seconds] [--addr 0x1240000] [--size 0x2000]
"""
import os
import sys
import threading
import time

import hid

VID, PID = 0x0D28, 0x0204
CMD_RTT = 0x31
ACT_STOP, ACT_START, ACT_STATUS, ACT_CONFIG, ACT_TARGET = 0, 1, 2, 7, 10

CB_ADDR = 0x1240000      # &_SEGGER_RTT (see nm on the flood firmware)
CB_SIZE = 0x2000


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


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    port = argv[0] if argv else "COM5"
    secs = float(argv[1]) if len(argv) > 1 else 5.0
    addr, size = CB_ADDR, CB_SIZE
    if "--addr" in sys.argv:
        addr = int(sys.argv[sys.argv.index("--addr") + 1], 0)
    if "--size" in sys.argv:
        size = int(sys.argv[sys.argv.index("--size") + 1], 0)

    dev = open_hid()

    print("1. target kind = RISC-V (JTAG)")
    cmd(dev, ACT_TARGET, [1])
    print("2. chunk = 1024 B")
    cmd(dev, ACT_CONFIG, u32(0) + [0x00, 0x04, 0, 0xFF])   # hz=keep, chunk=1024
    cmd(dev, ACT_STOP)

    # 先把 COM 口打开并开始读，**再**启动桥：反过来（先 start、后 open +
    # reset_input_buffer）会把桥搬出的第一块数据整个丢掉。
    import serial
    ser = serial.Serial(port, 115200, timeout=0.2)
    ser.reset_input_buffer()

    print("3. start bridging [0x%08X, +0x%X) channel 0" % (addr, size))
    r = cmd(dev, ACT_START, u32(addr) + u32(size) + [0])
    w = words(r)
    rc = (w[0] >> 16) & 0xFF if w else -1
    print("   rc=%d  (0 = ok; 1 = SWD/JTAG init fail; 3 = control block not found)" % rc)

    print("4. draining %s for %.1fs ..." % (port, secs))
    n = 0
    got = 0
    rbuf = bytearray(1 << 20)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < secs:
        n = ser.readinto(rbuf)
        if n:
            got += n
    dt = time.perf_counter() - t0

    # 先停桥再收尾：生产者停了以后还差的才是真丢包。
    cmd(dev, ACT_STOP)
    t1 = time.perf_counter()
    while time.perf_counter() - t1 < 1.0:
        n = ser.readinto(rbuf)
        if n:
            got += n
            t1 = time.perf_counter()
    ser.close()

    w = words(cmd(dev, ACT_STATUS))
    print("   host read %d bytes in %.2fs -> %.1f KB/s" % (got, dt, got / dt / 1024.0))
    if w:
        print("   bridge: drained=%d bytes, polls=%d, moves=%d, rderr=%d, wderr=%d, "
              "lastmove=%d, empty=%d" %
              (w[3], w[4] & 0xFFFF, w[4] >> 16, w[5] & 0xFFFF, w[5] >> 16,
               w[6] & 0xFFFF, w[6] >> 16))
    return 0


if __name__ == "__main__":
    watchdog(120)
    sys.exit(main())
