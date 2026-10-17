"""Verify the CDC bridge auto-arms: no force-start, just open the port and echo.

Usage: python evk_verify.py [COMxx] [baud] [label]
"""
import os
import sys
import time
import threading

import hid
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM52"
BAUD = int(sys.argv[2]) if len(sys.argv) > 2 else 115200
LABEL = sys.argv[3] if len(sys.argv) > 3 else "verify"
PAYLOAD = b"HELLO-0123456789-abcdefghij"

DEV = None


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT")
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


def open_hid():
    global DEV
    infos = hid.enumerate(0x0D28, 0x0204)
    cand = [i for i in infos if i.get("usage_page") == 0xFF00]
    if not cand:
        raise RuntimeError("custom HID not found")
    DEV = hid.device()
    DEV.open_path(cand[0]["path"])
    DEV.set_nonblocking(1)


def xfer(cmd, args=(), tmo=1.0):
    req = [0x01, 0x01, cmd] + list(args)
    req += [0] * (64 - len(req))
    DEV.write(req)
    t0 = time.time()
    while time.time() - t0 < tmo:
        d = DEV.read(64, timeout_ms=150)
        if d and d[0] == 0x02 and d[2] == cmd:
            return d
    return None


def rd_block(idx):
    d = xfer(0x03, [idx])
    if not d:
        return None
    raw = bytes(d[3:35])
    return [int.from_bytes(raw[i * 4:i * 4 + 4], "little") for i in range(8)]


def flags_str(f):
    names = ["config_uart", "cfg_xfer", "usbtx_idle", "uarttx_idle",
             "usbrx_idle", "req_idle", "resp_idle"]
    return ",".join(n for i, n in enumerate(names) if f & (1 << i)) or "-"


def main():
    open_hid()
    b3 = rd_block(3)
    print("[%s] before open : flags=[%s] rate=%d" % (LABEL, flags_str(b3[0]), b3[1]))
    # reset the data-path counters so the numbers below belong to this round
    xfer(0x30, [3, 0, 0, 1])

    got = b""
    ser = serial.Serial(PORT, BAUD, timeout=0.2, write_timeout=2)
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        time.sleep(0.2)
        ser.write(PAYLOAD)
        ser.flush()
        t0 = time.time()
        while time.time() - t0 < 1.5:
            c = ser.read(512)
            if c:
                got += c
    finally:
        ser.close()

    b2 = rd_block(2)
    b3 = rd_block(3)
    print("[%s] after  open : flags=[%s]" % (LABEL, flags_str(b3[0])))
    print("[%s] counters   : cdc_out=%d usbrx=%d uart_tx=%d ringrx=%d cdc_in=%d" %
          (LABEL, b2[0], b2[1], b2[2], b2[3], b2[4]))
    ok = got == PAYLOAD
    print("[%s] echo       : sent %d, got %d %s" %
          (LABEL, len(PAYLOAD), len(got), "MATCH" if ok else repr(got[:48])))
    print("[%s] RESULT     : %s" % (LABEL, "PASS" if ok else "FAIL"))
    return 0 if ok else 2


if __name__ == "__main__":
    watchdog(30)
    sys.exit(main())
