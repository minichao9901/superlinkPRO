"""Send the akaLinkPro HID CMD_ENTER_DFU(0xFF) to reboot into the DFU bootloader.

Usage: python evk_dfu_enter.py
"""
import os
import sys
import time
import threading

import hid


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT")
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


def main():
    infos = hid.enumerate(0x0D28, 0x0204)
    cand = [i for i in infos if i.get("usage_page") == 0xFF00]
    if not cand:
        print("no custom HID interface found (device in APP mode?)")
        for i in infos:
            print("  usage_page=0x%04X intf=%s" % (i.get("usage_page", 0), i.get("interface_number")))
        return 1
    h = hid.device()
    h.open_path(cand[0]["path"])
    h.set_nonblocking(1)
    h.write([0x01, 0x01, 0xFF] + [0] * 61)
    time.sleep(0.3)
    print("CMD_ENTER_DFU sent")
    return 0


if __name__ == "__main__":
    watchdog(10)
    sys.exit(main())
