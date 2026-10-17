"""Read the running APP's identity and params over HID.

Usage: python evk_ident.py
"""
import os
import sys
import time
import threading

import hid

CMD_GET_CONFIG = 0x01
CMD_GET_VOLTAGE = 0x03
CMD_GET_MODEL = 0x10
CMD_GET_FWVER = 0x13
CMD_GET_BLVER = 0x14
CMD_GET_FW_COMPILE_DATE = 0x16


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG")
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


def main():
    infos = hid.enumerate(0x0D28, 0x0204)
    cand = [i for i in infos if i.get("usage_page") == 0xFF00]
    if not cand:
        print("no custom HID - device in APP mode?")
        return 1
    d = hid.device()
    d.open_path(cand[0]["path"])
    d.set_nonblocking(1)

    def ask(cmd, args=()):
        req = [0x01, 0x01, cmd] + list(args)
        req += [0] * (64 - len(req))
        d.write(req)
        t0 = time.time()
        while time.time() - t0 < 1.0:
            r = d.read(64, timeout_ms=150)
            if r and r[0] == 0x02 and r[2] == cmd:
                n = r[1]
                body = bytes(r[3:3 + max(0, n - 1)])
                return body
        return None

    cfg = ask(CMD_GET_CONFIG)
    if cfg and len(cfg) >= 7:
        print("CONFIG   : output_mode=%d usb5v=%d clock_accel=%d led1=%d led2=%d vref=%d mV" %
              (cfg[0], cfg[1], cfg[2], cfg[3], cfg[4], cfg[5] | (cfg[6] << 8)))
    print("FW       : %s" % (ask(CMD_GET_FWVER) or b"").decode(errors="replace").rstrip("\x00"))
    print("BL       : %s" % (ask(CMD_GET_BLVER) or b"").decode(errors="replace").rstrip("\x00"))
    print("FW built : %s" % (ask(CMD_GET_FW_COMPILE_DATE) or b"").decode(errors="replace").rstrip("\x00"))
    print("MODEL    : %s" % (ask(CMD_GET_MODEL) or b"").decode(errors="replace").rstrip("\x00"))
    v = ask(CMD_GET_VOLTAGE)
    if v:
        print("VOLTAGE  : %d mV (len=%d -> %s diag firmware)" %
              (v[0] | (v[1] << 8) if len(v) >= 2 else -1, len(v),
               "NO" if len(v) == 2 else "YES/other"))
    d.close()
    return 0


if __name__ == "__main__":
    watchdog(20)
    sys.exit(main())
