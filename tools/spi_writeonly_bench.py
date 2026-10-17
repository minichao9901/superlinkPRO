"""Write-only timing: the panel-pixel path (no RX, CPU can idle during DMA)."""
import sys

sys.path.insert(0, r"E:\Share\github\akaLinkPro\script_test")
import spi_bridge_test as T  # noqa: E402

h = T.Hid()
b = T.Bulk()
h.enable(1)
b.drain()


def timeit(tx, force, reps=9):
    times = []
    for _ in range(reps):
        seq = b.next_seq()
        b.send(T.frame(T.T_XFER, T.xfer_payload(T.TC_LINES_1, cmd=0, tx=tx, rx_len=0),
                       T.F_RSP | force, seq))
        r = T.parse_rsp(b.recv(3000))
        if r is None or r["status"] != 0:
            return None
        times.append(h.status()["last_ticks"])
    times.sort()
    return times[len(times) // 2] / 24.0


print("SCLK = %d MHz   (write-only frames)" % (h.status()["sclk"] // 1000000))
print("%6s | %9s | %9s | %8s | %s" % ("len", "poll us", "dma us", "speedup", "line-rate us"))
print("-" * 62)
for ln in (32, 64, 128, 256, 492):
    tx = bytes(((i * 7 + ln) & 0xFF) for i in range(ln))
    p = timeit(tx, T.F_NO_DMA)
    d = timeit(tx, T.F_FORCE_DMA)
    if p is None or d is None:
        print("%6d | FAILED" % ln)
        continue
    sclk = h.status()["sclk"]
    ideal = ln * 8 / sclk * 1e6
    print("%6d | %9.2f | %9.2f | %7sx | %.2f" %
          (ln, p, d, ("%.2f" % (p / d)) if d else "-", ideal))

st = h.status()
print()
print("counters: tx_poll=%d tx_dma=%d err=%d" % (st["tx_poll"], st["tx_dma"], st["frames_err"]))
b.drain()
h.close()
