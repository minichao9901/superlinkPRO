"""Measure the probe-side RTT bridge (firmware feature) end to end.

1. OpenOCD: boost the STM32F103 target to 64 MHz (the RTT producer runs on the
   target, so an 8 MHz core would cap the stream at ~277 KB/s) and release the
   probe.
2. HID CMD_RTT(0x31) autostart: the probe finds the control block itself and
   starts polling it over SWD.
3. Read the CDC port and measure - no host polling at all, so this is the
   bridge's own ceiling.
4. Verify the stream is exactly the "hello world!\n" the fixture produces, then
   read the bridge's counters back over HID.

Usage: python rtt_probe_bridge.py [COMxx] [seconds] [kHz]
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

def _arg(idx, default, cast=str):
    """Tolerant argv read: also lets other scripts import this one as a module
    (their own flags must not blow up the import)."""
    try:
        return cast(sys.argv[idx])
    except (IndexError, ValueError):
        return default


COM = _arg(1, "COM52")
WINDOW_S = _arg(2, 5.0, float)
KHZ = _arg(3, 36000, int)
# 可选第 4 参：桥的 SWD 档（MHz）。0 = 用固件默认（45M 起阶梯）。
# 走 HID CMD_RTT action 7（ACT_CONFIG）把 s_swd_clock_req 记成该档，AUTOSTART
# 时固件按"请求档 → 往下找第一个能用的"起链路；status 字 [11] 高 8 位是**实际**
# 生效档，启动后打印出来（60M 长跑抖动时会自动降到 45M，这是正常行为）。
CLK = _arg(4, 0, int)

HERE = os.path.dirname(os.path.abspath(__file__))
SDK_ENV = os.environ.get("HPM_SDK_ENV_DIR", r"E:\sdk_env_v1.11.0")
OPENOCD = os.environ.get("OPENOCD_EXE", os.path.join(SDK_ENV, "tools", "openocd", "openocd.exe"))
SCRIPTS = os.environ.get("OPENOCD_SCRIPTS", os.path.join(SDK_ENV, "tools", "openocd", "tcl"))
CFG = os.path.join(HERE, "openocd_stm32f1_swd.cfg")

CMD_RTT = 0x31
ACT_STOP, ACT_START, ACT_STATUS, ACT_AUTOSTART = 0, 1, 2, 3
RCC_CR, RCC_CFGR, FLASH_ACR = 0x40021000, 0x40021004, 0x40022000
PATTERN = b"hello world!\n"


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT")
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


class Telnet:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.settimeout(30)
        self.buf = b""
        self.read_until(b"> ")

    def read_until(self, marker):
        while marker not in self.buf:
            c = self.sock.recv(65536)
            if not c:
                raise ConnectionError("closed")
            self.buf += c
        out, _, self.buf = self.buf.partition(marker)
        return out.decode("utf-8", "replace")

    def cmd(self, c, timeout=60.0):
        self.sock.settimeout(timeout)
        self.sock.sendall((c + "\n").encode())
        return self.read_until(b"> ")


def rd(tn, addr):
    out = tn.cmd("mdw 0x%08X" % addr)
    m = re.search(r":\s*([0-9a-fA-F]{8})", out)
    return int(m.group(1), 16) if m else None


def decode_clock(cfgr):
    """RCC_CFGR -> (MHz, 描述)。晶振 8 MHz、HSI 8 MHz（HSI 进 PLL 要先 /2）。"""
    pllsrc = (cfgr >> 16) & 1
    mull = ((cfgr >> 18) & 0xF) + 2
    sws = (cfgr >> 2) & 3
    mhz = (8.0 if pllsrc else 4.0) * mull
    if sws != 2:
        return 8.0, "SWS=%d 没跑在 PLL 上（HSI 8 MHz 直出）" % sws
    return mhz, "PLL=%g MHz (%s x%d)" % (mhz, "HSE" if pllsrc else "HSI/2", mull)


def boost_target():
    """让目标跑在高频上。

    测试固件（script_test/stm32f103_rtt_speed）现在**自己**就超频到 96 MHz
    （main.c 的 clock_init），所以这里默认只做两件事：复位让固件重新跑一遍
    clock_init，然后读回 RCC_CFGR 报出实际频率 —— **不再去写 RCC**。
    （以前无条件写 RCC_CFGR 会把固件设好的 APB 分频覆盖掉，实测交付率因此
      从 2503 KB/s 掉到 1389 KB/s。）

    只有目标还跑在 8 MHz 的老固件上（SWS != 2）时，才退回老办法：从 HSI/2 x16
    顶上 64 MHz。"""
    proc = subprocess.Popen([OPENOCD, "-s", SCRIPTS, "-f", CFG,
                             "-c", "gdb port disabled", "-c", "tcl port disabled",
                             "-c", "telnet port 4453"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        tn = None
        for _ in range(60):
            try:
                tn = Telnet(4453)
                break
            except OSError:
                time.sleep(0.3)
        if tn is None:
            print("  ! OpenOCD did not come up - is the target wired?")
            return False
        tn.cmd("reset halt")
        cfgr = rd(tn, RCC_CFGR) or 0
        mhz, desc = decode_clock(cfgr)
        if ((cfgr >> 2) & 3) == 2:
            tn.cmd("adapter speed %d" % KHZ)
            tn.cmd("reset run")
            print("  target RCC_CFGR=0x%08X (固件自带超频: %s)" % (cfgr, desc))
        else:
            tn.cmd("mww 0x%08X 0x00000012" % FLASH_ACR)
            tn.cmd("mww 0x%08X 0x00380400" % RCC_CFGR)
            cr = rd(tn, RCC_CR) or 0
            tn.cmd("mww 0x%08X 0x%08X" % (RCC_CR, cr | 0x01000000))
            for _ in range(50):
                if (rd(tn, RCC_CR) or 0) & 0x02000000:
                    break
                time.sleep(0.02)
            tn.cmd("mww 0x%08X 0x00380402" % RCC_CFGR)
            tn.cmd("adapter speed %d" % KHZ)
            tn.cmd("reset run")
            cfgr = rd(tn, RCC_CFGR) or 0
            print("  target RCC_CFGR=0x%08X (脚本顶上: %s)" % (cfgr, decode_clock(cfgr)[1]))
        try:
            tn.cmd("shutdown")  # OpenOCD exits without a trailing prompt
        except (ConnectionError, OSError):
            pass
        return True
    finally:
        time.sleep(0.5)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def rtt_cmd(dev, action, addr=0, size=0, channel=0):
    req = [0x01, 0x01, CMD_RTT, action,
           addr & 0xFF, (addr >> 8) & 0xFF, (addr >> 16) & 0xFF, (addr >> 24) & 0xFF,
           size & 0xFF, (size >> 8) & 0xFF, (size >> 16) & 0xFF, (size >> 24) & 0xFF,
           channel]
    req += [0] * (64 - len(req))
    dev.write(req)
    t0 = time.time()
    while time.time() - t0 < 1.0:
        r = dev.read(64, timeout_ms=200)
        if r and r[0] == 0x02 and r[2] == CMD_RTT:
            rc = r[3]
            words = [int.from_bytes(bytes(r[4 + i * 4:8 + i * 4]), "little") for i in range(12)]
            return rc, words
    return None, None


def show_status(words, tag):
    if not words:
        print("  [%s] no status" % tag)
        return
    running = words[0] & 1
    chan = (words[0] >> 8) & 0xFF
    swd = (words[0] >> 16) & 1
    clkdel = (words[0] >> 24) & 0xFF
    print("  [%s] running=%d channel=%d swd=%d clock_delay=%d cb=0x%08X up=0x%08X" %
          (tag, running, chan, swd, clkdel, words[1], words[2]))
    print("  [%s] drained=%d B, polls=%d drains=%d, rd_err=%d wr_err=%d" %
          (tag, words[3], words[4] & 0xFFFF, words[4] >> 16, words[5] & 0xFFFF, words[5] >> 16))
    print("  [%s] last_poll=%d B, idle=%d, rescan=%d" %
          (tag, words[6] & 0xFFFF, words[6] >> 16, words[7] >> 16))
    rsp = words[9]
    print("  [%s] start_rc=%d  last DAP cmd=0x%02X" % (tag, (words[10] & 0xFF) - (256 if (words[10] & 0xFF) > 127 else 0), words[8] & 0xFF))
    if False:
        print("  [%s] last DAP cmd=0x%02X rsp id=0x%02X cnt=%d/%d value=0x%02X%s" %
          (tag, words[8] & 0xFF, rsp & 0xFF, rsp >> 8 & 0xFF, rsp >> 16 & 0xFF, rsp >> 24 & 0xFF,
           " (OK)" if (rsp >> 24) & 0xFF == 1 else ""))


def main():
    print("=== probe-side RTT bridge test ===")
    infos = hid.enumerate(0x0D28, 0x0204)
    cand = [i for i in infos if i.get("usage_page") == 0xFF00]
    if not cand:
        print("no custom HID interface (device in APP mode?)")
        return 1
    dev = hid.device()
    dev.open_path(cand[0]["path"])
    dev.set_nonblocking(1)

    print("1. boosting the target via OpenOCD, then releasing the probe")
    boost_target()
    time.sleep(0.3)

    print("2. HID: start the probe-side RTT bridge (autostart)")
    # Open the port first and let it run, then stop the bridge once so the CDC
    # ring starts empty: a saturated ring at capture start would fold the
    # startup transient into the measured stream. The bridge's own start resets
    # the ring and the counters, so what follows is one clean window.
    ser = serial.Serial(COM, 115200, timeout=0.2)
    ser.reset_input_buffer()
    rtt_cmd(dev, ACT_STOP)
    t_end = time.perf_counter() + 0.5
    while time.perf_counter() < t_end:
        ser.read(65536)
    ser.reset_input_buffer()

    if CLK > 0:
        # ACT_CONFIG: req[4..7]=SWD clock Hz，[8..9]=chunk(0=保持)，[10]=flags，
        # [11]=delay(0xFF=保持 Set_Clock_Delay 的档位值)。
        rtt_cmd(dev, 7, CLK * 1000000, 0xFF000000, 0)
        print("   requested SWD clock: %d MHz" % CLK)

    rtt_cmd(dev, ACT_AUTOSTART)
    # the start runs from the main loop; poll until it reports a result
    rc, words = None, None
    for _ in range(40):
        time.sleep(0.1)
        rc, words = rtt_cmd(dev, ACT_STATUS)
        if words and words[10] != 0xFFFFFF9C:
            break
    start_rc = words[10] & 0xFF if words else 0xFF
    if start_rc > 127:
        start_rc -= 256
    print("   start rc=%d (0=ok, negative = rtt_swd_init step)" % start_rc)
    show_status(words, "after-start")
    if words:
        print("   active SWD clock: %d MHz" % (words[11] >> 24))
    if start_rc != 0:
        print("   bridge refused to start")
        ser.close()
        return 2

    print("3. draining %s for %.1fs (no host polling involved)" % (COM, WINDOW_S))
    try:
        # Let the ring run empty first, then take the byte counter as the
        # baseline: only the delta over the timed window is comparable with
        # what the host reads.
        t_end = time.perf_counter() + 0.3
        while time.perf_counter() < t_end:
            ser.read(65536)
        ser.reset_input_buffer()
        _, w0 = rtt_cmd(dev, ACT_STATUS)
        d0 = w0[3] if w0 else 0

        total = 0
        stream = bytearray()
        rbuf = bytearray(1 << 20)
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < WINDOW_S:
            # readinto + 复用大缓冲，而不是 ser.read(n)：Windows 上 pyserial 的
            # read() 每次都要新分配缓冲，实测主机侧被压到 2169 KB/s，readinto 能到
            # 2956 KB/s —— 差 36%，会被误读成"设备只能跑这么快"。
            n = ser.readinto(rbuf)
            if n:
                total += n
                stream += rbuf[:n]
        dur = time.perf_counter() - t0

        # Stop the bridge first, then collect the tail still sitting in the
        # probe's CDC ring / in flight: with the producer stopped, whatever the
        # host is missing after this is real loss, not just buffering.
        rtt_cmd(dev, ACT_STOP)
        t1 = time.perf_counter()
        while time.perf_counter() - t1 < 1.0:
            n = ser.readinto(rbuf)
            if n:
                total += n
                stream += rbuf[:n]
                t1 = time.perf_counter()
    finally:
        ser.close()

    kbs = total / dur / 1024.0
    print("   got %d bytes in %.2fs -> %.1f KB/s (%.2f MB/s)" % (total, dur, kbs, kbs / 1024))

    # The fixture emits nothing but the 13-byte pattern in a tight loop, so a
    # lossless stream is one contiguous run of patterns. Align to the first full
    # pattern (the capture starts mid-message) and then walk pattern by pattern:
    # every place where the next occurrence is not exactly n bytes away is a gap
    # (dropped) or an overlap (duplicated) - that is the real loss metric. A
    # partial pattern at the very end is normal (the capture just stopped).
    n = len(PATTERN)
    off = stream.find(PATTERN)
    if off < 0:
        print("   stream check: pattern not found at all -> GARBLED")
    else:
        pos = off
        blocks = 0
        gaps = []
        while True:
            i = stream.find(PATTERN, pos)
            if i < 0:
                break
            if i != pos:
                gaps.append((pos, i - pos))
            pos = i + n
            blocks += 1
        tail = stream[pos:]
        tail_ok = (tail == PATTERN[:len(tail)])
        lost = sum(g for _, g in gaps if g > 0)
        dup = -sum(g for _, g in gaps if g < 0)
        verdict = "LOSSLESS" if (not gaps and tail_ok) else "LOSSY/GARBLED"
        print("   stream check: %d patterns, %d leading bytes skipped, %d trailing (partial=%s)" %
              (blocks, off, len(tail), tail_ok))
        print("   %s: %d gap(s), %d bytes lost, %d bytes duplicated%s" %
              (verdict, len(gaps), lost, dup,
               "" if not gaps else " (first at byte %d)" % gaps[0][0]))

    rc, words = rtt_cmd(dev, ACT_STATUS)
    show_status(words, "after-drain")
    if words and words[3] > d0:
        moved = words[3] - d0
        print("   bridge moved %d B during the window; host received %d (delta %+d = in-flight tail)" %
              (moved, total, total - moved))

    print("4. bridge stopped")
    return 0


if __name__ == "__main__":
    watchdog(90)
    sys.exit(main())
