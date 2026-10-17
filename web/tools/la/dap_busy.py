#!/usr/bin/env python3
"""dap_busy.py —— 让探针**持续跑块传输**，给逻辑分析仪做定量分析用。

和 `swd_min_seq.py --loop` 的区别：那个每轮只发两笔小事务（USB 往返占满），
本脚本发 `DAP_TransferBlock` —— 一条命令里搬 1KB/多字，**时钟是连续跑的**，
所以能拿 LA 量出"时钟占空比"（时间到底花在时钟上还是花在 USB/固件开销上）。

用法：python tools\\dap_busy.py --mode read|write|mix --seconds 20 [--chunk 504] [--addr 0x20000000]

🚨 **坑①：一条命令必须装进一个 512B 包**（2026-09-21 定论，本脚本为此改过默认块长）：
   本固件（与上游 CherryDAP 一样）的 `dap_out_callback()` **忽略 `nbytes`**、每个 USB 包都当成
   一条完整命令处理。所以 `DAP_PACKET_SIZE=512` 就是命令长度的硬上限：
     · 块写请求 = 5 + 4N 字节 → **N ≤ 126 字（504B）**
     · 块读响应 = 4 + 4N 字节 → **N ≤ 127 字**
   发 1029B 的块写（N=256）会怎样：包 1 被当完整命令 → 用**截断的**数据写 256 个字，
   后面两个包（首字节是数据 0x5A）被当成"未知命令"→ 各回 1 字节 `0xFF`（`ID_DAP_Invalid`）。
   症状极具迷惑性：**探针照常吐时钟、吞吐数字好看，但写进去的数据是错的、失败计数乱涨**。
   （`--chunk` 超过上限本脚本会直接拒绝。）

🚨 **坑②：响应必须按"字节数"精确收，不能按 512B 包收**：
   固件可能把**一条响应拆进多个 USB 包**（实测见过 `[0x06]` 单独一个包、剩下 3 字节在下一个包），
   而 `read(512)` 一次只保证"拿到当前这一包" → 命令一路错位。
   → 见下面 `resp_len()`：由**请求内容**算出响应该有多少字节，凑够了才返回。

🚨 **坑③：别靠 AP 的 TAR 自动递增省那次往返**：连发 20 条块写时实测**数据写错位**
   （带索引图案 + pyOCD 独立回读复核）。默认每条重写 TAR（正确、稍慢）；要"只要时钟"用 `--auto-tar`。
"""
import argparse
import os
import subprocess
import sys
import time

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
    print("[i] 换成 venv 解释器重跑", flush=True)
    raise SystemExit(subprocess.call([VENV_PY, os.path.abspath(__file__)] + sys.argv[1:]))


ensure_pyusb()

import usb.core        # noqa: E402
import usb.util        # noqa: E402
import libusb_package  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

VID, PID, PKT = 0x0D28, 0x0204, 512
SWD_ACTIVATION = [0x9E, 0xE7] + [0xFF] * 8 + [0x00]
ACK = {1: "OK", 2: "WAIT", 4: "FAULT", 7: "NO-ACK"}


def resp_len(payload: list) -> int:
    """由**请求**算出响应该有多少字节（CMSIS-DAP v2 规范）。

    - `0x05 DAP_Transfer`：请求 [05, idx, cnt, (req[, 4B 数据])*cnt]
      响应 [05, cnt, (resp[, 4B 数据])*cnt] —— **写**在请求里带数据、**读**在响应里带数据。
    - `0x06 DAP_TransferBlock`：响应 [06, cnt_lo, cnt_hi, resp] +（读时 cnt×4 字节）。
    - 其余控制类命令一律 [cmd, status] 两字节（`0x00 DAP_Info` 本脚本不用）。
    """
    c = payload[0]
    if c == 0x05:
        cnt, i, extra = payload[2], 3, 0
        for _ in range(cnt):
            t = payload[i]
            i += 1
            if t & 0x02:          # RnW=1 读：数据在**响应**里
                extra += 4
            else:                 # RnW=0 写：数据在**请求**里
                i += 4
        return 2 + cnt + extra
    if c == 0x06:
        cnt = payload[2] | (payload[3] << 8)
        return 4 + (cnt * 4 if payload[4] & 0x02 else 0)
    return 2


