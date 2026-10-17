"""Is the SRAM throughput limited by the *target's* clock?

Reads the STM32F103 RCC state, measures 20KB SRAM write/read at a fixed SWD
clock, then boosts the target to 64 MHz (PLL from HSI/2 - no crystal needed)
and measures again.

Usage: python evk_target_clock_test.py [kHz]
"""
import os
import re
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SDK_ENV = os.environ.get("HPM_SDK_ENV_DIR", r"E:\sdk_env_v1.11.0")
OPENOCD = os.environ.get("OPENOCD_EXE", os.path.join(SDK_ENV, "tools", "openocd", "openocd.exe"))
SCRIPTS = os.environ.get("OPENOCD_SCRIPTS", os.path.join(SDK_ENV, "tools", "openocd", "tcl"))
CFG = os.path.join(HERE, "openocd_stm32f1_swd.cfg")
SRC = os.path.join(HERE, "sram_test.bin").replace("\\", "/")
DST = os.path.join(HERE, "sram_out.bin").replace("\\", "/")
SIZE = 20 * 1024
PORT = 4448
KHZ = int(sys.argv[1]) if len(sys.argv) > 1 else 60000

RCC_CR = 0x40021000
RCC_CFGR = 0x40021004
FLASH_ACR = 0x40022000

SWS_NAME = {0: "HSI", 1: "HSE", 2: "PLL", 3: "not ready"}
HPRE_DIV = {0: 1, 1: 2, 2: 4, 3: 8, 4: 16, 5: 64, 6: 128, 7: 256,
            8: 2, 9: 4, 10: 8, 11: 16, 12: 64, 13: 128, 14: 256, 15: 512}
PLLMUL = {0: 2, 1: 3, 2: 4, 3: 5, 4: 6, 5: 7, 6: 8, 7: 9, 8: 10, 9: 11,
          10: 12, 11: 13, 12: 14, 13: 15, 14: 16, 15: 16}


class Telnet:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.settimeout(60)
        self.buf = b""
        self.read_until(b"> ")

    def read_until(self, marker):
        while marker not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        out, _, self.buf = self.buf.partition(marker)
        return out.decode("utf-8", "replace")

    def cmd(self, c, timeout=120.0):
        self.sock.settimeout(timeout)
        self.sock.sendall((c + "\n").encode())
        return self.read_until(b"> ")


def rd(tn, addr):
    out = tn.cmd("mdw 0x%08X" % addr)
    m = re.search(r"0x%08x:\s*([0-9a-fA-F]{8})" % addr, out)
    if not m:
        m = re.search(r":\s*([0-9a-fA-F]{8})", out)
    return int(m.group(1), 16) if m else None


def report_clock(tn, tag):
    cr = rd(tn, RCC_CR)
    cfgr = rd(tn, RCC_CFGR)
    acr = rd(tn, FLASH_ACR)
    sws = (cfgr >> 2) & 3
    hpre = (cfgr >> 4) & 0xF
    pllsrc_hsi2 = not (cfgr >> 16) & 1
    pllmul = (cfgr >> 18) & 0xF
    base = {0: 8.0, 1: 8.0, 2: (4.0 if pllsrc_hsi2 else 8.0) * PLLMUL[pllmul]}[sws]
    hclk = base / HPRE_DIV[hpre]
    print("  [%s] RCC_CR=0x%08X CFGR=0x%08X ACR=0x%08X -> SYSCLK src=%s %.1f MHz, HCLK=%.1f MHz, flash latency=%d" %
          (tag, cr, cfgr, acr, SWS_NAME[sws], base, hclk, acr & 7))
    return hclk


def measure(tn, label):
    tn.cmd("reset halt")
    best_w = best_r = 0.0
    ok = True
    for _ in range(2):
        t0 = time.perf_counter()
        tn.cmd('load_image "%s" 0x20000000 bin' % SRC)
        t1 = time.perf_counter()
        tn.cmd('dump_image "%s" 0x20000000 %d' % (DST, SIZE))
        t2 = time.perf_counter()
        ref = open(SRC, "rb").read()
        got = open(DST, "rb").read() if os.path.exists(DST) else b""
        ok = ok and got == ref
        best_w = max(best_w, SIZE / (t1 - t0) / 1024)
        best_r = max(best_r, SIZE / (t2 - t1) / 1024)
    print("  [%s] write %7.1f KB/s   read %7.1f KB/s   verified=%s" % (label, best_w, best_r, ok))
    return best_w


def boost_to_64mhz(tn):
    """HSI/2 * 16 = 64 MHz, AHB /1, APB1 /2, APB2 /1, flash latency 2."""
    tn.cmd("mww 0x%08X 0x00000012" % FLASH_ACR)          # latency 2 + prefetch
    tn.cmd("mww 0x%08X 0x00380400" % RCC_CFGR)           # PLLSRC=HSI/2, x16, HPRE=/1, PPRE1=/2
    cr = rd(tn, RCC_CR) or 0
    tn.cmd("mww 0x%08X 0x%08X" % (RCC_CR, cr | 0x01000000))   # PLLON
    for _ in range(50):
        if (rd(tn, RCC_CR) or 0) & 0x02000000:
            break
        time.sleep(0.02)
    tn.cmd("mww 0x%08X 0x00380402" % RCC_CFGR)           # SW = PLL
    time.sleep(0.05)


def main():
    if not os.path.exists(SRC):
        open(SRC, "wb").write(bytes((i * 37 + 11) & 0xFF for i in range(SIZE)))

    log = open(os.path.join(HERE, "_openocd_probe.log"), "wb")
    proc = subprocess.Popen(
        [OPENOCD, "-s", SCRIPTS, "-f", CFG, "-c", "gdb port disabled",
         "-c", "tcl port disabled", "-c", "telnet port %d" % PORT],
        stdout=log, stderr=subprocess.STDOUT)
    try:
        tn = None
        for _ in range(60):
            if proc.poll() is not None:
                log.flush()
                print(open(os.path.join(HERE, "_openocd_probe.log")).read()[-800:])
                return 1
            try:
                tn = Telnet(PORT)
                break
            except OSError:
                time.sleep(0.3)
        if tn is None:
            print("no telnet")
            return 1

        tn.cmd("adapter speed %d" % KHZ)
        print("SWD clock requested: %d kHz" % KHZ)
        tn.cmd("reset halt")
        report_clock(tn, "as reset")
        measure(tn, "HCLK as-is")

        print("\nboosting the target to 64 MHz (PLL from HSI/2) ...")
        boost_to_64mhz(tn)
        report_clock(tn, "boosted ")
        measure(tn, "HCLK 64 MHz")

        try:
            tn.cmd("shutdown")
        except Exception:
            pass
    finally:
        time.sleep(0.5)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
