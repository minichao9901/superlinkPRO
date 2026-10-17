"""USB→SPI 桥的烟雾测试 + DMA 通道无泄漏回归（代码审查第二round P2-3 / P2-4 的验收）。

**不需要接屏**：XFER 帧用"只写"（rx_len=0），CS/SCLK/MOSI 打在 J3 排针的空脚上。
验证四件事：
  1. HID 0x35 控制面活着（STATUS / ENABLE / SET_CFG / GET_CFG 回环）；
  2. **硬件重初始化不在 USB 中断里跑**（P2-4）：ENABLE 与 enabled 状态下的 SET_CFG
     现在只置 hw_req 标志、由主循环执行 —— 表现为 ENABLE 立即应答，随后帧照常执行；
  3. **DMA 通道不泄漏**（P2-3）：修复前每次 sb_spi_hw_init 都会丢一个 DMA 通道，
     本机池很小（CDC 已占 2 个），约 6 轮 SET_CFG 后 request 必然失败、桥静默退回
     轮询。这里连做 16 轮 SET_CFG 再发一笔 ≥ 阈值的大块写，断言 `tx_dma_cnt ≥ 1`
     （通道还申请得到 ⇒ 没泄漏）；
  4. 帧执行 + 应答配对（RSP seq 与请求一致）。

用法：python spi_bridge_smoke_test.py
"""
import struct
import sys
import threading
import time

import hid
import usb.core
import usb.util

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

VID, PID = 0x0D28, 0x0204
SB_HID = 0x35
SB_IN_EP, SB_OUT_EP = 0x8B, 0x0B
SB_MAGIC = 0x4253

T_XFER, T_PING = 0x01, 0x05
F_RSP, F_FORCE_DMA = 0x01, 0x20
SB_CFG_TX_DMA_THRESHOLD = 0

ACT_STATUS, ACT_ENABLE, ACT_RESET, ACT_SET_CFG, ACT_GET_CFG, ACT_ABORT = 0, 1, 2, 3, 4, 6

FAILS = []


def check(cond, note, detail=""):
    print("  %-40s %-5s %s" % (note, "[ok]" if cond else "[FAIL]", detail))
    if not cond:
        FAILS.append(note)
    return cond


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT")
        import os
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


def open_hid():
    for _ in range(30):
        cand = [i for i in hid.enumerate(VID, PID) if i.get("usage_page") == 0xFF00]
        if cand:
            d = hid.device()
            d.open_path(cand[0]["path"])
            d.set_nonblocking(1)
            time.sleep(0.2)
            return d
        time.sleep(0.3)
    raise RuntimeError("no custom HID interface")


def hid_xfer(dev, action, args=b"", tmo=2.0):
    pkt = [0x01, 2 + 1 + len(args), SB_HID, action] + list(args)
    pkt += [0] * (64 - len(pkt))
    dev.write(pkt)
    t0 = time.time()
    while time.time() - t0 < tmo:
        r = dev.read(64, timeout_ms=200)
        if r and r[0] == 0x02 and r[2] == SB_HID and r[3] == action:
            return list(r)
    return None


def status_word(dev):
    r = hid_xfer(dev, ACT_STATUS)
    if not r:
        return None, None
    st = int.from_bytes(bytes(r[4:8]), "little")
    stats = struct.unpack_from("<8I", bytes(r[8:40]))
    return st, stats


def find_bulk():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        raise RuntimeError("probe not found (pyusb)")
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass
    for intf in dev.get_active_configuration():
        eps = {ep.bEndpointAddress: ep for ep in intf}
        if SB_OUT_EP in eps and SB_IN_EP in eps:
            return dev, intf.bInterfaceNumber, eps[SB_OUT_EP], eps[SB_IN_EP]
    raise RuntimeError("SPI bridge bulk endpoints (0x0B/0x8B) not found — 这块板编了 SPI 桥吗？")


def frame(ftype, payload=b"", flags=0, seq=0):
    return struct.pack("<HBBHH", SB_MAGIC, ftype, flags, seq, len(payload)) + payload


def xfer_payload(tx, rx_len=0):
    # cmd, tcfg(1 线), addr_len=0, dummy=0, tx_len, rx_len, addr
    return struct.pack("<BBBBHHI", 0x00, 0x00, 0x00, 0x00, len(tx), rx_len, 0) + tx


def parse_rsp(blob):
    magic, rtype, st, seq, ln = struct.unpack_from("<HBBHH", blob, 0)
    return magic, rtype, st, seq, blob[8:8 + ln]


