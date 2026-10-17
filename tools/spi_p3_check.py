"""P3 verification: manual CS (cs_policy=2) and CS_HOLD spanning several XFERs.

Uses the STATUS bit2 (CS currently asserted) reported by the probe, so it needs
no LA wiring; a separate LA capture confirms the physical waveform.
"""
import struct
import sys
import time

sys.path.insert(0, r"E:\Share\github\akaLinkPro\script_test")
import spi_bridge_test as T  # noqa: E402

CS_BIT = 1 << 2

h = T.Hid()
b = T.Bulk()


def set_policy(p):
    cfg = bytearray(h.cfg_get())
    cfg[6] = p
    h.cfg_set(bytes(cfg))
    h.enable(0)
    h.enable(1)


def cs_now():
    return 1 if (h.status()["status"] & CS_BIT) else 0


def xfer(tx, flags=0, rx=None, tmo=2000):
    b.drain()
    seq = b.next_seq()
    if rx is None:
        rx = len(tx)
    b.send(T.frame(T.T_XFER, T.xfer_payload(T.TC_LINES_1, cmd=0, tx=tx, rx_len=rx),
                   T.F_RSP | flags, seq))
    return T.parse_rsp(b.recv(tmo))


def cs_frame(level):
    seq = b.next_seq()
    b.send(T.frame(T.T_CS, bytes([level]), T.F_RSP, seq))
    return T.parse_rsp(b.recv(1000))


print("=== A. auto CS (policy 0): CS_HOLD must span frames ===")
set_policy(0)
print("  idle CS asserted? %d (expect 0)" % cs_now())
r = xfer(b"\x01\x02\x03\x04", flags=T.F_CS_HOLD)
print("  xfer#1 with CS_HOLD: status=%s  CS now=%d (expect 1)" % (r and r["status"], cs_now()))
r = xfer(b"\x05\x06\x07\x08", flags=T.F_CS_HOLD)
print("  xfer#2 with CS_HOLD: status=%s  CS now=%d (expect 1)" % (r and r["status"], cs_now()))
r = xfer(b"\x09\x0a\x0b\x0c")
print("  xfer#3 no hold     : status=%s  CS now=%d (expect 0)" % (r and r["status"], cs_now()))

print()
print("=== B. manual CS (policy 2): only CS frames touch it ===")
set_policy(2)
print("  idle CS asserted? %d (expect 0)" % cs_now())
r = xfer(b"\x11\x12\x13\x14")
print("  xfer with manual CS: status=%s  CS now=%d (expect 0 - nothing asserted it)"
      % (r and r["status"], cs_now()))
r = cs_frame(1)
print("  CS(assert)         : status=%s  CS now=%d (expect 1)" % (r and r["status"], cs_now()))
r = xfer(b"\x21\x22\x23\x24")
print("  xfer inside window : status=%s  CS now=%d (expect 1)" % (r and r["status"], cs_now()))
r = xfer(b"\x31\x32\x33\x34")
print("  xfer inside window : status=%s  CS now=%d (expect 1)" % (r and r["status"], cs_now()))
r = cs_frame(0)
print("  CS(release)        : status=%s  CS now=%d (expect 0)" % (r and r["status"], cs_now()))

print()
print("=== C. aux GPIO lines (DC/RST/BL) + RESET frame ===")
for line, name in ((0, "DC"), (3, "BL")):
    for lvl in (1, 0):
        seq = b.next_seq()
        b.send(T.frame(T.T_GPIO, bytes([line, lvl]), T.F_RSP, seq))
        r = T.parse_rsp(b.recv(1000))
        print("  GPIO %s = %d -> %s" % (name, lvl, r and r["status"]))
# RESET frame: 2 ms low + 5 ms post
seq = b.next_seq()
b.send(T.frame(T.T_RESET, struct.pack("<HH", 2, 5), T.F_RSP, seq))
t0 = time.time()
r = T.parse_rsp(b.recv(2000))
dt = (time.time() - t0) * 1000
print("  RESET(low=2ms,post=5ms) -> %s  (non-blocking, ACK came back in %.1f ms)"
      % (r and r["status"], dt))

print()
print("=== D. AUX_IN (TE input) ===")
seq = b.next_seq()
b.send(T.frame(T.T_AUX_IN, b"", T.F_RSP, seq))
r = T.parse_rsp(b.recv(1000))
print("  AUX_IN -> status=%s data=%s" % (r and r["status"], (r and r["data"].hex()) or "-"))

print()
print("=== E. non-blocking delay: DAP/HID must stay alive during 200 ms ===")
seq = b.next_seq()
b.send(T.frame(T.T_DELAY, struct.pack("<I", 200000)))
t0 = time.time()
alive = 0
while (time.time() - t0) < 0.02:  # during the first 20 ms of the delay
    if h.status() is not None:
        alive += 1
print("  HID STATUS round-trips during the 200 ms DELAY: %d (expect >0)" % alive)

set_policy(0)
b.drain()
h.close()
