"""Live jumper watch: enable the UART TX heartbeat and poll the RX counters.

Watch the "rx" column: it must start growing as soon as J3.8 (TX) and J3.10 (RX)
are shorted together. Usage: python evk_watch_jumper.py [seconds]
"""
import os
import sys
import time
import threading

import hid

CMD_GET_VOLTAGE = 0x03
CMD_DIAG = 0x30
SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 60


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT")
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


def open_hid():
    infos = hid.enumerate(0x0D28, 0x0204)
    cand = [i for i in infos if i.get("usage_page") == 0xFF00]
    if not cand:
        raise RuntimeError("custom HID not found")
    d = hid.device()
    d.open_path(cand[0]["path"])
    d.set_nonblocking(1)
    return d


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


def blk2(dev):
    d = xfer(dev, CMD_GET_VOLTAGE, [2])
    if not d:
        return None
    raw = bytes(d[3:35])
    return [int.from_bytes(raw[i * 4:i * 4 + 4], "little") for i in range(8)]


def main():
    dev = open_hid()
    print("heartbeat ON (0x55 on PB15/J3.8 every 250 ms), watching RX for %ds" % SECONDS)
    xfer(dev, CMD_DIAG, [3, 1, 0, 1])  # heartbeat on, internal loopback off, reset counters
    print("t   rx_dma_max rx_now ringrx uart_tx(thr+tx) cdc_in")
    t0 = time.time()
    last = None
    while time.time() - t0 < SECONDS:
        b = blk2(dev)
        if not b:
            print("  (hid read failed)")
            time.sleep(1)
            continue
        now = (b[6], b[5], b[3], b[2])
        mark = ""
        if last is not None and now[0] != last[0]:
            mark = "  <== RX ACTIVITY!"
        last = now
        print("%4.1fs  %6d %8d %6d %8d %6d%s" %
              (time.time() - t0, b[6], b[5], b[3], b[2], b[4], mark))
        time.sleep(0.7)
    xfer(dev, CMD_DIAG, [3, 0, 0, 0])  # heartbeat off
    b = blk2(dev)
    print("final: rx_dma_max=%d rx_now=%d ringrx=%d uart_tx=%d cdc_in=%d" %
          (b[5], b[6], b[3], b[2], b[4]))
    if b[5] > 2:
        print("VERDICT: jumper PRESENT - RX data is coming back")
    else:
        print("VERDICT: still no RX data - J3.8 <-> J3.10 is not connected")


if __name__ == "__main__":
    watchdog(SECONDS + 20)
    main()
