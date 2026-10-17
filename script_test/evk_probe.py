"""EVKLite CDC/UART quick probe: listen on the CDC port + read the HID diag word.

Usage: python evk_probe.py [COMxx] [baud]
Safe: only sends CMD_GET_VOLTAGE(0x03) over HID and some bytes over the CDC port.
"""
import os
import sys
import time
import threading
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM52"
BAUD = int(sys.argv[2]) if len(sys.argv) > 2 else 115200
LISTEN_S = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT - aborting")
        os._exit(9)
    t = threading.Thread(target=_f, daemon=True)
    t.start()


def hexs(b):
    return " ".join("%02X" % x for x in b)


def hid_diag():
    try:
        import hid
    except Exception as e:  # pragma: no cover
        print("HID: import failed:", e)
        return
    try:
        infos = hid.enumerate(0x0D28, 0x0204)
    except Exception as e:
        print("HID: enumerate failed:", e)
        return
    for i in infos:
        print("HID iface: usage_page=0x%04X usage=0x%04X intf=%s" % (
            i.get("usage_page", 0), i.get("usage", 0), i.get("interface_number")))
    cand = [i for i in infos if i.get("usage_page") == 0xFF00]
    if not cand:
        print("HID: no usage_page 0xFF00 interface (custom HID not enumerated)")
        return
    try:
        h = hid.device()
        h.open_path(cand[0]["path"])
        h.set_nonblocking(1)
        # [report_id=1?, cmd_len=1, CMD_GET_VOLTAGE=0x03, ...]
        h.write([0x01, 0x01, 0x03] + [0] * 61)
        t0 = time.time()
        resp = None
        while time.time() - t0 < 1.0:
            d = h.read(64, timeout_ms=150)
            if d:
                resp = d
                break
        if not resp:
            print("HID: no response to CMD_GET_VOLTAGE")
        else:
            print("HID resp:", hexs(resp[:12]))
            if len(resp) > 11 and resp[1] == 9 and resp[2] == 0x03:
                d0 = resp[3] | (resp[4] << 8) | (resp[5] << 16) | (resp[6] << 24)
                d1 = resp[7] | (resp[8] << 8) | (resp[9] << 16) | (resp[10] << 24)
                print("  DIAG firmware running")
                print("  diag0=0x%08X tx_func=%d rx_func=%d com=%d rx_dma=%d tx_dma=%d clk=%dMHz" % (
                    d0, d0 & 0xFF, (d0 >> 8) & 0xFF, (d0 >> 16) & 1,
                    (d0 >> 17) & 1, (d0 >> 18) & 1, (d0 >> 19) & 0x3F))
                print("  diag1=applied_baud=%d" % d1)
            else:
                print("  (old firmware: CMD_GET_VOLTAGE len=%d)" % resp[1])
        h.close()
    except Exception as e:
        print("HID: error:", repr(e))


def cdc_probe():
    try:
        ser = serial.Serial(PORT, BAUD, timeout=0.2, write_timeout=2)
    except Exception as e:
        print("CDC: open %s failed: %r" % (PORT, e))
        return
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        time.sleep(0.2)
        print("CDC: listening %.1fs (auto banner?) ..." % LISTEN_S)
        rx = b""
        t0 = time.time()
        while time.time() - t0 < LISTEN_S:
            c = ser.read(512)
            if c:
                rx += c
        print("CDC: spontaneous rx = %d bytes: %r" % (len(rx), rx[:80]))

        payload = b"HELLO-0123456789-abcdefghij"
        print("CDC: writing %d bytes" % len(payload))
        ser.write(payload)
        ser.flush()
        rx2 = b""
        t0 = time.time()
        while time.time() - t0 < 1.5:
            c = ser.read(512)
            if c:
                rx2 += c
        print("CDC: loopback rx = %d bytes: %r" % (len(rx2), rx2[:80]))
    finally:
        try:
            ser.close()
        except Exception:
            pass


if __name__ == "__main__":
    watchdog(20)
    print("=== port %s @ %d ===" % (PORT, BAUD))
    cdc_probe()
    print()
    hid_diag()
    print("done")
