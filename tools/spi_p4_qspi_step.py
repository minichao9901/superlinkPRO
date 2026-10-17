"""One profile-2 (QSPI) STEP frame, for LA decoding: expect 02 00 F0 00 28 on MOSI
(command byte is the 2nd address byte; corrected 2026-09-30, see spi_bridge.c sb_step_qspi)."""
import struct
import sys

sys.path.insert(0, r"E:\Share\github\akaLinkPro\script_test")
import spi_bridge_test as T  # noqa: E402

h = T.Hid()
b = T.Bulk()
prof = bytearray(h.profile_get())
prof[0] = T.PROFILES["qspi"]
h.profile_set(bytes(prof))
h.enable(0)
h.enable(1)
b.drain()

payload = struct.pack("<BBH", 0xF0, 1, 0) + b"\x28"
seq = b.next_seq()
b.send(T.frame(T.T_STEP, payload, T.F_RSP, seq))
r = T.parse_rsp(b.recv(2000))
print("QSPI STEP cmd=0xF0 params=[0x28] -> status=%s" % (r and r["status"]))
print("expect on MOSI: 02 00 F0 00 28")

b.drain()
h.close()
