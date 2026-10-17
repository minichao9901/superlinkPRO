"""Drive the TianMa 2P01 / AXS15352 panel (SPI + DC) through the probe's SPI bridge.

Flow: configure -> reset -> 30 init steps -> address window -> fill a colour -> backlight.
Init sequence is parsed straight from the vendor txt so nothing is transcribed by hand.

Pixel writes use CS_HOLD across frames so the RAMWR command and the whole pixel
stream share ONE CS window (the DC line is what distinguishes command from data).
"""
import re
import struct
import sys
import time

sys.path.insert(0, r"E:\Share\github\akaLinkPro\script_test")
import spi_bridge_test as T  # noqa: E402

SEQ_TXT = r"E:\esp-idf-wsh\资料\panel_init\extra_parts\TianMa2p01+AXS15352_SPI_565_20260716.txt"
W, H = 240, 296


def parse_init(path):
    """-> [(kind, cmd, params, delay_ms)]  kind: 'step' | 'delay'"""
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
        if not nums:
            continue
        out.append(("step", nums[0], bytes(nums[1:]), 0))
    return out


h = T.Hid()
b = T.Bulk()

# ---- 1. configure: profile 1 (spi_dcx), DC active high, default aux pads ----
prof = bytearray(h.profile_get())
prof[0] = T.PROFILES["spi_dcx"]
prof[1] = 1      # def_lines
prof[2] = 1      # dc_active_high (AXS15352)
prof[3] = 1      # cs_hold_in_step
h.profile_set(bytes(prof))

cfg = bytearray(h.cfg_get())
struct.pack_into("<I", cfg, 0, 40000000)   # 40 MHz：跑通工程用的就是这个档
cfg[6] = 0                                  # cs_policy = auto GPIO CS (PA26)
cfg[8] = T.PADS["PA02"]                     # DC
cfg[9] = T.PADS["PA31"]                     # RST
cfg[11] = T.PADS["PA10"]                    # BL
cfg[13] = T.PADS["none"]                    # TE
cfg[12] = 0x06                              # RST/CS active low
h.cfg_set(bytes(cfg))
h.enable(0)
h.enable(1)
b.drain()
print("config: profile=1 dc=PB11 rst=PB12 bl=PB13 sclk=%d" % h.status()["sclk"])


def step(cmd, params=b"", delay_ms=0):
    payload = struct.pack("<BBH", cmd, len(params), delay_ms) + params
    seq = b.next_seq()
    b.send(T.frame(T.T_STEP, payload, T.F_RSP, seq))
    return T.parse_rsp(b.recv(3000))


def xfer(tx, dc=None, flags=0, rx=0):
    tcfg = T.TC_LINES_1
    if dc is not None:
        tcfg |= T.TC_DC_EN | (T.TC_DC_LEVEL if dc else 0)
    seq = b.next_seq()
    b.send(T.frame(T.T_XFER, T.xfer_payload(tcfg, cmd=0, tx=tx, rx_len=rx), T.F_RSP | flags, seq))
    return T.parse_rsp(b.recv(3000))


# ---- 2. hardware reset: RST low 10 ms, then 120 ms ----
seq = b.next_seq()
b.send(T.frame(T.T_RESET, struct.pack("<HH", 10, 120), T.F_RSP, seq))
r = T.parse_rsp(b.recv(2000))
print("RESET(10ms/120ms) -> status=%s" % (r and r["status"]))

# ---- 3. init sequence ----
# ⚠️ 顺序照 ESP-IDF 的 esp_lcd_st77916 组件（那份工程是跑通过的）：
#    MADCTL(0x36) → COLMOD(0x3A) → 厂家 30 条 → 0x11/100ms/0x29
#    厂家 txt 里没有 0x36/0x3A（COLMOD 是组件按 16bpp 补发的 0x55），
#    少了它们屏会黑 —— 这轮实测就是这么黑的。
print("pre: MADCTL(0x36)=0x00, COLMOD(0x3A)=0x55 (RGB565/16bpp)")
for cmd, params in ((0x36, b"\x00"), (0x3A, b"\x55")):
    r = step(cmd, params)
    print("  STEP 0x%02X %s -> %s" % (cmd, params.hex(), r and r["status"]))

items = parse_init(SEQ_TXT)
print("init sequence: %d entries (%d steps + %d delays)"
      % (len(items), sum(1 for i in items if i[0] == "step"), sum(1 for i in items if i[0] == "delay")))
t0 = time.time()
bad = 0
for kind, cmd, params, delay in items:
    if kind == "delay":
        seq = b.next_seq()
        b.send(T.frame(T.T_DELAY, struct.pack("<I", delay * 1000), T.F_RSP, seq))
        r = T.parse_rsp(b.recv(3000))
    else:
        r = step(cmd, params, delay)
    if r is None or r["status"] != 0:
        bad += 1
        print("  !! cmd=0x%02X -> %s" % (cmd, r and r["status"]))
print("init done in %.1f ms, failures=%d" % ((time.time() - t0) * 1000, bad))

# ---- 4. backlight on ----
seq = b.next_seq()
b.send(T.frame(T.T_GPIO, bytes([3, 1]), T.F_RSP, seq))
r = T.parse_rsp(b.recv(1000))
print("backlight on -> status=%s" % (r and r["status"]))

# ---- 5. address window + solid colour ----
for cmd, params in ((0x2A, struct.pack(">HH", 0, W - 1)),  # CASET
                    (0x2B, struct.pack(">HH", 0, H - 1))):  # RASET
    r = step(cmd, params)
    print("  STEP 0x%02X %s -> %s" % (cmd, params.hex(), r and r["status"]))

PX = struct.pack("<H", 0xF800) * W       # one row of red (RGB565)
print("filling %dx%d red (RGB565 0xF800), one row per XFER, CS held across the whole write" % (W, H))
t0 = time.time()
r = xfer(b"\x2c", dc=0, flags=T.F_CS_HOLD)      # RAMWR as a command byte, CS stays low
if r is None or r["status"] != 0:
    print("  RAMWR command failed: %s" % (r and r["status"]))
for row in range(H):
    last = (row == H - 1)
    r = xfer(PX, dc=1, flags=0 if last else T.F_CS_HOLD)
    if r is None or r["status"] != 0:
        print("  row %d failed: %s" % (row, r and r["status"]))
        break
dt = (time.time() - t0) * 1000
print("pixel fill done in %.1f ms (%d rows x %d B, %.2f MB/s)"
      % (dt, H, W * 2, (W * H * 2 / 1024 / 1024) / (dt / 1000.0)))
st = h.status()
print("probe counters: frames_ok=%d err=%d bytes_tx=%d last_ticks=%d"
      % (st["frames_ok"], st["frames_err"], st["bytes_tx"], st["last_ticks"]))

b.drain()
h.close()
print()
print(">>> 屏应该整屏变红。若没亮：先看背光(BL)/复位，再用 LA 看 CS-DC-SCLK。")
