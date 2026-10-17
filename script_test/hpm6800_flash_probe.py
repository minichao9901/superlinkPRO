"""Reboot the akaLinkPro probe into DFU and drop the packed APP image on the
virtual U-disk, then wait for the probe to come back.

This is the probe's own update path (no J-Link, no extra wiring): the APP's DFU
runtime interface switches the MCU back into the bootloader, which exposes a
MSC volume; writing the packed image onto it makes the bootloader validate the
header (signature + length + CRC32) and boot the new APP.

Usage:
  python hpm6800_flash_probe.py                 # build output, pack image
  python hpm6800_flash_probe.py <image.bin>     # explicit image
  python hpm6800_flash_probe.py --no-build      # skip the cmake build
"""
import os
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
APP_DIR = os.path.join(REPO, "firmware", "application_5301")
DEFAULT_IMAGE = os.path.join(APP_DIR, "build_dfu_evklite", "output", "akaLinkPro_App_pack.bin")

CMD_ENTER_DFU = 0xFF
VID, PID = 0x0D28, 0x0204


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT (%ss)" % sec)
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


def drives():
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-Volume | Where-Object { $_.DriveType -eq 'Removable' } | "
                          "Select-Object -ExpandProperty DriveLetter"],
                         capture_output=True, text=True, timeout=30)
    return [c for c in out.stdout.split() if c.strip()]


def build():
    print("building APP ...")
    r = subprocess.run(["cmd", "/c", "build_dfu_evklite.bat"], cwd=APP_DIR,
                       capture_output=True, text=True, timeout=900)
    tail = (r.stdout or "")[-600:]
    print(tail)
    if r.returncode != 0:
        raise RuntimeError("build failed")


def enter_dfu():
    import hid
    infos = hid.enumerate(VID, PID)
    cand = [i for i in infos if i.get("usage_page") == 0xFF00]
    if not cand:
        raise RuntimeError("probe HID interface not found")
    d = hid.device()
    d.open_path(cand[0]["path"])
    d.set_nonblocking(1)
    time.sleep(0.2)
    req = [0x01, 0x01, CMD_ENTER_DFU] + [0] * 61
    try:
        d.write(req)
    except Exception as e:
        print("  (write raised %r - the device rebooted, that is expected)" % e)
    print("  DFU reboot requested")


def wait_drive(timeout=25.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        for letter in drives():
            root = letter + ":\\"
            try:
                if os.path.exists(os.path.join(root, "INFO_UF2.TXT")) or True:
                    return root
            except OSError:
                pass
        time.sleep(0.5)
    return None


def main():
    image = DEFAULT_IMAGE
    if "--no-build" not in sys.argv:
        build()
    for a in sys.argv[1:]:
        if not a.startswith("--"):
            image = a
    if not os.path.exists(image):
        raise RuntimeError("image not found: %s" % image)
    size = os.path.getsize(image)
    print("image: %s (%d bytes)" % (image, size))

    before = set(drives())
    print("removable drives before: %s" % sorted(before))

    # Already sitting in the bootloader? Then there is nothing to trigger.
    if before and "--no-dfu" in sys.argv:
        root = sorted(before)[0] + ":\\"
        print("using the existing DFU volume %s" % root)
    else:
        enter_dfu()
        print("waiting for the DFU volume ...")
        t0 = time.time()
        root = None
        while time.time() - t0 < 30 and root is None:
            now = set(drives()) - before
            if now:
                root = sorted(now)[0] + ":\\"
                break
            time.sleep(0.5)
        if root is None:
            raise RuntimeError("no new removable drive appeared")
    print("DFU volume: %s (contents: %s)" % (root, os.listdir(root)))

    dest = os.path.join(root, os.path.basename(image))
    # The bootloader commits on a *directory change* it sees through the MSC
    # write path: a plain python copy is silently ignored, while removing and
    # re-copying the file through CopyFileEx always triggers it. Reproduce the
    # known-good sequence instead of guessing at the bootloader's trigger.
    print("writing %s ..." % dest)
    committed = False
    for attempt in range(1, 6):
        if not os.path.exists(root):
            committed = True
            break
        ps = ("$ErrorActionPreference='SilentlyContinue';"
              "Remove-Item -LiteralPath '%s' -Force;"
              "Start-Sleep -Milliseconds 800;"
              "Copy-Item -LiteralPath '%s' -Destination '%s' -Force;"
              "if (Test-Path -LiteralPath '%s') { (Get-Item -LiteralPath '%s').Length } else { 0 }"
              % (dest, image, root, dest, dest))
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=120)
        print("  attempt %d: wrote %s bytes" % (attempt, (r.stdout or "").strip()))
        time.sleep(2.0)
        if not os.path.exists(root):
            committed = True
            break
    if not committed and os.path.exists(root):
        print("  !! volume still present after 5 writes")

    print("waiting for the probe to come back ...")
    t0 = time.time()
    ok = False
    while time.time() - t0 < 40:
        try:
            import hid
            if [i for i in hid.enumerate(VID, PID) if i.get("usage_page") == 0xFF00]:
                ok = True
                break
        except Exception:
            pass
        time.sleep(0.5)
    if not ok:
        raise RuntimeError("probe did not re-enumerate")
    time.sleep(1.5)
    print("probe is back")
    return 0


if __name__ == "__main__":
    watchdog(600)
    sys.exit(main())
