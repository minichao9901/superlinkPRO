#!/usr/bin/env python3
"""实验 3：比一比 read_memory(RPC 文本) 与 dump_image(telnet 二进制) 的吞吐，
决定 bridge 扫描控制块时用哪条路。"""
import glob, os, socket, subprocess, sys, time

ocd = sorted(glob.glob(os.path.join(os.path.expanduser("~"), ".espressif", "tools",
                                    "openocd-esp32", "*", "openocd-esp32", "bin", "openocd.exe")))[-1]
scripts = os.path.join(os.path.dirname(os.path.dirname(ocd)), "share", "openocd", "scripts")
cfg = os.path.join(scripts, "board", "esp32s31-builtin.cfg")
log = open(os.path.join(os.environ.get("TEMP", "."), "ocd_probe3.log"), "wb")
p = subprocess.Popen([ocd, "-s", scripts, "-f", cfg, "-c", "init"], stdout=log, stderr=subprocess.STDOUT)


def wait(port, secs=20):
    t0 = time.time()
    while time.time() - t0 < secs:
        if p.poll() is not None:
            sys.exit("OpenOCD 退了")
        try:
            return socket.create_connection(("127.0.0.1", port), 0.3)
        except OSError:
            time.sleep(0.25)
    sys.exit("等端口超时")


def rpc(s, line, timeout=60.0):
    s.sendall(line.encode() + b"\x1a")
    s.settimeout(timeout)
    buf = b""
    while b"\x1a" not in buf:
        d = s.recv(1 << 20)
        if not d:
            break
        buf += d
    return buf.split(b"\x1a")[0]


def telnet(s, line, timeout=0.5):
    s.sendall(line.encode() + b"\n")
    time.sleep(timeout)
    out = b""
    try:
        while True:
            d = s.recv(65536)
            if not d:
                break
            out += d
    except socket.timeout:
        pass
    return out


a = wait(6666)
try:
    for kb in (4, 16, 64):
        n = kb * 256                                    # 32 位字数
        t0 = time.time()
        r = rpc(a, "read_memory 0x2f000000 32 %d" % n)
        dt = time.time() - t0
        got = len([t for t in r.split() if t.startswith(b"0x")])
        print("RPC  read_memory %3d KB: %5.0f ms → %6.1f KB/s (拿到 %d 字)" % (kb, dt * 1000, kb / dt, got))

    tmp = os.path.join(os.environ.get("TEMP", "."), "ocd_dump.bin")
    for kb in (4, 16, 64, 256):
        t0 = time.time()
        rpc(a, "ocd_dump_image {%s} 0x2f000000 %d" % (tmp, kb * 1024))
        dt = time.time() - t0
        sz = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        print("RPC  dump_image     %3d KB: %5.0f ms → %6.1f KB/s (文件 %d B)" % (kb, dt * 1000, (sz / 1024) / dt, sz))
finally:
    p.kill()
print("完成")
