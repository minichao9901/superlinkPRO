"""Start OpenOCD, set a fixed SWD speed, transfer 20KB, and show the full log.

Usage: python evk_swd_probe.py [kHz] [repeats]
Prints OpenOCD's own warnings/retries (SWD WAIT/ACK issues) which the speed
benchmark swallows, plus a coarse host CPU-load reading.
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
PORT = 4447

KHZ = int(sys.argv[1]) if len(sys.argv) > 1 else 60000
REPEATS = int(sys.argv[2]) if len(sys.argv) > 2 else 3


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


def main():
    if not os.path.exists(SRC):
        data = bytes((i * 37 + 11) & 0xFF for i in range(SIZE))
        open(SRC, "wb").write(data)

    with open(os.path.join(HERE, "_openocd_probe.log"), "wb") as log:
        proc = subprocess.Popen(
            [OPENOCD, "-d3", "-s", SCRIPTS, "-f", CFG,
             "-c", "gdb port disabled", "-c", "tcl port disabled",
             "-c", f"telnet port {PORT}"],
            stdout=log, stderr=subprocess.STDOUT)

        tn = None
        for _ in range(60):
            if proc.poll() is not None:
                log.flush()
                print(open(os.path.join(HERE, "_openocd_probe.log")).read()[-1500:])
                return 1
            try:
                tn = Telnet(PORT)
                break
            except OSError:
                time.sleep(0.3)
        if tn is None:
            print("no telnet")
            return 1

        print(tn.cmd(f"adapter speed {KHZ}").strip() or "(speed set)")
        for i in range(REPEATS):
            tn.cmd("reset halt")
            t0 = time.perf_counter()
            w = tn.cmd(f'load_image "{SRC}" 0x20000000 bin')
            t1 = time.perf_counter()
            r = tn.cmd(f'dump_image "{DST}" 0x20000000 {SIZE}')
            t2 = time.perf_counter()
            ref = open(SRC, "rb").read()
            got = open(DST, "rb").read() if os.path.exists(DST) else b""
            ok = got == ref
            print("run%d @%d kHz: write %7.1f KB/s  read %7.1f KB/s  verified=%s" %
                  (i + 1, KHZ, SIZE / (t1 - t0) / 1024, SIZE / (t2 - t1) / 1024, ok))
            for tag, out in (("w", w), ("r", r)):
                for line in out.splitlines():
                    if re.search(r"(Warn|warn|Error|error|WAIT|ack|retry|fail)", line):
                        print("   [%s] %s" % (tag, line.strip()))
        try:
            tn.cmd("shutdown")
        except Exception:
            pass
        time.sleep(0.5)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("\n--- openocd log tail ---")
    txt = open(os.path.join(HERE, "_openocd_probe.log"), errors="replace").read()
    for line in txt.splitlines()[-12:]:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
