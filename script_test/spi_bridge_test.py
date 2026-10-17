"""USB -> SPI/QSPI bridge test (akaLinkPro probe, HID 0x35 + bulk 0x0B/0x8B).

Protocol reference: firmware/application_5301/src/spi_bridge/spi_bridge_proto.h
Design doc:        docs/usb-spi-bridge-plan.md

Usage:
  python spi_bridge_test.py info                     read config / profile / status
  python spi_bridge_test.py cfg --sclk 20000000 ...  write the config block
  python spi_bridge_test.py profile qspi             select the panel profile
  python spi_bridge_test.py enable 1                 turn the bridge on (pins get muxed)
  python spi_bridge_test.py loop                     MOSI<->MISO jumper loopback sweep
  python spi_bridge_test.py frames                   PING/DELAY/GPIO/AUX_IN/CS smoke test
  python spi_bridge_test.py status

The loopback test needs a wire between J3[28] (MOSI) and J3[27] (MISO); without it
the reads come back as 0x00/0xFF and the test fails loudly (that is the point).

Every blocking point has a short timeout and the process carries a watchdog.
"""
import argparse
import os
import struct
import sys
import threading
import time

import hid
import usb.core
import usb.util

VID, PID = 0x0D28, 0x0204
USAGE_PAGE = 0xFF00

# ---- HID 0x35 actions (sb_hid_action_t) ----
CMD_SPI = 0x35
ACT_STATUS, ACT_ENABLE, ACT_RESET, ACT_SET_CFG, ACT_GET_CFG = 0, 1, 2, 3, 4
ACT_PIN_CFG, ACT_ABORT, ACT_SET_PROFILE, ACT_GET_PROFILE = 5, 6, 7, 8
ACT_DBG = 10
ACT_PINTEST = 11
ACT_WIGGLE = 12

# ---- frames (sb_frame_type_t) ----
T_XFER, T_CS, T_GPIO, T_DELAY, T_PING, T_CFG, T_STEP, T_RESET, T_AUX_IN = 1, 2, 3, 4, 5, 6, 7, 8, 9
F_RSP, F_CS_HOLD, F_CS_OFF, F_CS_AUX, F_NO_DMA, F_FORCE_DMA = 1, 2, 4, 8, 16, 32
MAGIC = 0x4253

# ---- tcfg bits ----
TC_LINES_1, TC_LINES_2, TC_LINES_4 = 0, 1, 2
TC_CMD_EN, TC_ADDR_EN, TC_ADDR_QUAD, TC_DC_EN, TC_DC_LEVEL, TC_TOKEN_EN = 4, 8, 16, 32, 64, 128

# ---- pad table (sb_pad_t) ----
# 2026-09-30 起 PB10~PB13（索引 1~4）是 SPI2 的 SCLK/MISO/MOSI/CS，PA30(12) 被板上 Q1 短到地，都别选
# 2026-09-30 新增 14~17 = PA26/PA27/PA28/PA29（SPI1 时代显示接口那四根，SPI2 迁移后固件里零引用，实测释放）
PADS = {"none": 0, "PB11": 1, "PB12": 2, "PB13": 3, "PB10": 4, "PA02": 5, "PA09": 6,
        "PA00": 7, "PA01": 8, "PY00": 9, "PY01": 10, "PA10": 11, "PA30": 12, "PA31": 13,
        "PA26": 14, "PA27": 15, "PA28": 16, "PA29": 17}
PAD_NAMES = {v: k for k, v in PADS.items()}

PROFILES = {"raw": 0, "spi_dcx": 1, "qspi": 2}
PROFILE_NAMES = {v: k for k, v in PROFILES.items()}

STATUS_BITS = [(0, "enabled"), (1, "active"), (2, "cs"), (3, "in_flow"), (4, "out_full")]
ERR_NAMES = {0: "OK", 1: "DISABLED", 2: "BAD_MAGIC", 3: "BAD_FRAME", 4: "RANGE",
             5: "TIMEOUT", 6: "IN_FULL", 7: "DMA", 8: "BUSY", 9: "GPIO"}