def main():
    dev = open_hid()

    st, stats = status_word(dev)
    if st is None:
        print("HID 0x35 无响应 —— 探针固件太旧或这块板没编 SPI 桥")
        return 1
    check(True, "基线：HID 0x35 STATUS 应答", "status=0x%08X" % st)

    # --- ENABLE=1（P2-4：ISR 只置标志，主循环做硬件初始化） ---
    t0 = time.time()
    r = hid_xfer(dev, ACT_ENABLE, [1])
    check(r is not None and time.time() - t0 < 0.5,
          "ENABLE=1 立即应答（硬件初始化已下沉主循环）", "%.0f ms" % ((time.time() - t0) * 1000))
    st, _ = status_word(dev)
    check(st is not None and (st & 0x01), "状态字 ENABLED=1")

    # --- CFG 回环 ---
    r = hid_xfer(dev, ACT_GET_CFG)
    cfg0 = bytes(r[4:4 + 32]) if r else None
    check(cfg0 is not None and len(cfg0) == 32, "GET_CFG 回 32 B 配置块")
    # 改 SCLK=20 MHz、tx_dma_threshold=8（保证后面 480 B 走 DMA），其余保持
    cfg = bytearray(cfg0)
    struct.pack_into("<I", cfg, 0, 20000000)
    cfg[4] = SB_CFG_TX_DMA_THRESHOLD
    r = hid_xfer(dev, ACT_SET_CFG, bytes(cfg))
    st, _ = status_word(dev)
    check(st is not None and ((st >> 8) & 0xFF) == 0, "SET_CFG 无错误码", "status=0x%08X" % (st or 0))
    r = hid_xfer(dev, ACT_GET_CFG)
    check(r and bytes(r[4:36]) == bytes(cfg), "GET_CFG 回读与写入一致")

    # --- DMA 无泄漏：16 轮 SET_CFG（每轮 sb_spi_hw_init → sb_dma_init）---
    for _ in range(16):
        r = hid_xfer(dev, ACT_SET_CFG, bytes(cfg))
        if r is None:
            check(False, "16 轮 SET_CFG 中途中断（无响应）")
            return 1
    _, stats = status_word(dev)
    check(True, "16 轮 SET_CFG 完成（修复前每轮泄漏 1 个 DMA 通道）")

    # --- bulk 帧执行 + 应答配对 + DMA 计数 ---
    ud, intf, ep_out, ep_in = find_bulk()
    try:
        if ud.is_kernel_driver_active(intf):
            ud.detach_kernel_driver(intf)
    except Exception:
        pass
    usb.util.claim_interface(ud, intf)

    time.sleep(0.3)                       # 给主循环落 hw_req
    seq = 1
    tx = bytes((i * 7 + 3) & 0xFF for i in range(480))
    ud.write(ep_out, frame(T_XFER, xfer_payload(tx), F_RSP | F_FORCE_DMA, seq), 1000)
    blob = b""
    t0 = time.time()
    while time.time() - t0 < 2.0:
        try:
            blob += bytes(ep_in.read(512, timeout=200))
        except usb.core.USBTimeoutError:
            continue
        except Exception:
            break
        if len(blob) >= 8:
            break
    ok = False
    if len(blob) >= 8:
        magic, rtype, stc, rseq, _ = parse_rsp(blob[:512])
        ok = (magic == SB_MAGIC and rtype == 0x81 and stc == 0 and rseq == seq)
    check(ok, "只写 XFER 帧执行成功（RSP seq 配对）",
          "rsp=%s" % (blob[:8].hex() if blob else "无"))
    st, stats = status_word(dev)
    check(st is not None and ((st >> 8) & 0xFF) == 0, "执行后无错误码", "status=0x%08X" % (st or 0))
    # ⚠️ STATUS 线序（10 × u32）：frames_ok, bytes_tx, bytes_rx, tx_poll, tx_dma,
    #    overrun, drop, actual_sclk, frames_err, last_ticks —— frames_err 在最后第二个，
    #    不按 sb_stats_t 的字段顺序（见 spi_bridge_proto.h 的说明）。
    check(stats is not None and stats[4] >= 1,
          "tx_dma_cnt=%d —— 16 轮重配后 DMA 通道仍可用（无泄漏）" % (stats[4] if stats else -1))
    check(stats is not None and stats[0] >= 1, "frames_ok=%d" % (stats[0] if stats else -1))
    check(stats is not None and stats[7] == 20000000,
          "actual_sclk=%d（与 SET_CFG 一致）" % (stats[7] if stats else -1))

    # --- PING 测往返（带 RSP） ---
    seq += 1
    ud.write(ep_out, frame(T_PING, b"", F_RSP, seq), 1000)
    blob = b""
    t0 = time.time()
    while time.time() - t0 < 1.0:
        try:
            blob += bytes(ep_in.read(512, timeout=200))
        except usb.core.USBTimeoutError:
            continue
        except Exception:
            break
        if len(blob) >= 8:
            break
    ok = False
    if len(blob) >= 8:
        magic, rtype, stc, rseq, _ = parse_rsp(blob[:512])
        ok = (magic == SB_MAGIC and rtype == 0x81 and stc == 0 and rseq == seq)
    check(ok, "PING 往返正常")

    # --- ABORT 清 IN 队列（P3：现在 OUT/IN 两侧一起清） ---
    r = hid_xfer(dev, ACT_ABORT)
    check(r is not None, "ABORT 应答")
    time.sleep(0.1)
    st, _ = status_word(dev)
    check(st is not None and not (st & 0x08), "ABORT 后 IN 侧不处于流控（队列已清）")

    # --- DISABLE ---
    r = hid_xfer(dev, ACT_ENABLE, [0])
    st, _ = status_word(dev)
    check(st is not None and not (st & 0x01), "DISABLE 后 ENABLED=0")
    check(alive(dev), "收尾：探针应答正常")

    usb.util.dispose_resources(ud)
    print("\n%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项：%s" % (len(FAILS), FAILS)))
    return 0 if not FAILS else 1


def alive(dev):
    r = hid_xfer(dev, 0x31, [2])          # CMD_RTT status：借另一条命令验整机活着
    return r is not None and len(r) >= 10


if __name__ == "__main__":
    watchdog(90)
    sys.exit(main())
