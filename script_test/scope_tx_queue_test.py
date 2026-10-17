"""bulk IN 0x83 发送队列回归：**推出多少就必须收到多少**（二轮审查 N1 / 本轮修复）。

背景（修复前的静默故障）：
  DWC2 端点**只认一笔在飞传输**，而移植层把底层 `usb_device_edpt_xfer()` 的 bool
  丢掉了（`usb_dc_hpm.c:254`）—— 端点忙时 `usbd_ep_start_write` 照样返回 0。于是
  "后推的那一包"其实没发出去：没有完成回调，它的包缓冲永远回不来。
  实测（修复前，8 通道 200 µs 档 3 s）：推出 1005 包 / 主机收到 999 / 完成回调恰好
  也是 999 —— 差的 6 个全是 STAT，池子几秒内从 8 个缩到 2 个。
  最典型的受害者就是 STAT：它紧跟在 DATA 推包之后（间隔 <1 µs），必然撞上在飞。

  STAT 是主机侧"探针丢样本 / 排空不及 / 实际周期 / SWD 档位"的唯一来源（网页
  view.js 的 `case P.KIND.STAT`），它到不了 = 面板上这几个数永远是死的。

修复：固件自己排队（同一时刻只允许 1 笔在飞，其余按 seq 躺在环里，完成回调接着踢
下一包），见 scope_sampler.c 的 scope_tx_kick()/scope_tx_enqueue()。

判据（三条必须同时成立，缺一条就是又漏了）：
  · 收到的包数 == 探针报的 seq（推出多少收多少）；
  · txDone 的增量 == 收到的包数（每一笔都真的完成过 ⇒ 没有缓冲被占死）；
  · STAT 数 == floor(seq / 64)（±1，窗口边界）且 seq 无缺口。

用法：python scope_tx_queue_test.py [--period 200] [--secs 3] [--base 0x20001000]
      （需要探针接着 SWD 靶子；8 个连续 u32 = 每包 15 样本，包率足够高）
"""
import argparse
import os
import struct
import sys
import threading
import time

import hid
import usb.core

try:                                    # Windows 控制台默认 GBK，中文/符号会直接抛异常
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

VID, PID = 0x0D28, 0x0204
HID_CMD = 0x32
ACT = {'STOP': 0, 'START': 1, 'STATUS': 2, 'CLOCK': 3, 'CONFIG': 7}
KIND = {1: 'DEF', 2: 'DATA', 3: 'STAT', 4: 'EVT'}
MAGIC = 0x4A53
PACKET = 512
STAT_EVERY = 64

FAILS = []


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT")
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


def xfer(dev, data, tmo=3.0):
    req = [0x01, 2 + len(data), HID_CMD] + list(data)
    req += [0] * (64 - len(req))
    dev.write(req)
    t0 = time.time()
    while time.time() - t0 < tmo:
        r = dev.read(64, timeout_ms=200)
        if r and r[0] == 0x02 and r[2] == HID_CMD:
            return list(r)[1:]
    return None


def status(dev):
    r = xfer(dev, [ACT['STATUS']])
    if not r:
        return None
    w = [int.from_bytes(bytes(r[3 + i * 4:7 + i * 4]), "little") for i in range(12)]
    return {'startRc': r[2] - 256 if r[2] > 127 else r[2], 'running': w[0] & 1,
            'spans': (w[0] >> 8) & 0xFF, 'nvars': (w[0] >> 24) & 0xFF,
            'produced': w[2], 'dropped': w[3], 'usbDrop': (w[4] >> 16) & 0xFFFF,
            'swdErr': w[5] & 0xFFFF, 'seq': w[6], 'txDone': (w[9] >> 16) & 0xFFFF}


def cfg(dev, period_us, vars_, flags=0):
    d = [ACT['CONFIG']] + list(struct.pack('<I', period_us)) + [flags & 0xFF, len(vars_)]
    for _, addr, size, typ in vars_:
        d += list(struct.pack('<I', addr)) + [size, typ]
    return xfer(dev, d)