FRAME_MAX = 504
XFER_HDR = 12


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT (%ss)" % sec)
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


# --------------------------------------------------------------------------- HID
class Hid:
    def __init__(self):
        t0 = time.time()
        last = None
        while time.time() - t0 < 8.0:
            try:
                cand = [i for i in hid.enumerate(VID, PID) if i.get("usage_page") == USAGE_PAGE]
                if cand:
                    d = hid.device()
                    d.open_path(cand[0]["path"])
                    d.set_nonblocking(1)
                    time.sleep(0.15)
                    self.d = d
                    return
            except Exception as e:  # device busy while re-enumerating
                last = e
            time.sleep(0.3)
        raise RuntimeError("custom HID interface not found (%r)" % last)

    def close(self):
        try:
            self.d.close()
        except Exception:
            pass

    def xfer(self, cmd, args=(), tmo=1.0):
        req = [0x01, 1 + len(args), cmd] + list(args)
        if len(req) > 64:
            raise ValueError("HID report too long")
        req += [0] * (64 - len(req))
        self.d.write(req)
        t0 = time.time()
        while time.time() - t0 < tmo:
            d = self.d.read(64, timeout_ms=150)
            if d and d[0] == 0x02 and d[2] == cmd:
                return list(d)
        return None

    # ---- 0x35 helpers ----
    def cfg_get(self):
        r = self.xfer(CMD_SPI, [ACT_GET_CFG])
        if r is None:
            raise RuntimeError("GET_CFG timeout")
        return bytes(r[4:4 + 32])

    def cfg_set(self, blob):
        assert len(blob) == 32
        r = self.xfer(CMD_SPI, [ACT_SET_CFG] + list(blob))
        if r is None:
            raise RuntimeError("SET_CFG timeout")
        return struct.unpack_from("<I", bytes(r[4:8]))[0]

    def profile_get(self):
        r = self.xfer(CMD_SPI, [ACT_GET_PROFILE])
        if r is None:
            raise RuntimeError("GET_PROFILE timeout")
        return bytes(r[4:4 + 16])

    def profile_set(self, blob):
        assert len(blob) == 16
        r = self.xfer(CMD_SPI, [ACT_SET_PROFILE] + list(blob))
        if r is None:
            raise RuntimeError("SET_PROFILE timeout")
        return struct.unpack_from("<I", bytes(r[4:8]))[0]

    def enable(self, on):
        r = self.xfer(CMD_SPI, [ACT_ENABLE, 1 if on else 0])
        if r is None:
            raise RuntimeError("ENABLE timeout")
        return struct.unpack_from("<I", bytes(r[4:8]))[0]

    def status(self):
        r = self.xfer(CMD_SPI, [ACT_STATUS])
        if r is None:
            raise RuntimeError("STATUS timeout")
        w = struct.unpack_from("<11I", bytes(r[4:48]))
        return {"status": w[0], "frames_ok": w[1], "bytes_tx": w[2], "bytes_rx": w[3],
                "tx_poll": w[4], "tx_dma": w[5], "out_overrun": w[6], "in_drop": w[7],
                "sclk": w[8], "frames_err": w[9], "last_ticks": w[10]}

    def reset(self):
        self.xfer(CMD_SPI, [ACT_RESET])

    def abort(self):
        self.xfer(CMD_SPI, [ACT_ABORT])

    def pin_cfg(self, line, pad):
        r = self.xfer(CMD_SPI, [ACT_PIN_CFG, line, pad, 0])
        if r is None:
            raise RuntimeError("PIN_CFG timeout")
        return struct.unpack_from("<I", bytes(r[4:8]))[0]

    def dbg(self):
        r = self.xfer(CMD_SPI, [ACT_DBG])
        if r is None:
            raise RuntimeError("DBG timeout")
        return struct.unpack_from("<13I", bytes(r[4:56]))

    def pintest(self):
        r = self.xfer(CMD_SPI, [ACT_PINTEST])
        if r is None:
            raise RuntimeError("PINTEST timeout")
        return struct.unpack_from("<I", bytes(r[4:8]))[0]

    def wiggle(self):
        r = self.xfer(CMD_SPI, [ACT_WIGGLE], tmo=3.0)
        if r is None:
            raise RuntimeError("WIGGLE timeout")
        return struct.unpack_from("<I", bytes(r[4:8]))[0]


