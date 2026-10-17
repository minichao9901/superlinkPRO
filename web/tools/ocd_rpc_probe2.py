#!/usr/bin/env python3
"""实验 2：RPC 的 read_memory/write_memory 支持到什么程度（大块、非对齐、写回），
以及 reset/halt/resume。决定 bridge 里 OpenOCD 后端的实现细节。"""
import glob, os, socket, subprocess, sys, time

ocd = sorted(glob.glob(os.path.join(os.path.expanduser("~"), ".espressif", "tools",
                                    "openocd-esp32", "*", "openocd-esp32", "bin", "openocd.exe")))[-1]
scripts = os.path.join(os.path.dirname(os.path.dirname(ocd)), "share", "openocd", "scripts")
cfg = os.path.join(scripts, "board", "esp32s31-builtin.cfg")
log = open(os.path.join(os.environ.get("TEMP", "."), "ocd_probe2.log"), "wb")
p = subprocess.Popen([ocd, "-s", scripts, "-f", cfg, "-c", "init"], stdout=log, stderr=subprocess.STDOUT)


def wait(port, secs=20):
    t0 = time.time()
    while time.time() - t0 < secs:
        if p.poll() is not None:
            sys.exit("OpenOCD 退了，看 ocd_probe2.log")
        try:
            return socket.create_connection(("127.0.0.1", port), 0.3)
        except OSError:
            time.sleep(0.25)
    sys.exit("等端口超时")


def rpc(s, line, timeout=15.0):
    s.sendall(line.encode() + b"\x1a")
    s.settimeout(timeout)
    buf = b""
    while b"\x1a" not in buf:
        try:
            d = s.recv(1 << 20)
        except socket.timeout:
            break
        if not d:
            break
        buf += d
    return buf.split(b"\x1a")[0]


def words(resp):
    return [int(t, 16) for t in resp.decode("ascii", "replace").split() if t.startswith("0x")]


s = wait(6666)
try:
    # A. 大块读（4KB=1024 字）
    for n in (64, 512, 1024, 4096):
        t0 = time.time()
        r = rpc(s, "read_memory 0x2f05da08 32 %d" % n)
        w = words(r)
        print("[A] read %5d 字 = %6d B: 拿到 %5d 字, %.0f ms, 首字=%s" %
              (n, n * 4, len(w), (time.time() - t0) * 1000, hex(w[0]) if w else "None"))

    # B. 非对齐 / 字节粒度
    r = rpc(s, "read_memory 0x2f05da09 8 8")
    print("[B] 8 位宽读 8 字节@+1:", [hex(x) for x in words(r)])
    r = rpc(s, "mdw 0x2f05da08 2")
    print("[B] mdw 对照:", r.decode("ascii", "replace").strip())

    # C. 写回（找一块 RAM 里的空区域做读-改-写测试）
    addr = 0x2f05f000
    before = words(rpc(s, "read_memory 0x%08x 32 2" % addr))
    rpc(s, "write_memory 0x%08x 32 {0xA1A2A3A4 0xA5A6A7A8}" % addr)
    after = words(rpc(s, "read_memory 0x%08x 32 2" % addr))
    print("[C] 写前=%s 写后=%s" % ([hex(x) for x in before], [hex(x) for x in after]))
    r = rpc(s, "write_memory 0x%08x 8 {0x11 0x22 0x33}" % (addr + 4))
    back = words(rpc(s, "read_memory 0x%08x 8 4" % (addr + 4)))
    print("[C] 8 位宽写 3 字节: %s（返回 %r）" % ([hex(x) for x in back], r[:20]))

    # D. 目标控制
    for cmd in ("halt", "resume", "reset halt", "reset run"):
        t0 = time.time()
        r = rpc(s, cmd, 20)
        print("[D] %-11s → %r  (%.0f ms)" % (cmd, r[:40], (time.time() - t0) * 1000))

    # E. version / 目标信息
    print("[E] version:", rpc(s, "version").decode("ascii", "replace").strip()[:60])
finally:
    p.kill()
    try:
        p.wait(timeout=3)
    except Exception:
        pass
print("完成")
