"""H743（或任何 flash 算法跑不起来的核）上的 RTT 桥测速：**把固件加载到 RAM 里跑**。

这块 H743 的 flash 算法在 OpenOCD 下跑不起来（halt 能通、AHB-AP 读写正常、flash 全
0xFF、RDP=0xAA，但 "timed out while waiting for target halted"），所以走 RAM：
  1. OpenOCD 用 AHB-AP 直接把 `fw_ram.elf` 写进 AXI SRAM（不需要 flash 算法）；
  2. 关掉 I/D cache 并把 SP/PC 指向镜像（`Reset_Handler|1`、`_estack`）；
  3. resume 之后用探针侧 RTT 桥扫 **AXI SRAM(0x24000000)** 取数。
     ⚠️ H7 的 DTCM(0x20000000) 是内核私有总线，**AHB-AP 读不到**，所以不能像 F103
     那样用默认搜索区间。

前置：`pwsh -File script_test\\stm32h743_rtt_speed\\build.ps1 -Ram`

用法：python rtt_h743_bridge.py [COMxx] [--sec 6] [--clocks 20,36,45,60]
"""
import os
import re
import socket
import subprocess
import sys
import threading
import time

import hid
import serial

import rtt_probe_bridge as rb

COM = sys.argv[1] if len(sys.argv) > 1 else "COM52"
SEC = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
CLOCKS = [20, 36, 45, 60]

SDK = os.environ.get("HPM_SDK_ENV_DIR", r"E:\sdk_env_v1.11.0")
OPENOCD = os.environ.get("OPENOCD_EXE", os.path.join(SDK, "tools", "openocd", "openocd.exe"))
SCRIPTS = os.environ.get("OPENOCD_SCRIPTS", os.path.join(SDK, "tools", "openocd", "tcl"))
HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, "openocd_stm32h7_swd.cfg")
ELF = os.path.join(HERE, "stm32h743_rtt_speed", "build", "fw_ram.elf")

CB_ADDR = 0x24000000     # AXI SRAM：RTT 控制块在这里
CB_SIZE = 0x00080000

# Cortex-M7 的 CCR（0xE000ED14）：bit17=IC(指令 cache)、bit16=DC(数据 cache)。
# 默认 0 = 都关（原行为）。H743_CCR=0x20000 只开 I-cache 用来做对照实验：
# 全 RAM 版的代码就在 AXI SRAM 上，I-cache 一关，M7 每条取指都和探针的调试读
# 抢同一块内存；开 I-cache 后取指流量几乎归零，可以验证这是不是掉速的原因。
# （不要开 DC：RTT 缓冲一旦被 cache 住，探针写的 RdOff 目标看不见，数据也会陈旧。）
CCR = int(os.environ.get("H743_CCR", "0"), 0)


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT")
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


def symbols():
    """从 elf 里取 Reset_Handler 与 _estack（不清符号表就退化成默认值）。"""
    nm = os.path.join(os.path.dirname(os.environ.get("ARM_GCC", r"E:\Share\env-windows\tools\gnu_gcc\arm_gcc\mingw\bin\arm-none-eabi-gcc.exe")), "arm-none-eabi-nm.exe")
    pc, sp = 0x240000c5, 0x24080000
    try:
        out = subprocess.run([nm, ELF], capture_output=True, text=True, timeout=20).stdout
        for line in out.splitlines():
            m = re.match(r"^([0-9a-f]{8}) \w (\S+)$", line.strip())
            if not m:
                continue
            if m.group(2) == "Reset_Handler":
                pc = int(m.group(1), 16) | 1
            elif m.group(2) == "_estack":
                sp = int(m.group(1), 16)
    except Exception as e:
        print("  (nm 失败，用默认入口: %s)" % e)
    return pc, sp


def load_into_ram():
    if os.environ.get("H743_NO_LOAD"):
        # 板上已经烧好 **flash 版**（代码在 0x08000000、CB+栈在 AXI SRAM）时用它：
        # 再用 fw_ram.elf 覆盖 AXI SRAM 就把 flash 版冲掉了。
        # 用途：对比 "代码在 AXI SRAM（全 RAM 版）" 与 "代码在 flash" 两种布局下
        # 探针轮询 RTT 的交付率 —— 前者 M7 每条取指都打在探针正在读的同一块内存上。
        print("1. H743_NO_LOAD=1：跳过载入，直接用板上已运行的固件")
        return True
    pc, sp = symbols()
    print("1. 把 %s 载入 AXI SRAM 并运行（pc=0x%08X sp=0x%08X）" % (os.path.basename(ELF), pc, sp))
    cmd = [OPENOCD, "-s", SCRIPTS, "-f", CFG,
           "-c", "init", "-c", "halt",
           "-c", "mww 0xE000ED14 0x%08X" % CCR,  # CCR：默认 0 关 I/D cache；H743_CCR 可只开 IC
           "-c", "mww 0xE000EF50 0x00000000",   # ICIALLU
           "-c", 'load_image "%s" 0 elf' % ELF.replace("\\", "/"),
           "-c", "reg sp 0x%08X" % sp, "-c", "reg pc 0x%08X" % pc,
           "-c", "resume", "-c", "shutdown"]
    out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, timeout=60).stdout
    ok = "downloaded" in out
    print("   %s" % ("OK" if ok else "失败:\n" + (out or "(无输出)")[-600:]))
    return ok