# -------------------------------------------------------------------------- bulk
class Bulk:
    """bulk OUT 0x0B / IN 0x8B on the vendor-specific interface (0xFF, 2 endpoints)."""

    def __init__(self):
        d = usb.core.find(idVendor=VID, idProduct=PID)
        if d is None:
            raise RuntimeError("probe not found")
        try:
            d.set_configuration()
        except usb.core.USBError:
            pass
        self.dev = d
        cfg = d.get_active_configuration()
        intf = None
        for i in cfg:
            if i.bInterfaceClass == 0xFF and i.bNumEndpoints == 2:
                intf = i
                break
        if intf is None:
            raise RuntimeError("SPI bridge interface not found (old firmware?)")
        self.intf_num = intf.bInterfaceNumber
        self.out = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        self.inn = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
        if self.out is None or self.inn is None:
            raise RuntimeError("bulk endpoints missing")
        self.seq = 0

    def drain(self, rounds=32):
        n = 0
        while n < rounds:
            try:
                r = bytes(self.inn.read(512, 30))
            except usb.core.USBTimeoutError:
                break
            if not r:
                break
            n += 1

    def send(self, blob, timeout=2000):
        self.out.write(blob, timeout)

    def recv(self, timeout=2000):
        try:
            return bytes(self.inn.read(512, timeout))
        except usb.core.USBTimeoutError:
            return b""

    def next_seq(self):
        self.seq = (self.seq + 1) & 0xFFFF
        return self.seq


def frame(ftype, payload=b"", flags=0, seq=0):
    if len(payload) > FRAME_MAX:
        raise ValueError("payload too long (%d > %d)" % (len(payload), FRAME_MAX))
    return struct.pack("<HBBHH", MAGIC, ftype, flags, seq, len(payload)) + payload


def xfer_payload(tcfg, cmd=0, addr=0, addr_len=0, dummy=0, tx=b"", rx_len=0):
    return struct.pack("<BBBBHHI", cmd, tcfg, addr_len, dummy, len(tx), rx_len, addr) + tx


def parse_rsp(blob):
    if len(blob) < 8:
        return None
    magic, rtype, status, seq, ln = struct.unpack_from("<HBBHH", blob, 0)
    if magic != MAGIC:
        return None
    return {"type": rtype, "status": status, "seq": seq, "data": blob[8:8 + ln]}


# ----------------------------------------------------------------------- commands
def decode_status(word):
    names = [n for bit, n in STATUS_BITS if word & (1 << bit)]
    return "|".join(names) if names else "-"


def cmd_info(args):
    h = Hid()
    cfg = h.cfg_get()
    prof = h.profile_get()
    st = h.status()
    sclk, mode, bits, cs_policy, thr = struct.unpack_from("<IBBBB", cfg, 0)
    pad_dc, pad_rst, pad_cs, pad_bl, active_low, pad_te = struct.unpack_from("<BBBBBB", cfg, 8)
    print("config   : sclk=%d Hz mode=%d bits=%d cs_policy=%d dma_threshold=%d" % (sclk, mode, bits, cs_policy, thr))
    print("aux pads : dc=%s rst=%s cs_aux=%s bl=%s te=%s active_low=0x%02X" %
          (PAD_NAMES.get(pad_dc, pad_dc), PAD_NAMES.get(pad_rst, pad_rst), PAD_NAMES.get(pad_cs, pad_cs),
           PAD_NAMES.get(pad_bl, pad_bl), PAD_NAMES.get(pad_te, pad_te), active_low))
    print("profile  : %s def_lines=%d dc_active_high=%d qspi_wr=0x%02X qspi_color=0x%02X addr_bytes=%d" %
          (PROFILE_NAMES.get(prof[0], prof[0]), prof[1], prof[2], prof[4], prof[5], prof[6]))
    print("status   : 0x%08X (%s) err=%s" % (st["status"], decode_status(st["status"]),
                                             ERR_NAMES.get((st["status"] >> 8) & 0xFF, "?")))
    print("actual   : sclk=%d Hz" % st["sclk"])
    print("counters : ok=%d err=%d tx=%d B rx=%d B poll=%d dma=%d out_ovf=%d in_drop=%d" %
          (st["frames_ok"], st["frames_err"], st["bytes_tx"], st["bytes_rx"],
           st["tx_poll"], st["tx_dma"], st["out_overrun"], st["in_drop"]))
    h.close()
    return 0


