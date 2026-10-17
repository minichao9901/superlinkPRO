#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SRAM 20KB read/write speed test over akaLinkPro (CMSIS-DAP) + OpenOCD.

Starts OpenOCD as a daemon, connects to the telnet port, and times
load_image (write) / dump_image (read) of a 20KB blob into the
STM32F103 SRAM at 0x20000000 across several SWD clock speeds.
Data integrity is verified per run by comparing the dumped file.
"""
import os
import re
import socket
import subprocess
import sys
import time

# Paths are resolved relative to this file (and the HPM SDK environment) so the
# script works from any clone. Override with OPENOCD_EXE / OPENOCD_SCRIPTS /
# OPENOCD_CFG / HPM_SDK_ENV_DIR when the tools live elsewhere.
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SDK_ENV = os.environ.get("HPM_SDK_ENV_DIR", r"E:\sdk_env_v1.11.0")

OPENOCD = os.environ.get("OPENOCD_EXE", os.path.join(SDK_ENV, "tools", "openocd", "openocd.exe"))
SCRIPTS = os.environ.get("OPENOCD_SCRIPTS", os.path.join(SDK_ENV, "tools", "openocd", "tcl"))
CFG = os.environ.get("OPENOCD_CFG", os.path.join(HERE, "openocd_stm32f1_swd.cfg"))
WORKDIR = HERE

SRAM_ADDR = 0x20000000
SIZE = 20 * 1024  # 20480 bytes = full SRAM of STM32F103C8
SPEEDS_KHZ = [1000, 2000, 4000, 10000, 20000, 36000, 45000, 60000]

# Target clock normalisation (see main()): STM32F103 RCC/FLASH registers.
BOOST = "--no-boost" not in sys.argv
BOOST_LABEL = "64 MHz (PLL from HSI/2)"
RCC_CR = 0x40021000
RCC_CFGR = 0x40021004
FLASH_ACR = 0x40022000
SWS_NAME = {0: "HSI", 1: "HSE", 2: "PLL", 3: "n/a"}
HPRE_DIV = {0: 1, 1: 2, 2: 4, 3: 8, 4: 16, 5: 64, 6: 128, 7: 256,
            8: 2, 9: 4, 10: 8, 11: 16, 12: 64, 13: 128, 14: 256, 15: 512}
PLLMUL = {0: 2, 1: 3, 2: 4, 3: 5, 4: 6, 5: 7, 6: 8, 7: 9, 8: 10, 9: 11,
          10: 12, 11: 13, 12: 14, 13: 15, 14: 16, 15: 16}

TELNET_PORT = 4444


class Telnet:
    def __init__(self, port, timeout=30.0):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.settimeout(timeout)
        self.buf = b""
        self.read_until(b"> ")  # banner + first prompt

    def read_until(self, marker):
        while marker not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("OpenOCD closed the connection")
            self.buf += chunk
        out, _, self.buf = self.buf.partition(marker)
        return out.decode("utf-8", "replace")

    def cmd(self, c, timeout=120.0):
        self.sock.settimeout(timeout)
        self.sock.sendall((c + "\n").encode())
        return self.read_until(b"> ")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def gen_test_file(path):
    # deterministic pseudo-random content (xorshift), avoids all-0x00/0xFF
    data = bytearray()
    x = 0x12345678
    for _ in range(SIZE):
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        data.append(x & 0xFF)
    with open(path, "wb") as f:
        f.write(bytes(data))


def _rd(tn, addr):
    """Read one 32-bit word through the target."""
    out = tn.cmd("mdw 0x%08X" % addr)
    m = re.search(r"0x%08x:\s*([0-9a-fA-F]{8})" % addr, out) or \
        re.search(r":\s*([0-9a-fA-F]{8})", out)
    return int(m.group(1), 16) if m else None


def describe_clock(tn):
    """Human readable SYSCLK/HCLK + flash latency of the STM32F1 target."""
    cr = _rd(tn, RCC_CR)
    cfgr = _rd(tn, RCC_CFGR)
    acr = _rd(tn, FLASH_ACR)
    if None in (cr, cfgr, acr):
        return "unknown (RCC read failed)"
    sws = (cfgr >> 2) & 3
    base = {0: 8.0, 1: 8.0, 2: (4.0 if not (cfgr >> 16) & 1 else 8.0) * PLLMUL[(cfgr >> 18) & 0xF]}[sws]
    hclk = base / HPRE_DIV[(cfgr >> 4) & 0xF]
    return "SYSCLK=%s %.1f MHz, HCLK=%.1f MHz, flash latency=%d" % (SWS_NAME[sws], base, hclk, acr & 7)


def boost_target_clock(tn):
    """Run the target from its PLL: HSI/2 * 16 = 64 MHz (works without a crystal).

    Must be re-applied after every 'reset halt' - the reset restores the RCC
    defaults and the throughput would silently fall back to the 8 MHz HSI rate.

    但测试固件（script_test/stm32f103_rtt_speed）现在**自己**就超频到 96 MHz
    （HSE 8 MHz x12，见其 main.c 的 clock_init），所以只要看到已经跑在 PLL 上就
    直接返回 —— 再去写 RCC_CFGR 会把固件设好的 APB 分频覆盖掉（实测交付率因此
    从 2503 掉到 1389 KB/s，见 docs §5.9）。
    """
    cfgr = _rd(tn, RCC_CFGR) or 0
    if ((cfgr >> 2) & 3) == 2:      # SWS = PLL：固件已经顶上去了，别动
        return
    tn.cmd("mww 0x%08X 0x00000012" % FLASH_ACR)        # 2 wait states + prefetch
    tn.cmd("mww 0x%08X 0x00380400" % RCC_CFGR)         # PLLSRC=HSI/2, x16, HPRE=/1, PPRE1=/2
    cr = _rd(tn, RCC_CR) or 0
    tn.cmd("mww 0x%08X 0x%08X" % (RCC_CR, cr | 0x01000000))  # PLLON
    for _ in range(50):
        if (_rd(tn, RCC_CR) or 0) & 0x02000000:        # PLLRDY
            break
        time.sleep(0.02)
    tn.cmd("mww 0x%08X 0x00380402" % RCC_CFGR)         # SW = PLL
    time.sleep(0.05)


def ocd_speed(text):
    """Rate OpenOCD itself reports for load_image/dump_image: '... (3384.095 KiB/s)'.

    That timer covers the target transfer only - no telnet round trip, no image
    file read/format - so it is the caliber the upstream akaLinkPro benchmark
    (script_test/swd/benchmark_readback.tcl) quotes. Both are reported: the wall
    clock answers "how long does the command take end to end", this one answers
    "how fast does the data actually move".
    """
    m = re.search(r"\(([0-9.]+) (KiB|MiB)/s\)", text)
    if not m:
        return None
    value = float(m.group(1))
    return value * 1024.0 if m.group(2) == "MiB" else value


def main():
    os.makedirs(WORKDIR, exist_ok=True)
    src_bin = os.path.join(WORKDIR, "sram_test.bin")
    dst_bin = os.path.join(WORKDIR, "sram_out.bin")
    gen_test_file(src_bin)
    ref = open(src_bin, "rb").read()
    # OpenOCD telnet/Tcl eats backslashes: always use forward slashes
    src_tel = src_bin.replace("\\", "/")
    dst_tel = dst_bin.replace("\\", "/")

    args = [
        OPENOCD,
        "-s", SCRIPTS,
        "-f", CFG,
        "-c", "gdb port disabled",
        "-c", "tcl port disabled",
        "-c", "telnet port 4444",
    ]
    print("Starting OpenOCD daemon ...")
    proc = subprocess.Popen(args,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True)

    try:
        # Wait for the telnet port, but bail out early when OpenOCD dies on its
        # own (usually "cannot read IDR" = no target / target not powered).
        tn = None
        deadline = time.time() + 20
        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.communicate()[0] or ""
                print(out.strip()[-1500:])
                print()
                if "cannot read IDR" in out:
                    raise RuntimeError(
                        "OpenOCD could not talk to the target DP. Check the "
                        "STM32F103 wiring (SWCLK/SWDIO/GND on the probe's J5), "
                        "target power and BOOT0, then retry.")
                raise RuntimeError("OpenOCD exited early (see its log above)")
            try:
                tn = Telnet(TELNET_PORT)
                break
            except OSError:
                time.sleep(0.3)
        if tn is None:
            raise RuntimeError("could not connect to OpenOCD telnet")

        print(tn.cmd("targets").strip())
        print()

        # The measured throughput is capped by min(SWD clock, target AHB clock):
        # every AP transaction costs several HCLK cycles, so a target left at its
        # reset default (STM32F103: HSI 8 MHz) saturates near 1.4 MB/s no matter
        # how fast the SWD clock is. Normalise it unless --no-boost was given.
        print("target clock:", describe_clock(tn))
        if BOOST:
            print("normalising target clock to %s ..." % BOOST_LABEL)
            boost_target_clock(tn)
            print("target clock:", describe_clock(tn))
        print()

        results = []
        consecutive_fail = 0
        for spd in SPEEDS_KHZ:
            r = tn.cmd(f"adapter speed {spd}")
            label = f"{spd/1000:g} MHz"

            # warmup / baseline small transfer (overhead context)
            ok = True
            tn.cmd("reset halt")
            if BOOST:
                # the reset puts the RCC back to its default, re-apply every round
                boost_target_clock(tn)

            t0 = time.perf_counter()
            out_w = tn.cmd(f'load_image "{src_tel}" 0x{SRAM_ADDR:X} bin')
            t1 = time.perf_counter()

            out_r = tn.cmd(f'dump_image "{dst_tel}" 0x{SRAM_ADDR:X} {SIZE}')
            t2 = time.perf_counter()

            err = "Error" in out_w or "error" in out_w or "Error" in out_r or "error" in out_r
            data_ok = False
            if not err and os.path.exists(dst_bin):
                got = open(dst_bin, "rb").read()
                data_ok = (got == ref)
            if err or not data_ok:
                print(f"{label:>8} | FAILED (err={err} data_ok={data_ok})")
                consecutive_fail += 1
                if consecutive_fail >= 2:
                    print("two consecutive failures, stopping.")
                    break
                continue
            consecutive_fail = 0

            w_kbps = SIZE / (t1 - t0) / 1024.0
            r_kbps = SIZE / (t2 - t1) / 1024.0
            w_xfer = ocd_speed(out_w)
            r_xfer = ocd_speed(out_r)
            results.append((label, w_kbps, r_kbps, w_xfer, r_xfer))
            print(f"{label:>8} | write {w_kbps:8.1f} KB/s | read {r_kbps:8.1f} KB/s | verified")
            print(f"{'':>8} |   xfer-only: write {w_xfer or 0:8.1f} | read {r_xfer or 0:8.1f} KB/s"
                  f"  <- OpenOCD's own timer")

        print()
        print("=== summary (20KB via CMSIS-DAP + OpenOCD load/dump) ===")
        print("wall = this script's timer (one telnet round trip per command included)")
        print("xfer = OpenOCD's own timer (target transfer only; upstream's caliber)")
        print(f"{'SWD clock':>9} | {'wall w':>9} | {'wall r':>9} | {'xfer w':>9} | {'xfer r':>9}")
        for label, w, r, wx, rx in results:
            sw = f"{wx:9.1f}" if wx else "      n/a"
            sr = f"{rx:9.1f}" if rx else "      n/a"
            print(f"{label:>9} | {w:8.1f}K | {r:8.1f}K | {sw} | {sr}")

        try:
            tn.cmd("shutdown")
        except (ConnectionError, OSError):
            pass  # OpenOCD exits without a trailing prompt
        tn.close()
    finally:
        time.sleep(0.5)
        proc.terminate()
        try:
            out = proc.communicate(timeout=5)[0] or ""
        except subprocess.TimeoutExpired:
            proc.kill()
            out = ""
        if proc.returncode not in (0, None) and out:
            print(out[-2000:])


if __name__ == "__main__":
    sys.exit(main())
