#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cdc_test.py —— CherryUSB CDC ACM 固件连通性测试（F103 / H7B0 / H743 通用）

用法：
  # 列出所有串口，挑选目标
  python cdc_test.py --list

  # 跑一遍自检（PING / INFO / MARK / ECHO / 回环吞吐）
  python cdc_test.py --port COM7

  # 只发一条命令
  python cdc_test.py --port COM7 --cmd "INFO"

  # 回环吞吐测试（默认关 echo，然后发 200KB 看回来的字节数）
  python cdc_test.py --port COM7 --speed --kb 200

设计要点（避免踩坑）：
  * 所有读写都有**短超时**（默认 1.5s），绝不无限等；
  * 全程有看门狗，卡住直接退出码 9；
  * 先发 "ECHO off" 关掉固件的原始回环，这样后续响应里不会有命令自身的回声，
    解析起来干净；Echo 测试单独做（ECHO <text> 是显式命令，不受该开关影响）。
"""
import argparse
import sys
import threading
import time

WATCHDOG_S = 120


def _watchdog():
    print(f"[watchdog] 超过 {WATCHDOG_S}s，强制退出", file=sys.stderr)
    sys.exit(9)


def list_ports():
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        print("需要 pyserial：pip install pyserial", file=sys.stderr)
        return 1
    ports = list(lp.comports())
    if not ports:
        print("没有找到任何串口")
        return 1
    print(f"{'端口':<8} {'VID:PID':<12} {'描述'}")
    print("-" * 60)
    for p in ports:
        vidpid = f"{p.vid:04X}:{p.pid:04X}" if p.vid else "-"
        print(f"{p.device:<8} {vidpid:<12} {p.description}")
    return 0


class Cdc:
    def __init__(self, port, baud=115200, timeout=1.5, dtr=True):
        import serial
        self.ser = serial.Serial(port, baudrate=baud, timeout=timeout,
                                 write_timeout=timeout)
        # 必须拉 DTR：固件侧 dtr_enable 为 0 时不会往外发（和真实终端一致）
        self.ser.dtr = dtr
        time.sleep(0.3)
        self.ser.reset_input_buffer()

    def cmd(self, text, wait=0.5, expect=b"\n", timeout=2.0):
        self.ser.reset_input_buffer()
        self.ser.write(text.encode() + b"\r\n")
        self.ser.flush()
        return self.read_until(expect, timeout=timeout)

    def read_until(self, expect, timeout=2.0):
        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline:
            chunk = self.ser.read(256)
            if chunk:
                buf += chunk
                if expect and expect in buf:
                    break
        return buf

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


def run_selftest(port):
    dev = Cdc(port)
    ok = 0
    fail = 0

    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  [PASS] {name} {detail}")
        else:
            fail += 1
            print(f"  [FAIL] {name} {detail}")

    print(f"== {port} 自检 ==")

    # 0) 先关掉原始回环，后续输出干净
    r = dev.cmd("ECHO off")
    print(f"  (ECHO off -> {r!r})")

    # 1) PING
    r = dev.cmd("PING")
    check("PING -> PONG", b"PONG" in r, repr(r))

    # 2) INFO
    r = dev.cmd("INFO", timeout=3.0)
    check("INFO 有 chip=/sysclk=", b"chip=" in r and b"sysclk=" in r, repr(r))

    # 3) MARK（trace 打点）
    r = dev.cmd("MARK")
    check("MARK -> OK", b"OK MARK" in r, repr(r))

    # 4) ECHO <text>
    r = dev.cmd("ECHO hello-cherryusb")
    check("ECHO 回显正确", b"hello-cherryusb" in r, repr(r))

    # 5) 未知命令
    r = dev.cmd("NOSUCHCMD")
    check("未知命令有 ERR", b"ERR" in r, repr(r))

    # 6) 打开回环，验证"收到什么回什么"
    dev.cmd("ECHO on")
    dev.ser.reset_input_buffer()
    dev.ser.write(b"loopback-12345")
    dev.ser.flush()
    r = dev.read_until(b"loopback-12345", timeout=2.0)
    check("原始回环", b"loopback-12345" in r, repr(r))
    dev.cmd("ECHO off")

    dev.close()
    print(f"== 结果：{ok} 通过 / {fail} 失败 ==")
    return 0 if fail == 0 else 1


def run_speed(port, kb):
    dev = Cdc(port, timeout=3.0)
    dev.cmd("ECHO on")
    dev.ser.reset_input_buffer()
    payload = bytes((i & 0xFF) for i in range(64)) * 16  # 1024 字节
    total = kb * 1024
    sent = 0
    got = 0
    t0 = time.time()
    while sent < total:
        n = min(len(payload), total - sent)
        dev.ser.write(payload[:n])
        sent += n
        # 边发边收，避免缓冲打满
        got += len(dev.ser.read(max(1, n)))
    # 收尾
    deadline = time.time() + 5.0
    while got < sent and time.time() < deadline:
        chunk = dev.ser.read(4096)
        if not chunk:
            break
        got += len(chunk)
    dt = time.time() - t0
    dev.cmd("ECHO off")
    dev.close()
    print(f"发出 {sent} 字节，收回 {got} 字节，用时 {dt:.2f}s")
    if dt > 0:
        print(f"回环速率 ≈ {sent / dt / 1024:.1f} KiB/s（含 USB 往返 + 主机轮询开销）")
    print("注意：全速 USB CDC 的理论上限约 1.2 MB/s（12Mbps 总线 - 协议开销）")
    return 0 if got >= sent * 0.99 else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--port")
    ap.add_argument("--cmd")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--speed", action="store_true")
    ap.add_argument("--kb", type=int, default=100)
    args = ap.parse_args()

    threading.Thread(target=_watchdog, daemon=True).start()

    if args.list:
        return list_ports()

    if not args.port:
        print("需要 --port COMx（先 --list 看看有哪些）", file=sys.stderr)
        return 2

    try:
        if args.speed:
            return run_speed(args.port, args.kb)
        if args.cmd:
            dev = Cdc(args.port, baud=args.baud)
            r = dev.cmd(args.cmd, timeout=3.0)
            print(repr(r))
            dev.close()
            return 0
        return run_selftest(args.port)
    except ImportError:
        print("需要 pyserial：pip install pyserial", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