def cmd_cfg(args):
    h = Hid()
    cfg = bytearray(h.cfg_get())
    if args.sclk is not None:
        struct.pack_into("<I", cfg, 0, args.sclk)
    if args.mode is not None:
        cfg[4] = args.mode
    if args.cs_policy is not None:
        cfg[6] = args.cs_policy
    if args.threshold is not None:
        cfg[7] = args.threshold
    if args.module_clk is not None:
        struct.pack_into("<I", cfg, 24, args.module_clk)
    for name, idx in (("dc", 8), ("rst", 9), ("cs_aux", 10), ("bl", 11), ("te", 13)):
        val = getattr(args, name, None)
        if val is not None:
            if val not in PADS:
                raise SystemExit("unknown pad %r (choices: %s)" % (val, ", ".join(PADS)))
            cfg[idx] = PADS[val]
    if args.clear_on_enable is not None:
        cfg[14] = (cfg[14] | 1) if args.clear_on_enable else (cfg[14] & ~1)
    st = h.cfg_set(bytes(cfg))
    print("SET_CFG -> status 0x%08X (%s)" % (st, decode_status(st)))
    print("          err=%s" % ERR_NAMES.get((st >> 8) & 0xFF, "?"))
    cmd_info(args)
    h.close()
    return 0


def cmd_profile(args):
    h = Hid()
    prof = bytearray(h.profile_get())
    prof[0] = PROFILES[args.name]
    if args.def_lines is not None:
        prof[1] = args.def_lines
    if args.dc_active_high is not None:
        prof[2] = 1 if args.dc_active_high else 0
    st = h.profile_set(bytes(prof))
    print("SET_PROFILE %s -> status 0x%08X (%s)" % (args.name, st, decode_status(st)))
    h.close()
    return 0


def cmd_enable(args):
    h = Hid()
    st = h.enable(args.on)
    print("ENABLE %d -> status 0x%08X (%s) err=%s" % (args.on, st, decode_status(st),
                                                      ERR_NAMES.get((st >> 8) & 0xFF, "?")))
    if args.on:
        cfg = h.cfg_get()
        st2 = h.status()
        print("          sclk=%d Hz" % st2["sclk"])
    h.close()
    return 0


def cmd_dbg(args):
    h = Hid()
    d = h.dbg()
    print("--- SPI bring-up snapshot ---")
    print("reset self-test iters : 0x%08X %s" %
          (d[0], "(STUCK - reset bits never clear!)" if d[0] >= 100000 else "(cleared)"))
    print("CTRL after selftest   : 0x%08X" % d[1])
    print("spi node clk          : %d Hz" % d[2])
    print("actual sclk           : %d Hz" % d[3])
    print("STATUS (last xfer)    : 0x%08X  (txfull=%d rxempty=%d active=%d)" %
          (d[4], (d[4] >> 23) & 1, (d[4] >> 20) & 1, 0))
    print("CTRL   (last xfer)    : 0x%08X" % d[5])
    print("TRANSCTRL             : 0x%08X" % d[6])
    print("TRANSFMT              : 0x%08X" % d[7])
    print("TIMING                : 0x%08X" % d[8])
    print("WR|RD_TRANS_CNT       : wr=%d rd=%d" % (d[9] & 0xFFFF, (d[9] >> 16) & 0xFFFF))
    print("SDK status            : %d" % d[10])
    print("busy before call      : %d   wcnt=%d" % (d[11] & 1, (d[11] >> 16) & 0xFFFF))
    print("stage                 : %d  (1=就绪 2=spi_transfer 已返回)" % d[12])
    h.close()
    return 0


