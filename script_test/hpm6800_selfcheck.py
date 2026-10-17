"""End-to-end integrity check of the probe-side RISC-V engine.

The probe writes a known byte pattern (k*7+3) into target memory through the SBA
and reads it straight back, so a single run exercises both directions without
needing a second debugger. Used as the pass/fail gate while tuning the JTAG
timing knobs (DMI_CAP_HIGH_NOP / DMI_NAV_*_NOP).

Usage: python hpm6800_selfcheck.py [addr] [bytes]
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hpm6800_riscv as R  # noqa: E402


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT (%ss)" % sec)
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


def expected_sum(nbytes):
    data = bytes(((k * 7 + 3) & 0xFF) for k in range(nbytes))
    total = 0
    for i in range(0, nbytes, 4):
        total = (total + int.from_bytes(data[i:i + 4], "little")) & 0xFFFFFFFF
    return total


def main():
    addr = int(sys.argv[1], 0) if len(sys.argv) > 1 else 0x1200000
    size = int(sys.argv[2], 0) if len(sys.argv) > 2 else 1024

    dev = R.open_hid()
    w = R.request(dev, R.ACT_OPEN)
    ok_open = (w[0] & 0xFF) == 1
    dmstatus = w[3]
    print("open=%d rc=0x%X dmstatus=0x%08X" % (w[0] & 0xFF, (w[0] >> 16) & 0xFF, dmstatus))

    # OPEN reads dmstatus through a write+read pair, so a sane dmstatus is
    # already strong evidence the DTM link works (version 1/2/3, not 0).
    dmstatus_ok = (dmstatus & 0xF) in (1, 2, 3)

    R.request(dev, R.ACT_WBENCH, addr=addr, a1=size, a2=1, timeout=60)
    w = R.request(dev, R.ACT_RCHECK, addr=addr, a1=4, timeout=60)
    got = w[9]
    want = expected_sum(16)
    data_ok = (got == want)
    print("write+readback checksum: got 0x%08X want 0x%08X -> %s" %
          (got, want, "OK" if data_ok else "MISMATCH"))

    R.request(dev, R.ACT_STOP)
    ok = ok_open and dmstatus_ok and data_ok
    print("RESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    watchdog(120)
    sys.exit(main())
