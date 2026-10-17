"""Simple CDC loopback check (no firmware diagnostics needed).

Usage: python evk_echo.py [COMxx] [baud] [label]
Exit code 0 = the payload came back byte-identical.
"""
import os
import sys
import time
import threading

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM52"
BAUD = int(sys.argv[2]) if len(sys.argv) > 2 else 115200
LABEL = sys.argv[3] if len(sys.argv) > 3 else "echo"
PAYLOAD = b"HELLO-0123456789-abcdefghij"


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT")
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


def main():
    ser = serial.Serial(PORT, BAUD, timeout=0.2, write_timeout=2)
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        time.sleep(0.2)
        ser.write(PAYLOAD)
        ser.flush()
        got = b""
        t0 = time.time()
        while time.time() - t0 < 1.5:
            c = ser.read(256)
            if c:
                got += c
    finally:
        ser.close()
    ok = got == PAYLOAD
    print("[%s] %s @ %d: sent %d got %d -> %s" %
          (LABEL, PORT, BAUD, len(PAYLOAD), len(got), "PASS" if ok else "FAIL %r" % got[:32]))
    return 0 if ok else 2


if __name__ == "__main__":
    watchdog(20)
    sys.exit(main())
