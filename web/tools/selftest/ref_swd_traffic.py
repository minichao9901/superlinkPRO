#!/usr/bin/env python3
"""参考实现（pyusb）持续流量发生器：给逻辑分析仪抓波形 + 对照"正确"的响应。

按本工作区验证过的 tools\\cmsis_dap_raw.py 的顺序初始化，然后反复发两类传输：
  ① DP SELECT 写（地址 0x08，就是浏览器那边报 NO ACK 的那一笔）
  ② DP IDCODE 读（0x00）
每笔都打印响应，好直接看出参考实现拿到的是 ACK 还是 NO ACK。

用法： python ref_swd_traffic.py [持续秒数，默认 4]
"""
import sys
import os
import time

import libusb_package
import usb.core
import usb.util

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

VID, PID = 0x0D28, 0x0202          # MicroLink CMSIS-DAP
PKT = 512

ID_DAP_Connect = 0x02
ID_DAP_TransferConfigure = 0x04
ID_DAP_Transfer = 0x05
ID_DAP_SWJ_Clock = 0x11
ID_DAP_SWJ_Sequence = 0x12
ID_DAP_SWD_Configure = 0x13

# SWD 激活序列（88 位）：JTAG-to-SWD(0xE79E) + 线复位(64 个 1) + 空闲(8 个 0)
SWD_ACTIVATION = [0x9E, 0xE7] + [0xFF] * 8 + [0x00]
SWD_ACTIVATION_BITS = 88

ACK = {1: "OK", 2: "WAIT", 4: "FAULT", 7: "NO ACK"}


def le(v):
    return [v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF]


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
    if dev is None:
        return print(f"找不到探针 {VID:04X}:{PID:04X}") or 1
    cfg = dev.get_active_configuration()
    intf = cfg[(0, 0)]
    ep_out = usb.util.find_descriptor(intf, custom_match=lambda e:
                                      usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
    ep_in = usb.util.find_descriptor(intf, custom_match=lambda e:
                                     usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
    try:
        usb.util.claim_interface(dev, 0)
    except usb.core.USBError as e:
        print(f"认领接口失败（别的程序占着？）：{e}")
        return 1

    def xfer(payload):
        buf = bytes(payload) + b"\x00" * (PKT - len(payload))
        ep_out.write(buf, timeout=2000)
        return bytes(ep_in.read(PKT, timeout=2000))

    def show(label, r):
        print(f"  {label:<28} -> {' '.join(f'{b:02X}' for b in r[:12])}", flush=True)

    # ---- 初始化（照 cmsis_dap_raw.py）----
    show("Connect(SWD)", xfer([ID_DAP_Connect, 0x01]))
    show("SWJ_Clock(1MHz)", xfer([ID_DAP_SWJ_Clock] + le(1_000_000)))
    show("SWD_Configure(0)", xfer([ID_DAP_SWD_Configure, 0x00]))
    show("SWJ_Sequence(88 位)", xfer([ID_DAP_SWJ_Sequence, SWD_ACTIVATION_BITS] + SWD_ACTIVATION))
    show("TransferConfigure", xfer([ID_DAP_TransferConfigure, 0x00, 0xE8, 0x03, 0x00, 0x00]))

    def read_idcode():
        return xfer([ID_DAP_Transfer, 0x00, 0x01, 0x02, 0x00, 0x00, 0x00, 0x00])

    def write_select0():
        # DP 写 SELECT = 0（APnDP=0, RnW=0, A[3:2]=10 -> 请求字节 0x08）
        return xfer([ID_DAP_Transfer, 0x00, 0x01, 0x08, 0x00, 0x00, 0x00, 0x00])

    r = read_idcode()
    show("读 DP IDCODE", r)
    print(f"      ACK = {ACK.get(r[2] & 0x07, r[2] & 0x07)}", flush=True)

    r = write_select0()
    show("写 DP SELECT=0", r)
    print(f"      ACK = {ACK.get(r[2] & 0x07, r[2] & 0x07)}   ← 浏览器那边就是这笔报 NO ACK", flush=True)

    print(f"\n持续发流量 {secs} 秒（给 LA 抓波形）...", flush=True)
    act_only = os.environ.get("ACT_ONLY") == "1"
    if act_only:
        print("（ACT_ONLY=1：只刷激活序列）", flush=True)
    t0 = time.time()
    n = 0
    while time.time() - t0 < secs:
        if act_only:
            xfer([ID_DAP_Connect, 0x01])
            xfer([ID_DAP_SWD_Configure, 0x00])
            xfer([ID_DAP_SWJ_Sequence, SWD_ACTIVATION_BITS] + SWD_ACTIVATION)
        else:
            write_select0()
            read_idcode()
        n += 1
        time.sleep(0.002)
    print(f"发了 {n} 轮，结束", flush=True)

    r = write_select0()
    print(f"收尾再写 SELECT -> {' '.join(f'{b:02X}' for b in r[:8])}  ACK={ACK.get(r[2] & 0x07, r[2] & 0x07)}")
    r = read_idcode()
    print(f"收尾再读 IDCODE -> {' '.join(f'{b:02X}' for b in r[:8])}  ACK={ACK.get(r[2] & 0x07, r[2] & 0x07)}")
    usb.util.release_interface(dev, 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
