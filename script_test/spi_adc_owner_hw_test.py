"""Hardware regression: the shared SPI/ADC bulk buffers must never wedge.

  py script_test/spi_adc_owner_hw_test.py

Background (2026-10-06 field failure): SPI bridge STATUS reported enabled=0,
active=0, cs=0, frames_ok=0 and the ADC stream reported owned=0, yet HID CAPS
kept returning flags=2. The web therefore refused to start ADC and no host
command could clear the bit -> the probe had to be unplugged.

The current admission fence includes native USB ownership. DRAIN with no bulk
reader must block ADC OPEN; disabling/ABORT cannot cancel the armed transfer.
This HID-only test checks rejection, not successful bulk handover. Run the ADC
WebUSB finite/continuous tests separately to verify real IN/OUT retirement.
"""
import sys
import threading
import time

import hid

VID, PID = 0x0D28, 0x0204
USAGE_PAGE = 0xFF00

SB_CMD = 0x35
SB_STATUS, SB_ENABLE, SB_RESET, SB_ABORT, SB_DRAIN = 0, 1, 2, 6, 9
SB_ST_ENABLED = 1

ADC_CMD = 0x38
ADC_CAPS, ADC_OPEN, ADC_END, ADC_CLOSE, ADC_STATUS = 9, 10, 11, 12, 14
ADC_BUSY = 2

checks = 0
failures = 0


def check(condition, label, extra=""):
    global checks, failures
    checks += 1
    if condition:
        print("  PASS  %s" % label)
    else:
        failures += 1
        print("  FAIL  %s%s" % (label, (" · " + extra) if extra else ""))


def watchdog(sec):
    def _fire():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT (%ss)" % sec)
        sys.stdout.flush()
        import os
        os._exit(9)
    threading.Thread(target=_fire, daemon=True).start()


def open_hid(timeout=8.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            infos = [i for i in hid.enumerate(VID, PID) if i.get("usage_page") == USAGE_PAGE]
            if infos:
                dev = hid.device()
                dev.open_path(infos[0]["path"])
                dev.set_nonblocking(1)
                time.sleep(0.15)
                return dev
        except Exception as exc:
            last = exc
        time.sleep(0.3)
    raise SystemExit("probe custom HID interface not found (%r)" % (last,))


def xfer(dev, cmd, args=(), tmo=1.5):
    req = [0x01, 1 + len(args), cmd] + list(args)
    req += [0] * (64 - len(req))
    dev.write(req)
    deadline = time.time() + tmo
    while time.time() < deadline:
        data = dev.read(64, timeout_ms=150)
        if data and data[0] == 0x02 and data[2] == cmd:
            return list(data)
    return None


def adc_flags(dev):
    res = xfer(dev, ADC_CMD, (ADC_CAPS,))
    if not res or res[1] != 28:
        raise SystemExit("ADC CAPS reply malformed: %r" % (res,))
    if list(res[8:12]) != [65, 68, 66, 50]:  # 'ADB2'
        raise SystemExit("ADC CAPS signature missing: %r" % (res[8:12],))
    return res[24], res[25]


def spi_status(dev):
    res = xfer(dev, SB_CMD, (SB_STATUS,))
    if not res:
        raise SystemExit("SPI STATUS reply missing")
    word = res[4] | (res[5] << 8) | (res[6] << 16) | (res[7] << 24)
    return word


def spi_word(dev, action, args=()):
    res = xfer(dev, SB_CMD, (action,) + tuple(args))
    if not res:
        raise SystemExit("SPI action %d reply missing" % action)
    return res[4] | (res[5] << 8) | (res[6] << 16) | (res[7] << 24)


def adc_open(dev, bits=16, rate=1000, count=4):
    args = (bits, rate & 0xFF, (rate >> 8) & 0xFF, (rate >> 16) & 0xFF, (rate >> 24) & 0xFF,
            count & 0xFF, (count >> 8) & 0xFF, (count >> 16) & 0xFF, (count >> 24) & 0xFF)
    res = xfer(dev, ADC_CMD, (ADC_OPEN,) + args)
    if not res:
        raise SystemExit("ADC OPEN reply missing")
    return res[4]


def main():
    dev = open_hid()
    # Leave no state behind from a previous (possibly failed) run: whatever the bridge
    # was doing, disable it and drop its queues before measuring the baseline.
    spi_word(dev, SB_ENABLE, (0,))
    spi_word(dev, SB_ABORT)
    time.sleep(0.4)
    check(spi_status(dev) & SB_ST_ENABLED == 0, "baseline: SPI/QSPI bridge is disabled")
    start_flags, supported = adc_flags(dev)
    check(supported == 1, "CAPS reports the shared-buffer ADC DMA")
    check(start_flags & 2 == 0, "baseline: shared buffers are not owned", "flags=%d" % start_flags)

    # 1) DRAIN leaves the IN ring busy with no reader. This is the state the old
    #    firmware folded into bit1 and could never clear.
    spi_word(dev, SB_DRAIN, (4,))
    time.sleep(0.3)
    flags, _ = adc_flags(dev)
    check(flags & 2 != 0, "DRAIN without a reader retains native IN ownership", "flags=%d" % flags)
    check(adc_open(dev) == ADC_BUSY, "ADC OPEN cannot overwrite the unread IN DMA buffer")

    # 2) A really enabled bridge must still exclude ADC.
    spi_word(dev, SB_ENABLE, (1,))
    time.sleep(0.4)
    check(spi_status(dev) & SB_ST_ENABLED != 0, "ENABLE 1 shows up in the status word")
    flags, _ = adc_flags(dev)
    check(flags & 2 != 0, "an enabled bridge reports bit1", "flags=%d" % flags)
    check(adc_open(dev) == ADC_BUSY, "ADC OPEN is refused while the bridge owns the buffers")
    spi_word(dev, SB_DRAIN, (2,))
    time.sleep(0.2)
    flags, _ = adc_flags(dev)
    check(flags & 2 != 0, "DRAIN does not release the mutual exclusion", "flags=%d" % flags)

    # 3) Disable does not cancel the IN DMA started by DRAIN (no bulk reader).
    spi_word(dev, SB_ENABLE, (0,))
    time.sleep(0.4)
    flags, _ = adc_flags(dev)
    check(spi_status(dev) & SB_ST_ENABLED == 0, "ENABLE 0 clears enabled in the status word")
    check(flags & 2 != 0, "disable preserves pending native IN ownership", "flags=%d" % flags)
    check(adc_open(dev) == ADC_BUSY, "disabled bridge still fences the pending native DMA")

    # 4) Residual control-path noise must not resurrect the busy bit.
    spi_word(dev, SB_ABORT)
    spi_word(dev, SB_RESET)
    spi_word(dev, SB_DRAIN, (4,))
    time.sleep(0.3)
    flags, _ = adc_flags(dev)
    check(flags & 2 != 0, "ABORT/RESET/DRAIN do not erase native IN ownership", "flags=%d" % flags)

    print()
    print("SPI/ADC shared-buffer hardware check: %d passed / %d failed" % (checks - failures, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    watchdog(60)
    sys.exit(main())
