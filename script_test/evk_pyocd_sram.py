"""STM32F103 SRAM read/write throughput measured through pyOCD (not OpenOCD).

Cross-checks the OpenOCD numbers with a completely different host stack, so a
host-side bottleneck can be told apart from a device-side one.

Usage: python evk_pyocd_sram.py [kHz ...]      (needs pyocd in the interpreter)
"""
import sys
import time

from pyocd.core.helpers import ConnectHelper

UID_PREFIX = "B4444F"      # akaLinkPro on HPM5301EVKLite
ADDR = 0x20000000
SIZE = 20 * 1024
SPEEDS_KHZ = [int(x) for x in sys.argv[1:]] or [20000, 60000]
ROUNDS = 3


def pick_probe():
    for s in ConnectHelper.get_sessions_for_all_connected_probes():
        p = s.probe
        if p.unique_id.upper().startswith(UID_PREFIX):
            return p.unique_id
    raise RuntimeError("probe not found")


def main():
    uid = pick_probe()
    payload = bytes((i * 37 + 11) & 0xFF for i in range(SIZE))

    print("%8s | %12s | %12s | verify" % ("SWD", "write", "read"))
    for khz in SPEEDS_KHZ:
        session = ConnectHelper.session_with_chosen_probe(
            unique_id=uid,
            target_override="stm32f103ze",
            blocking=False,
            options={"frequency": khz * 1000, "cmsis_dap.prefer_v1": False},
        )
        try:
            target = session.target
            target.halt()
            wbest = rbest = 0.0
            ok = True
            for _ in range(ROUNDS):
                t0 = time.perf_counter()
                target.write_memory_block8(ADDR, payload)
                t1 = time.perf_counter()
                got = bytes(target.read_memory_block8(ADDR, SIZE))
                t2 = time.perf_counter()
                if got != payload:
                    ok = False
                wbest = max(wbest, SIZE / (t1 - t0) / 1024)
                rbest = max(rbest, SIZE / (t2 - t1) / 1024)
            print("%5d kHz | %8.1f KB/s | %8.1f KB/s | %s" %
                  (khz, wbest, rbest, "OK" if ok else "MISMATCH"))
        finally:
            session.close()


if __name__ == "__main__":
    main()
