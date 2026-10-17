#!/usr/bin/env python3
"""探针/链路一键体检：**先分清楚是"探针卡了"还是"目标没应答"**，再谈修代码。

这一晚上的排查教训（H7B0 与 F103 各踩一次）：
  · 「DP 读得到、AP 全 WAIT/FAULT」= 目标内部时钟域停摆（多半是被写 PWR_CR3 之类的操作弄的）
  · 「IDCODE 都读不到、ACK=0/7」        = 探针到目标之间的物理链路（供电 / 接线 / 共地）
  · 「探针命令都不回包」               = USB 队列卡了：换浏览器/工具没用，拔插探针
  · 想判定一条链路好不好，**先看 OpenOCD**：它跟页面代码无关，是干净的对照组

用法：
  python tools\\dev\\probe-triage.py                       # 扫描所有 CMSIS-DAP 探针并读 IDCODE
  python tools\\dev\\probe-triage.py --ocd target/stm32f1x.cfg   # 顺带用 OpenOCD 做对照
"""
import argparse, os, subprocess, sys, time

VENV_PY = os.path.expandvars(r"%USERPROFILE%\.venvs\pyocd\Scripts\python.exe")
OCD = r"E:\Share\env-windows\xpack-openocd-0.12.0-6\bin\openocd.exe"
OCD_SCRIPTS = r"E:\Share\env-windows\xpack-openocd-0.12.0-6\openocd\scripts"
PKT = 512
SWD_ACT = [0x9E, 0xE7] + [0xFF] * 8 + [0x00]
ACKS = {0: "0（目标没驱动 SWDIO / 探针没执行）", 1: "OK", 2: "WAIT", 4: "FAULT", 7: "NO ACK"}


def ensure_pyusb():
    try:
        import usb.core, libusb_package  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get("_DAP_REEXEC") == "1" or not os.path.exists(VENV_PY):
        sys.exit(f"需要 pyusb + libusb-package：用 {VENV_PY} 跑本脚本")
    os.environ["_DAP_REEXEC"] = "1"
    raise SystemExit(subprocess.call([VENV_PY, os.path.abspath(__file__)] + sys.argv[1:]))


ensure_pyusb()
import usb.core, usb.util, libusb_package  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

le = lambda v: [v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF]  # noqa: E731


def find_daps():
    out = []
    for d in usb.core.find(find_all=True, backend=libusb_package.get_libusb1_backend()):
        try:
            cfg = d.get_active_configuration()
        except Exception:
            continue
        for i in cfg:
            eps = list(i.endpoints())
            if i.bInterfaceClass == 0xFF and len(eps) >= 2:
                out.append((d, i))
                break
    return out


def probe_one(dev, intf, khz):
    tag = f"{(dev.idVendor or 0):04X}:{(dev.idProduct or 0):04X} {dev.product or '?'}"
    try:
        usb.util.claim_interface(dev, intf.bInterfaceNumber)
    except usb.core.USBError as e:
        print(f"  {tag}\n    ✗ 接口被占用（{e}）—— OpenOCD/pyOCD/J-Link/另一个浏览器窗口还开着？")
        return
    ep_out = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
    ep_in = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)

    def xfer(p, timeout=1500):
        ep_out.write(bytes(p) + b"\x00" * (PKT - len(p)), timeout=timeout)
        return bytes(ep_in.read(PKT, timeout=timeout))

    drained = 0
    while True:
        try:
            if not bytes(ep_in.read(PKT, timeout=60)):
                break
        except usb.core.USBError:
            break
        drained += 1

    def idcode():
        xfer([0x02, 0x01]); xfer([0x11] + le(khz * 1000)); xfer([0x13, 0x00])
        xfer([0x12, 88] + SWD_ACT); xfer([0x04, 0x00, 0xE8, 0x03, 0x00, 0x00])
        r = xfer([0x05, 0x00, 0x01, 0x02])
        if len(r) < 3:
            return None, None
        ack = r[2] & 0x07
        val = int.from_bytes(r[3:7], "little") if ack == 1 and len(r) >= 7 else None
        return ack, val

    print(f"  {tag}  （接口 {intf.bInterfaceNumber}，排空时吃掉 {drained} 条陈旧响应）")
    ack, val = idcode()
    if val is not None:
        print(f"    ✓ DP IDCODE = 0x{val:08X} → SWD 链路通，问题在更高层（DP 上电 / AP / 页面逻辑）")
    else:
        print(f"    ✗ 读 IDCODE: ACK={ACKS.get(ack, ack)}")
        r = xfer([0x0A])
        if len(r) >= 2 and r[1] == 0:
            time.sleep(0.2)
            ack2, val2 = idcode()
            print(f"    · 试过 DAP_ResetTarget 后：" + (f"IDCODE=0x{val2:08X} ✓（刚才是复位按着目标）" if val2 else f"仍 ACK={ACKS.get(ack2, ack2)}"))
        print("    → 判据：探针命令都能回（上面这些都有回包）而 IDCODE 不通 = **目标侧**问题：")
        print("       ① 目标板有没有电  ② SWCLK/SWDIO/GND 是否插在**这块**板上、GND 是否共地  ③ 目标是否被按在复位")
    try:
        usb.util.release_interface(dev, intf.bInterfaceNumber)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--khz", type=int, default=1000)
    ap.add_argument("--ocd", default="", help="顺带用 OpenOCD 做对照，如 target/stm32f1x.cfg")
    args = ap.parse_args()

    daps = find_daps()
    print(f"=== 找到 {len(daps)} 个 CMSIS-DAP 类探针 ===")
    if not daps:
        print("  （一个都没有：探针没插？或者它的接口被别的程序独占了）")
    for dev, intf in daps:
        probe_one(dev, intf, args.khz)

    if args.ocd:
        print(f"\n=== OpenOCD 对照（{args.ocd}，与页面代码无关）===")
        if not os.path.exists(OCD):
            print("  （本机没找到 openocd）")
        else:
            r = subprocess.run([OCD, "-s", OCD_SCRIPTS, "-f", "interface/cmsis-dap.cfg",
                                "-c", "cmsis-dap backend usb_bulk", "-f", args.ocd,
                                "-c", f"adapter speed {args.khz}; init; exit"],
                               capture_output=True, text=True, timeout=90)
            for line in ((r.stdout or "") + (r.stderr or "")).splitlines():
                if any(k in line for k in ("Using CMSIS", "DPIDR", "Cortex-M", "Error", "stalled")):
                    print("  " + line.strip())


if __name__ == "__main__":
    main()
