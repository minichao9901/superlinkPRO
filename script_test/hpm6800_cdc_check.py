"""Two questions, one run:

  A) is COM5 really the probe's CDC and does it deliver bytes at all?
     -> stop the bridge, write to COM5, look for the UART2 hardware echo.
  B) while the bridge runs, does g_uartrx advance in step with the host read?
     -> sample in/out and the host byte count every 200 ms.

Usage: python hpm6800_cdc_check.py [COM5]
"""
import os
import sys
import threading
import time

import hid
import serial

VID, PID = 0x0D28, 0x0204
CMD_RTT = 0x31
ACT_STOP, ACT_START, ACT_PEEK, ACT_CONFIG, ACT_TARGET = 0, 1, 5, 7, 10

CB_ADDR, CB_SIZE = 0x1240000, 0x2000
RING_IN, RING_OUT = 0x82010, 0x82014


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


def peek(dev, addr, n=1):
    w = words(cmd(dev, ACT_PEEK, u32(addr) + [n] + [0, 0, 0]))
    return w[:n] if w else None


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM5"
    dev = open_hid()
    cmd(dev, ACT_TARGET, [1])
    cmd(dev, ACT_STOP)
    time.sleep(0.3)

    ser = serial.Serial(port, 115200, timeout=0.1)
    ser.reset_input_buffer()

    print("A) CDC echo test (bridge stopped, UART2 hardware loopback expected)")
    ser.write(b"CDC-CHECK-0123456789\r\n")
    ser.flush()
    t0 = time.time()
    back = b""
    while time.time() - t0 < 1.0:
        back += ser.read(4096)
    print("   wrote 23 bytes, echoed back %d bytes: %r" % (len(back), back[:64]))

    print("B) start bridge, watch ring vs host")
    r = cmd(dev, ACT_START, u32(CB_ADDR) + u32(CB_SIZE) + [0])
    w = words(r)
    print("   rc=%d" % (((w[0] >> 16) & 0xFF) if w else -1))

    got = 0
    ser.reset_input_buffer()
    t0 = time.time()
    while time.time() - t0 < 3.0:
        n = len(ser.read(65536))
        got += n
        if n:
            print("   +%d bytes at t=%.2fs (total %d)" % (n, time.time() - t0, got))
        v = peek(dev, RING_IN, 2)
        if v:
            print("   t=%.2fs ring in=%d out=%d used=%d host=%d"
                  % (time.time() - t0, v[0], v[1], (v[0] - v[1]) & 0x7FFF, got))
        time.sleep(0.2)

    print("   final host bytes = %d" % got)
    cmd(dev, ACT_STOP)
    ser.close()
    return 0


if __name__ == "__main__":
    watchdog(60)
    sys.exit(main())
