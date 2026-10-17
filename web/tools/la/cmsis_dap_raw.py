#!/usr/bin/env python3
"""cmsis_dap_raw.py —— 协议级抓包：直接发 CMSIS-DAP v2 命令，dump **原始响应字节**

用途：判断"批量读串位"到底是谁的错 —— 主机软件（pyOCD）还是探针固件。
不看任何客户端代码，只按 CMSIS-DAP 规范发包、把回来的字节原样打出来，并自动比对期望长度。

⚠️ 命令号一定从固件里的 ARM 原版 DAP.h 抄（本脚本上一版把 DAP_Transfer 记成 0x0E，
   结果固件回了一个 ID_DAP_Invalid=0xFF —— 说明它拒绝得对，是我发错了）：
   0x02 Connect / 0x04 TransferConfigure / 0x05 Transfer / 0x06 TransferBlock

规范（CMSIS-DAP v2.x）：
  DAP_Transfer 请求: [0x05, DAP索引, 传输条数, 传输请求1, [数据1], 传输请求2, [数据2], ...]
               数据**只在写传输**里有（4 字节）
  DAP_Transfer 响应: [0x05, 传输条数, 响应字节] + 每个**读**传输 4 字节数据
               ⚠️ 响应字节只有**一个**（整条命令一个，ARM 参考实现 DAP.c 的 response_head[1]），
                  不是每条传输一个 —— 我第一次就是按"每传输一个"算的，得出了错误的"期望长度"。
  → 「写 TAR + 读 DRW」= 3 + 4 = **7 字节**；「2 写 2 读」= 3 + 8 = **11 字节**

传输请求字节：bit0=APnDP、bit1=RnW、bit2..3=地址(A2,A3)
  写 DP SELECT(0x8)=0x08  写 AP CSW(0x0)=0x01  写 AP TAR(0x4)=0x05  读 AP DRW(0xC)=0x1F

用法：python tools\\cmsis_dap_raw.py [--addr 0xE0042000]
     （用哪个 python 都行：脚本发现当前解释器没有 pyusb 会自动换成 venv 的解释器重跑）
"""
import argparse
import os
import subprocess
import sys

VENV_PY = os.path.expandvars(r"%USERPROFILE%\.venvs\pyocd\Scripts\python.exe")


def ensure_pyusb() -> None:
    """pyusb + libusb-package 在那个独立 venv 里（见 AGENTS §4.11），自动换过去重跑，
    这样用哪个 python 跑本脚本都行。"""
    try:
        import usb.core  # noqa: F401
        import libusb_package  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get("_DAP_REEXEC") == "1" or not os.path.exists(VENV_PY):
        sys.exit("需要 pyusb + libusb-package。用 %s 跑本脚本（见 AGENTS.md §4.11）。" % VENV_PY)
    os.environ["_DAP_REEXEC"] = "1"
    print("[i] 当前解释器没有 pyusb，自动换成 %s 重跑" % VENV_PY, flush=True)
    raise SystemExit(subprocess.call([VENV_PY, os.path.abspath(__file__)] + sys.argv[1:]))


ensure_pyusb()

try:
    import usb.core
    import usb.util
    import libusb_package
except ImportError:
    sys.exit("需要 pyusb + libusb-package（本机路径：%s）" % VENV_PY)

# Windows 控制台默认 GBK，输出里只要有编不出的字符就 UnicodeEncodeError 崩
# （本工作区为这个症状白查过好几次，见 AGENTS §4.9）——强制 UTF-8 + replace。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

VID, PID = 0x0D28, 0x0204
PKT = 512          # 固件 DAP_PACKET_SIZE，bulk 端点 wMaxPacketSize 也是 512

ID_DAP_Connect = 0x02
ID_DAP_TransferConfigure = 0x04
ID_DAP_Transfer = 0x05
ID_DAP_TransferBlock = 0x06
ID_DAP_SWJ_Clock = 0x11
ID_DAP_SWJ_Sequence = 0x12
ID_DAP_SWD_Configure = 0x13

# SWD 激活序列（88 位）：JTAG-to-SWD 切换(0xE79E) + 线复位(64 个 1) + 空闲(8 个 0)
# 🚨 少了它，SWJ-DP 还停在 JTAG 模式 → 传输返回 NO ACK(0x07)：
#    本脚本第一版就是这样，害我以为"固件不应答"（其实是我不合规）。
#    pyOCD 每次连接都会发这个序列，所以它一直好用。
SWD_ACTIVATION = [0x9E, 0xE7] + [0xFF] * 8 + [0x00]
SWD_ACTIVATION_BITS = 88

