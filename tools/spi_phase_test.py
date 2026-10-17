"""Try the SDK-example style transfer: cmd + addr phases + data phase."""
import sys
import time

sys.path.insert(0, r"E:\Share\github\akaLinkPro\script_test")
import spi_bridge_test as T  # noqa: E402

h = T.Hid()
b = T.Bulk()
tx = bytes([0x08, 0x0F, 0x16, 0x1D])


def run(label, tcfg, cmd=0, addr=0, addr_len=0, dummy=0, mode=0, rx=4):
    cfg = bytearray(h.cfg_get())
    if cfg[4] != mode:
        cfg[4] = mode
        h.cfg_set(bytes(cfg))
    h.enable(0)
    h.enable(1)
    b.drain()
    seq = b.next_seq()
    pl = T.xfer_payload(tcfg, cmd=cmd, addr=addr, addr_len=addr_len, dummy=dummy,
                        tx=tx, rx_len=rx)
    b.send(T.frame(T.T_XFER, pl, T.F_RSP, seq))
    r = T.parse_rsp(b.recv(2000))
    print("  %-42s status=%s got=%s" %
          (label, r and r["status"], (r and r["data"].hex()) or "-"))
    return r


print("=== plain data phase (what we had) ===")
run("data only, mode 0", T.TC_LINES_1, mode=0)

print()
print("=== + command phase (SDK style) ===")
run("cmd=0x03 only, mode 0", T.TC_CMD_EN | T.TC_LINES_1, cmd=0x03, mode=0)

print()
print("=== + command + 3-byte address (NOR read style) ===")
for mode in (0, 1, 2, 3):
    run("cmd+addr, mode %d" % mode, T.TC_CMD_EN | T.TC_ADDR_EN | T.TC_LINES_1,
        cmd=0x03, addr_len=3, addr=0x001234, mode=mode)

h.enable(0)
b.drain()
h.close()
