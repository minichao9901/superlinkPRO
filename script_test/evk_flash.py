"""Flash the current EVKLite APP build over the DFU MSC virtual disk.

Usage: python evk_flash.py [path-to-pack.bin]
Entering DFU kicks the device off the bus, so this only sends the HID DFU
command; the caller (or the wrapper script) waits for the disk and copies.
"""
import os
import sys
import time
import shutil
import glob
import threading

import hid

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_BIN = os.path.join(REPO, "firmware", "application_5301", "build_dfu_evklite",
                           "output", "akaLinkPro_App_pack.bin")


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT")
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


def enter_dfu():
    infos = hid.enumerate(0x0D28, 0x0204)
    cand = [i for i in infos if i.get("usage_page") == 0xFF00]
    if not cand:
        return False
    h = hid.device()
    h.open_path(cand[0]["path"])
    h.set_nonblocking(1)
    h.write([0x01, 0x01, 0xFF] + [0] * 61)
    time.sleep(0.3)
    return True


def find_dfu_disk(timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
            root = letter + ":\\"
            if os.path.exists(os.path.join(root, "INFO.TXT")) or \
               glob.glob(os.path.join(root, "AKALINK*")):
                return root
        time.sleep(0.5)
    return None


def wait_app(timeout=45):
    t0 = time.time()
    while time.time() - t0 < timeout:
        infos = hid.enumerate(0x0D28, 0x0204)
        if any(i.get("usage_page") == 0xFF00 for i in infos):
            return True
        time.sleep(0.5)
    return False


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BIN
    if not os.path.exists(src):
        print("missing image:", src)
        return 1
    print("image:", src, os.path.getsize(src), "bytes")
    if not enter_dfu():
        print("device not in APP mode (no custom HID) - already in DFU?")
    disk = find_dfu_disk()
    if not disk:
        print("DFU MSC disk not found")
        return 1
    print("DFU disk:", disk)
    dst = os.path.join(disk, "akaLinkPro_App_pack.bin")
    shutil.copyfile(src, dst)
    print("copied, waiting for the APP to come back...")
    if wait_app():
        print("device back in APP mode")
        return 0
    print("device did not come back as APP within the timeout")
    return 1


if __name__ == "__main__":
    watchdog(90)
    sys.exit(main())
