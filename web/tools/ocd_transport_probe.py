#!/usr/bin/env python3
"""探针实验：OpenOCD 的 Tcl RPC(6666) 与 telnet(4444) 各能不能读内存。
结论直接决定 bridge 里 OpenOCD 后端走哪条路。"""
import glob, os, socket, subprocess, sys, time

RAM_ADDR = 0x2f05da08          # rtt_nano_s31 的 _SEGGER_RTT（来自 ELF 符号）
EXPECT = b"SEGGER RTT"

ocd = sorted(glob.glob(os.path.join(os.path.expanduser("~"), ".espressif", "tools",
                                    "openocd-esp32", "*", "openocd-esp32", "bin", "openocd.exe")))[-1]
scripts = os.path.join(os.path.dirname(os.path.dirname(ocd)), "share", "openocd", "scripts")
cfg = os.path.join(scripts, "board", "esp32s31-builtin.cfg")
print("openocd:", ocd)
print("cfg    :", cfg, os.path.exists(cfg))

log = open(os.path.join(os.environ.get("TEMP", "."), "ocd_probe.log"), "wb")
p = subprocess.Popen([ocd, "-s", scripts, "-f", cfg, "-c", "init"],
                     stdout=log, stderr=subprocess.STDOUT)


def wait(port, secs=20):
    t0 = time.time()
    while time.time() - t0 < secs:
        if p.poll() is not None:
            sys.exit("OpenOCD 自己退了，看 ocd_probe.log")
        try:
            s = socket.create_connection(("127.0.0.1", port), 0.3)
            return s
        except OSError:
            time.sleep(0.25)
    sys.exit(f"等 {port} 超时")


def rpc(s, line, timeout=5.0):
    s.sendall(line.encode() + b"\x1a")
    s.settimeout(timeout)
    buf = b""
    while b"\x1a" not in buf:
        try:
            d = s.recv(65536)
        except socket.timeout:
            break
        if not d:
            break
        buf += d
    return buf

try:
    # ---------- A. Tcl RPC ----------
    a = wait(6666)
    r1 = rpc(a, "read_memory 0x%08x 32 4" % RAM_ADDR)
    print("\n[A] RPC read_memory 原始返回 %d 字节: %r" % (len(r1), r1[:40]))
    print("[A] == 'SEGGER RTT' ？", r1[:10] == EXPECT)
    r2 = rpc(a, "mdw 0x%08x 4" % RAM_ADDR)
    print("[A] RPC mdw 文本返回: %r" % r2[:100])
    a.close()

    # ---------- B. telnet ----------
    b = wait(4444)
    time.sleep(0.3)
    try:
        b.recv(65536)
    except socket.timeout:
        pass
    b.sendall(b"mdw 0x%08x 4\n" % RAM_ADDR)
    time.sleep(0.6)
    out = b""
    try:
        while True:
            d = b.recv(65536)
            if not d:
                break
            out += d
            if out.endswith(b"> "):
                break
    except socket.timeout:
        pass
    print("\n[B] telnet mdw 返回: %r" % out[:160])

    tmp = os.path.join(os.environ.get("TEMP", "."), "ocd_probe.bin")
    b.sendall(("dump_image {%s} 0x%08x 1024\n" % (tmp, RAM_ADDR)).encode())
    time.sleep(0.8)
    try:
        b.recv(65536)
    except socket.timeout:
        pass
    data = open(tmp, "rb").read() if os.path.exists(tmp) else b""
    print("[B] dump_image 文件 %d 字节, 开头: %r  匹配=%s" % (len(data), data[:16], data[:10] == EXPECT))
    b.close()
finally:
    p.kill()
    try:
        p.wait(timeout=3)
    except Exception:
        pass
print("\n完成")
