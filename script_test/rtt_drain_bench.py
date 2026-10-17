"""RTT drain ceiling: run the poll loop INSIDE OpenOCD (rtt_bench.tcl).

This is the "what can this probe + link actually do" number, as opposed to
rtt_speed_test.py which measures OpenOCD's own rtt server. The TCL version has
no telnet round trips, reads 32-bit words in bounded chunks (long block reads
fail while the target runs) and reports both the poll rate and the target-side
produced bytes.

Usage: python rtt_drain_bench.py [kHz ...]
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SDK_ENV = os.environ.get("HPM_SDK_ENV_DIR", r"E:\sdk_env_v1.11.0")
OPENOCD = os.environ.get("OPENOCD_EXE", os.path.join(SDK_ENV, "tools", "openocd", "openocd.exe"))
SCRIPTS = os.environ.get("OPENOCD_SCRIPTS", os.path.join(SDK_ENV, "tools", "openocd", "tcl"))
CFG = os.path.join(HERE, "openocd_stm32f1_swd.cfg")
TCL = "script_test/rtt_bench.tcl" if os.getcwd().endswith("akaLinkPro") else \
      os.path.join(HERE, "rtt_bench.tcl").replace("\\", "/")

CHUNK = os.environ.get("RTT_CHUNK", "1024")
SPEEDS_KHZ = [int(x) for x in sys.argv[1:]] or [10000, 20000, 36000, 45000, 60000]


def run(khz):
    args = [OPENOCD, "-s", SCRIPTS, "-f", CFG,
            "-c", "gdb port disabled", "-c", "tcl port disabled", "-c", "telnet port disabled",
            "-c", "init", "-c", "reset run",
            "-c", "adapter speed %d" % khz,
            "-c", "set BOOST 1", "-c", "set CHUNK %s" % CHUNK,
            "-c", "source %s" % TCL, "-c", "shutdown"]
    try:
        # OpenOCD sends TCL 'echo' output to stderr, so merge the streams
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=90)
        out = p.stdout or ""
    except subprocess.TimeoutExpired as e:
        return None
    res = {}
    for key, pat in (("drained", r"drained (\d+) bytes in (\d+) ms -> ([\d.]+) KB/s"),
                     ("polls", r"polls=([\d.]+)/s drains=(\d+) empties=(\d+) maxdrain=(\d+) avg=(\d+) B"),
                     ("produced", r"produced (\d+) bytes in (\d+) ms -> ([\d.]+) KB/s"),
                     ("errors", r"errors: badcb=(\d+) cb_read=(\d+) data_read=(\d+) rd_write=(\d+)")):
        m = re.search(pat, out)
        res[key] = m.groups() if m else None
    res["target"] = (re.search(r"target: (.*)", out).group(1)
                     if re.search(r"target: (.*)", out) else "?")
    res["raw"] = out
    return res


def main():
    print("RTT drain inside OpenOCD (32-bit reads, %s B chunks, target clock normalised)" % CHUNK)
    print("%8s | %12s | %10s | %10s | %12s | errors" % ("SWD", "throughput", "polls/s", "bytes/poll", "target made"))
    for khz in SPEEDS_KHZ:
        r = run(khz)
        if not r or not r["drained"]:
            print("%6d k | (run failed - target/link busy?)" % khz)
            for line in (r or {}).get("raw", "").splitlines():
                if "rror" in line:
                    print("           %s" % line.strip())
            continue
        _, _, kbs = r["drained"]
        tps, drains, empties, maxdrain, avg = r["polls"]
        pkbs = r["produced"][2] if r["produced"] else "-"
        errs = sum(int(x) for x in r["errors"]) if r["errors"] else -1
        print("%6d k | %9s KB/s | %9s | %9s B | %9s KB/s | %d" %
              (khz, kbs, tps, avg, pkbs, errs))
    print()
    print("reference: OpenOCD's own rtt server (make rtt-test) reaches ~0.9 MB/s at")
    print("20 MHz; the halted-SRAM number (2.5 MB/s) is not reachable here because RTT")
    print("is polled - every drain costs 3 host<->probe transactions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
