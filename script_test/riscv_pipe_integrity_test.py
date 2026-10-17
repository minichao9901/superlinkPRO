"""RISC-V 单字流水读（hold/pipe）的**值完整性**验收（代码审查第二round P1-1 的回归）。

靶子：script_test/hpm6800evk_scope（HPM6800EVK / HPM6880，契约块 g_v @ 0x01240000）。
采 u_hi（g_v+4，契约 = 0x10000000 | (tick & 0xFFFF)，高位恒 1）——单变量 u32 会走
`riscv_jtag_hold_prepare/hold_read` 的单字流水（每拍 1 次 DMI 扫描，响应滞后一拍）。

为什么单采速率/斜率不够：hold_read 每 32 拍做一次 SBCS sticky 错误检查，早先的实现
借 dmi_read()（第一个 dmi_post 把 pending 响应丢掉）——在流水模式下被丢的正是上一拍
SBDATA0 读的真值，本拍交付的却是那个 NOP 的响应（data 无意义）。表现为**每 32 拍
静默毁一个样本（约 3%），不报任何错**；而标定（bench）只测时间不验值，端到端斜率
检查对单变量路径又不生效，所以只能用"每个样本都满足契约"来抓。

判据：所有样本 (v >> 28) == 1（SBCS 值的高 4 位是 0，必不满足）。PASS = 0 例违例。

用法：python riscv_pipe_integrity_test.py [--period 100] [--secs 4] [--base 0x01240000]
"""
import argparse
import struct
import sys
import threading
import time

import hid
import usb.core
import usb.util

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

VID, PID = 0x0D28, 0x0204
EP_IN = 0x83
HID_SCOPE, HID_RTT = 0x32, 0x31
ACT_CONFIG, ACT_START, ACT_STOP, ACT_STATUS = 7, 1, 0, 2
SCOPE_FLAG_RISCV = 0x40
MAGIC, PACKET, HEADER = 0x4A53, 512, 16


def open_hid():
    for _ in range(30):
        cand = [i for i in hid.enumerate(VID, PID) if i.get("usage_page") == 0xFF00]
        if cand:
            d = hid.device()
            d.open_path(cand[0]["path"])
            d.set_nonblocking(1)
            time.sleep(0.2)
            return d
        time.sleep(0.3)
    raise RuntimeError("no HID")


def hid_xfer(dev, cmd, data, tmo=3.0):
    req = [0x01, 2 + len(data), cmd] + list(data)
    req += [0] * (64 - len(req))
    dev.write(req)
    t0 = time.time()
    while time.time() - t0 < tmo:
        r = dev.read(64, timeout_ms=200)
        if r and r[0] == 0x02 and r[2] == cmd:
            return list(r)[1:]
    return None


def scope_status(dev):
    r = hid_xfer(dev, HID_SCOPE, [ACT_STATUS])
    if not r:
        return None
    w = [int.from_bytes(bytes(r[3 + i * 4:7 + i * 4]), "little") for i in range(12)]
    return {'startRc': r[2] if r[2] < 128 else r[2] - 256, 'running': w[0] & 1,
            'riscv': (w[0] >> 1) & 1, 'produced': w[2], 'dropped': w[3], 'seq': w[6],
            'swdErr': w[5] & 0xFFFF, 'raw': w}


def find_ep83():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        raise RuntimeError("probe not found (pyusb)")
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass
    for intf in dev.get_active_configuration():
        for ep in intf:
            if ep.bEndpointAddress == EP_IN:
                return dev, intf.bInterfaceNumber
    raise RuntimeError("bulk IN 0x83 not found")