FAILS = []


def hexdump(data: bytes) -> str:
    body = " ".join(f"{b:02X}" for b in data[:40])
    return f"[{len(data):3d}] {body}" + (" ..." if len(data) > 40 else "")


def check(name: str, got: bytes, exp_len: int, exp_head: bytes = b"") -> bool:
    ok_len = len(got) == exp_len
    ok_head = got.startswith(exp_head) if exp_head else True
    verdict = "PASS" if (ok_len and ok_head) else "FAIL"
    if verdict == "FAIL":
        FAILS.append(name)
    note = ""
    if not ok_len:
        note = f"   期望 {exp_len} 字节，实际 {len(got)} 字节"
        if len(got) > exp_len:
            note += "  <-- 多了数据槽"
    print(f"   [{verdict}] {name}{note}")
    return verdict == "PASS"


def ack_ok(resp: bytes, cmd: int) -> bool:
    """传输类响应：第 3 个字节的低 3 位 = ACK（1=OK 2=WAIT 4=FAULT 7=NO ACK）。"""
    if len(resp) < 3 or resp[0] != cmd:
        return False
    ack = resp[2] & 0x07
    return ack == 0x01


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", type=lambda s: int(s, 0), default=0xE0042000, help="要读的目标地址")
    args = ap.parse_args()

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
    print(f"探针 {dev.manufacturer} / {dev.product}")
    print(f"接口0 名字 = {usb.util.get_string(dev, intf.iInterface)!r}   (pyOCD 要求含 'CMSIS-DAP')")
    print(f"bulk OUT=0x{ep_out.bEndpointAddress:02X} IN=0x{ep_in.bEndpointAddress:02X} mps={ep_out.wMaxPacketSize}\n")

    def xfer(payload: list) -> bytes:
        buf = bytes(payload) + b"\x00" * (PKT - len(payload))
        ep_out.write(buf, timeout=2000)
        return bytes(ep_in.read(PKT, timeout=2000))

    # 开场先排掉上一个会话可能残留的响应包：裸客户端不走 pyOCD 那套
    # 握手/收场，紧跟在别的会话后面跑时偶尔会把旧响应当成自己的
    # （实测遇到过一次：DP IDCODE 读得到，紧接着的 AP 访问全 NO ACK，
    #   而同一时刻 pyOCD 读同一个地址是好的 -> 是会话状态，不是链路坏）。
    drained = 0
    while True:
        try:
            stale = bytes(ep_in.read(PKT, timeout=120))
        except usb.core.USBTimeoutError:
            break
        if not stale:
            break
        drained += 1
    if drained:
        print(f"[i] 开场排掉 {drained} 个残留响应包\n")

    a = args.addr
    le = lambda v: [v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF]   # noqa: E731
    wr_tar = [0x05] + le(a)          # 写 AP TAR（APnDP=1,RnW=0,A[3:2]=01）
    wr_tar2 = [0x05] + le(a + 4)
    rd_drw = [0x0F]                  # 读 AP DRW（APnDP=1,RnW=1,A[3:2]=11）——注意不是 0x1F！
    rd_dp_idcode = [0x02]            # 读 DP IDCODE

    print("== 握手 ==")
    r = xfer([ID_DAP_Connect, 0x01])
    print("  DAP_Connect(SWD)          ->", hexdump(r))
    check("DAP_Connect", r, 2, bytes([ID_DAP_Connect, 0x01]))

    r = xfer([ID_DAP_SWJ_Clock] + le(1_000_000))
    print("  DAP_SWJ_Clock(1 MHz)      ->", hexdump(r))
    check("DAP_SWJ_Clock", r, 2, bytes([ID_DAP_SWJ_Clock]))

    r = xfer([ID_DAP_SWD_Configure, 0x00])
    print("  DAP_SWD_Configure         ->", hexdump(r))
    check("DAP_SWD_Configure", r, 2, bytes([ID_DAP_SWD_Configure]))

    # 🚨 关键：SWD 激活序列。少了它 SWJ-DP 还在 JTAG 模式，传输会返回 NO ACK(0x07)
    r = xfer([ID_DAP_SWJ_Sequence, SWD_ACTIVATION_BITS] + SWD_ACTIVATION)
    print(f"  DAP_SWJ_Sequence({SWD_ACTIVATION_BITS} 位 SWD 激活) ->", hexdump(r))
    check("DAP_SWJ_Sequence", r, 2, bytes([ID_DAP_SWJ_Sequence]))

    r = xfer([ID_DAP_TransferConfigure, 0x00, 0xE8, 0x03, 0x00, 0x00])
    print("  DAP_TransferConfigure     ->", hexdump(r))
    check("DAP_TransferConfigure", r, 2, bytes([ID_DAP_TransferConfigure]))

    print("\n== 先确认 SWD 真的在应答（拿 DP IDCODE）==")
    r = xfer([ID_DAP_Transfer, 0x00, 0x01] + rd_dp_idcode)
    print("  读 DP IDCODE              ->", hexdump(r))
    if not ack_ok(r, ID_DAP_Transfer):
        ack = r[2] & 0x07 if len(r) > 2 else -1
        print(f"  [FAIL] 没有 ACK（响应字节 0x{r[2]:02X}，ACK={ack}）——"
              f"先查激活序列/接线/目标供电，长度结论此时无意义")
        return 2
    print(f"      DP IDCODE = 0x{int.from_bytes(r[3:7], 'little'):08X}（STM32F1 应为 0x1BA01477）")

    print("\n== DAP_Transfer 单条 ==")
    r = xfer([ID_DAP_Transfer, 0x00, 0x01] + wr_tar)
    print(f"  写 TAR=0x{a:08X}          ->", hexdump(r))
    check("单条写 (期望 3 = 2+1+0)", r, 3, bytes([ID_DAP_Transfer, 0x01, 0x01]))

    r = xfer([ID_DAP_Transfer, 0x00, 0x01] + rd_drw)
    print("  读 DRW                    ->", hexdump(r))
    if len(r) >= 7:
        print(f"      读回值 = 0x{int.from_bytes(r[3:7], 'little'):08X}")
    check("单条读 (期望 7 = 3+4)", r, 7, bytes([ID_DAP_Transfer, 0x01, 0x01]))

    print("\n== 关键：多条传输打包（这就是 pyOCD 挂起读干的事）==")
    r = xfer([ID_DAP_Transfer, 0x00, 0x02] + wr_tar + rd_drw)
    print("  2 条: 写TAR + 读DRW       ->", hexdump(r))
    check("2 条传输 写TAR+读DRW (期望 7 = 3+4)", r, 7, bytes([ID_DAP_Transfer, 0x02]))
    if len(r) > 7:
        print(f"      多出 {len(r)-7} 字节；第 3..6 字节 = {r[2:6].hex(' ')}"
              f"（写进去的地址小端 = {bytes(le(a)).hex(' ')}）")

    r = xfer([ID_DAP_Transfer, 0x00, 0x04] + wr_tar + rd_drw + wr_tar2 + rd_drw)
    print("  4 条: (写+读)x2           ->", hexdump(r))
    check("4 条传输 2写2读 (期望 11 = 3+8)", r, 11, bytes([ID_DAP_Transfer, 0x04]))

    print("\n== DAP_TransferBlock（块读，速度测试走的就是这条）==")
    xfer([ID_DAP_Transfer, 0x00, 0x01] + wr_tar)              # 先把 TAR 指到 a
    r = xfer([ID_DAP_TransferBlock, 0x00, 0x04, 0x00, 0x0F])  # 读 4 个字
    print("  块读 4 个字               ->", hexdump(r))
    check("块读 4 字 (期望 20 = 4+16)", r, 20, bytes([ID_DAP_TransferBlock, 0x04, 0x00, 0x01]))
    if len(r) >= 20:
        words = [int.from_bytes(r[4 + i * 4:8 + i * 4], "little") for i in range(4)]
        print("      读回:", " ".join(f"{w:08X}" for w in words))

    print("\n== 结论 ==")
    if FAILS:
        print("  失败的项:", ", ".join(FAILS))
        print("  长度对不上 => 问题在**固件**的响应拼装（规范明确：数据只跟读传输）")
    else:
        print("  全部符合规范 => 协议层没问题（长度+数据槽都对）")
    xfer([0x03])            # DAP_Disconnect：体面收场，别把状态留给下一个会话
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
