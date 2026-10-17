#!/usr/bin/env python3
"""swd_min_seq.py —— 固定顺序的 SWD 最小序列，**每步都打印原始响应字节**，失败也继续。

和 `cmsis_dap_raw.py` 的区别：那个脚本一发现"读 IDCODE 没 ACK"就 `return 2` 退出，
于是逻辑分析仪上后面**什么都没有** —— 分不清"写没发时钟"还是"脚本没走到写"。
本脚本永远把整套序列跑完，配合逻辑分析仪抓波，两边对账。

序列（每一步之间只隔一次 USB 往返，没有 sleep）：
  1 Connect(SWD)          4 SWJ_Sequence(88 位激活)
  2 SWJ_Clock(1MHz)       5 读 DP IDCODE
  3 SWD_Configure         6 写 AP TAR
                          7 读 AP DRW
                          8 写 AP DRW
                          9 读 AP DRW
用法：python tools\\swd_min_seq.py [--addr 0x20000000] [--clock 1000000] [--loop 0]
     --loop N>0：激活只做一次，然后"读 DRW + 写 DRW"重复 N 轮（给 LA 自由采集用）
"""
import argparse
import os
import subprocess
import sys

VENV_PY = os.path.expandvars(r"%USERPROFILE%\.venvs\pyocd\Scripts\python.exe")


def ensure_pyusb() -> None:
    try:
        import usb.core  # noqa: F401
        import libusb_package  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get("_DAP_REEXEC") == "1" or not os.path.exists(VENV_PY):
        sys.exit("需要 pyusb + libusb-package，请用 %s 跑" % VENV_PY)
    os.environ["_DAP_REEXEC"] = "1"
    print("[i] 换成 venv 解释器重跑：%s" % VENV_PY, flush=True)
    raise SystemExit(subprocess.call([VENV_PY, os.path.abspath(__file__)] + sys.argv[1:]))


ensure_pyusb()

import usb.core          # noqa: E402
import usb.util          # noqa: E402
import libusb_package    # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

VID, PID, PKT = 0x0D28, 0x0204, 512
CMD = {"Connect": 0x02, "Disconnect": 0x03, "TransferConfigure": 0x04,
       "Transfer": 0x05, "TransferBlock": 0x06, "SWJ_Clock": 0x11,
       "SWJ_Sequence": 0x12, "SWD_Configure": 0x13}
SWD_ACTIVATION = [0x9E, 0xE7] + [0xFF] * 8 + [0x00]
ACK_NAME = {0: "NO-DATA", 1: "OK", 2: "WAIT", 4: "FAULT", 7: "NO-ACK(无响应)"}


def hexdump(d: bytes) -> str:
    return f"[{len(d):3d}] " + " ".join(f"{b:02X}" for b in d[:24])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", type=lambda s: int(s, 0), default=0x20000000)
    ap.add_argument("--clock", type=int, default=1_000_000)
    ap.add_argument("--loop", type=int, default=0, help=">0：重复 N 轮读+写")
    ap.add_argument("--vid", type=lambda s: int(s, 0), default=VID,
                    help="探针 VID（默认 0D28=DAPLink 系；MicroLink 0202 / cherrydap 0204）")
    ap.add_argument("--pid", type=lambda s: int(s, 0), default=PID, help="探针 PID")
    args = ap.parse_args()

    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=args.vid, idProduct=args.pid, backend=backend)
    if dev is None:
        return print(f"找不到探针 {args.vid:04X}:{args.pid:04X}") or 1
    cfg = dev.get_active_configuration()
    intf = cfg[(0, 0)]
    ep_out = usb.util.find_descriptor(intf, custom_match=lambda e:
                                      usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
    ep_in = usb.util.find_descriptor(intf, custom_match=lambda e:
                                     usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
    print(f"探针 {dev.manufacturer} / {dev.product}  接口0={usb.util.get_string(dev, intf.iInterface)!r}")

    def xfer(payload: list) -> bytes:
        buf = bytes(payload) + b"\x00" * (PKT - len(payload))
        ep_out.write(buf, timeout=2000)
        return bytes(ep_in.read(PKT, timeout=2000))

    # 排掉上一个会话的残留响应
    while True:
        try:
            if not bytes(ep_in.read(PKT, timeout=120)):
                break
        except usb.core.USBTimeoutError:
            break

    le = lambda v: [v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF]   # noqa: E731
    n = 0

    def step(name: str, payload: list, show_data: bool = False) -> bytes:
        nonlocal n
        n += 1
        r = xfer(payload)
        line = f"  {n}. {name:<26} -> {hexdump(r)}"
        if len(r) >= 3 and payload[0] == CMD["Transfer"]:
            ack = r[2] & 0x07
            line += f"   ACK={ack}({ACK_NAME.get(ack,'?')})"
        if show_data and len(r) >= 7:
            line += f"   数据=0x{int.from_bytes(r[3:7],'little'):08X}"
        print(line, flush=True)
        return r

    print("\n== 握手 + 激活 ==")
    step("Connect(SWD)", [CMD["Connect"], 0x01])
    step(f"SWJ_Clock({args.clock/1e6:g}MHz)", [CMD["SWJ_Clock"]] + le(args.clock))
    step("SWD_Configure", [CMD["SWD_Configure"], 0x00])
    step("SWJ_Sequence(88位激活)", [CMD["SWJ_Sequence"], 88] + SWD_ACTIVATION)
    step("TransferConfigure", [CMD["TransferConfigure"], 0x00, 0xE8, 0x03, 0x00, 0x00])

    print("\n== 传输 ==")
    a = args.addr
    rd_idcode = [0x02]
    wr_tar = [0x05] + le(a)
    rd_drw = [0x0F]
    wr_drw = [0x0D] + le(0x11223344)

    step("读 DP IDCODE", [CMD["Transfer"], 0x00, 0x01] + rd_idcode, True)
    step("写 AP TAR", [CMD["Transfer"], 0x00, 0x01] + wr_tar)
    step("读 AP DRW", [CMD["Transfer"], 0x00, 0x01] + rd_drw, True)
    step("写 AP DRW=11223344", [CMD["Transfer"], 0x00, 0x01] + wr_drw)
    step("读 AP DRW(回读)", [CMD["Transfer"], 0x00, 0x01] + rd_drw, True)

    if args.loop:
        print(f"\n== 自由采集模式：{args.loop} 轮 读+写 (TAR=0x{a:08X}) ==")
        import time
        t0 = time.time()
        okr = okw = 0
        for i in range(args.loop):
            r = xfer([CMD["Transfer"], 0x00, 0x02] + wr_tar + rd_drw)   # 写 TAR + 读 DRW
            if len(r) >= 3 and (r[2] & 0x07) == 1:
                okr += 1
            if i % 200 == 199:
                print(f"    {i+1} 轮，用时 {time.time()-t0:.2f}s", flush=True)
        print(f"  完成 {args.loop} 轮：ACK=OK {okr}")
        _ = okw

    step("Disconnect", [CMD["Disconnect"]])
    print("\n(全部步骤都发完了 —— 逻辑分析仪上应当看到 1 段激活 + 5 笔传输)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
