"""P4 partial verification (no panel needed): what STEP frames actually put on the wire.

profile 1 (spi_dcx): one CS window, 8-bit cmd then params (DC toggles in between).
profile 2 (qspi)   : opcode 0x02 + 24-bit address (= panel cmd << 16) + params, 1 line.

Decode the MOSI bitstream with the LA to confirm the byte sequence; a panel would
additionally confirm DC/CS timing, which needs its own clips.
"""
import struct
import sys

sys.path.insert(0, r"E:\Share\github\akaLinkPro\script_test")
import spi_bridge_test as T  # noqa: E402

h = T.Hid()
b = T.Bulk()


def set_profile(name, **kw):
    prof = bytearray(h.profile_get())
    prof[0] = T.PROFILES[name]
    for k, v in kw.items():
        if k == "def_lines":
            prof[1] = v
    h.profile_set(bytes(prof))
    h.enable(0)
    h.enable(1)


def step(cmd, params=b"", delay_ms=0, flags=T.F_RSP):
    payload = struct.pack("<BBH", cmd, len(params), delay_ms) + params
    b.drain()
    seq = b.next_seq()
    b.send(T.frame(T.T_STEP, payload, flags, seq))
    return T.parse_rsp(b.recv(2000))


print("=== profile 1 (spi_dcx): AXS15352 style step ===")
set_profile("spi_dcx")
for cmd, params in ((0xCE, b"\x5A\xA5"), (0x11, b""), (0x36, b"\x00")):
    r = step(cmd, params)
    print("  STEP cmd=0x%02X nparams=%d -> status=%s" % (cmd, len(params), r and r["status"]))
print("  (wire: CS low, cmd byte, DC flips to data, params, CS high)")

print()
print("=== profile 2 (qspi): ST77916 style step ===")
set_profile("qspi")
prof = h.profile_get()
print("  profile block: profile=%d def_lines=%d qspi_wr=0x%02X qspi_color=0x%02X addr_bytes=%d"
      % (prof[0], prof[1], prof[4], prof[5], prof[6]))
for cmd, params in ((0xF0, b"\x28"), (0x11, b""), (0x3A, b"\x55")):
    r = step(cmd, params)
    print("  STEP cmd=0x%02X nparams=%d -> status=%s" % (cmd, len(params), r and r["status"]))
print("  (wire: 02 | 00 <cmd> 00 | params, all 1-line)")

print()
print("=== profile 0 (raw): plain cmd phase + params ===")
set_profile("raw")
r = step(0x9F, b"\x00\x00\x00")
print("  STEP cmd=0x9F nparams=3 -> status=%s" % (r and r["status"]))

set_profile("raw")
b.drain()
h.close()
