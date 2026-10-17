"""Sweep SPI mode (CPOL/CPHA) and a few knobs, live over HID - no reflash needed."""
import struct
import sys
import time

sys.path.insert(0, r"E:\Share\github\akaLinkPro\script_test")
import spi_bridge_test as T  # noqa: E402

h = T.Hid()
b = T.Bulk()


def one_xfer(tx, mode=0, tcfg=T.TC_LINES_1, rx_len=None, dummy=0, cmd=0, addr_len=0, addr=0,
             force=0, reset_first=True):
    cfg = bytearray(h.cfg_get())
    if cfg[4] != mode:
        cfg[4] = mode
        h.cfg_set(bytes(cfg))
    if reset_first:
        h.enable(0)
    h.enable(1)
    b.drain()
    seq = b.next_seq()
    if rx_len is None:
        rx_len = len(tx)
    payload = T.xfer_payload(tcfg, cmd=cmd, addr=addr, addr_len=addr_len, dummy=dummy,
                             tx=tx, rx_len=rx_len)
    b.send(T.frame(T.T_XFER, payload, T.F_RSP | force, seq))
    r = T.parse_rsp(b.recv(2000))
    return r


tx = bytes([0x08, 0x0F, 0x16, 0x1D])
print("=== mode sweep (write_read_together, 4 bytes) ===")
for mode in (0, 1, 2, 3):
    r = one_xfer(tx, mode=mode)
    print("  mode %d: status=%s got=%s want=%s" %
          (mode, r and r["status"], (r and r["data"].hex()) or "-", tx.hex()))

print()
print("=== read-only (MOSI idle) - shows what MISO idles at ===")
r = one_xfer(b"", mode=0, rx_len=4, dummy=1)
print("  read-only: status=%s got=%s" % (r and r["status"], (r and r["data"].hex()) or "-"))

print()
print("=== write-only (no rx) then read-only, mode 0 ===")
r = one_xfer(b"\xff\xff\xff\xff", mode=0, rx_len=0)
print("  write FF: status=%s" % (r and r["status"]))
r = one_xfer(b"", mode=0, rx_len=4, dummy=1)
print("  read after: got=%s (MOSI was left driving FF)" % ((r and r["data"].hex()) or "-"))

h.enable(0)
b.drain()
h.close()
