"""akaLinkPro probe helpers for the HPM6800EVK (RISC-V / JTAG) bring-up.

Small CLI around the custom-HID protocol (see app_5301/Custom HID Protocol.md)
so the JTAG tests do not have to re-implement the 64-byte report handshake.

Usage:
  python hpm6800_probe.py info                 version / compile date / config
  python hpm6800_probe.py set-mode 1           output_mode: 0=SWD+VCOM, 1=SWD+JTAG
  python hpm6800_probe.py save                 persist the config in flash
  python hpm6800_probe.py reset                reboot the probe (CDC/HID drop out)
  python hpm6800_probe.py connect 2            raw DAP_Connect, port 2 = JTAG
  python hpm6800_probe.py jtag-idcode          raw DAP_JTAG_IdCode (expect the TAP id)

Every blocking point has a short timeout and the process carries a watchdog.
"""
import os
import sys
import threading
import time

import hid

VID, PID = 0x0D28, 0x0204
USAGE_PAGE = 0xFF00

CMD_GET_CONFIG, CMD_SET_CONFIG, CMD_GET_VOLTAGE = 0x01, 0x02, 0x03
CMD_SAVE, CMD_RESET_DEVICE = 0x04, 0xFE

ID_DAP_INFO, ID_DAP_CONNECT, ID_DAP_DISCONNECT = 0x00, 0x02, 0x03
ID_DAP_SWJ_PINS, ID_DAP_SWJ_CLOCK, ID_DAP_SWJ_SEQUENCE = 0x10, 0x11, 0x12
ID_DAP_JTAG_SEQUENCE, ID_DAP_JTAG_CONFIGURE, ID_DAP_JTAG_IDCODE = 0x14, 0x15, 0x16

MODE_NAMES = {0: "SWD+VCOM", 1: "SWD+JTAG"}


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT (%ss)" % sec)
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


def open_hid(timeout=8.0):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        try:
            infos = hid.enumerate(VID, PID)
            cand = [i for i in infos if i.get("usage_page") == USAGE_PAGE]
            if cand:
                d = hid.device()
                d.open_path(cand[0]["path"])
                d.set_nonblocking(1)
                time.sleep(0.15)
                return d
        except Exception as e:      # device busy while it re-enumerates
            last = e
        time.sleep(0.3)
    raise RuntimeError("custom HID interface not found (%r)" % last)


def xfer(dev, cmd, args=(), tmo=1.0):
    """Send one custom-HID request, return the matching response list or None."""
    req = [0x01, 1 + len(args), cmd] + list(args)
    if len(req) > 64:
        raise ValueError("HID report too long")
    req += [0] * (64 - len(req))
    dev.write(req)
    t0 = time.time()
    while time.time() - t0 < tmo:
        d = dev.read(64, timeout_ms=150)
        if d and d[0] == 0x02 and d[2] == cmd:
            return d
    return None


def ascii_field(resp, off=3, n=20):
    raw = bytes(resp[off:off + n])
    return raw.split(b"\x00")[0].decode("ascii", "replace")


def show_info(dev):
    print("model     : %s" % ascii_field(xfer(dev, 0x10) or [0] * 64, 3, 24))
    print("serial    : %s" % ascii_field(xfer(dev, 0x11) or [0] * 64, 3, 16))
    print("hw  ver   : %s" % ascii_field(xfer(dev, 0x12) or [0] * 64, 3, 8))
    print("fw  ver   : %s" % ascii_field(xfer(dev, 0x13) or [0] * 64, 3, 8))
    print("bl  ver   : %s" % ascii_field(xfer(dev, 0x14) or [0] * 64, 3, 8))
    print("prod date : %s" % ascii_field(xfer(dev, 0x15) or [0] * 64, 3, 20))
    print("fw  built : %s" % ascii_field(xfer(dev, 0x16) or [0] * 64, 3, 20))
    print("bl  built : %s" % ascii_field(xfer(dev, 0x17) or [0] * 64, 3, 20))
    show_config(dev)


