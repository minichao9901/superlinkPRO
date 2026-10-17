"""EVKLite VCOM bring-up diagnostics (TEMP-DIAG-2 firmware).

Reads the HID diagnostic blocks, runs the pin/jumper wire test and the UART
internal loopback self-test, then exercises the CDC port and the forced bridge.

Usage: python evk_diag.py [COMxx] [baud]
"""
import os
import sys
import time
import threading

import hid
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM52"
BAUD = int(sys.argv[2]) if len(sys.argv) > 2 else 115200

VID, PID = 0x0D28, 0x0204
CMD_GET_VOLTAGE = 0x03
CMD_DIAG = 0x30

DEV = None


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT - aborting")
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


def open_hid():
    global DEV
    infos = hid.enumerate(VID, PID)
    cand = [i for i in infos if i.get("usage_page") == 0xFF00]
    if not cand:
        raise RuntimeError("custom HID (usage_page 0xFF00) not found")
    DEV = hid.device()
    DEV.open_path(cand[0]["path"])
    DEV.set_nonblocking(1)


def hid_xfer(cmd, args=(), tmo=1.0):
    req = [0x01, 0x01, cmd] + list(args)
    req += [0] * (64 - len(req))
    DEV.write(req)
    t0 = time.time()
    while time.time() - t0 < tmo:
        d = DEV.read(64, timeout_ms=150)
        if d and d[0] == 0x02 and d[2] == cmd:
            return d
    return None


def rd_block(idx, tmo=1.0):
    d = hid_xfer(CMD_GET_VOLTAGE, [idx], tmo)
    if not d:
        return None
    raw = bytes(d[3:35])
    return [int.from_bytes(raw[i * 4:i * 4 + 4], "little") for i in range(8)]


def ctrl(action, args=(), tmo=2.0):
    d = hid_xfer(CMD_DIAG, [action] + list(args), tmo)
    if not d:
        return None
    return int.from_bytes(bytes(d[3:7]), "little")


def flags_str(f):
    names = ["config_uart", "cfg_xfer", "usbtx_idle", "uarttx_idle",
             "usbrx_idle", "req_idle", "resp_idle"]
    return ",".join(n for i, n in enumerate(names) if f & (1 << i)) or "-"


def dump_blocks(tag):
    print("\n===== %s =====" % tag)
    b0 = rd_block(0)
    b1 = rd_block(1)
    b2 = rd_block(2)
    b3 = rd_block(3)
    b4 = rd_block(4)
    if not all((b0, b1, b2, b3, b4)):
        print("HID read failed:", b0, b1, b2, b3, b4)
        return None
    print("B0 mux  : TX FUNC_CTL=0x%X  RX FUNC_CTL=0x%X  TX PAD_CTL=0x%X RX PAD_CTL=0x%X" %
          (b0[0] & 0x1F, b0[1] & 0x1F, b0[2], b0[3]))
    print("B0 state: com_mode=%d output_mode=%d rx_dma=%d tx_dma=%d uart=0x%08X clk=%d Hz baud=%d" %
          (b0[4] & 0xFF, (b0[4] >> 8) & 0xFF, (b0[4] >> 16) & 1, (b0[4] >> 17) & 1,
           b0[5], b0[6], b0[7]))
    print("B1 uart : LCR=0x%X MCR=0x%X LSR=0x%08X IER=0x%X FCRR=0x%X IDLE_CFG=0x%X CFG=0x%X" %
          (b1[0], b1[1], b1[2], b1[3], b1[4], b1[5], b1[6]))
    print("B2 count: cdc_out=%d usbrx=%d uart_tx=%d ringrx=%d cdc_in=%d rx_dma_max=%d rx_now=%d rb_pos=%d" %
          tuple(b2))
    print("B3 cdc  : flags=[%s] rate=%d fmt=%d cb(out/in)=%d/%d uartrx_used=%d usbrx=%d/%d txlen=%d" %
          (flags_str(b3[0]), b3[1], b3[2], b3[3] & 0xFFFF, (b3[3] >> 16) & 0xFFFF,
           b3[4], b3[5], b3[6], b3[7]))
    print("B4 isr  : flush=%d uart=%d tx_tc=%d rx_tc=%d wire(pd=%s pu=%s) selftest=0x%06X hb=%d intloop=%d marker=0x%08X" %
          (b4[0], b4[1], b4[2], b4[3], fmt_wire(b4[4] & 0xFF), fmt_wire((b4[4] >> 8) & 0xFF),
           b4[5], b4[6] & 0xFF, (b4[6] >> 8) & 0xFF, b4[7]))
    return b0, b1, b2, b3, b4


