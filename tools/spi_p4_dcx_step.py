"""profile 1 (spi_dcx) single STEP, for LA: CS must stay low while DC flips mid-window.

Expected on the wire: CS down -> DC low -> 0xCE -> DC high -> 5A A5 -> CS up.
"""
import struct
import sys

sys.path.insert(0, r"E:\Share\github\akaLinkPro\script_test")
import spi_bridge_test as T  # noqa: E402

h = T.Hid()
b = T.Bulk()
prof = bytearray(h.profile_get())
prof[0] = T.PROFILES["spi_dcx"]
prof[2] = 1  # dc_active_high
h.profile_set(bytes(prof))
cfg = bytearray(h.cfg_get())
cfg[8] = T.PADS["PA02"]   # pad_dc
cfg[9] = T.PADS["PA31"]   # pad_rst
cfg[11] = T.PADS["PA10"]  # pad_bl
h.cfg_set(bytes(cfg))
h.enable(0)
h.enable(1)
b.drain()

payload = struct.pack("<BBH", 0xCE, 2, 0) + b"\x5a\xa5"
seq = b.next_seq()
b.send(T.frame(T.T_STEP, payload, T.F_RSP, seq))
r = T.parse_rsp(b.recv(2000))
print("profile 1 STEP cmd=0xCE params=[5A A5] -> status=%s" % (r and r["status"]))
print("expect: CS stays LOW across the whole thing; DC=0 for 0xCE, DC=1 for 5A A5")

b.drain()
h.close()
