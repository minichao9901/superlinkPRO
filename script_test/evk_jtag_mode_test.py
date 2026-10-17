"""Verify the COM port survives DAP output_mode = SWD+JTAG on the EVKLite.

Usage: python evk_jtag_mode_test.py [COMxx]
Restores output_mode = 0 at the end.
"""
import os
import sys
import time
import threading

import hid
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM52"
CMD_SET_CONFIG = 0x02
CMD_GET_VOLTAGE = 0x03
CMD_RESET_DEVICE = 0xFE
PAYLOAD = b"JK"


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT")
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


def open_hid(timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            infos = hid.enumerate(0x0D28, 0x0204)
            cand = [i for i in infos if i.get("usage_page") == 0xFF00]
            if cand:
                d = hid.device()
                d.open_path(cand[0]["path"])
                d.set_nonblocking(1)
                time.sleep(0.3)
                return d
        except Exception:
            pass
        time.sleep(0.4)
    raise RuntimeError("HID did not come back")


def xfer(dev, cmd, args=(), tmo=1.0):
    req = [0x01, 0x01, cmd] + list(args)
    req += [0] * (64 - len(req))
    dev.write(req)
    t0 = time.time()
    while time.time() - t0 < tmo:
        d = dev.read(64, timeout_ms=150)
        if d and d[0] == 0x02 and d[2] == cmd:
            return d
    return None


def blk(dev, idx):
    d = xfer(dev, CMD_GET_VOLTAGE, [idx])
    if not d:
        return None
    raw = bytes(d[3:35])
    return [int.from_bytes(raw[i * 4:i * 4 + 4], "little") for i in range(8)]


def config_snapshot(dev, tag):
    b0 = blk(dev, 0)
    print("[%s] output_mode=%d com_mode=%d TX_FUNC=0x%X RX_FUNC=0x%X clk=%d baud=%d" %
          (tag, (b0[4] >> 8) & 0xFF, b0[4] & 0xFF, b0[0] & 0x1F, b0[1] & 0x1F, b0[6], b0[7]))
    return b0


def loopback(dev):
    got = b""
    try:
        ser = serial.Serial(PORT, 115200, timeout=0.2, write_timeout=2)
    except Exception as e:
        print("  CDC open failed: %r" % e)
        return False
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        time.sleep(0.2)
        ser.write(PAYLOAD)
        ser.flush()
        t0 = time.time()
        while time.time() - t0 < 1.0:
            c = ser.read(64)
            if c:
                got += c
    finally:
        ser.close()
    print("  echo: %r -> %s" % (got[:16], "OK" if got == PAYLOAD else "FAIL"))
    return got == PAYLOAD


def set_output_mode(dev, mode):
    d = xfer(dev, CMD_SET_CONFIG, [mode, 1, 0, 1, 0, 0, 0xC4, 0x0C])  # led modes/vref left as defaults
    ok = bool(d)
    print("  CMD_SET_CONFIG output_mode=%d -> %s" % (mode, "acked" if ok else "no reply"))
    return ok


def reboot(dev):
    """CMD_RESET_DEVICE power-cycles the MCU: send and do not wait for a reply."""
    req = [0x01, 0x01, CMD_RESET_DEVICE] + [0] * 61
    try:
        dev.write(req)
    except Exception:
        pass
    time.sleep(3.5)


def main():
    dev = open_hid()
    print("=== 1. baseline (output_mode = SWD only) ===")
    config_snapshot(dev, "swd")
    r1 = loopback(dev)

    print("=== 2. switch to SWD+JTAG at runtime ===")
    set_output_mode(dev, 1)
    time.sleep(0.5)
    b0 = config_snapshot(dev, "jtag-run")
    r2 = loopback(dev)

    print("=== 3. reboot with output_mode = 1 stored in flash ===")
    reboot(dev)
    dev = open_hid()
    b0b = config_snapshot(dev, "jtag-boot")
    r3 = loopback(dev)

    print("=== 4. restore output_mode = 0 ===")
    set_output_mode(dev, 0)
    time.sleep(0.5)
    reboot(dev)
    dev = open_hid()
    config_snapshot(dev, "restored")
    r4 = loopback(dev)

    ok = r1 and r2 and r3 and r4 and ((b0b[4] & 0xFF) == 1)
    print("\nRESULT: %s" % ("PASS - COM port stays alive in JTAG mode, pins muxed at boot"
                            if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    watchdog(90)
    sys.exit(main())