def cmd_pintest(args):
    """MOSI/MISO 当普通 GPIO：验 pad 输入通路 + J3[28]<->J3[27] 跳线到底通不通。"""
    h = Hid()
    r = h.pintest()
    bits = [("MISO reads 0 while MOSI=0   ", 0, "float+PD ok"),
            ("MISO reads 1 while MOSI=1   ", 1, "JUMPER OK"),
            ("MISO float + pulldown = 0   ", 2, "input path ok"),
            ("MISO float + pullup  = 1    ", 3, "input path ok"),
            ("drive MISO -> MOSI reads 1  ", 4, "jumper ok (reverse)"),
            ("self-test ran to completion ", 7, "")]
    for name, bit, note in bits:
        print("%s: %d   %s" % (name, (r >> bit) & 1, note))
    wired = ((r >> 1) & 1) and ((r >> 0) & 1)
    print()
    print("jumper J3[28]<->J3[27]: %s" % ("CONNECTED" if wired else "*** NOT CONNECTED (or MISO pad dead) ***"))
    h.close()
    return 0 if wired else 1


def cmd_wiggle(args):
    """在 SCLK/CS/MOSI 脚上发慢方波（SCLK 最快、CS 4 分频、MOSI 16 分频），给 LA 验接线。"""
    h = Hid()
    r = h.wiggle()
    print("wiggle done (result=0x%02X) - SCLK=100 toggles, CS=25, MOSI=~7 over ~3 ms" % r)
    h.close()
    return 0


