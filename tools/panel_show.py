"""一键：初始化天马 2P01 / AXS15352 并显示测试图案。

    python tools/panel_show.py                 # 彩条（默认，能同时看色序和几何）
    python tools/panel_show.py -p solid -c red
    python tools/panel_show.py -p gradient
    python tools/panel_show.py --madctl 0x00 --bigendian

约定（与跑通的 ESP-IDF 工程一致）：
    MADCTL = 0x00（RGB 顺序）  +  RGB565 **高字节在前**（struct.pack(">H")）
    颜色不对先试 --madctl 0x08 / --littleendian，别急着改数据。
"""
import argparse
import re
import struct
import sys
import time

sys.path.insert(0, r"E:\Share\github\akaLinkPro\script_test")
import spi_bridge_test as T  # noqa: E402

SEQ_TXT = r"E:\esp-idf-wsh\资料\panel_init\extra_parts\TianMa2p01+AXS15352_SPI_565_20260716.txt"
W, H = 240, 296

COLORS = {"red": 0xF800, "green": 0x07E0, "blue": 0x001F, "white": 0xFFFF,
          "black": 0x0000, "yellow": 0xFFE0, "cyan": 0x07FF, "magenta": 0xF81F,
          "gray": 0x8410}
BARS = ["white", "yellow", "cyan", "green", "magenta", "red", "blue", "black"]


def parse_init(path):
    out = []
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.split("//")[0].strip()
        if not line:
            continue
        m = re.match(r"Delay\((\d+)\)", line)
        if m:
            out.append(("delay", 0, b"", int(m.group(1))))
            continue
        nums = [int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{1,2})", line)]
        if nums:
            out.append(("step", nums[0], bytes(nums[1:]), 0))
    return out


def build(pattern, color, be):
    pk = (lambda v: struct.pack(">H", v)) if be else (lambda v: struct.pack("<H", v))
    if pattern == "solid":
        row = pk(COLORS[color]) * W
        return [row] * H
    if pattern == "bars":
        seg = W // len(BARS)
        line = b"".join(pk(COLORS[BARS[min(i // seg, len(BARS) - 1)]]) for i in range(W))
        return [line] * H
    if pattern == "gradient":
        rows = []
        for y in range(H):
            rows.append(b"".join(pk(((y * 31 // (H - 1)) << 11) | ((x * 63 // (W - 1)) << 5))
                                 for x in range(W)))
        return rows
    if pattern == "checker":
        rows = []
        cell = 16
        for y in range(H):
            rows.append(b"".join(pk(0xFFFF if ((x // cell) + (y // cell)) % 2 else 0x0000)
                                 for x in range(W)))
        return rows
    raise SystemExit("unknown pattern %r" % pattern)


ap = argparse.ArgumentParser()
ap.add_argument("-p", "--pattern", default="bars", choices=["bars", "solid", "gradient", "checker"])
ap.add_argument("-c", "--color", default="red", choices=list(COLORS))
ap.add_argument("--sclk", type=int, default=40000000)
ap.add_argument("--madctl", default="0x00", help="0x00 = RGB（默认）/ 0x08 = BGR")
ap.add_argument("--littleendian", dest="be", action="store_false", default=True,
                help="默认高字节在前；红蓝互换时两个都试")
ap.add_argument("--no-init", action="store_true", help="跳过复位+初始化（面板已经初始化过）")
a = ap.parse_args()

h = T.Hid()
b = T.Bulk()
cfg = bytearray(h.cfg_get())
struct.pack_into("<I", cfg, 0, a.sclk)
cfg[6] = 0
cfg[8], cfg[9], cfg[11], cfg[13] = T.PADS["PA02"], T.PADS["PA31"], T.PADS["PA10"], T.PADS["none"]
cfg[12] = 0x06
h.cfg_set(bytes(cfg))
prof = bytearray(h.profile_get())
prof[0] = T.PROFILES["spi_dcx"]
prof[2] = 1
h.profile_set(bytes(prof))
h.enable(0)
h.enable(1)

sclk = h.status()["sclk"]
print("probe: sclk=%d Hz  profile=spi_dcx  dc=PA02(J3[7]) rst=PA31(J3[11]) bl=PA10(J3[33]) te=none" % sclk)
b.drain()


def step(cmd, params=b"", delay_ms=0):
    seq = b.next_seq()
    b.send(T.frame(T.T_STEP, struct.pack("<BBH", cmd, len(params), delay_ms) + params, T.F_RSP, seq))
    return T.parse_rsp(b.recv(3000))


if not a.no_init:
    seq = b.next_seq()
    b.send(T.frame(T.T_RESET, struct.pack("<HH", 10, 120), T.F_RSP, seq))
    r = T.parse_rsp(b.recv(2000))
    print("reset -> %s" % (r and r["status"]))
    for cmd, params in ((0x36, bytes([int(a.madctl, 16)])), (0x3A, b"\x55")):
        r = step(cmd, params)
        if r is None or r["status"] != 0:
            print("  !! STEP 0x%02X failed: %s" % (cmd, r and r["status"]))
    print("MADCTL=0x%02X  COLMOD=0x55" % int(a.madctl, 16))
    bad = 0
    for kind, cmd, params, delay in parse_init(SEQ_TXT):
        r = step(cmd, params, delay)
        if r is None or r["status"] != 0:
            bad += 1
    print("vendor init: %d commands, %d failed" % (len(parse_init(SEQ_TXT)), bad))
    seq = b.next_seq()
    b.send(T.frame(T.T_GPIO, bytes([3, 1]), T.F_RSP, seq))   # BL on
    T.parse_rsp(b.recv(1000))

# window
step(0x2A, struct.pack(">HH", 0, W - 1))
step(0x2B, struct.pack(">HH", 0, H - 1))

rows = build(a.pattern, a.color, a.be)
t0 = time.time()
# RAMWR 作为"命令"（DC=0），CS 一直保持；像素作为"数据"（DC=1）
seq = b.next_seq()
b.send(T.frame(T.T_XFER, T.xfer_payload(T.TC_LINES_1 | T.TC_DC_EN, cmd=0, tx=b"\x2c", rx_len=0),
               T.F_RSP | T.F_CS_HOLD, seq))
T.parse_rsp(b.recv(2000))
for i, row in enumerate(rows):
    last = (i == len(rows) - 1)
    tcfg = T.TC_LINES_1 | T.TC_DC_EN | T.TC_DC_LEVEL
    fl = (0 if last else T.F_CS_HOLD) | (T.F_RSP if last else 0)
    # 一帧一次 bulk 写（绝不能拼成一个大 blob：协议要求一帧不跨包）
    b.send(T.frame(T.T_XFER, T.xfer_payload(tcfg, cmd=0, tx=row, rx_len=0), fl, seq), timeout=5000)
r = T.parse_rsp(b.recv(5000))
dt = (time.time() - t0) * 1000
st = h.status()
print("pattern=%s color=%s be=%s -> %s  %.1f ms  %.2f MB/s   frames_ok=%d err=%d"
      % (a.pattern, a.color, a.be, "OK" if (r and r["status"] == 0) else "FAIL(%s)" % (r and r["status"]),
         dt, (W * H * 2 / 1024 / 1024) / (dt / 1000.0), st["frames_ok"], st["frames_err"]))
if a.pattern == "bars":
    print("从左到右应该是: " + " / ".join(BARS))
b.drain()
h.close()
