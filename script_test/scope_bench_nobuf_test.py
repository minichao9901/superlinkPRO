"""HID 0x32 边界回归：**8 个包缓冲全在飞时请求标定**（代码审查报告 #4）。

复现的就是这条路径：主机停止读 bulk IN 0x83（网页被挂起、脚本卡住、只读不排空）
→ 8 个包缓冲全被占住 → 此时点「标定真实速率」（action 8）。

修复前 `scope_run_bench()` 在函数入口就 `&s_pkt[s_fill_buf][SCOPE_HDR]`，而
`s_fill_buf` 的合法值里有 0xFF 这个"没有缓冲"哨兵 —— 取到的是 `s_pkt[255]`，
一个越界 130 KB 的指针，随后按 `s_bench_iters`（上限 100000）持续往里写。
修复后应干净地回 `bench_err = -5`（没有空闲包缓冲），探针照常应答。

★ 拿到 -5 就同时证明了两件事：这个哨兵状态**真的会被走到**（所以越界写在修复前
  是真实可触发的），以及现在不再往里写。

用法：python scope_bench_nobuf_test.py
"""
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
ACT = {'STOP': 0, 'START': 1, 'STATUS': 2, 'CONFIG': 7, 'BENCH': 8, 'BENCH_RESULT': 9}
NO_FREE_BUF = -5

V_ONE = [("g_tick", 0x20001044, 4, 4)]     # 靶子 stm32f103_scope 的单个 u32

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
    # 字 4 = (已推字节数 & 0xFFFF) | ((usbDrop & 0xFFFF) << 16)，见 scope_sampler_status
    return {'startRc': r[2] - 256 if r[2] > 127 else r[2], 'running': w[0] & 1,
            'produced': w[2], 'dropped': w[3], 'usbDrop': (w[4] >> 16) & 0xFFFF,
            'txDone': (w[9] >> 16) & 0xFFFF, 'swdErr': w[5] & 0xFFFF}


def drain(seconds=1.5):
    """把 bulk IN 0x83 上排队的包读走 —— 只有完成回调回来，包缓冲才会还给采样器。"""
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        return 0
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass
    got, t0 = 0, time.time()
    while time.time() - t0 < seconds:
        try:
            got += len(dev.read(0x83, 8192, timeout=500))
        except usb.core.USBError:
            pass
    return got


def check(cond, note, detail=""):
    print("  %-30s %-5s %s" % (note, "[ok]" if cond else "[FAIL]", detail))
    if not cond:
        FAILS.append(note)


def main():
    dev = open_hid()
    print("=== 1. 配置 + 启动（周期 2 us，单 u32，**不读 0x83**）===")
    d = [ACT['CONFIG']] + list(struct.pack('<I', 2)) + [0, len(V_ONE)]
    for _, addr, size, typ in V_ONE:
        d += list(struct.pack('<I', addr)) + [size, typ]
    check(xfer(dev, d) is not None, "配置报文有回")
    xfer(dev, [ACT['START']])
    time.sleep(0.5)                     # 8 个 512 B 包 ≈ 几 ms 就填满，这里给足余量

    st = status(dev)
    check(st is not None, "状态可读", str(st))
    check(st and st['running'] == 1, "采样在跑")
    check(st and st['usbDrop'] > 0, "包缓冲已被占满（usbDrop 在涨）",
          "usbDrop=%s" % (st['usbDrop'] if st else "?"))

    print("\n=== 2. 此时请求标定（修复前就是这一刻开始越界写）===")
    xfer(dev, [ACT['BENCH']] + list(struct.pack('<I', 2000)))
    time.sleep(0.5)
    r = xfer(dev, [ACT['BENCH_RESULT']])
    ok = r is not None
    check(ok, "标定结果有回（探针没死）")
    if ok:
        ticks = int.from_bytes(bytes(r[3:7]), "little")
        iters = int.from_bytes(bytes(r[7:11]), "little")
        err = int.from_bytes(bytes(r[11:15]), "little", signed=True)
        check(err == NO_FREE_BUF, "bench_err = -5（没有空闲包缓冲）",
              "err=%d iters=%d ticks=%d" % (err, iters, ticks))

    st = status(dev)
    check(st is not None and st['running'] == 1, "标定之后采样仍在跑", str(st))

    print("\n=== 3. 收尾：主机把包读走之后，标定应该能正常跑 ===")
    print("    （不读 0x83 的话那 8 个缓冲会永远卡在“在飞”，所以先排空 1.5 s）")
    xfer(dev, [ACT['STOP']])
    time.sleep(0.2)
    got = drain(1.5)
    print("    排空 %d B" % got)
    xfer(dev, [ACT['BENCH']] + list(struct.pack('<I', 2000)))
    time.sleep(0.5)
    r = xfer(dev, [ACT['BENCH_RESULT']])
    if r is not None:
        err = int.from_bytes(bytes(r[11:15]), "little", signed=True)
        ticks = int.from_bytes(bytes(r[3:7]), "little")
        us = (ticks / 2000.0) / 24.0 if ticks else 0
        check(err == 0, "空闲后标定成功", "err=%d  %.3f us/样本" % (err, us))
    else:
        check(False, "空闲后标定结果有回")

    print("\n%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项：%s" % (len(FAILS), FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    watchdog(45)
    sys.exit(main())
