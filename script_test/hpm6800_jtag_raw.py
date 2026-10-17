"""Raw CMSIS-DAP JTAG throughput of the akaLinkPro probe (HPM6800EVK bring-up).

Drives the DAPv2 bulk interface directly with pyusb and times DAP_JTAG_Sequence
batches, so we can tell whether the ~100 KB/s OpenOCD SRAM number comes from the
probe's TCK engine, the per-request USB overhead, or OpenOCD's RISC-V protocol
overhead.

akaLinkPro response framing: byte 0 = command echo, byte 1.. = payload.

The probe must be in SWD+JTAG output mode:  python hpm6800_probe.py set-mode 1

Usage: python hpm6800_jtag_raw.py [--repeat 200] [--max-req 512]
"""
import os
import sys
import threading
import time

import usb.core
import usb.util

VID, PID = 0x0D28, 0x0204
ID_DAP_CONNECT = 0x02
ID_DAP_DISCONNECT = 0x03
ID_DAP_JTAG_SEQUENCE = 0x14
ID_DAP_JTAG_CONFIGURE = 0x15
ID_DAP_JTAG_IDCODE = 0x16

JTAG_SEQUENCE_TMS = 0x40
JTAG_SEQUENCE_TDO = 0x80


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT (%ss)" % sec)
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


class Dap:
    def __init__(self):
        d = usb.core.find(idVendor=VID, idProduct=PID)
        if d is None:
            raise RuntimeError("probe not found")
        try:
            d.set_configuration()
        except usb.core.USBError:
            pass
        cfg = d.get_active_configuration()
        intf = usb.util.find_descriptor(cfg, bInterfaceNumber=0)
        self.out = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        self.inn = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
        self.mps_out = self.out.wMaxPacketSize
        self.mps_in = self.inn.wMaxPacketSize

    def drain(self):
        n = 0
        while n < 64:
            try:
                r = bytes(self.inn.read(512, 50))
            except usb.core.USBTimeoutError:
                break
            if not r:
                break
            print("  [drain] %d stale byte(s): %s" % (len(r), r[:12].hex()))
            n += 1

    def xfer(self, req, timeout=3000):
        self.out.write(bytes(req), timeout)
        try:
            r = bytes(self.inn.read(4096, timeout))
        except usb.core.USBTimeoutError:
            r = b""
        if not r or r[0] != req[0]:
            raise RuntimeError("DAP desync: cmd=0x%02X resp=%s" % (req[0], r[:16].hex()))
        return r

    def connect(self, port=2):
        return self.xfer([ID_DAP_CONNECT, port])[1]

    def jtag_configure(self, ir_lengths):
        return self.xfer([ID_DAP_JTAG_CONFIGURE, len(ir_lengths)] + list(ir_lengths))


def build_batch(n_seq, bits, capture=False, tms=0):
    nbytes = (bits + 7) // 8
    info = (JTAG_SEQUENCE_TMS if tms else 0) | (JTAG_SEQUENCE_TDO if capture else 0)
    req = [ID_DAP_JTAG_SEQUENCE, n_seq]
    for _ in range(n_seq):
        req += [info, bits] + [0xFF] * nbytes
    return req


def bench(d, req, repeat, expect_resp_min=1):
    r = d.xfer(req)
    if len(r) < expect_resp_min:
        raise RuntimeError("short response %d" % len(r))
    t0 = time.perf_counter()
    for _ in range(repeat):
        d.xfer(req)
    t1 = time.perf_counter()
    return (t1 - t0) / repeat, len(r)


def main():
    repeat = int(sys.argv[sys.argv.index("--repeat") + 1]) if "--repeat" in sys.argv else 200
    max_req = int(sys.argv[sys.argv.index("--max-req") + 1]) if "--max-req" in sys.argv else 512
    d = Dap()
    d.drain()
    print("out mps=%d in mps=%d, request cap=%d B" % (d.mps_out, d.mps_in, max_req))
    port = d.connect(2)
    print("DAP_Connect(2) -> port %d %s" % (port, "(JTAG)" if port == 2 else "(!! refused)"))
    if port != 2:
        print("  -> probe is not in SWD+JTAG output mode; run: python hpm6800_probe.py set-mode 1")
        return 1
    d.jtag_configure([5])
    print()

    print("A. burst length sweep, 64-bit scans (TMS=0, no TDO capture)")
    print("   %8s %10s %10s %12s" % ("seq/req", "us/req", "bits/req", "Mbit/s (TCK)"))
    for n_seq in (1, 2, 4, 8, 16, 32, 56):
        req = build_batch(n_seq, 64)
        if len(req) > max_req:
            continue
        dt, _ = bench(d, req, repeat)
        bits = n_seq * 64
        print("   %8d %10.1f %10d %12.3f" % (n_seq, dt * 1e6, bits, bits / dt / 1e6))
    print()

    print("B. burst length sweep, 64-bit scans with TDO capture")
    print("   %8s %10s %10s %12s" % ("seq/req", "us/req", "bits/req", "Mbit/s (TCK)"))
    for n_seq in (1, 2, 4, 8, 16, 32, 56):
        req = build_batch(n_seq, 64, capture=True)
        if len(req) > max_req:
            continue
        dt, _ = bench(d, req, repeat)
        bits = n_seq * 64
        print("   %8d %10.1f %10d %12.3f" % (n_seq, dt * 1e6, bits, bits / dt / 1e6))
    print()

    print("C. short scans (what a DMI access really looks like)")
    for bits, cap in ((1, False), (1, True), (5, False), (5, True), (41, True)):
        nbytes = (bits + 7) // 8
        n_seq = (max_req - 1) // (2 + nbytes)
        req = build_batch(n_seq, bits, capture=cap)
        dt, _ = bench(d, req, repeat)
        total = n_seq * bits
        print("   %3d x %2d bit%-6s : %9.1f us/req  %8.3f Mbit/s  %8.2f Mseq/s"
              % (n_seq, bits, " (TDO)" if cap else "", dt * 1e6,
                 total / dt / 1e6, n_seq / dt / 1e6))
    print()

    print("D. RISC-V DMI access pattern: IR(5b, no cap) + DR(41b, cap) pairs")
    per_pair = 3 + 8
    n_pair = (max_req - 1) // per_pair
    req = [ID_DAP_JTAG_SEQUENCE, 2 * n_pair]
    for _ in range(n_pair):
        req += [0x00, 5] + [0x11]
        req += [JTAG_SEQUENCE_TDO, 41] + [0] * 6
    dt, _ = bench(d, req, repeat)
    print("   %3d DMI/req: %9.1f us/req -> %7.3f M DMI/s -> %6.3f MB/s of 32-bit payload"
          % (n_pair, dt * 1e6, n_pair / dt / 1e6, n_pair / dt / 1e6 * 4))

    d.xfer([ID_DAP_DISCONNECT])
    return 0


if __name__ == "__main__":
    watchdog(120)
    sys.exit(main())
