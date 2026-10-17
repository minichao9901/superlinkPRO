#!/usr/bin/env python3
"""抢在探针启动窗口里进 bootloader（两条通道同时打）。

背景：feature 固件失败时是"设备枚举出来了但 class 端点全哑"——
HID 报文写出去没有 ACK，DFU_DETACH 也可能被 STALL。能用的窗口只有
上电后头几百毫秒，所以这个脚本**守着设备的消失→重现**，一出现就猛敲：

  1) EP0 的 DFU_DETACH（控制传输，走 class 请求 → dfu_runtime_handler）
  2) HID CMD_ENTER_DFU (0xFF)（走 OUT 端点 → api_param_proc_hid）
  3) 顺带试 Windows 侧的 DFU 入口（可选）

成功判据：多出一个可移动盘（bootloader 的 MSC）。

用法：
  py script_test/probe_dfu_race.py            # 先等设备消失再重现（配合人手插拔）
  py script_test/probe_dfu_race.py --now      # 设备现在就在，直接开敲
  py script_test/probe_dfu_race.py --wait 300 # 最多等 300 s 插拔
"""
import argparse
import os
import subprocess
import sys
import threading
import time

VID, PID = 0x0D28, 0x0204


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT (%ss)" % sec)
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


def drives():
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-Volume | Where-Object { $_.DriveType -eq 'Removable' } | "
                          "Select-Object -ExpandProperty DriveLetter"],
                         capture_output=True, text=True, timeout=30)
    return [c for c in out.stdout.split() if c.strip()]


def usb_present():
    try:
        import usb.core
        return usb.core.find(idVendor=VID, idProduct=PID) is not None
    except Exception:
        return False


def hid_path():
    try:
        import hid
    except ImportError:
        return None
    for d in hid.enumerate(VID, PID):
        if d.get("usage_page") == 0xFF00:
            return d["path"]
    return None


def poke_ep0():
    """DFU_DETACH(接口 6)。设备重启时不会回响应，Pipe error 属预期。"""
    try:
        import usb.core
        import usb.util
        dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is None:
            return
        try:
            dev.ctrl_transfer(0x21, 0x00, 1000, 6, None, timeout=250)
        except Exception:
            pass
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass
    except Exception:
        pass


def poke_hid():
    path = hid_path()
    if path is None:
        return
    try:
        import hid
        h = hid.device()
        h.open_path(path)
        # 非阻塞写：只要端点还认，报文就出去了；有没有 ACK 这里不关心。
        h.set_nonblocking(1)
        h.write([0x01, 0x01, 0xFF] + [0] * 61)
        h.close()
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", action="store_true", help="设备现在就在，直接开始敲")
    ap.add_argument("--wait", type=float, default=300.0, help="等插拔的最长秒数")
    ap.add_argument("--poke", type=float, default=12.0, help="敲击持续的秒数")
    args = ap.parse_args()

    before = set(drives())
    print("插拔前的可移动盘: %s" % sorted(before))
    if before:
        print("已经有一个可移动盘了——bootloader 可能就在，直接看它是不是探针的。")

    if not args.now:
        print("等设备从总线上消失（请你现在拔掉探针）...")
        t0 = time.time()
        while time.time() - t0 < args.wait:
            if not usb_present():
                print("  设备已消失（%.1fs），等你插回来" % (time.time() - t0))
                break
            time.sleep(0.05)
        else:
            print("等超时：设备一直没消失")
            return 1
        # 等它重新出现
        t0 = time.time()
        while time.time() - t0 < args.wait:
            if usb_present():
                print("  设备回来了（%.3fs），立刻开敲" % (time.time() - t0))
                break
            time.sleep(0.02)
        else:
            print("等超时：设备没回来")
            return 1

    t0 = time.time()
    won = None
    while time.time() - t0 < args.poke:
        poke_ep0()
        poke_hid()
        now = set(drives()) - before
        if now:
            won = sorted(now)[0] + ":\\"
            print("  ★ 抢到了！DFU 盘 %.3fs -> %s" % (time.time() - t0, won))
            break
        time.sleep(0.05)

    if won is None:
        now = set(drives()) - before
        if now:
            won = sorted(now)[0] + ":\\"
    if won:
        print("DFU 卷: %s 内容: %s" % (won, os.listdir(won)))
        return 0
    print("没抢到：%.1fs 内既没进 bootloader，也没出现新可移动盘" % args.poke)
    return 2


if __name__ == "__main__":
    watchdog(900)
    sys.exit(main())
