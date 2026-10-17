#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SEGGER RTT throughput test over akaLinkPro (CMSIS-DAP) + OpenOCD.

Target firmware: script_test/stm32f103_rtt_speed (BLOCK_IF_FIFO_FULL mode,
target writes "hello world!\\n" in a tight loop, so host drain rate == RTT
throughput). OpenOCD finds the RTT control block, streams channel 0 over a
TCP server, and we count bytes for a fixed window per SWD clock speed.

Cross-check afterwards: g_bytes/g_loops/g_ms global variables.
"""
import os
import re
import socket
import subprocess
import sys
import time

# Paths are resolved relative to this file (and the HPM SDK environment) so the
# script works from any clone. Override with OPENOCD_EXE / OPENOCD_SCRIPTS /
# OPENOCD_CFG / FW_BIN / HPM_SDK_ENV_DIR when the tools live elsewhere.
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SDK_ENV = os.environ.get("HPM_SDK_ENV_DIR", r"E:\sdk_env_v1.11.0")

OPENOCD = os.environ.get("OPENOCD_EXE", os.path.join(SDK_ENV, "tools", "openocd", "openocd.exe"))
SCRIPTS = os.environ.get("OPENOCD_SCRIPTS", os.path.join(SDK_ENV, "tools", "openocd", "tcl"))
CFG = os.environ.get("OPENOCD_CFG", os.path.join(HERE, "openocd_stm32f1_swd.cfg"))
FW_BIN = os.environ.get("FW_BIN", os.path.join(HERE, "stm32f103_rtt_speed", "build", "fw.bin"))
WORKDIR = HERE

RTT_SERVER_PORT = 21021
TELNET_PORT = 4444
# SWD clocks that stay reliable while the target RUNS. At 45/60 MHz the AHB-AP
# reads start failing on this setup (16/20 at 60 MHz), which the poller would
# see as data corruption rather than as an error - see rtt_link_matrix.tcl.
SPEEDS_KHZ = [1000, 10000, 20000, 36000]
WINDOW_S = 8.0

# Target clock normalisation (same trap as sram_speed_test.py): the RTT
# producer is target code, so an STM32F1 left at HSI 8 MHz caps the stream at
# ~277 KB/s on its own.
BOOST = "--no-boost" not in sys.argv
RCC_CR = 0x40021000
RCC_CFGR = 0x40021004
FLASH_ACR = 0x40022000
SWS_NAME = {0: "HSI", 1: "HSE", 2: "PLL", 3: "n/a"}
HPRE_DIV = {0: 1, 1: 2, 2: 4, 3: 8, 4: 16, 5: 64, 6: 128, 7: 256,
            8: 2, 9: 4, 10: 8, 11: 16, 12: 64, 13: 128, 14: 256, 15: 512}
PLLMUL = {0: 2, 1: 3, 2: 4, 3: 5, 4: 6, 5: 7, 6: 8, 7: 9, 8: 10, 9: 11,
          10: 12, 11: 13, 12: 14, 13: 15, 14: 16, 15: 16}

G_BYTES = 0x20000000
G_LOOPS = 0x20000004
G_MS = 0x20000008


class Telnet:
    def __init__(self, port, timeout=30.0):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.settimeout(timeout)
        self.buf = b""
        self.read_until(b"> ")

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


def _rd(tn, addr):
    out = tn.cmd("mdw 0x%08X" % addr)
    m = re.search(r"0x%08x:\s*([0-9a-fA-F]{8})" % addr, out) or \
        re.search(r":\s*([0-9a-fA-F]{8})", out)
    return int(m.group(1), 16) if m else None


def describe_clock(tn):
    cr = _rd(tn, RCC_CR)
    cfgr = _rd(tn, RCC_CFGR)
    acr = _rd(tn, FLASH_ACR)
    if None in (cr, cfgr, acr):
        return "unknown"
    sws = (cfgr >> 2) & 3
    base = {0: 8.0, 1: 8.0, 2: (4.0 if not (cfgr >> 16) & 1 else 8.0) * PLLMUL[(cfgr >> 18) & 0xF]}[sws]
    return "SYSCLK=%s %.1f MHz, HCLK=%.1f MHz" % (SWS_NAME[sws], base, base / HPRE_DIV[(cfgr >> 4) & 0xF])


def boost_target_clock(tn):
    """64 MHz from HSI/2 * 16 - no crystal needed. Target must be halted."""
    tn.cmd("mww 0x%08X 0x00000012" % FLASH_ACR)
    tn.cmd("mww 0x%08X 0x00380400" % RCC_CFGR)
    cr = _rd(tn, RCC_CR) or 0
    tn.cmd("mww 0x%08X 0x%08X" % (RCC_CR, cr | 0x01000000))
    for _ in range(50):
        if (_rd(tn, RCC_CR) or 0) & 0x02000000:
            break
        time.sleep(0.02)
    tn.cmd("mww 0x%08X 0x00380402" % RCC_CFGR)
    time.sleep(0.05)


def main():
    args = [        OPENOCD,
        "-s", SCRIPTS,
        "-f", CFG,
        "-c", "gdb port disabled",
        "-c", "tcl port disabled",
        "-c", "telnet port 4444",
    ]
    print("Starting OpenOCD daemon ...")
    proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)

    try:
        tn = None
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                tn = Telnet(TELNET_PORT)
                break
            except OSError:
                time.sleep(0.3)
        if tn is None:
            raise RuntimeError("could not connect to OpenOCD telnet")

        # --- flash the RTT firmware (program = erase + write + verify) ---
        fw = FW_BIN.replace("\\", "/")
        out = tn.cmd(f'program "{fw}" verify 0x08000000')
        if "Verified OK" not in out:
            raise RuntimeError("flash verify failed:\n" + out[-800:])
        print("flash: programmed + verified OK")

        # The RTT *producer* runs on the target: at the STM32F1 reset default
        # (HSI 8 MHz) the firmware's SEGGER_RTT_Write loop tops out near
        # 277 KB/s no matter how fast the SWD clock is, which is what used to
        # cap this test. Boost the core while it is halted, then let it run.
        if BOOST:
            tn.cmd("reset halt")
            boost_target_clock(tn)
            print("target clock:", describe_clock(tn))
        tn.cmd("reset run")
        time.sleep(0.5)

        # --- RTT setup (search whole 20KB RAM for the control block) ---
        print(tn.cmd('rtt setup 0x20000000 0x5000 "SEGGER RTT"').strip())
        print(tn.cmd("rtt start").strip())
        # 1ms poll interval (default 100ms caps the server at ~9 KB/s)
        print(tn.cmd("rtt polling_interval 1").strip())
        print(tn.cmd("rtt server start 21021 0").strip())

        results = []
        for spd in SPEEDS_KHZ:
            tn.cmd(f"adapter speed {spd}")
            label = f"{spd/1000:g} MHz"
            g_before = _rd(tn, G_BYTES)

            sock = socket.create_connection(("127.0.0.1", RTT_SERVER_PORT), timeout=10)
            total = 0
            polls = 0
            t0 = time.perf_counter()
            sock.settimeout(0.5)
            while True:
                now = time.perf_counter()
                if now - t0 >= WINDOW_S:
                    break
                sock.settimeout(min(0.5, WINDOW_S - (now - t0)))
                try:
                    chunk = sock.recv(65536)
                    if chunk:
                        total += len(chunk)
                        polls += 1
                except socket.timeout:
                    pass
            dur = time.perf_counter() - t0
            sock.close()
            kbps = total / dur / 1024.0
            results.append((label, total, dur, kbps, polls))
            print(f"{label:>8} | {total:9d} bytes in {dur:5.1f}s | {kbps:8.1f} KB/s | {polls} reads")

        # --- cross-check target-side counters ---
        vals = tn.cmd(f"mdw 0x{G_BYTES:X} 3").strip()
        print()
        print("target counters (g_bytes, g_loops, g_ms):")
        print("   ", vals)

        print()
        print("=== summary (RTT ch0, host = OpenOCD rtt server, target %s) ===" %
              ("boosted" if BOOST else "at reset clock"))
        print(f"{'SWD clock':>9} | {'throughput':>12}")
        for label, total, dur, kbps, polls in results:
            print(f"{label:>9} | {kbps:8.1f} K/s")
        print()
        print("notes:")
        print("  * OpenOCD's rtt server is the limiter here; at 20 MHz it reaches")
        print("    ~0.9 MB/s, at 1 MHz the 1 ms polling interval dominates.")
        print("  * 36 MHz and above fall over while the target RUNS (AP reads fail);")
        print("    rtt_link_matrix.tcl shows the failure rate per SWD clock.")
        print("  * rtt_bench.tcl drains the ring inside OpenOCD itself (32-bit reads)")
        print("    and reports both sides: ~1.28 MB/s with a 12 KB ring.")

        try:
            tn.cmd("shutdown")
        except (ConnectionError, OSError):
            pass
        tn.close()
    finally:
        time.sleep(0.5)
        proc.terminate()
        try:
            out = proc.communicate(timeout=5)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            out = ""
        if proc.returncode not in (0, None):
            print(out[-2000:])


if __name__ == "__main__":
    sys.exit(main())
