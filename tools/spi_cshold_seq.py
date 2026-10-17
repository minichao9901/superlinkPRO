"""Drive the CS_HOLD sequence (3 frames in one CS window, then a normal frame)."""
import sys

sys.path.insert(0, r"E:\Share\github\akaLinkPro\script_test")
import spi_bridge_test as T  # noqa: E402

h = T.Hid()
b = T.Bulk()
cfg = bytearray(h.cfg_get())
cfg[6] = 0
h.cfg_set(bytes(cfg))
h.enable(1)
b.drain()

for i in range(3):
    seq = b.next_seq()
    b.send(T.frame(T.T_XFER, T.xfer_payload(T.TC_LINES_1, cmd=0, tx=b"\x11\x22\x33\x44",
                                             rx_len=4), T.F_RSP | T.F_CS_HOLD, seq))
    r = T.parse_rsp(b.recv(2000))
    print("hold frame %d -> status=%s CS=%d" % (i, r and r["status"], 1 if h.status()["status"] & 4 else 0))

seq = b.next_seq()
b.send(T.frame(T.T_XFER, T.xfer_payload(T.TC_LINES_1, cmd=0, tx=b"\x55\x66\x77\x88", rx_len=4),
               T.F_RSP, seq))
r = T.parse_rsp(b.recv(2000))
print("final frame  -> status=%s CS=%d" % (r and r["status"], 1 if h.status()["status"] & 4 else 0))

b.drain()
h.close()