def show_config(dev):
    d = xfer(dev, CMD_GET_CONFIG)
    if not d:
        print("config    : no reply")
        return None
    v = xfer(dev, CMD_GET_VOLTAGE)
    mv = (v[3] | (v[4] << 8)) if v else -1
    print("output_md : %d (%s)" % (d[3], MODE_NAMES.get(d[3], "?")))
    print("5v out    : %d" % d[4])
    print("clk accel : %d" % d[5])
    print("led1/led2 : %d / %d" % (d[6], d[7]))
    print("vref set  : %d mV" % (d[8] | (d[9] << 8)))
    print("vref meas : %d mV" % mv)
    return d


def set_mode(dev, mode):
    d = xfer(dev, CMD_SET_CONFIG, [mode, 1, 0, 1, 0, 0xC4, 0x0C])
    print("set output_mode=%d -> %s" % (mode, "acked" if d else "NO REPLY"))
    return bool(d)


def save(dev):
    d = xfer(dev, CMD_SAVE)
    print("save config -> %s" % ("acked" if d else "NO REPLY"))
    return bool(d)


def reset(dev):
    req = [0x01, 0x01, CMD_RESET_DEVICE] + [0] * 61
    try:
        dev.write(req)
    except Exception:
        pass
    print("reset sent; waiting for re-enumeration ...")
    time.sleep(3.0)
    return open_hid(timeout=15.0)


def dap_raw(dev, req, label=""):
    """Send a raw CMSIS-DAP request over the bulk endpoints (interface 0)."""
    import usb.core
    import usb.util
    d = usb.core.find(idVendor=VID, idProduct=PID)
    if d is None:
        print("no USB device")
        return b""
    cfg = d.get_active_configuration()
    intf = usb.util.find_descriptor(cfg, bInterfaceNumber=0)
    ep_out = usb.util.find_descriptor(
        intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
    ep_in = usb.util.find_descriptor(
        intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
    ep_out.write(bytes(req), 1500)
    try:
        r = bytes(ep_in.read(512, 1500))
    except Exception as e:
        print("  %-22s no IN response: %r" % (label, e))
        return b""
    print("  %-22s req=%-30s -> %s" % (label,
                                       " ".join("%02X" % b for b in req[:10]),
                                       " ".join("%02X" % b for b in r[:10])))
    return r


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    dev = open_hid()

    if cmd == "info":
        show_info(dev)
    elif cmd == "set-mode":
        set_mode(dev, int(argv[1], 0))
        show_config(dev)
    elif cmd == "save":
        save(dev)
    elif cmd == "reset":
        reset(dev)
    elif cmd == "connect":
        port = int(argv[1], 0) if len(argv) > 1 else 2
        r = dap_raw(dev, [ID_DAP_CONNECT, port], "DAP_Connect")
        if r:
            got = r[1]
            print("  port granted = %d (%s)" % (got, {0: "DISABLED", 1: "SWD", 2: "JTAG"}.get(got, "?")))
    elif cmd == "jtag-idcode":
        r = dap_raw(dev, [ID_DAP_JTAG_IDCODE], "DAP_JTAG_IdCode")
        if r:
            print("  status=0x%02X idcode=0x%08X" % (r[1], int.from_bytes(r[2:6], "little")))
    elif cmd == "swj-clock":
        hz = int(argv[1], 0)
        r = dap_raw(dev, [ID_DAP_SWJ_CLOCK, hz & 0xFF, (hz >> 8) & 0xFF,
                          (hz >> 16) & 0xFF, (hz >> 24) & 0xFF], "DAP_SWJ_Clock")
        if r:
            print("  status=0x%02X (requested %d Hz)" % (r[1], hz))
    elif cmd == "swj-pins":
        # value/select masks, DAP_SWJ_* bit order: 0=TCK,1=TMS,2=TDO,3=TDI,4=nTRST,5=nRESET
        val, sel = int(argv[1], 0), int(argv[2], 0)
        r = dap_raw(dev, [ID_DAP_SWJ_PINS, val, sel, 0, 0, 0, 0], "DAP_SWJ_Pins")
        if r:
            print("  pin state=0x%02X" % r[1])
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    watchdog(40)
    sys.exit(main(sys.argv[1:]))
