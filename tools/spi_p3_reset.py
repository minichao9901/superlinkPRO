"""P3 physical check: RESET pulse (low 2 ms + post 5 ms) and BL on/off, for the LA."""
import struct
import sys
import time

sys.path.insert(0, r"E:\Share\github\akaLinkPro\script_test")
import spi_bridge_test as T  # noqa: E402

h = T.Hid()
b = T.Bulk()
cfg = bytearray(h.cfg_get())
cfg[8] = T.PADS["PA02"]   # DC
cfg[9] = T.PADS["PA31"]   # RST
cfg[11] = T.PADS["PA10"]  # BL
cfg[13] = T.PADS["none"]  # TE
cfg[12] = 0x06            # RST/CS active low
h.cfg_set(bytes(cfg))
h.enable(0)
h.enable(1)
b.drain()


def gpio(line, level, name):
    seq = b.next_seq()
    b.send(T.frame(T.T_GPIO, bytes([line, level]), T.F_RSP, seq))
    r = T.parse_rsp(b.recv(1000))
    print("  GPIO %-3s = %d -> status=%s" % (name, level, r and r["status"]))
    return r


print("=== BL on/off (watch CH2/PB13) ===")
gpio(3, 1, "BL")
time.sleep(0.3)
gpio(3, 0, "BL")

print()
print("=== RESET frame: low 2 ms + post 5 ms (watch CH1/PB12) ===")
t0 = time.time()
seq = b.next_seq()
b.send(T.frame(T.T_RESET, struct.pack("<HH", 2, 5), T.F_RSP, seq))
r = T.parse_rsp(b.recv(2000))
ack_ms = (time.time() - t0) * 1000
print("  RESET -> status=%s, ACK returned in %.1f ms (non-blocking; the 2+5 ms run on the probe)"
      % (r and r["status"], ack_ms))

b.drain()
h.close()
