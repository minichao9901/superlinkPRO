"""Decode the SPI loopback capture: clock edges vs MOSI/MISO levels.

Answers: at the sampling edge, does MISO actually carry the byte we sent?
"""
import sys

sys.path.insert(0, r"E:\Share\github\akaLinkPro\tools")
import kingst_la as K

CSV = sys.argv[1] if len(sys.argv) > 1 else r"E:\Share\github\akaLinkPro\captures\spi_real2.csv"
WANT = bytes([0x08, 0x0F, 0x16, 0x1D, 0x24, 0x2B, 0x32, 0x39])

init, tracks, chans = K.parse_csv(CSV)
print("channels in file:", chans, " initial levels:", init)

CS, MISO, MOSI, SCLK = 0, 1, 2, 4
clk = tracks.get(SCLK, [])
rise = [t for t, lv in clk if lv == 1]
fall = [t for t, lv in clk if lv == 0]
print("SCLK edges: %d rising, %d falling" % (len(rise), len(fall)))

cs_track = tracks.get(CS, [])
print("CS:", [(round(t * 1e6, 2), lv) for t, lv in cs_track][:6], "us")


def level(track, t, ch):
    return K.level_at(track, t, init.get(ch, 0))


def bits_at(edges, ch):
    out = []
    for t in edges:
        out.append(level(tracks.get(ch, []), t + 1e-12, ch))
    return out


def bits_to_bytes(bits, msb_first=True):
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        chunk = bits[i:i + 8]
        v = 0
        for k, b in enumerate(chunk):
            v |= b << (7 - k if msb_first else k)
        out.append(v)
    return bytes(out)


for name, edges in (("RISING", rise), ("FALLING", fall)):
    for ch, label in ((MOSI, "MOSI"), (MISO, "MISO")):
        b = bits_at(edges, ch)
        for order, tag in ((True, "MSB"), (False, "LSB")):
            data = bits_to_bytes(b, order)
            hit = data[:8] == WANT
            if hit or (ch == MISO and order is True and name == "RISING"):
                print("  sample@%-7s %s %s-first: %s %s" %
                      (name, label, tag, data[:8].hex(), "  <== MATCHES want" if hit else ""))
            elif label == "MOSI" and order is True:
                print("  sample@%-7s %s %s-first: %s" % (name, label, tag, data[:8].hex()))

print()
print("want                              : %s" % WANT.hex())

# also: raw levels of MISO around the first few rising edges
print()
print("first 12 rising edges (ns): ", [round(t * 1e9, 1) for t in rise[:12]])
print("MOSI level at them        : ", bits_at(rise[:12], MOSI))
print("MISO level at them        : ", bits_at(rise[:12], MISO))
print("MOSI level at fallings    : ", bits_at(fall[:12], MOSI))
print("MISO level at fallings    : ", bits_at(fall[:12], MISO))
