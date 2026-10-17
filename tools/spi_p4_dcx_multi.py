"""Three consecutive profile-1 STEPs, to see whether the CS->DC gap is fixed overhead."""
import struct
import sys

sys.path.insert(0, r"E:\Share\github\akaLinkPro\script_test")
import spi_bridge_test as T  # noqa: E402

h = T.Hid()
b = T.Bulk()
prof = bytearray(h.profile_get())
prof[0] = T.PROFILES["spi_dcx"]
prof[2] = 1
h.profile_set(bytes(prof))
cfg = bytearray(h.cfg_get())
cfg[8] = T.PADS["PA02"]
cfg[9] = T.PADS["PA31"]
cfg[11] = T.PADS["PA10"]
h.cfg_set(bytes(cfg))
h.enable(0)
h.enable(1)
b.drain()

for i in range(4):
    payload = struct.pack("<BBH", 0xCE, 2, 0) + b"\x5a\xa5"
    seq = b.next_seq()
    b.send(T.frame(T.T_STEP, payload, T.F_RSP, seq))
    r = T.parse_rsp(b.recv(2000))
    print("STEP #%d -> status=%s last_ticks=%d (%.2f us)"
          % (i, r and r["status"], h.status()["last_ticks"], h.status()["last_ticks"] / 24.0))

b.drain()
h.close()
