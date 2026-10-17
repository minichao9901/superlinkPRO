#!/usr/bin/env python3
"""探针活性分级诊断：把"探针不应答"拆成 USB 栈 / 主循环 / 各类命令 三层。

层级含义（从硬到软）：
  L1 EP0 控制传输   —— USB ISR + 协议栈活着吗（失败=CPU 挂死或 USB 外设死）
  L2 CDC 串口收发   —— 主循环还转吗（需要 J3 回环跳线；只写不读也能看出端口能否打开）
  L3 HID 自定义命令 —— 主循环里的 HID 请求分发路径（0x31 RTT / 0x36 I2C）
  L4 DAP/bulk       —— 上层协议通道

用法：
  py script_test/probe_liveness.py            # 全量
  py script_test/probe_liveness.py --quick    # 只测 L1+L3
"""
import argparse
import os
import sys
import time

VID = 0x0D28
PID = 0x0204
HID_USAGE_PAGE = 0xFF00  # 自定义 HID 接口


def _t0():
    return time.perf_counter()


def l1_ep0():
    """L1：向 EP0 发一次真正的控制传输（取 product string 描述符）。"""
    try:
        import usb.core
        import usb.util
    except ImportError as e:
        return None, f"pyusb 不可用: {e}"
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        return None, "libusb 找不到 0d28:0204（设备没上总线？）"
    t = _t0()
    try:
        # langid 0x0409 + product string index 2
        s = dev.ctrl_transfer(0x80, 0x06, (0x03 << 8) | 2, 0x0409, 255, timeout=1000)
        txt = ""
        try:
            txt = s[2:].tobytes().decode("utf-16-le", "replace").rstrip("\x00")
        except Exception:
            pass
        return True, f"{_t0()-t:.3f}s 取回 {len(s)} B：{txt!r}"
    except Exception as e:
        return False, f"{_t0()-t:.3f}s {type(e).__name__}: {e}"
    finally:
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass


def l1b_descriptor():
    """L1b：设备描述符（比 string 更靠底层，能确认 SETUP/DATA 两向都通）。"""
    try:
        import usb.core
        import usb.util
    except ImportError:
        return None, "pyusb 不可用"
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        return None, "找不到设备"
    t = _t0()
    try:
        d = dev.ctrl_transfer(0x80, 0x06, 0x0100, 0, 18, timeout=1000)
        return True, f"{_t0()-t:.3f}s bcdUSB=0x{d[2]:02x}{d[3]:02x} iProduct={d[15]}"
    except Exception as e:
        return False, f"{_t0()-t:.3f}s {type(e).__name__}: {e}"
    finally:
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass


def l2_cdc(port, wait=1.0):
    """L2：打开 CDC 并做一次回环收发（没跳线则只有 open 结果有意义）。"""
    try:
        import serial
    except ImportError as e:
        return None, f"pyserial 不可用: {e}"
    t = _t0()
    try:
        with serial.Serial(port, 115200, timeout=0.05, write_timeout=1.0) as ser:
            open_t = _t0() - t
            ser.reset_input_buffer()
            payload = b"LIVENESS\n"
            ser.write(payload)
            ser.flush()
            got = b""
            end = time.time() + wait
            while time.time() < end and len(got) < len(payload):
                got += ser.read(64)
        if got == payload:
            return True, f"open {open_t:.3f}s，回环 {len(got)} B 一致"
        return False, f"open {open_t:.3f}s，回环收到 {len(got)} B {got!r}（跳线没接？）"
    except Exception as e:
        return False, f"{_t0()-t:.3f}s {type(e).__name__}: {e}"


def l3_hid(action=0, cmd=0x36, timeout=1.0):
    """L3：HID 自定义命令一次往返。缺省 0x36 action 0 = I2C STATUS。"""
    try:
        import hid
    except ImportError as e:
        return None, f"hid 库不可用: {e}"
    path = None
    for d in hid.enumerate(VID, PID):
        if d.get("usage_page") == HID_USAGE_PAGE and d.get("interface_number", 0) != 0:
            path = d["path"]
            break
    if path is None:
        for d in hid.enumerate(VID, PID):
            if d.get("usage_page") == HID_USAGE_PAGE:
                path = d["path"]
                break
    if path is None:
        return None, "找不到自定义 HID 接口（vendor-defined）"
    t = _t0()
    try:
        h = hid.device()
        h.open_path(path)
        h.set_nonblocking(1)
        try:
            # 报文 = 报告号(1) + 63 字节载荷，**总共 64 字节**；req[1] 按
            # i2c_bridge_test.py 的约定写 `2 + len(data)`（data=[action] ⇒ 3）。
            # 🚨 写成 0 的话命令会被当空请求丢掉 —— 这一条先后坑了两次。
            req = [0x01, 0x03, cmd, action] + [0] * 60
            h.write(req)
            end = time.time() + timeout
            while time.time() < end:
                r = h.read(64, timeout_ms=100)
                if r and len(r) >= 4 and r[0] == 0x02 and r[2] == cmd and r[3] == action:
                    st = int.from_bytes(bytes(r[4:8]), "little") if len(r) >= 8 else 0
                    return True, f"{_t0()-t:.3f}s status=0x{st:08x} len={r[1]}"
            return False, f"{_t0()-t:.3f}s 超时：写进去了但没等到应答（cmd=0x{cmd:02x} action={action}）"
        finally:
            h.close()
    except Exception as e:
        return False, f"{_t0()-t:.3f}s {type(e).__name__}: {e}"


def _show(tag, res):
    if res is None:
        print(f"  {tag}: 跳过（{res if isinstance(res, str) else '未知'}）")
        return
    ok, msg = res
    print(f"  {tag}: {'OK  ' if ok else 'FAIL'} {msg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None, help="CDC 串口名，缺省自动找")
    ap.add_argument("--quick", action="store_true", help="只测 L1 + L3")
    ap.add_argument("--i2c", action="store_true",
                    help="额外发一条 HID 0x36（I2C STATUS）；旧固件上这条会把探针挂死")
    args = ap.parse_args()

    port = args.port
    if port is None and not args.quick:
        try:
            from serial.tools import list_ports
            cands = [p.device for p in list_ports.comports()
                     if (p.vid, p.pid) == (VID, PID)]
            port = cands[0] if cands else None
        except Exception:
            port = None

    print(f"== 探针活性诊断 ({time.strftime('%H:%M:%S')}) ==")
    _show("L1  EP0 设备描述符", l1b_descriptor())
    _show("L1  EP0 字符串描述符", l1_ep0())
    if not args.quick:
        if port:
            _show(f"L2  CDC {port} 回环", l2_cdc(port))
        else:
            print("  L2  CDC: 跳过（没找到 CDC 口）")
    _show("L3  HID 0x31 action 0 (RTT STATUS)", l3_hid(0, cmd=0x31))
    if args.i2c:
        _show("L3  HID 0x36 action 0 (I2C STATUS)", l3_hid(0))
    else:
        print("  L3  HID 0x36: 跳过（2026-10-02 前的固件上，这条命令会让探针挂在 ISR 里；"
              "要测请加 --i2c，并确认固件已含修复）")


if __name__ == "__main__":
    sys.exit(main() or 0)