def cmd_bench(args):
    """轮询 vs DMA 两条路径的耗时对照（探针侧 mchtmr tick，24 MHz = 41.7 ns）。

    每档跑 N 次取中位数（单次会被 USB 中断/主循环调度打散）。NO_DMA / FORCE_DMA
    是单帧覆盖位，所以这里两种都发一遍，用探针报的 last_ticks 作判据 ——
    它只量"一次事务从开始到收尾"，不含主机往返，比墙钟干净得多。
    """
    h = Hid()
    h.enable(1)
    b = Bulk()
    b.drain()

    lens = [int(x) for x in args.lens.split(",")]
    reps = args.reps
    print("%6s | %10s | %10s | %8s | %s" % ("len", "poll us", "dma us", "speedup", "verify"))
    print("-" * 62)
    for ln in lens:
        tx = bytes(((i * 7 + ln) & 0xFF) for i in range(ln))
        row = {}
        for tag, force in (("poll", F_NO_DMA), ("dma", F_FORCE_DMA)):
            times = []
            ok = True
            for _ in range(reps):
                seq = b.next_seq()
                b.send(frame(T_XFER, xfer_payload(TC_LINES_1, cmd=0, tx=tx, rx_len=ln),
                             F_RSP | force, seq))
                r = parse_rsp(b.recv(3000))
                if r is None or r["status"] != 0 or r["data"] != tx:
                    ok = False
                    break
                times.append(h.status()["last_ticks"])
            times.sort()
            row[tag] = (times[len(times) // 2] if times else 0, ok)
        p_us = row["poll"][0] / 24.0
        d_us = row["dma"][0] / 24.0
        print("%6d | %10.2f | %10.2f | %7sx | %s" %
              (ln, p_us, d_us,
               ("%.2f" % (p_us / d_us)) if d_us else "-",
               "ok" if (row["poll"][1] and row["dma"][1]) else "FAIL"))
    st = h.status()
    print()
    print("counters: tx_poll=%d tx_dma=%d err=%d" % (st["tx_poll"], st["tx_dma"], st["frames_err"]))
    b.drain()
    h.close()
    return 0


def cmd_status(args):
    h = Hid()
    if args.reset_counters:
        h.reset()
        time.sleep(0.1)
    st = h.status()
    for k, v in st.items():
        print("%-14s: %s" % (k, ("0x%08X (%s)" % (v, decode_status(v))) if k == "status" else v))
    h.close()
    return 0


def cmd_frames(args):
    """PING / DELAY / GPIO / AUX_IN / CS smoke test (no panel required)."""
    h = Hid()
    b = Bulk()
    b.drain()
    ok = True

    # PING with RSP
    seq = b.next_seq()
    b.send(frame(T_PING, b"", F_RSP, seq))
    r = parse_rsp(b.recv(1000))
    print("PING     : %s" % (r and ("seq=%d status=%s" % (r["seq"], ERR_NAMES.get(r["status"])))) or "NO RESPONSE")
    ok &= bool(r and r["seq"] == seq and r["status"] == 0)

    # short DELAY then a second PING: the delay must not stall the loop for long
    seq = b.next_seq()
    b.send(frame(T_DELAY, struct.pack("<I", 2000)) + frame(T_PING, b"", F_RSP, seq))
    t0 = time.time()
    r = parse_rsp(b.recv(1500))
    dt = (time.time() - t0) * 1000
    print("DELAY+PING: %s (%.1f ms wall)" % (r and ("seq=%d status=%s" % (r["seq"], ERR_NAMES.get(r["status"]))) or "NO RESPONSE", dt))
    ok &= bool(r and r["status"] == 0)

    # AUX_IN (TE line) - only meaningful when a pad is configured
    seq = b.next_seq()
    b.send(frame(T_AUX_IN, b"", F_RSP, seq))
    r = parse_rsp(b.recv(1000))
    if r is None:
        print("AUX_IN   : NO RESPONSE")
        ok = False
    else:
        print("AUX_IN   : status=%s data=%s" % (ERR_NAMES.get(r["status"]), r["data"].hex() or "-"))

    # CS frame is only legal in manual mode; report what happened either way
    seq = b.next_seq()
    b.send(frame(T_CS, bytes([0]), F_RSP, seq))
    r = parse_rsp(b.recv(1000))
    print("CS(rel)  : %s" % (r and ERR_NAMES.get(r["status"]) or "NO RESPONSE"))

    b.drain()
    h.close()
    print("frames   : %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def cmd_loop(args):
    """MOSI<->MISO jumper loopback: every byte sent must come back unchanged."""
    h = Hid()
    if not args.no_enable:
        st = h.enable(1)
        if not (st & 1):
            print("!! bridge did not enable: 0x%08X" % st)
            return 1
    b = Bulk()
    b.drain()

    lens = [int(x) for x in args.lens.split(",")]
    fails = 0
    # 单线回环 = MOSI 短接到 MISO，是唯一安全的跳线回环。双线/四线**不能**这样验：
    # 那两种相位下 DAT0/DAT1(/DAT2/DAT3) 都是推挽输出，短接等于两个输出对打
    # （我们的引脚还配了最大驱动 DS=4）。多线相位请用逻辑分析仪看波形，或接真从器件。
    lines_wanted = [int(x) for x in args.lines.split(",")]
    cases = []
    for ln in lines_wanted:
        if ln == 1:
            cases.append((1, TC_LINES_1))
        elif ln == 2:
            cases.append((2, TC_LINES_2))
        elif ln == 4:
            cases.append((4, TC_LINES_4))
        else:
            raise SystemExit("--lines only accepts 1/2/4")
    if len(cases) > 1:
        print("!! 警告：多线相位在跳线短接下是输出对打，仅作「是否发得出去」的冒烟，"
              "结果不作为数据正确性判据")
    for lines, tcfg in cases:
        for ln in lens:
            if ln > FRAME_MAX - XFER_HDR:
                continue
            for force in ((F_NO_DMA,) if args.no_dma else (0, F_FORCE_DMA)):
                seq = b.next_seq()
                tx = bytes(((i * 7 + ln) & 0xFF) for i in range(ln))
                fl = F_RSP | force
                tag = "dma" if (force & F_FORCE_DMA) else "poll"
                b.send(frame(T_XFER, xfer_payload(tcfg, cmd=0, tx=tx, rx_len=ln), fl, seq))
                r = parse_rsp(b.recv(2000))
                if r is None:
                    print("  lines=%d len=%3d %-4s : NO RESPONSE" % (lines, ln, tag))
                    fails += 1
                    continue
                if (r["status"] != 0) or (r["data"] != tx):
                    fails += 1
                    print("  lines=%d len=%3d %-4s : FAIL status=%s got=%s want=%s" %
                          (lines, ln, tag, ERR_NAMES.get(r["status"]), r["data"][:8].hex(), tx[:8].hex()))
                elif args.verbose:
                    print("  lines=%d len=%3d %-4s : ok" % (lines, ln, tag))

    st = h.status()
    print("loopback : %d case(s) failed; frames_ok=%d err=%d bytes_tx=%d bytes_rx=%d" %
          (fails, st["frames_ok"], st["frames_err"], st["bytes_tx"], st["bytes_rx"]))
    b.drain()
    h.close()
    print("loopback : %s" % ("PASS" if fails == 0 else "FAIL"))
    return 0 if fails == 0 else 1


def main():
    watchdog(90)
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info").set_defaults(func=cmd_info)

    c = sub.add_parser("cfg")
    c.add_argument("--sclk", type=int, default=None)
    c.add_argument("--mode", type=int, choices=[0, 1, 2, 3], default=None)
    c.add_argument("--cs-policy", type=int, choices=[0, 1, 2, 3], default=None, dest="cs_policy")
    c.add_argument("--threshold", type=int, default=None)
    c.add_argument("--module-clk", type=int, default=None, dest="module_clk",
                   help="SPI2 module clock target in Hz (0 = auto); debug knob")
    c.add_argument("--dc", default=None)
    c.add_argument("--rst", default=None)
    c.add_argument("--cs-aux", default=None, dest="cs_aux")
    c.add_argument("--bl", default=None)
    c.add_argument("--te", default=None)
    c.add_argument("--clear-on-enable", type=int, choices=[0, 1], default=None, dest="clear_on_enable")
    c.set_defaults(func=cmd_cfg)

    p = sub.add_parser("profile")
    p.add_argument("name", choices=list(PROFILES))
    p.add_argument("--def-lines", type=int, choices=[1, 2, 4], default=None, dest="def_lines")
    p.add_argument("--dc-active-high", type=int, choices=[0, 1], default=None, dest="dc_active_high")
    p.set_defaults(func=cmd_profile)

    e = sub.add_parser("enable")
    e.add_argument("on", type=int, choices=[0, 1])
    e.set_defaults(func=cmd_enable)

    s = sub.add_parser("status")
    s.add_argument("--reset-counters", action="store_true", dest="reset_counters")
    s.set_defaults(func=cmd_status)

    sub.add_parser("frames").set_defaults(func=cmd_frames)
    sub.add_parser("dbg").set_defaults(func=cmd_dbg)
    sub.add_parser("pintest").set_defaults(func=cmd_pintest)
    sub.add_parser("wiggle").set_defaults(func=cmd_wiggle)

    bn = sub.add_parser("bench")
    bn.add_argument("--lens", default="8,16,32,64,99,128,256,492")
    bn.add_argument("--reps", type=int, default=9)
    bn.set_defaults(func=cmd_bench)

    l = sub.add_parser("loop")
    l.add_argument("--lens", default="1,2,32,99,100,101,256,492")
    l.add_argument("--lines", default="1", help="1=单线（跳线回环的正确用法）；2/4 仅冒烟")
    l.add_argument("--no-dma", action="store_true", dest="no_dma")
    l.add_argument("--no-enable", action="store_true", dest="no_enable")
    l.add_argument("-v", "--verbose", action="store_true")
    l.set_defaults(func=cmd_loop)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