def decode_frames(blob):
    """把字节流切成 (kind, seq, t_us, n, aux, payload) 包；按 magic 重同步。"""
    out = []
    buf = bytearray(blob)
    while len(buf) >= PACKET:
        if int.from_bytes(buf[0:2], "little") != MAGIC:
            i = buf.find(struct.pack('<H', MAGIC), 1)
            if i < 0:
                break
            del buf[:i]
            continue
        p = bytes(buf[:PACKET])
        del buf[:PACKET]
        out.append((p[3], int.from_bytes(p[4:8], 'little'), int.from_bytes(p[8:12], 'little'),
                    int.from_bytes(p[12:14], 'little'), int.from_bytes(p[14:16], 'little'),
                    p[HEADER:]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--period', type=int, default=100, help='采样周期 us（默认 100 = 靶子节拍）')
    ap.add_argument('--secs', type=float, default=4.0)
    ap.add_argument('--base', type=lambda s: int(s, 0), default=0x01240000, help='g_v 地址')
    a = ap.parse_args()

    dev = open_hid()
    # 全局目标类型切 RISC-V（与 flags bit6 双保险）
    hid_xfer(dev, HID_RTT, [10, 1])
    time.sleep(0.05)

    # 单变量 u32 @ u_hi（契约：高 4 位恒 1）→ s_nspans==1 且 4 字节直读 ⇒ 单字流水路径
    cfg = [ACT_CONFIG] + list(struct.pack('<I', a.period)) + [SCOPE_FLAG_RISCV, 1]
    cfg += list(struct.pack('<I', a.base + 4)) + [4, 4]
    assert len(cfg) <= 61
    r = hid_xfer(dev, HID_SCOPE, cfg)
    if not r:
        print("config no response")
        return 1
    st = scope_status(dev)
    print("配置: period=%d us, spans=%d, riscv=%d" % (a.period, (st['raw'][0] >> 8) & 0xFF, st['riscv']))
    if not st['riscv']:
        print("!! 生效后端不是 RISC-V —— 检查固件/靶子")
        return 1

    ud, intf = find_ep83()
    try:
        if ud.is_kernel_driver_active(intf):
            ud.detach_kernel_driver(intf)
    except Exception:
        pass
    usb.util.claim_interface(ud, intf)

    raw = []
    stop = threading.Event()
    nb = {'bytes': 0}

    def reader():
        while not stop.is_set():
            try:
                b = bytes(ep_read(ud))
            except usb.core.USBTimeoutError:
                continue
            except Exception:
                break
            raw.append(b)
            nb['bytes'] += len(b)

    th = threading.Thread(target=reader, daemon=True)
    th.start()

    hid_xfer(dev, HID_SCOPE, [ACT_START])
    rc = -100
    for _ in range(20):
        time.sleep(0.12)
        st = scope_status(dev)
        rc = st['startRc']
        if rc != -100:
            break
    print("启动 rc=%d" % rc)
    if rc != 0:
        stop.set()
        usb.util.dispose_resources(ud)
        return 1

    time.sleep(a.secs)
    stop.set()
    th.join(timeout=1.5)
    hid_xfer(dev, HID_SCOPE, [ACT_STOP])
    # 收尾
    t1 = time.perf_counter()
    while time.perf_counter() - t1 < 0.4:
        try:
            raw.append(bytes(ep_read(ud)))
        except usb.core.USBTimeoutError:
            break
        except Exception:
            break
    usb.util.dispose_resources(ud)
    st = scope_status(dev)

    pkts = decode_frames(b"".join(raw))
    samples = []
    for kind, seq, t_us, n, aux, payload in pkts:
        if kind != 2:
            continue
        for i in range(n):
            samples.append(int.from_bytes(payload[i * 4:i * 4 + 4], 'little'))

    bad = [i for i, v in enumerate(samples) if (v >> 28) != 1]
    n = len(samples)
    print("样本 %d 个（%d 包），违例 %d 个（高位 != 1）" % (n, len(pkts), len(bad)))
    for i in bad[:8]:
        print("  sample[%d] = 0x%08X" % (i, samples[i]))
    if n == 0:
        print("FAIL: 没有样本")
        return 1
    expect_bug = n // 32
    if not bad:
        print("PASS: 每个样本都满足 u_hi 契约（修复前此测试约每 32 拍违例 1 个 ≈ %d）" % expect_bug)
        return 0
    print("FAIL: %d/%d 违例（带 bug 的预期量级 ≈ %d，即样本数/32）" % (len(bad), n, expect_bug))
    return 1


def ep_read(ud, size=16384, timeout=120):
    return ud.read(0x83, size, timeout)


if __name__ == "__main__":
    watchdog = threading.Thread(target=lambda: (time.sleep(120), print("!! WATCHDOG"), sys.exit(9)),
                                daemon=True)
    watchdog.start()
    sys.exit(main())
