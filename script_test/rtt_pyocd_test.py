#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SEGGER RTT throughput test via pyOCD (prefers the CMSIS-DAP v2 bulk backend).

Same methodology as rtt_fast_poll.py (OpenOCD telnet), but the host polls the
RTT control block through pyOCD's DAP layer with no text-protocol overhead:
  - read WrOff/RdOff (one 2-word read)
  - drain the ring (block read)
  - write RdOff back (freeing space for the BLOCK_IF_FIFO_FULL firmware)
  - verify the stream is seamless "hello world!\\n" repeats
  - cross-check target-side g_bytes

The firmware is script_test/stm32f103_rtt_speed (already flashed on the F103).
Run with the Python that has pyocd installed (pyocd[usb] for the v2 backend).
"""
import sys
import time

from pyocd.core.helpers import ConnectHelper

TARGET_UID_PREFIX = "B4444F"      # akaLinkPro on HPM5301EVKLite (OTP UID serial)
CB = 0x2000000C                   # _SEGGER_RTT control block
G_BYTES = 0x20000000
WR_ADDR = CB + 24 + 12            # UpBuffer[0].WrOff
RD_ADDR = CB + 24 + 16            # UpBuffer[0].RdOff
SPEEDS_HZ = [1_000_000, 10_000_000, 36_000_000, 60_000_000]
WINDOW_S = 8.0


def pick_probe():
    sessions = ConnectHelper.get_sessions_for_all_connected_probes()
    uids = []
    for s in sessions:
        p = s.probe
        print(f"  probe: {p.vendor_name} {p.product_name} [{p.unique_id}]")
        uids.append(p.unique_id)
    for u in uids:
        if u.upper().startswith(TARGET_UID_PREFIX):
            return u
    raise RuntimeError("akaLinkPro probe not found (UID prefix " + TARGET_UID_PREFIX + ")")


def main():
    print("probes:")
    uid = pick_probe()
    print(f"\ndraining RTT {WINDOW_S:.0f}s per SWD speed (pyOCD) ...\n")

    session = ConnectHelper.session_with_chosen_probe(
        unique_id=uid,
           # proper F1 target: creates the core
        blocking=False,
        options={
            "target_override": "stm32f103c8",
            "frequency": SPEEDS_HZ[0],
            "cmsis_dap.prefer_v1": False,   # prefer the v2 bulk backend
        },
    )
    # NOTE: session_with_chosen_probe() returns an already-opened session
    # (connected in halt mode); do NOT open it again.

    results = []
    try:
        target = session.target
        probe = session.probe
        target.resume()                   # target must run for RTT

        ident_words = target.read32(CB, 4)
        ident = b"".join(w.to_bytes(4, "little") for w in ident_words)
        if not ident.startswith(b"SEGGER RTT"):
            raise RuntimeError(f"bad RTT id: {ident!r}")
        name_ptr, buf_ptr, size, wr, rd = target.read32(CB + 24, 5)
        print(f"  ring: ptr=0x{buf_ptr:08X} size={size} wr={wr} rd={rd}\n")

        def read_ring(addr, count):
            """Read `count` bytes (any alignment, no wrap handling)."""
            out = bytearray()
            head = (-addr) % 4
            if head:
                head = min(head, count)
                out += bytes(target.read8(addr, head))
                addr += head
                count -= head
            words = count // 4
            if words:
                out += b"".join(w.to_bytes(4, "little")
                                for w in target.read_memory_block32(addr, words))
                addr += words * 4
                count -= words * 4
            if count:
                out += bytes(target.read8(addr, count))
            return bytes(out)

        def drain_once():
            wr, rd = target.read32(WR_ADDR, 2)
            avail = (wr - rd) % size
            if avail == 0:
                return b"", 0
            if rd + avail <= size:
                data = read_ring(buf_ptr + rd, avail)
            else:
                data = (read_ring(buf_ptr + rd, size - rd) +
                        read_ring(buf_ptr, avail - (size - rd)))
            target.write32(RD_ADDR, (rd + avail) % size)
            return data, avail

        for f in SPEEDS_HZ:
            label = f"{f/1e6:g} MHz"
            try:
                try:
                    probe.set_clock(f)
                except Exception as e:
                    print(f"  (set_clock failed: {e})")

                # pre-drain backlog, bounded by time
                t0 = time.perf_counter()
                while time.perf_counter() - t0 < 1.0:
                    d, a = drain_once()
                    if a == 0:
                        break

                g0 = target.read32(G_BYTES, 1)[0]
                stream = bytearray()
                polls = 0
                max_fill = 0
                t0 = time.perf_counter()
                while True:
                    if time.perf_counter() - t0 >= WINDOW_S:
                        break
                    d, a = drain_once()
                    max_fill = max(max_fill, a)
                    stream += d
                    polls += 1
                dur = time.perf_counter() - t0
                total = len(stream)

                g1 = target.read32(G_BYTES, 1)[0]
                target_wrote = (g1 - g0) & 0xFFFFFFFF

                pat = b"hello world!\n"
                seamless = (total % len(pat) == 0) and \
                           all(stream[i:i + 13] == pat for i in range(0, total, 13))

                kbps = total / dur / 1024.0
                results.append((label, kbps, seamless))
                print(f"{label:>8} | host {total:8d} B in {dur:4.1f}s = {kbps:7.1f} KB/s | "
                      f"target wrote {target_wrote:8d} B | {polls} polls | "
                      f"peak fill {max_fill} B | seamless {'yes' if seamless else 'NO'}")
            except Exception as e:
                print(f"{label:>8} | FAILED: {type(e).__name__}: {e}")
                break
    finally:
        try:
            session.close()
        except Exception:
            pass

    print("\n=== summary (RTT ch0 4KB ring, pyOCD) ===")
    print(f"{'SWD clock':>9} | {'throughput':>12} | {'seamless':>8}")
    for label, kbps, se in results:
        print(f"{label:>9} | {kbps:9.1f} KB/s | {'yes' if se else 'NO':>8}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