class Cmd:
    """带缓冲的收发：按字节数精确取，跨包自动补齐。"""

    def __init__(self, ep_out, ep_in):
        self.out, self.in_, self.buf = ep_out, ep_in, b""

    def _more(self, timeout=3000):
        self.buf += bytes(self.in_.read(PKT, timeout=timeout))

    def take(self, n, timeout=3000) -> bytes:
        while len(self.buf) < n:
            self._more(timeout)
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def drain(self, timeout=120):
        """排掉上一个会话残留的响应（读到超时为止）。"""
        try:
            while self.in_.read(PKT, timeout=timeout):
                pass
        except usb.core.USBTimeoutError:
            pass
        self.buf = b""

    def send(self, payload: list, timeout=3000) -> bytes:
        self.out.write(bytes(payload) + b"\x00" * (PKT - len(payload)), timeout=timeout)
        n = resp_len(payload)
        r = self.take(n, timeout)
        if r[0] != payload[0]:                      # 错位了就立刻说清楚，别一路跑歪
            raise RuntimeError(f"响应错位：请求 {payload[0]:#04x}，收到 {r[0]:#04x}（{r.hex(' ')}）")
        return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="read", choices=["read", "write", "mix"])
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--chunk", type=int, default=504,
                    help="每条 TransferBlock 搬多少字节（默认 504 = 126 字，正好一个 512B 包）")
    ap.add_argument("--addr", type=lambda s: int(s, 0), default=0x20000000)
    ap.add_argument("--auto-tar", action="store_true",
                    help="靠 AP 的 TAR 自动递增（只在走出 --span 时重写）。⚠️ 实测背靠背洪泛时数据会写错位，"
                         "只适合「只要一段连续时钟」的场景；要数据可信就用默认的每条重写 TAR")
    ap.add_argument("--span", type=lambda s: int(s, 0), default=0x4000,
                    help="--auto-tar 时的自动递增窗口（默认 16KB，别超过目标 RAM）")
    ap.add_argument("--no-check", action="store_true", help="跑完不做回读校验")
    args = ap.parse_args()

    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
    if dev is None:
        return print(f"找不到探针 {VID:04X}:{PID:04X}") or 1
    intf = dev.get_active_configuration()[(0, 0)]
    ep_out = usb.util.find_descriptor(intf, custom_match=lambda e:
                                      usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
    ep_in = usb.util.find_descriptor(intf, custom_match=lambda e:
                                     usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
    c = Cmd(ep_out, ep_in)
    c.drain()

    le = lambda v: [v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF]   # noqa: E731

    def setup(name: str, payload: list, rd: bool = False) -> bytes:
        r = c.send(payload)
        note = ""
        if payload[0] == 0x05 and len(r) >= 3:
            note = f"   ACK={r[2] & 7}({ACK.get(r[2] & 7, '?')})"
            if rd and len(r) >= 7:
                note += f"  数据=0x{int.from_bytes(r[3:7], 'little'):08X}"
        print(f"  {name:<30} -> {r[:9].hex(' '):<26}{note}", flush=True)
        return r

    setup("Connect", [0x02, 0x01])
    setup("SWJ_Sequence(88 位激活)", [0x12, 88] + SWD_ACTIVATION)
    setup("TransferConfigure", [0x04, 0x00, 0xE8, 0x03, 0x00, 0x00])
    setup("读 DP IDCODE", [0x05, 0x00, 0x01, 0x02], rd=True)

    # 🚨 关键：AP 访问前必须给 DP **上电**（写 CTRL/STAT 的 CSYSPWRUPREQ|CDBGPWRUPREQ）。
    #    少了这一步，所有 AP 访问一律 FAULT/NO-ACK —— pyOCD 的 DebugPortSetup 序列就是干这个的
    #    （本脚本第一版漏了，于是"块传输"压根没跑起来，量出来的占空比全是假的）。
    setup("写 DP CTRL/STAT=0x50000000", [0x05, 0x00, 0x01, 0x04] + le(0x50000000))
    time.sleep(0.05)
    setup("读 DP CTRL/STAT", [0x05, 0x00, 0x01, 0x06], rd=True)
    setup("写 DP SELECT=0", [0x05, 0x00, 0x01, 0x08] + le(0))
    setup("写 AP CSW=0x23000052", [0x05, 0x00, 0x01, 0x01] + le(0x23000052))
    setup("读 AP CSW", [0x05, 0x00, 0x01, 0x03], rd=True)
    setup(f"写 AP TAR=0x{args.addr:08X}", [0x05, 0x00, 0x01, 0x05] + le(args.addr))
    setup("读 AP DRW", [0x05, 0x00, 0x01, 0x0F], rd=True)

    # 🚨 命令长度硬上限 = DAP_PACKET_SIZE(512)：请求 5+4N、响应 4+4N 都要装得下
    words = args.chunk // 4
    if args.chunk % 4 or args.chunk <= 0 or 5 + args.chunk > 512 or 4 + args.chunk > 512:
        return print(f"--chunk {args.chunk} 不合法：必须是 4 的倍数且 ≤ 504"
                     f"（本固件一条命令只能装进一个 512B 包）") or 2
    lo, hi = words & 0xFF, (words >> 8) & 0xFF
    print(f"块长 {args.chunk} B（{words} 字）：请求 {5+args.chunk} B / 响应 {4+args.chunk} B，"
          f"都在 512B 包内 ✓", flush=True)

    def tar(a=args.addr) -> int:
        return c.send([0x05, 0x00, 0x01, 0x05] + le(a))[2] & 7

    def blk_read() -> bytes:
        return c.send([0x06, 0x00, lo, hi, 0x0F])

    def blk_write(data: list) -> bytes:
        return c.send([0x06, 0x00, lo, hi, 0x0D] + data)

    # TAR 是**自动递增**的，所以理论上不用每条命令都重写它（那会多一个 USB 往返）。
    # 🚨 但实测（2026-09-21）：**背靠背洪泛时靠自动递增会写错位**
    #    —— 20 条块写连发，用带索引的图案回读发现数据落在了非 504B 整数倍的偏移上、
    #    后几条压根没写进去（pyOCD 独立回读复核过）。每条重写 TAR 则 100% 正确，
    #    所以**默认每条重写**；`--auto-tar` 只留给"只要连续时钟、不在乎数据"的场合。
    cursor = [None]
    tar_writes = [0]

    def ensure_tar(nbytes: int) -> None:
        if not args.auto_tar:
            tar(args.addr)
            tar_writes[0] += 1
            return
        if cursor[0] is None or cursor[0] + nbytes > args.addr + args.span:
            tar(args.addr)
            cursor[0] = args.addr
            tar_writes[0] += 1
        cursor[0] += nbytes

    r = c.send([0x06, 0x00, lo, hi, 0x0F])
    print(f"自检：块读 {words} 字 -> count={int.from_bytes(r[1:3], 'little')} "
          f"resp=0x{r[3]:02X}（要 count={words} resp=0x01）", flush=True)

    t0 = time.time()
    n = err = 0
    data = le(0x5A5A0000) * words
    while time.time() - t0 < args.seconds:
        if args.mode in ("read", "mix"):
            ensure_tar(args.chunk)
            r = blk_read()
            n += 1
            if len(r) < 4 or (r[3] & 0x07) != 1 or int.from_bytes(r[1:3], "little") != words:
                err += 1
                cursor[0] = None            # 出错就别信地址了，下一条重新写 TAR
        if args.mode in ("write", "mix"):
            ensure_tar(args.chunk)
            r = blk_write(data)
            n += 1
            if len(r) < 4 or (r[3] & 0x07) != 1 or int.from_bytes(r[1:3], "little") != words:
                err += 1
                cursor[0] = None
        el = time.time() - t0
        if int(el * 2) != int((el - 0.02) * 2):
            print(f"  {el:5.1f}s  {n} 条命令  {n*args.chunk/el/1e6:6.2f} MB/s  失败 {err}", flush=True)
    el = time.time() - t0
    print(f"结束：{n} 条 × {args.chunk} B / {el:.1f}s = {n*args.chunk/el/1e6:.2f} MB/s，失败 {err}"
          f"（TAR 只重写了 {tar_writes[0]} 次）")

    if not args.no_check and args.mode in ("write", "mix"):
        # 回读校验：证明这些时钟真的把数据写进了对的地方
        #（每条重写 TAR 时所有块写都打在同一地址，所以只查第一块）
        nblk = (args.span // args.chunk) if args.auto_tar else 1
        bad = 0
        for i in range(nblk):
            tar(args.addr + i * args.chunk)
            r = blk_read()
            got = r[4:4 + args.chunk]
            w = [int.from_bytes(got[j:j + 4], "little") for j in range(0, len(got), 4)]
            bad += sum(1 for v in w if v != 0x5A5A0000)
        print(f"回读自检（0x{args.addr:08X} 起 {nblk}×{words} 字）：不一致 {bad} 字"
              + ("  ← 数据真的写进去了" if bad == 0 else "  ← 有数据没写对！"))
        cursor[0] = None

    c.send([0x03])
    return 0


if __name__ == "__main__":
    sys.exit(main())
