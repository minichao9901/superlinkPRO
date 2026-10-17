"""Peek at HPM6800EVK memory through OpenOCD - the *reference* view.

Why this exists: the probe's RISC-V J-Scope backend reads target memory with
**SBA (system bus access)**, which bypasses the CPU's D-cache. OpenOCD can read
the very same address through either channel, and which one it uses is exactly
the question that decides whether a mismatch is a probe bug or a cache effect:

  * `--mode progbuf` - OpenOCD loads a small program into the target, so the
    load is issued **by the CPU** and the D-cache is in the path (fresh values
    as soon as the core has written them, even if never written back).
  * `--mode sysbus`  - OpenOCD drives the debug module's system bus master:
    the *same* path the probe uses (main memory, D-cache bypassed).

Same snapshot, same TAP master, two channels => a clean cache/no-cache A/B.

⚠️ OpenOCD's `init` halts the core; every run here ends with an explicit
`resume` so the target keeps running afterwards (a leftover `halt` looks
exactly like "the fixture froze", which cost a debugging session once).

Usage:
  python hpm6800_peek.py 0x12001d8 8                 # default channel
  python hpm6800_peek.py 0x12001d8 8 --mode sysbus
  python hpm6800_peek.py 0x12001d8 8 --mode progbuf --repeat 3
"""
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SDK_ENV = os.environ.get("HPM_SDK_ENV_DIR", r"E:\sdk_env_v1.11.0")
OPENOCD = os.path.join(SDK_ENV, "tools", "openocd", "openocd.exe")
SCRIPTS = os.path.join(SDK_ENV, "tools", "openocd", "tcl")
SDK_BOARDS = os.path.join(SDK_ENV, "hpm_sdk", "boards", "openocd")
CFG = os.path.join(HERE, "openocd_hpm6800evk_dap.cfg")


def read_words(addr, words, mode=None, resume=True):
    cmds = ["-c", "init", "-c", "halt"]
    if mode:
        # OpenOCD defaults to "progbuf sysbus"; pin it down to one channel.
        cmds += ["-c", "riscv set_mem_access %s" % mode]
    cmds += ["-c", "mdw 0x%X %d" % (addr, words)]
    if resume:
        cmds += ["-c", "resume"]
    cmds += ["-c", "shutdown"]

    args = [OPENOCD, "-s", SCRIPTS, "-s", SDK_BOARDS, "-f", CFG] + cmds
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    out = (r.stdout or "") + (r.stderr or "")
    vals = []
    for line in out.splitlines():
        m = re.match(r"^0x([0-9a-fA-F]{8}):\s*(.*)$", line.strip())
        if m and int(m.group(1), 16) == addr:
            for tok in m.group(2).split():
                if re.fullmatch(r"[0-9a-fA-F]{8}", tok):
                    vals.append(int(tok, 16))
    err = [l for l in out.splitlines() if "rror" in l or "not " in l and "available" in l]
    return vals, err, out


def dual(addr, words, settle_ms=0):
    """One session: read the same words through both channels, back to back.

    progbuf -> the CPU issues the load, so the D-cache is in the path.
    sysbus  -> the debug module's own bus master, D-cache bypassed (== the probe).

    Equal values  => cache is transparent for this buffer (writeback works).
    progbuf != sysbus => the cache holds data main memory has never seen.
    """
    cmds = ["-c", "init", "-c", "halt", "-c", "reg pc"]
    if settle_ms:
        # let the core run for a while first, then halt and compare again
        cmds += ["-c", "resume", "-c", "sleep %d" % settle_ms, "-c", "halt", "-c", "reg pc"]
    cmds += [
        "-c", "riscv set_mem_access progbuf",
        "-c", "mdw 0x%X %d" % (addr, words),
        "-c", "riscv set_mem_access sysbus",
        "-c", "mdw 0x%X %d" % (addr, words),
        "-c", "resume",
        "-c", "shutdown",
    ]
    args = [OPENOCD, "-s", SCRIPTS, "-s", SDK_BOARDS, "-f", CFG] + cmds
    r = subprocess.run(args, capture_output=True, text=True, timeout=120)
    out = (r.stdout or "") + (r.stderr or "")
    pcs, rows = [], []
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("pc ") or s.startswith("pc/"):
            pcs.append(s)
        m = re.match(r"^0x([0-9a-fA-F]{8}):\s*(.*)$", s)
        if m and int(m.group(1), 16) == addr:
            vals = [int(t, 16) for t in m.group(2).split() if re.fullmatch(r"[0-9a-fA-F]{8}", t)]
            if vals:
                rows.append(vals)
    print("  pc: " + " | ".join(pcs))
    for i, row in enumerate(rows):
        print("  %-8s %s" % (["progbuf", "sysbus"][i] if i < 2 else "extra", " ".join("%08X" % v for v in row)))
    if len(rows) >= 2 and rows[0] == rows[1]:
        print("  => 两通道一致（缓存对该缓冲区透明 / 或核已停）")
    elif len(rows) >= 2:
        print("  => 两通道不一致：cache 与 SRAM 分叉（写回没生效）")
    return 0


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    addr = int(argv[0], 0)
    words = int(argv[1], 0) if len(argv) > 1 and not argv[1].startswith("--") else 8
    mode = None
    if "--mode" in argv:
        mode = argv[argv.index("--mode") + 1]
    rep = int(argv[argv.index("--repeat") + 1]) if "--repeat" in argv else 1
    if "--dual" in argv:
        ms = int(argv[argv.index("--dual") + 1]) if argv.index("--dual") + 1 < len(argv) else 0
        return dual(addr, words, ms)

    for i in range(rep):
        t0 = time.time()
        vals, err, out = read_words(addr, words, mode)
        print("0x%08X x%d mode=%s (%.1fs): %s" % (
            addr, words, mode or "default", time.time() - t0,
            " ".join("%08X" % v for v in vals) or "<no data>"))
        for e in err[:3]:
            print("   ! " + e.strip())
        if not vals:
            print("   raw tail: " + " | ".join(out.strip().splitlines()[-3:]))
        if i + 1 < rep:
            time.sleep(0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
