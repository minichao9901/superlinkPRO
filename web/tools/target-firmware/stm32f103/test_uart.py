#!/usr/bin/env python3
"""测试 STM32F103 测试固件的串口侧（DAPLink 的 CDC 桥）。
DAPLink 有两个 CDC（COM66 / COM61），脚本两个都试，看哪个桥到了 PA9/PA10。"""
import sys
import time

import serial

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CMDS = [b"help\r", b"info\r", b"ansi\r", b"hex\r"]


def probe(port):
    try:
        s = serial.Serial(port, 115200, timeout=0.3)
    except Exception as e:
        return f"  {port}: 打不开（{type(e).__name__}: {e}）"
    try:
        s.dtr = False
        s.rts = False
        s.reset_input_buffer()
        time.sleep(0.4)
        banner = s.read(8192)
        out = [f"  {port}: 收到 {len(banner)} 字节"]
        if banner:
            out.append("    首屏: " + repr(banner[:120]))
        total = 0
        for c in CMDS:
            s.write(c)
            time.sleep(0.5)
            r = s.read(8192)
            total += len(r)
            out.append(f"    >>> {c!r} → {len(r)} 字节: " + repr(r[:100]))
        out.append(f"  合计回包 {total} 字节")
        return "\n".join(out)
    finally:
        s.close()


if __name__ == "__main__":
    ports = sys.argv[1:] or ["COM66", "COM61"]
    print("=== STM32F103 串口测试 ===")
    for p in ports:
        print(probe(p))
