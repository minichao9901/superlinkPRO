"""Peek into the probe's own state while the SWD bring-up fails.

HID CMD_RTT action 5 reads up to 12 words of the probe's address space, so we
can inspect DAP_Data, the SWD pin muxing and the loaded bit-bang blob without
rebuilding the firmware.

Usage: python rtt_peek.py
"""
import os
import sys
import time
import threading

import hid

CMD_RTT = 0x31
ACT_PEEK = 5

# from the linker map / SDK headers
DAP_DATA = 0x0008127C     # .bss.DAP_Data (0x44 bytes)
SWD_OPS = 0x000058C0      # .fast.swd_ops
IOC_BASE = 0xF4040000
FGPIO_BASE = 0x000C0000


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG")
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


def open_hid():
    infos = hid.enumerate(0x0D28, 0x0204)
    cand = [i for i in infos if i.get("usage_page") == 0xFF00]
    if not cand:
        raise RuntimeError("no HID")
    d = hid.device()
    d.open_path(cand[0]["path"])
    d.set_nonblocking(1)
    return d


def peek(dev, addr, n):
    pkt = [0x01, 0x01, CMD_RTT, ACT_PEEK,
           addr & 0xFF, (addr >> 8) & 0xFF, (addr >> 16) & 0xFF, (addr >> 24) & 0xFF, n]
    pkt += [0] * (64 - len(pkt))
    dev.write(pkt)
    t0 = time.time()
    while time.time() - t0 < 1.0:
        r = dev.read(64, timeout_ms=200)
        if r and r[0] == 0x02 and r[2] == CMD_RTT:
            words = [int.from_bytes(bytes(r[4 + i * 4:8 + i * 4]), "little") for i in range(n)]
            return words
    return None


def main():
    dev = open_hid()

    print("=== DAP_Data @ 0x%08X (17 words) ===" % DAP_DATA)
    w = peek(dev, DAP_DATA, 12)
    w2 = peek(dev, DAP_DATA + 48, 5) if w else None
    if w:
        print("  [0] debug_port=%d fast_clock=%d abort=%d pad=%d" % tuple(w[0] & 0xFF for _ in [0]) if False else
              "  [0] debug_port=%d fast_clock=%d transfer_abort=%d" % (w[0] & 0xFF, w[0] >> 8 & 0xFF, w[0] >> 16 & 0xFF))
        print("  [1] clock_delay=%d" % w[1])
        print("  [2] timestamp=%d" % w[2])
        print("  [3] idle_cycles=%d" % (w[3] & 0xFF))
        print("  [4] retry_count=%d match_retry=%d" % (w[4] & 0xFFFF, w[4] >> 16))
        print("  [5] match_mask=0x%08X" % w[5])
        print("  [6] swd turnaround=%d data_phase=%d" % (w[6] & 0xFF, w[6] >> 8 & 0xFF))
    if w2:
        print("  [12..16] 0x%08X 0x%08X 0x%08X 0x%08X 0x%08X" % tuple(w2))

    print()
    print("=== swd_ops @ 0x%08X (blob loaded?) ===" % SWD_OPS)
    b = peek(dev, SWD_OPS, 4)
    print("  %s" % (" ".join("%08X" % x for x in b) if b else "n/a"))

    print()
    print("=== IOC pads (PA06=SWCLK, PA07=SWDIO) ===")
    for name, idx in (("PA06", 6), ("PA07", 7), ("PA04", 4), ("PA05", 5), ("PA08", 8)):
        a = IOC_BASE + idx * 8
        r = peek(dev, a, 2)
        if r:
            print("  %s FUNC_CTL=0x%08X (ALT=%d) PAD_CTL=0x%08X" %
                  (name, r[0], r[0] & 0x1F, r[1]))

    print()
    print("=== FGPIO port A: direction / output / input @ 0x%08X ===" % FGPIO_BASE)
    # GPIO_Type: DI[15] at 0x000, DO[15] at 0x100, OE[15] at 0x200
    di = peek(dev, FGPIO_BASE + 0x000, 1)
    do = peek(dev, FGPIO_BASE + 0x100, 1)
    oe = peek(dev, FGPIO_BASE + 0x200, 1)
    if di and do and oe:
        print("  DI[0]=0x%08X  DO[0]=0x%08X  OE[0]=0x%08X" % (di[0], do[0], oe[0]))
        print("  SWCLK(bit6) oe=%d out=%d in=%d | SWDIO(bit7) oe=%d out=%d in=%d" %
              ((oe[0] >> 6) & 1, (do[0] >> 6) & 1, (di[0] >> 6) & 1,
               (oe[0] >> 7) & 1, (do[0] >> 7) & 1, (di[0] >> 7) & 1))
    return 0


if __name__ == "__main__":
    watchdog(45)
    sys.exit(main())
