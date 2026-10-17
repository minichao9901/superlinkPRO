"""Does OpenOCD's rtt polling_interval limit the throughput?

Drains the RTT TCP server for a few seconds with different polling intervals
(and once without setting it, which reveals the built-in default), at a fixed
SWD clock with the target clock normalised.

Usage: python rtt_interval_test.py [kHz] [window_s]
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
TELNET_PORT = 4451
RTT_PORT = 21031
KHZ = int(sys.argv[1]) if len(sys.argv) > 1 else 36000
WINDOW_S = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0

RCC_CR, RCC_CFGR, FLASH_ACR = 0x40021000, 0x40021004, 0x40022000
INTERVALS = [None, 10, 5, 1, 0]   # None = leave OpenOCD's default


class Telnet:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.settimeout(30)
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

    def cmd(self, c, timeout=60.0):
        self.sock.settimeout(timeout)
        self.sock.sendall((c + "\n").encode())
        return self.read_until(b"> ")


def rd(tn, addr):
    out = tn.cmd("mdw 0x%08X" % addr)
    m = re.search(r":\s*([0-9a-fA-F]{8})", out)
    return int(m.group(1), 16) if m else None


def boost(tn):
    tn.cmd("reset halt")
    tn.cmd("mww 0x%08X 0x00000012" % FLASH_ACR)
    tn.cmd("mww 0x%08X 0x00380400" % RCC_CFGR)
    cr = rd(tn, RCC_CR) or 0
    tn.cmd("mww 0x%08X 0x%08X" % (RCC_CR, cr | 0x01000000))
    for _ in range(50):
        if (rd(tn, RCC_CR) or 0) & 0x02000000:
            break
        time.sleep(0.02)
    tn.cmd("mww 0x%08X 0x00380402" % RCC_CFGR)
    tn.cmd("reset run")
    time.sleep(0.4)


def drain(window):
    sock = socket.create_connection(("127.0.0.1", RTT_PORT), timeout=10)
    total = 0
    reads = 0
    t0 = time.perf_counter()
    sock.settimeout(0.5)
    while True:
        now = time.perf_counter()
        if now - t0 >= window:
            break
        sock.settimeout(min(0.5, window - (now - t0)))
        try:
            c = sock.recv(65536)
            if c:
                total += len(c)
                reads += 1
        except socket.timeout:
            pass
    dur = time.perf_counter() - t0
    sock.close()
    return total, dur, reads


def main():
    proc = subprocess.Popen([OPENOCD, "-s", SCRIPTS, "-f", CFG,
                             "-c", "gdb port disabled", "-c", "tcl port disabled",
                             "-c", "telnet port %d" % TELNET_PORT],
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        tn = None
        for _ in range(60):
            try:
                tn = Telnet(TELNET_PORT)
                break
            except OSError:
                time.sleep(0.3)
        if tn is None:
            print("no telnet")
            return 1

        boost(tn)
        tn.cmd("adapter speed %d" % KHZ)
        print("SWD %d kHz, target boosted, window %.1fs\n" % (KHZ, WINDOW_S))
        tn.cmd('rtt setup 0x20000000 0x5000 "SEGGER RTT"')
        tn.cmd("rtt start")
        tn.cmd("rtt server start %d 0" % RTT_PORT)
        time.sleep(0.3)

        print("%14s | %10s | %8s | %s" % ("polling_interval", "throughput", "reads/s", "note"))
        for iv in INTERVALS:
            # rtt_speed_test.py sets the interval BEFORE starting the server;
            # restart the server here so each setting is applied the same way.
            tn.cmd("rtt server stop %d" % RTT_PORT)
            if iv is not None:
                tn.cmd("rtt polling_interval %d" % iv)
            tn.cmd("rtt server start %d 0" % RTT_PORT)
            time.sleep(0.3)
            total, dur, reads = drain(WINDOW_S)
            kbs = total / dur / 1024.0
            tag = "OpenOCD default" if iv is None else ""
            if iv == 0:
                tag = "as fast as it can"
            print("%14s | %7.1f KB/s | %8.0f | %s" %
                  ("(unset)" if iv is None else "%d ms" % iv, kbs, reads / dur, tag))

        tn.cmd("shutdown")
    finally:
        time.sleep(0.4)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
