"""Run loopback until a case fails, then dump the probe's own diagnostics."""
import sys
import time

sys.path.insert(0, r"E:\Share\github\akaLinkPro\script_test")
import spi_bridge_test as T  # noqa: E402

h = T.Hid()
b = T.Bulk()
h.enable(1)
b.drain()

tx = bytes(((i * 7 + 32) & 0xFF) for i in range(32))
fails = 0
for attempt in range(200):
    for force, tag in ((T.F_NO_DMA, "poll"), (T.F_FORCE_DMA, "dma")):
        seq = b.next_seq()
        b.send(T.frame(T.T_XFER, T.xfer_payload(T.TC_LINES_1, cmd=0, tx=tx, rx_len=len(tx)),
                       T.F_RSP | force, seq))
        r = T.parse_rsp(b.recv(3000))
        if r is None or r["status"] != 0 or r["data"] != tx:
            fails += 1
            d = h.dbg()
            st = h.status()
            print("FAIL attempt=%d path=%s status=%s got=%s" %
                  (attempt, tag, r and r["status"], (r and r["data"][:16].hex()) or "-"))
            print("  counters: frames_ok=%d err=%d poll=%d dma=%d last_ticks=%d"
                  % (st["frames_ok"], st["frames_err"], st["tx_poll"], st["tx_dma"], st["last_ticks"]))
            print("  reset_iters=%d CTRL=%08X module=%d sclk=%d" % (d[0], d[1], d[2], d[3]))
            print("  STATUS=%08X CTRL=%08X TRANSCTRL=%08X TRANSFMT=%08X TIMING=%08X"
                  % (d[4], d[5], d[6], d[7], d[8]))
            print("  wr/rd_cnt=%08X sdkstat=%d busy_before=%d wcnt=%d stage=%d"
                  % (d[9], d[10], d[11] & 1, (d[11] >> 16) & 0xFFFF, d[12]))
            if fails >= 3:
                b.drain()
                h.close()
                sys.exit(1)
            time.sleep(0.3)
            b.drain()
    time.sleep(0.05)

print("no failure in 200 rounds")
b.drain()
h.close()