def xfer(dev, pkt, timeout=1.0):
    pkt = pkt + [0] * (64 - len(pkt))
    dev.write(pkt)
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = dev.read(64, timeout_ms=200)
        if r and r[0] == 0x02 and r[2] == rb.CMD_RTT:
            return r
    return None


def cfg(dev, hz=0, chunk=0, discard=0, delay=0xFF):
    xfer(dev, [0x01, 0x01, rb.CMD_RTT, 7] + list(int(hz).to_bytes(4, "little")) +
         list(int(chunk).to_bytes(2, "little")) + [discard, delay])


def status(dev):
    r = xfer(dev, [0x01, 0x01, rb.CMD_RTT, 2])
    return [int.from_bytes(bytes(r[4 + i * 4:8 + i * 4]), "little") for i in range(12)]


def main():
    global COM, SEC, CLOCKS
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        COM = args[0]
    # 同时支持 --sec=4 与 --sec 4 两种写法
    argv = sys.argv[1:]
    for i, o in enumerate(argv):
        if not o.startswith("--"):
            continue
        k, eq, v = o.partition("=")
        if not eq and (i + 1) < len(argv) and not argv[i + 1].startswith("--"):
            v = argv[i + 1]
        if k == "--sec":
            SEC = float(v)
        elif k == "--clocks":
            CLOCKS = [int(x) for x in v.split(",")]

    print("=== H743 RTT 桥测速（RAM 运行）===")
    if not load_into_ram():
        return 1
    time.sleep(0.5)

    infos = hid.enumerate(0x0D28, 0x0204)
    cand = [i for i in infos if i.get("usage_page") == 0xFF00]
    if not cand:
        print("no custom HID interface")
        return 1
    dev = hid.device()
    dev.open_path(cand[0]["path"])
    dev.set_nonblocking(1)

    print("\n2. 逐档测交付率（扫 AXI SRAM 0x%08X）" % CB_ADDR)
    print("| SWD 档 | 交付 | 每次搬运 | rd_err | wr_err | rescan | 流间隙 |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for clk in CLOCKS:
        cfg(dev, hz=clk * 1_000_000, chunk=2048)
        rb.rtt_cmd(dev, rb.ACT_STOP)
        rb.rtt_cmd(dev, rb.ACT_START, CB_ADDR, CB_SIZE, 0)
        w = None
        for _ in range(40):
            time.sleep(0.1)
            w = status(dev)
            if w and (w[10] & 0xFF) != 0xFFFFFF9C:
                break
        sr = w[10] & 0xFF
        if sr > 127:
            sr -= 256
        if sr != 0:
            print("| %d MHz | 启动失败 rc=%d | - | - | - | - | - |" % (clk, sr))
            continue

        ser = serial.Serial(COM, 115200, timeout=0.2)
        try:
            ser.reset_input_buffer()
            t = time.perf_counter() + 0.4
            while time.perf_counter() < t:
                ser.read(65536)
            ser.reset_input_buffer()
            w0 = status(dev)
            d0 = w0[3]
            total = 0
            stream = bytearray()
            rbuf = bytearray(1 << 20)
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < SEC:
                n = ser.readinto(rbuf)
                if n:
                    total += n
                    stream += rbuf[:n]
            dur = time.perf_counter() - t0
            rb.rtt_cmd(dev, rb.ACT_STOP)
        finally:
            ser.close()
        w = status(dev)
        used = (w[11] >> 24) & 0xFF
        moved = w[3] - d0
        npoll = max(w[4] >> 16, 1)
        n = len(rb.PATTERN)
        off = stream.find(rb.PATTERN)
        gaps = 0
        if off >= 0:
            pos = off
            while True:
                i = stream.find(rb.PATTERN, pos)
                if i < 0:
                    break
                if i != pos:
                    gaps += 1
                pos = i + n
        print("| %d MHz%s | %7.1f KB/s | %.0f B | %d | %d | %d | %d |" %
              (used, "" if used == clk else " (=%d)" % clk, total / dur / 1024,
               moved / npoll, w[5] & 0xFFFF, (w[5] >> 16) & 0xFFFF, (w[7] >> 16) & 0xFFFF, gaps))
    return 0


if __name__ == "__main__":
    watchdog(300)
    sys.exit(main())
