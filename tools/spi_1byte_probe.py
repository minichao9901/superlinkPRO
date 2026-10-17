"""One write-only XFER with tx_len=1 (and one with 8), to measure the CS window width."""
import sys

sys.path.insert(0, r"E:\Share\github\akaLinkPro\script_test")
import spi_bridge_test as T  # noqa: E402

h = T.Hid()
b = T.Bulk()
prof = bytearray(h.profile_get())
prof[0] = T.PROFILES["raw"]
h.profile_set(bytes(prof))
h.enable(0)
h.enable(1)
b.drain()

for ln in (1, 8):
    tx = bytes(range(ln))
    seq = b.next_seq()
    b.send(T.frame(T.T_XFER, T.xfer_payload(T.TC_LINES_1, cmd=0, tx=tx, rx_len=0),
                   T.F_RSP, seq))
    r = T.parse_rsp(b.recv(2000))
    st = h.status()
    print("XFER write-only len=%d -> status=%s last_ticks=%d (%.2f us)"
          % (ln, r and r["status"], st["last_ticks"], st["last_ticks"] / 24.0))

b.drain()
h.close()
