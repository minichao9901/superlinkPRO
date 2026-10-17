"""HID 0x32 边界回归：**变量宽度非法时必须整包拒绝**（第三轮代码审查 P1）。

背景（修复前的越界写，静默）：
  `api_param.c` 把主机报文里的 size 字节原样收下，`scope_make_plan()` 用它直接生成
  span —— 宽度 0xFF 时 span 长度 256 B，而中转缓冲 `s_stage` 只有 SCOPE_SPAN_MAX+4
  = 68 B ⇒ 越界写 ~188 B；更狠的是"帧"本身：8 个宽变量的 frame_bytes 能到 2016 B，
  零拷贝直读落点被推到 512 B 包缓冲外面最多 ~1.5 KB。
  触发只要两条 HID 报文：CONFIG（nvars=1、size=0xFF）+ BENCH（action 8 不需要 START）。

修复后应当：每个非法宽度都回 **-6**（配置被拒）、变量表清空（nvars=0/spans=0）、
随后 START/BENCH 报 **-3**（没有计划 ⇒ 一次读都不发），探针照常应答；合法配置不受影响。

用法：python scope_cfg_bound_test.py [--no-speed]
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
ACT = {'STOP': 0, 'START': 1, 'STATUS': 2, 'CLOCK': 3, 'CONFIG': 7, 'BENCH': 8, 'BENCH_RESULT': 9}
RC_BADVAR = -6                          # 配置被拒：变量宽度非法
RC_NOVARS = -3                          # 变量表为空（被拒之后必然如此）

V_ONE = [("g_tick", 0x20001044, 4, 4)]          # 靶子 stm32f103_scope 的单个 u32
# 最大合法：8 个 f64 ⇒ 帧 64 B（SCOPE_PAYLOAD 496 装得下）
V_MAX = [("v%d" % i, 0x20001000 + i * 8, 8, 7) for i in range(8)]
# 非法宽度：0（下溢）、3/5/16/65（合法值之外）、0xFF（报告里的攻击值）
BAD_SIZES = [0x00, 0x03, 0x05, 0x10, 0x41, 0xFF]

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


def rc_of(r):
    if not r:
        return None
    return r[2] - 256 if r[2] > 127 else r[2]


def status(dev):
    r = xfer(dev, [ACT['STATUS']])
    if not r:
        return None
    w = [int.from_bytes(bytes(r[3 + i * 4:7 + i * 4]), "little") for i in range(12)]
    return {'startRc': rc_of(r), 'running': w[0] & 1, 'spans': (w[0] >> 8) & 0xFF,
            'swdReady': (w[0] >> 16) & 1, 'nvars': (w[0] >> 24) & 0xFF,
            'swdHz': w[1], 'produced': w[2], 'swdErr': w[5] & 0xFFFF}


def cfg(dev, period_us, vars_, flags=0):
    d = [ACT['CONFIG']] + list(struct.pack('<I', period_us)) + [flags & 0xFF, len(vars_)]
    for _, addr, size, typ in vars_:
        d += list(struct.pack('<I', addr)) + [size, typ]
    assert len(d) <= 61, "配置报文 %d B 超上限" % len(d)
    return xfer(dev, d)


def bench(dev, iters=2000):
    xfer(dev, [ACT['BENCH']] + list(struct.pack('<I', iters)))
    time.sleep(0.35 + iters / 40000.0)
    r = xfer(dev, [ACT['BENCH_RESULT']])
    if r is None:
        return None
    ticks = int.from_bytes(bytes(r[3:7]), "little")
    it = int.from_bytes(bytes(r[7:11]), "little")
    err = int.from_bytes(bytes(r[11:15]), "little", signed=True)
    us = (ticks / float(it)) / 24.0 if it else 0.0
    return {'iters': it, 'ticks': ticks, 'err': err, 'us': us}


def check(cond, note, detail=""):
    print("  %-34s %-5s %s" % (note, "[ok]" if cond else "[FAIL]", detail))
    if not cond:
        FAILS.append(note)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-speed", action="store_true", help="跳过合法配置的标定（没有靶子时）")
    a = ap.parse_args()

    dev = open_hid()
    st = status(dev)
    check(st is not None, "探针在（HID 0x32 有应答）", str(st))

    print("\n=== 1. 非法宽度必须整包拒绝（每一条都单独发、单独看返回码）===")
    for sz in BAD_SIZES:
        r = cfg(dev, 100, [("bad", 0x20001000, sz, 4)])
        check(rc_of(r) == RC_BADVAR, "size=0x%02X → rc=-6" % sz, "rc=%s" % rc_of(r))
        st = status(dev)
        if st is None:
            check(False, "拒绝之后探针仍应答（size=0x%02X）" % sz)
            break
        check(st['nvars'] == 0 and st['spans'] == 0,
              "  变量表被清空（size=0x%02X）" % sz,
              "nvars=%d spans=%d" % (st['nvars'], st['spans']))

    print("\n=== 2. 被拒之后不能偷偷采样：START / BENCH 都应报 -3 ===")
    # 注意比的是**增量**：produced 只在真正启动成功时清零（scope_start_now 的 -3 是
    # 提前返回），所以"上一轮的账"会留在状态字里，不能直接断言它等于 0。
    st0 = status(dev)
    xfer(dev, [ACT['START']])
    time.sleep(0.4)
    st = status(dev)
    check(st is not None and st['startRc'] == RC_NOVARS, "START → -3（变量表为空）",
          "startRc=%s running=%s" % (st and st['startRc'], st and st['running']))
    check(st is not None and st['running'] == 0, "没有真的跑起来")
    check(st is not None and st0 is not None and st['produced'] == st0['produced'],
          "一个样本都没采（越界写的前提是真的去读）",
          "produced %s -> %s" % (st0 and st0['produced'], st and st['produced']))

    b = bench(dev, 500)
    check(b is not None and b['err'] == RC_NOVARS, "BENCH → err=-3（没有排计划）",
          str(b))

    print("\n=== 3. 合法配置不受影响 ===")
    r = cfg(dev, 100, V_ONE)
    check(rc_of(r) == 0, "单 u32 配置被采纳", "rc=%s" % rc_of(r))
    st = status(dev)
    check(st is not None and st['nvars'] == 1 and st['spans'] == 1,
          "nvars=1 spans=1", "nvars=%s spans=%s" % (st and st['nvars'], st and st['spans']))

    r = cfg(dev, 100, V_MAX)
    check(rc_of(r) == 0, "8 × f64（帧 64 B，最大合法）被采纳", "rc=%s" % rc_of(r))
    st = status(dev)
    check(st is not None and st['nvars'] == 8, "nvars=8", "nvars=%s spans=%s" % (st and st['nvars'], st and st['spans']))

    r = cfg(dev, 100, V_ONE)
    check(rc_of(r) == 0, "切回单 u32 仍被采纳", "rc=%s" % rc_of(r))

    if not a.no_speed:
        print("\n=== 4. 速度基线（合法配置 + 标定，确认加固没碰热路径）===")
        xfer(dev, [ACT['CLOCK']] + list(struct.pack('<I', 60000000)))
        xfer(dev, [ACT['STOP']])
        time.sleep(0.2)
        b = bench(dev, 2000)
        if b is None:
            check(False, "标定结果有回")
        elif b['err'] != 0:
            print("  (链路不可用 err=%d —— 没有靶子/未接 SWD，跳过速度对比；"
                  "拒绝路径本身已经验完)" % b['err'])
        else:
            # 基线：单 u32 @60 MHz = 1.588 µs/样本（README「单字快路径」；80% floor = 1.99 µs）
            check(b['us'] > 0 and b['us'] < 1.99,
                  "单 u32 标定 ≤ 1.99 µs/样本（基线 1.588）",
                  "%.3f µs/样本 → %.1f kHz" % (b['us'], 1000.0 / b['us']))

    st = status(dev)
    check(st is not None, "收尾：探针仍在线", str(st))

    print("\n%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项：%s" % (len(FAILS), FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    watchdog(90)
    sys.exit(main())
