"""Fill the AXS15352 panel with a solid colour - live knobs for MADCTL / clock / pipelining.

  python panel_fill.py --madctl 0x08 --color red
  python panel_fill.py --madctl 0x08 --color red --pipeline     # no per-row RSP

--pipeline 把 296 行像素一次性灌进 bulk OUT（设备环满时自然 NAK 背压），
只在最后一行要应答 —— 这才是主机侧该有的刷图姿势。
"""
import argparse
import struct
import sys
import time

sys.path.insert(0, r"E:\Share\github\akaLinkPro\script_test")
import spi_bridge_test as T  # noqa: E402

W, H = 240, 296
COLORS = {
    "red": 0xF800, "green": 0x07E0, "blue": 0x001F,
    "white": 0xFFFF, "black": 0x0000, "gray": 0x8410, "yellow": 0xFFE0,
}

ap = argparse.ArgumentParser()
ap.add_argument("--madctl", default="0x08")
ap.add_argument("--color", default="red", choices=list(COLORS))
ap.add_argument("--sclk", type=int, default=40000000)
ap.add_argument("--pipeline", action="store_true")
ap.add_argument("--rows-per-chunk", type=int, default=0, help="0 = all in one go")
a = ap.parse_args()

h = T.Hid()
b = T.Bulk()
cfg = bytearray(h.cfg_get())
struct.pack_into("<I", cfg, 0, a.sclk)
cfg[6] = 0
h.cfg_set(bytes(cfg))
h.enable(0)
h.enable(1)
b.drain()
print("sclk=%d Hz madctl=%s color=%s pipeline=%s"
      % (h.status()["sclk"], a.madctl, a.color, a.pipeline))


def step(cmd, params=b""):
    seq = b.next_seq()
    b.send(T.frame(T.T_STEP, struct.pack("<BBH", cmd, len(params), 0) + params, T.F_RSP, seq))
    return T.parse_rsp(b.recv(3000))


def xfer(tx, dc, flags=0):
    tcfg = T.TC_LINES_1 | T.TC_DC_EN | (T.TC_DC_LEVEL if dc else 0)
    seq = b.next_seq()
    b.send(T.frame(T.T_XFER, T.xfer_payload(tcfg, cmd=0, tx=tx, rx_len=0), T.F_RSP | flags, seq))
    return T.parse_rsp(b.recv(3000))


r = step(0x36, bytes([int(a.madctl, 16)]))
print("MADCTL -> %s" % (r and r["status"]))
step(0x2A, struct.pack(">HH", 0, W - 1))
step(0x2B, struct.pack(">HH", 0, H - 1))

PX = struct.pack("<H", COLORS[a.color]) * W
t0 = time.time()
xfer(b"\x2c", dc=0, flags=T.F_CS_HOLD)          # RAMWR, CS stays low
if a.pipeline:
    # ⚠️ **一帧一次 bulk 写**，绝不能把多帧拼成一个 blob：协议要求"一帧不跨 USB 包"，
    #    拼起来以后 512 B 的包会把帧切断，设备直接判 BAD_FRAME（实测 err=290）。
    #    设备 OUT 环满时会对主机 NAK，写调用自然阻塞 —— 这就是背压，不需要 RSP。
    seq = b.next_seq()
    for row in range(H):
        tcfg = T.TC_LINES_1 | T.TC_DC_EN | T.TC_DC_LEVEL
        flags = T.F_CS_HOLD if row < H - 1 else 0
        rsp = T.F_RSP if row == H - 1 else 0
        f = T.frame(T.T_XFER, T.xfer_payload(tcfg, cmd=0, tx=PX, rx_len=0), flags | rsp, seq)
        b.send(f, timeout=5000)
    r = T.parse_rsp(b.recv(5000))
    ok = r is not None and r["status"] == 0
    if not ok:
        print("  last row -> %s" % (r and r["status"]))
else:
    ok = True
    for row in range(H):
        r = xfer(PX, dc=1, flags=0 if row == H - 1 else T.F_CS_HOLD)
        if r is None or r["status"] != 0:
            ok = False
            print("  row %d -> %s" % (row, r and r["status"]))
            break
dt = (time.time() - t0) * 1000
kb = W * H * 2 / 1024.0
print("fill %s: %s in %.1f ms  (%.2f MB/s, line-rate %.2f MB/s)"
      % (a.color, "OK" if ok else "FAIL", dt, kb / 1024 / (dt / 1000.0),
         a.sclk / 8.0 / 1024 / 1024))
st = h.status()
print("frames_ok=%d err=%d bytes_tx=%d" % (st["frames_ok"], st["frames_err"], st["bytes_tx"]))
b.drain()
h.close()
