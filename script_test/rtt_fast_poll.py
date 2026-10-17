#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fast SEGGER RTT poller over akaLinkPro (CMSIS-DAP) + OpenOCD telnet.

OpenOCD's built-in `rtt server` polls at its own cadence (~9 Hz here, caps
throughput at ~9 KB/s regardless of SWD clock). This script polls the RTT
control block directly at full speed: read WrOff/RdOff, drain the new bytes
from the ring buffer, then write RdOff back.

The RdOff write-back is mandatory with this firmware: it uses
SEGGER_RTT_MODE_BLOCK_IF_FIFO_FULL, so the target stalls in the RTT write
until the host frees space by advancing RdOff. g_bytes then cross-checks
the account (target wrote == host received).
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
TELNET_PORT = 4444

CB = 0x2000000C          # _SEGGER_RTT (from fw.map)
G_BYTES, G_LOOPS, G_MS = 0x20000000, 0x20000004, 0x20000008
SPEEDS_KHZ = [1000, 10000, 36000, 60000]
WINDOW_S = 8.0

WORD_RE = re.compile(r"0x[0-9A-Fa-f]{8}:\s*((?:[0-9A-Fa-f]{8}\s*)+)")


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

    def cmd(self, c, timeout=60.0):
        self.sock.settimeout(timeout)
        self.sock.sendall((c + "\n").encode())
        return self.read_until(b"> ")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def mdw_words(tn, addr, n):
    """mdw -> list of ints (little-endian words)."""
    out = tn.cmd(f"mdw 0x{addr:X} {n}")
    words = []
    for m in WORD_RE.finditer(out):
        for w in m.group(1).split():
            words.append(int(w, 16))
    if len(words) < n:
        raise RuntimeError(f"mdw short read: wanted {n}, got {len(words)}\n{out[:300]}")
    return words


def mdb_bytes(tn, addr, n):
    """mdb -> list of ints (bytes)."""
    out = tn.cmd(f"mdb 0x{addr:X} {n}")
    vals = []
    for m in re.finditer(r"0x[0-9A-Fa-f]{8}:\s*((?:[0-9A-Fa-f]{2}\s*)+)", out):
        for b in m.group(1).split():
            vals.append(int(b, 16))
    if len(vals) < n:
        raise RuntimeError(f"mdb short read: wanted {n}, got {len(vals)}")
    return vals


def read_ring(tn, buf_ptr, size, start, count):
    """Read `count` bytes from the ring starting at index `start`."""
    out = bytearray()
    pos = start
    left = count
    while left > 0:
        chunk = min(left, size - pos)
        # word-aligned bulk read, then byte-granular remainder
        whole = chunk & ~3
        if whole:
            words = mdw_words(tn, buf_ptr + pos, whole // 4)
            out.extend(b"".join(w.to_bytes(4, "little") for w in words))
        rem = chunk - whole
        if rem:
            out.extend(bytes(mdb_bytes(tn, buf_ptr + pos + whole, rem)))
        pos = (pos + chunk) % size
        left -= chunk
    return bytes(out)


def main():
    args = [
        OPENOCD,
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

        fw = FW_BIN.replace("\\", "/")
        out = tn.cmd(f'program "{fw}" verify 0x08000000')
        if "Verified OK" not in out:
            raise RuntimeError("flash verify failed:\n" + out[-500:])
        print("flash: programmed + verified OK")
        tn.cmd("reset run")
        time.sleep(0.3)

        # Parse the control block: UpBuffer[0] descriptor at CB+24:
        #   namePtr, pBuffer, SizeOfBuffer, WrOff, RdOff, Flags
        words = mdw_words(tn, CB, 12)
        max_up = words[4]
        if max_up < 1:
            raise RuntimeError("no RTT up buffers")
        buf_ptr, buf_size = words[7], words[8]
        print(f"RTT up buffer: ptr=0x{buf_ptr:08X} size={buf_size} (CB @ 0x{CB:08X})")
        wr_off, rd_off = words[9], words[10]
        RD_OFF_ADDR = CB + 24 + 16  # UpBuffer[0].RdOff

        def drain_once():
            """Drain whatever is available; returns bytes (may be empty)."""
            w = mdw_words(tn, CB + 24 + 12, 2)
            wr, rd = w[0], w[1]
            avail = (wr - rd) % buf_size
            if avail == 0:
                return b"", 0
            data = read_ring(tn, buf_ptr, buf_size, rd, avail)
            rd = (rd + avail) % buf_size
            # mww = memory WRITE word: free ring space so the blocking
            # target can run again (mdw would only *display* the word)
            tn.cmd(f"mww 0x{RD_OFF_ADDR:X} 0x{rd:X}")
            return data, avail

        # drain the pre-window backlog so g_bytes starts near the stream edge
        backlog = 0
        for _ in range(100):
            data, avail = drain_once()
            backlog += len(data)
            if avail == 0:
                break
        print(f"pre-window backlog drained: {backlog} B")

        print(f"\nDraining RTT for {WINDOW_S:.0f}s per SWD speed ...")
        results = []
        for spd in SPEEDS_KHZ:
            try:
                tn.cmd(f"adapter speed {spd}")
            except Exception:
                continue
            label = f"{spd/1000:g} MHz"

            try:
                g0 = mdw_words(tn, G_BYTES, 1)[0]
                ms0 = mdw_words(tn, G_MS, 1)[0]
            except RuntimeError:
                # engine wedged by a previous failed speed: re-sync
                tn.cmd("adapter speed 1000")
                tn.cmd("reset run")
                time.sleep(0.5)
                tn.cmd(f"adapter speed {spd}")
                g0 = mdw_words(tn, G_BYTES, 1)[0]
                ms0 = mdw_words(tn, G_MS, 1)[0]

            total = 0
            polls = 0
            max_avail = 0
            stream = bytearray()
            t0 = time.perf_counter()
            while True:
                now = time.perf_counter()
                if now - t0 >= WINDOW_S:
                    break
                data, avail = drain_once()
                max_avail = max(max_avail, avail)
                stream += data
                total += len(data)
                polls += 1
            dur = time.perf_counter() - t0

            g1 = mdw_words(tn, G_BYTES, 1)[0]
            ms1 = mdw_words(tn, G_MS, 1)[0]
            target_wrote = (g1 - g0) & 0xFFFFFFFF
            kbps = total / dur / 1024.0
            # seamless-check: stream must be whole "hello world!\n" repeats
            pat = b"hello world!\n"
            seamless = (len(stream) % len(pat) == 0) and \
                       all(stream[i:i + 13] == pat for i in range(0, len(stream), 13))
            results.append((label, total, target_wrote, dur, kbps, polls, max_avail, ms1 - ms0, seamless))
            print(f"{label:>8} | host {total:8d} B in {dur:4.1f}s = {kbps:7.1f} KB/s | "
                  f"target wrote {target_wrote:8d} B | {polls} polls | peak fill {max_avail} B | "
                  f"seamless {'yes' if seamless else 'NO'}")

        print()
        print("=== summary (RTT ch0 4KB ring, custom fast poller over CMSIS-DAP v2) ===")
        print(f"{'SWD clock':>9} | {'throughput':>12} | {'seamless':>8}")
        for label, total, tw, dur, kbps, polls, ma, msd, se in results:
            print(f"{label:>9} | {kbps:9.1f} KB/s | {'yes' if se else 'NO':>8}")

        try:
            tn.cmd("shutdown")
        except (ConnectionError, OSError):
            pass
        tn.close()
    finally:
        time.sleep(0.5)
        proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