def fmt_wire(v):
    """bit0 = RX level while TX driven high, bit1 = RX level while TX driven low."""
    if v == 0b01:
        return "0b01 FOLLOWS(jumper OK)"
    if v == 0b10:
        return "0b10 stuck-low(no jumper?)"
    if v == 0b11:
        return "0b11 stuck-high(no jumper?)"
    if v == 0b00:
        return "0b00 driven-low?"
    return "0b%02b?" % v


def cdc_roundtrip(payload, wait=1.5):
    got = b""
    try:
        ser = serial.Serial(PORT, BAUD, timeout=0.2, write_timeout=2)
    except Exception as e:
        print("  CDC open failed: %r" % e)
        return None
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        time.sleep(0.2)
        ser.write(payload)
        ser.flush()
        t0 = time.time()
        while time.time() - t0 < wait:
            c = ser.read(512)
            if c:
                got += c
    finally:
        try:
            ser.close()
        except Exception:
            pass
    return got


def main():
    open_hid()
    b4 = rd_block(4)
    if not b4 or b4[7] != 0x47414944:
        print("!! diagnostic firmware marker missing (0x%08X) - wrong build?" %
              (b4[7] if b4 else -1))
        return 1

    dump_blocks("baseline (fresh boot)")

    print("\n===== action 1: pin/jumper wire test (TX driven as GPIO, RX read) =====")
    r = ctrl(1)
    print("raw=0x%04X  -> pull-down=%s  pull-up=%s" %
          (r or 0, fmt_wire((r or 0) & 0xFF), fmt_wire(((r or 0) >> 8) & 0xFF)))
    wire_ok = ((r or 0) & 0xFF) == 0x01
    print("VERDICT: jumper %s" % ("PRESENT (pins are bonded to J3.8/J3.10)" if wire_ok
                                 else "NOT SEEN - check the J3.8<->J3.10 jumper"))

    print("\n===== action 2: UART internal loopback self-test =====")
    r = ctrl(2)
    written, match, first_bad = (r or 0) & 0xFF, ((r or 0) >> 8) & 0xFF, ((r or 0) >> 16) & 0xFF
    print("raw=0x%06X written=%d matched=%d first_mismatch=%d" % (r or 0, written, match, first_bad))
    b5 = rd_block(5)
    cap = b"".join(bytes([(w >> (8 * k)) & 0xFF for k in range(4)]) for w in (b5[:4] if b5 else []))
    print("captured bytes:", " ".join("%02X" % x for x in cap[:8]),
          "(expect 55 AA 31 32 33 0D 0A 5A)")
    uart_ok = (written == 8 and match == 8)
    print("VERDICT: UART3 engine + RX DMA %s" % ("OK (internal loopback returns all 8 bytes)"
                                                 if uart_ok else "BROKEN"))

    print("\n===== counters reset, then CDC round-trip (host -> bridge -> host) =====")
    ctrl(3, [0, 0, 1])
    payload = b"HELLO-0123456789-abcdefghij"
    got = cdc_roundtrip(payload)
    print("sent %d bytes, got back %d bytes: %r" % (len(payload), len(got or b""), (got or b"")[:48]))
    dump_blocks("after CDC round-trip (no forced start)")

    print("\n===== action 4: force-start the bridge, then round-trip again =====")
    r = ctrl(4)
    print("force_start result=0x%08X" % (r or 0))
    ctrl(3, [0, 0, 1])
    got = cdc_roundtrip(payload)
    print("sent %d bytes, got back %d bytes: %r" % (len(payload), len(got or b""), (got or b"")[:48]))
    dump_blocks("after CDC round-trip (forced start)")

    print("\n===== action 3: heartbeat on (0x55 on TX every 250 ms) =====")
    ctrl(3, [1, 0, 1])
    time.sleep(1.5)
    b2 = rd_block(2)
    print("counters: cdc_out=%d uart_tx=%d rx_dma_max=%d rx_now=%d ringrx=%d cdc_in=%d" %
          (b2[0], b2[2], b2[5], b2[6], b2[3], b2[4]))
    if b2[6] > 0:
        print("VERDICT: heartbeat bytes are coming back on RX -> TX pin + jumper + RX path OK")
    else:
        print("VERDICT: no heartbeat seen on RX -> TX pin dead, jumper missing, or RX DMA dead")
    ctrl(3, [0, 0, 0])
    return 0


if __name__ == "__main__":
    watchdog(60)
    sys.exit(main())
