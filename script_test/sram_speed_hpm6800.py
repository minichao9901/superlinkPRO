#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HPM6800EVK (HPM6880, RISC-V) SRAM read/write throughput over the akaLinkPro
probe in JTAG mode.

Same two calibers as script_test/sram_speed_test.py:
  wall = this script's timer (includes one telnet round trip per command)
  xfer = OpenOCD's own timer, i.e. the target transfer only

The probe must be in SWD+JTAG output mode first:
    python script_test/hpm6800_probe.py set-mode 1

Usage:
    python script_test/sram_speed_hpm6800.py [--size 65536] [--speeds 8000,20000,45000,60000]
                                             [--regions axi,ilm,dlm] [--no-verify]
"""
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SDK_ENV = os.environ.get("HPM_SDK_ENV_DIR", r"E:\sdk_env_v1.11.0")

OPENOCD = os.environ.get("OPENOCD_EXE", os.path.join(SDK_ENV, "tools", "openocd", "openocd.exe"))
SCRIPTS = os.environ.get("OPENOCD_SCRIPTS", os.path.join(SDK_ENV, "tools", "openocd", "tcl"))
SDK_BOARDS = os.path.join(SDK_ENV, "hpm_sdk", "boards", "openocd")
CFG = os.environ.get("OPENOCD_CFG", os.path.join(HERE, "openocd_hpm6800evk_dap.cfg"))

TELNET_PORT = 4444

# HPM6880 on-chip memories (soc/HPM6800/HPM6880/toolchains/gcc/flash_sdram_xip.ld)
REGIONS = {
    "axi": (0x01200000, "AXI_SRAM", 512 * 1024),
    "ilm": (0x00000000, "ILM (core local)", 256 * 1024),
    "dlm": (0x00080000, "DLM (core local)", 256 * 1024),
    "ahb": (0xF0400000, "AHB_SRAM", 32 * 1024),
}

DEFAULT_SIZE = 64 * 1024
DEFAULT_SPEEDS = [1000, 2000, 4000, 8000, 20000, 36000, 45000, 60000]


class Telnet:
    """Minimal OpenOCD telnet client (same contract as sram_speed_test.py)."""

    def __init__(self, port, timeout=60.0):
        import socket
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

    def cmd(self, c, timeout=300.0):
        self.sock.settimeout(timeout)
        self.sock.sendall((c + "\n").encode())
        return self.read_until(b"> ")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def gen_data(n):
    data = bytearray()
    x = 0x12345678
    for _ in range(n):
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        data.append(x & 0xFF)
    return bytes(data)


def ocd_speed(text):
    m = re.search(r"\(([0-9.]+) (KiB|MiB)/s\)", text)
    if not m:
        return None
    v = float(m.group(1))
    return v * 1024.0 if m.group(2) == "MiB" else v


def arg_value(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def main():
    size = int(arg_value("--size", DEFAULT_SIZE))
    speeds = [int(s) for s in str(arg_value("--speeds", ",".join(str(x) for x in DEFAULT_SPEEDS))).split(",")]
    regions = str(arg_value("--regions", "axi")).split(",")
    verify = "--no-verify" not in sys.argv

    src = os.path.join(HERE, "sram_test.bin")
    dst = os.path.join(HERE, "sram_out.bin")
    ref = gen_data(size)
    with open(src, "wb") as f:
        f.write(ref)
    src_t = src.replace("\\", "/")
    dst_t = dst.replace("\\", "/")

    args = [OPENOCD, "-s", SCRIPTS, "-s", SDK_BOARDS, "-f", CFG,
            "-c", "gdb port disabled", "-c", "tcl port disabled", "-c", "telnet port %d" % TELNET_PORT]
    print("starting OpenOCD ...")
    proc = __import__("subprocess").Popen(args, stdout=__import__("subprocess").PIPE,
                                         stderr=__import__("subprocess").STDOUT, text=True)
    try:
        tn = None
        deadline = time.time() + 25
        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.communicate()[0] or ""
                print(out.strip()[-2000:])
                raise RuntimeError("OpenOCD exited early")
            try:
                tn = Telnet(TELNET_PORT)
                break
            except OSError:
                time.sleep(0.3)
        if tn is None:
            raise RuntimeError("no OpenOCD telnet")

        print(tn.cmd("targets").strip())
        print(tn.cmd("halt").strip())
        print()

        results = []
        for key in regions:
            base, label, cap = REGIONS[key]
            if size > cap:
                print("!! %s: %d bytes > %d byte region, skipped" % (label, size, cap))
                continue
            for khz in speeds:
                r = tn.cmd("adapter speed %d" % khz)
                m = re.search(r"clock speed (\d+) kHz", r)
                actual = m.group(1) if m else "?"

                t0 = time.perf_counter()
                out_w = tn.cmd('load_image "%s" 0x%08X bin' % (src_t, base))
                t1 = time.perf_counter()
                out_r = tn.cmd('dump_image "%s" 0x%08X %d' % (dst_t, base, size))
                t2 = time.perf_counter()

                err = "rror" in out_w or "rror" in out_r or "ailed" in out_w or "ailed" in out_r
                data_ok = None
                if verify and not err and os.path.exists(dst):
                    data_ok = open(dst, "rb").read() == ref
                ok = (not err) and (data_ok is not False)

                w = size / (t1 - t0) / 1024.0
                rr = size / (t2 - t1) / 1024.0
                wx, rx = ocd_speed(out_w), ocd_speed(out_r)
                tag = "" if ok else "  <-- FAILED(err=%s data_ok=%s)" % (err, data_ok)
                results.append((label, khz, actual, w, rr, wx, rx, ok))
                print("%-18s rate %6s kHz | write %8.1f KB/s | read %8.1f KB/s | "
                      "xfer w %8s r %8s | %s%s"
                      % (label, actual, w, rr,
                         ("%.1f" % wx) if wx else "n/a", ("%.1f" % rx) if rx else "n/a",
                         "verified" if data_ok else ("no-verify" if data_ok is None else "MISMATCH"), tag))

        print()
        print("=== summary (%d bytes, CMSIS-DAP JTAG + OpenOCD load/dump) ===" % size)
        print("%-18s %8s | %10s | %10s | %10s | %10s" %
              ("region", "rate kHz", "wall w", "wall r", "xfer w", "xfer r"))
        for label, khz, actual, w, rr, wx, rx, ok in results:
            print("%-18s %8s | %9.1fK | %9.1fK | %10s | %10s" %
                  (label, actual, w, rr, ("%.1f" % wx) if wx else "n/a", ("%.1f" % rx) if rx else "n/a"))

        try:
            tn.cmd("resume")
            tn.cmd("shutdown")
        except (ConnectionError, OSError):
            pass
        tn.close()
        return 0
    finally:
        time.sleep(0.4)
        proc.terminate()
        try:
            out = proc.communicate(timeout=5)[0] or ""
        except Exception:
            proc.kill()
            out = ""
        if out and proc.returncode not in (0, None):
            print(out[-2000:])


if __name__ == "__main__":
    sys.exit(main())
