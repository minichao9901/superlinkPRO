"""Step-by-step SWD bring-up through the probe's raw DAP passthrough.

Uses HID CMD_RTT action 4: run an arbitrary CMSIS-DAP request inside the
firmware and echo the 16-byte response. Lets us find out where the probe's own
SWD access stops working, without rebuilding the firmware each time.

Usage: python rtt_rawdap.py
"""
import os
import sys
import time
import threading

import hid

CMD_RTT = 0x31
ACT_RAW_DAP = 4
ACT_RAW_RESULT = 6
ID_DAP_CONNECT, ID_DAP_TRANSFER, ID_DAP_TRANSFER_BLOCK, ID_DAP_SWJ_SEQUENCE = 0x02, 0x05, 0x06, 0x12
APnDP, RnW = 0x01, 0x02
CSW_VAL = 0x23000052


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
        raise RuntimeError("no custom HID interface")
    d = hid.device()
    d.open_path(cand[0]["path"])
    d.set_nonblocking(1)
    return d


def xfer(dev, pkt, expect_len=True):
    """Write one HID report and return the firmware's CMD_RTT response."""
    pkt = pkt + [0] * (64 - len(pkt))
    dev.write(pkt)
    t0 = time.time()
    while time.time() - t0 < 1.0:
        r = dev.read(64, timeout_ms=200)
        if r and r[0] == 0x02 and r[2] == CMD_RTT:
            return r
    return None


def raw(dev, req, note=""):
    """Queue the DAP request (it runs in the main loop), then read the result.

    Both HID writes produce a response, so each one is consumed in order."""
    r0 = xfer(dev, [0x01, 0x01, CMD_RTT, ACT_RAW_DAP, len(req)] + list(req))
    if r0 is None:
        print("  %-26s no reply to the queue write" % note)
        return None
    time.sleep(0.05)  # let the main loop execute it
    r = xfer(dev, [0x01, 0x01, CMD_RTT, ACT_RAW_RESULT])
    if r is None:
        print("  %-26s no reply to the result read" % note)
        return None
    n = r[3]
    payload = bytes(r[4:4 + max(0, min(n, 16))])
    print("  %-26s req=%-28s -> %s" % (note, " ".join("%02X" % b for b in req[:9]),
                                       " ".join("%02X" % b for b in payload[:10])))
    return payload


def check_swd_response(resp, note):
    if not resp:
        return False
    value = resp[3]
    ok = (value & 0x01) != 0
    print("      value=0x%02X %s" % (value, "(OK)" if ok else "(no ACK / WAIT / FAULT)"))
    return ok


def main():
    dev = open_hid()
    print("=== raw DAP bring-up ===")

    print("1. DAP_Connect(SWD)")
    r = raw(dev, [ID_DAP_CONNECT, 0x01], "connect swd")
    print("      port=0x%02X" % (r[1] if r else 0))

    print("2. SWD line reset (51 clocks high) + JTAG-to-SWD + line reset")
    raw(dev, [ID_DAP_SWJ_SEQUENCE, 51, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x7F], "line reset 51")
    raw(dev, [ID_DAP_SWJ_SEQUENCE, 16, 0x9E, 0xE7], "jtag-to-swd")
    raw(dev, [ID_DAP_SWJ_SEQUENCE, 51, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x7F], "line reset 51")

    print("3. read DP IDCODE (expect 0x1BA01477 on an STM32F1)")
    r = raw(dev, [ID_DAP_TRANSFER, 0x00, 0x01, 0x02], "DP read IDCODE")
    check_swd_response(r, "idcode")
    if r:
        print("      data=%02X%02X%02X%02X" % (r[7], r[6], r[5], r[4]))

    print("4. write AHB-AP CSW = 0x%08X" % CSW_VAL)
    r = raw(dev, [ID_DAP_TRANSFER, 0x00, 0x01, APnDP | 0x00,
                  CSW_VAL & 0xFF, (CSW_VAL >> 8) & 0xFF, (CSW_VAL >> 16) & 0xFF, (CSW_VAL >> 24) & 0xFF],
            "AP write CSW")
    check_swd_response(r, "csw")

    print("5. write AHB-AP TAR = 0x20000000")
    r = raw(dev, [ID_DAP_TRANSFER, 0x00, 0x01, APnDP | 0x04, 0x00, 0x00, 0x00, 0x20], "AP write TAR")
    check_swd_response(r, "tar")

    print("6. block-read 4 words from AP DRW (expect 'SEGG' = 53 45 47 47)")
    r = raw(dev, [ID_DAP_TRANSFER_BLOCK, 0x00, 0x04, 0x00, APnDP | RnW | 0x0C], "AP block read x4")
    if check_swd_response(r, "block"):
        print("      data=%s" % " ".join("%02X" % b for b in r[4:20]))

    print("7. read the RTT control block signature at 0x2000000C")
    r = raw(dev, [ID_DAP_TRANSFER, 0x00, 0x01, APnDP | 0x04, 0x0C, 0x00, 0x00, 0x20], "TAR = 0x2000000C")
    check_swd_response(r, "tar2")
    r = raw(dev, [ID_DAP_TRANSFER_BLOCK, 0x00, 0x04, 0x00, APnDP | RnW | 0x0C], "read CB")
    if check_swd_response(r, "cb"):
        sig = bytes(r[4:14])
        print("      bytes=%s  ascii=%r" % (" ".join("%02X" % b for b in sig), sig))
    return 0


if __name__ == "__main__":
    watchdog(45)
    sys.exit(main())