def check(cond, note, detail=""):
    print("  %-32s %-5s %s" % (note, "[ok]" if cond else "[FAIL]", detail))
    if not cond:
        FAILS.append(note)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", type=int, default=200, help="采样周期 us（默认 200）")
    ap.add_argument("--secs", type=float, default=3.0)
    ap.add_argument("--base", type=lambda s: int(s, 0), default=0x20001000,
                    help="8 个连续 u32 的基址（默认 STM32F103 靶子的 RAM）")
    a = ap.parse_args()

    vars_ = [("v%d" % i, a.base + i * 4, 4, 4) for i in range(8)]

    dev = open_hid()
    ud = usb.core.find(idVendor=VID, idProduct=PID)
    if ud is None:
        print("找不到 USB 设备")
        return 1
    ud.set_configuration()

    xfer(dev, [ACT['STOP']])
    xfer(dev, [ACT['CLOCK']] + list(struct.pack('<I', 60000000)))
    cfg(dev, a.period, vars_, flags=0x20)      # CDC 桥让开：少几百周期/轮
    st0 = status(dev)
    check(st0 is not None and st0['nvars'] == 8 and st0['spans'] >= 1,
          "8 通道配置就绪", "nvars=%s spans=%s" % (st0 and st0['nvars'], st0 and st0['spans']))

    # 🚨 读线程必须在 START **之前**起来：启动后那段没人读的时间会整段算成 dropped
    pkts = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                b = bytes(ud.read(0x83, 16384, timeout=200))
            except usb.core.USBError:
                continue
            for o in range(0, len(b) - PACKET + 1, PACKET):
                if int.from_bytes(b[o:o + 2], 'little') != MAGIC:
                    continue
                pkts.append((b[o + 3], int.from_bytes(b[o + 4:o + 8], 'little')))

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    time.sleep(0.2)
    xfer(dev, [ACT['START']])
    time.sleep(a.secs)
    xfer(dev, [ACT['STOP']])
    time.sleep(0.4)
    stop.set()
    th.join(timeout=1.0)
    try:
        usb.util.dispose_resources(ud)
    except Exception:
        pass

    st = status(dev)
    if st is None:
        check(False, "探测结束还能读状态")
        print("\n失败 %d 项：%s" % (len(FAILS), FAILS))
        return 1
    if st0 is not None and st0['startRc'] != 0 and st['produced'] == (st0['produced'] if st0 else 0):
        print("  (没有采到样本：startRc=%s —— 探针没接 SWD 靶子？)" % st['startRc'])

    kinds = {}
    for k, _ in pkts:
        kinds[KIND.get(k, k)] = kinds.get(KIND.get(k, k), 0) + 1
    seqs = sorted(s for _, s in pkts)
    gaps = 0
    prev = None
    for s in seqs:
        if prev is not None and s != prev + 1:
            gaps += s - prev - 1
        prev = s
    stat_exp = st['seq'] // STAT_EVERY
    tx_delta = (st['txDone'] - st0['txDone']) & 0xFFFF if st0 else st['txDone']

    print("\n周期 %d us / %.1f s：收到 %d 包 %s" % (a.period, a.secs, len(pkts), kinds))
    print("探针 seq=%d  produced=%d dropped=%d usbDrop=%d txDone 增量=%d"
          % (st['seq'], st['produced'], st['dropped'], st['usbDrop'], tx_delta))

    print("\n=== 判据 ===")
    check(len(pkts) > 100, "真的采到了（收到包数 > 100）", "收到 %d" % len(pkts))
    check(kinds.get('DEF', 0) == 1, "DEF 起跑线到了", "DEF=%d" % kinds.get('DEF', 0))
    check(len(pkts) == st['seq'], "推出多少收到多少（修复前会少）",
          "推 %d / 收 %d" % (st['seq'], len(pkts)))
    check(tx_delta == len(pkts), "完成回调数 == 收到包数（没有缓冲被占死）",
          "txDone+%d vs 收 %d" % (tx_delta, len(pkts)))
    check(gaps == 0, "seq 无缺口", "缺口 %d" % gaps)
    got_stat = kinds.get('STAT', 0)
    check(abs(got_stat - stat_exp) <= 1, "STAT 按每 %d 包一个到齐（修复前一个都到不了）" % STAT_EVERY,
          "实收 %d / 应有 %d" % (got_stat, stat_exp))
    check(st['usbDrop'] == 0, "低速率档不该有排空不及", "usbDrop=%d" % st['usbDrop'])

    print("\n%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项：%s" % (len(FAILS), FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    watchdog(90)
    sys.exit(main())
