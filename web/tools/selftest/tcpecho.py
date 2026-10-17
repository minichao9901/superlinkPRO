#!/usr/bin/env python3
"""TCP echo 测试 —— HPM6800EVK lwIP tcpecho 例程配套（server / client 两种角色）。

被测例程（构建目录）:
    E:\\sdk_env_v1.11.0\\work\\lwip_lwip_tcpecho_hpm6800evk_flash_sdram_xip_debug
    源码: E:\\sdk_env_v1.11.0\\hpm_sdk\\samples\\lwip\\lwip_tcpecho

例程里板子是 **TCP 服务端**：tcp_echo_init() 监听 TCP_LOCAL_PORT=5001，
收多少回多少（lwip.c / tcp_echo.c）。CMakeLists 里 -DLWIP_DHCP=0，
静态地址取自 netinfo.h 的 IP0_CONFIG = 192.168.100.10/24（网关 .1）。
所以 PC 网卡要在 192.168.100.0/24 —— 本机有线网卡就是 192.168.100.11。

两种角色都"连发 3 条 hello, echo!\\n"，并校验这 3 条是否原样回来：

    make tcpecho            # ★ PC 当客户端，打板子的 echo（最常用）
    make tcpecho-server     # PC 当服务端（回显对照）
    make tcpecho-selftest   # 不开硬件，本地自检脚本本身

等价的手工调用：
    python tools/selftest/tcpecho.py client -H 192.168.100.10 -p 5001

退出码: 0 = PASS，2 = 回显校验失败，3 = 连接/绑定/超时等网络错误，9 = 看门狗超时。
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time

DEFAULT_PAYLOAD = b"hello, echo!\n"
DEFAULT_COUNT = 3
DEFAULT_HOST = "192.168.100.10"  # netinfo.h 的 IP0_CONFIG（例程默认静态 IP）
DEFAULT_PORT = 5001              # tcp_echo.h 的 TCP_LOCAL_PORT

E_OK, E_FAIL, E_NET = 0, 2, 3


def log(msg):
    print(msg, flush=True)


def watchdog(sec, tag):
    """硬超时兜底：脚本卡在 recv/connect 里时不会把终端挂死。"""
    def _fire():
        time.sleep(sec)
        log("!! WATCHDOG: %s 超过 %.1fs 未结束，强制退出" % (tag, sec))
        os._exit(9)

    threading.Thread(target=_fire, daemon=True).start()


def budget_of(args, kind):
    """看门狗时长：--budget 显式给了就用它，否则按角色算个够用的自动值。

    自动值不是拍脑袋：server 默认 --accept-timeout 60 得能等到人连上来，
    client 默认要够跑完 --retries 次（每次 connect + 读回显）再收工。
    """
    if args.budget > 0:
        return args.budget
    if kind == "server":
        return args.accept_timeout + args.timeout + 20.0
    if kind == "selftest":
        return args.timeout * 4 + 20.0
    return args.retries * (args.connect_timeout + 8.0 + args.retry_delay) + 5.0


def listen_socket(host, port):
    """建监听 socket。Windows 上用 SO_EXCLUSIVEADDRUSE：
    默认的 SO_REUSEADDR 在 Windows 上允许**两个进程绑同一端口**，
    结果是新起的 server 看着"启动成功"，连接却被旧进程接走，排查很费时间。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(1)
    return srv


def unescape(text):
    """把命令行里的 \\n \\r \\t \\\\ \\xNN 还原成真实字节（其余按 UTF-8 原样）。"""
    out = bytearray()
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            nxt = text[i + 1]
            simple = {"n": b"\n", "r": b"\r", "t": b"\t", "0": b"\0", "\\": b"\\"}
            if nxt in simple:
                out += simple[nxt]
                i += 2
                continue
            if nxt == "x" and i + 3 < n:
                try:
                    out += bytes([int(text[i + 2:i + 4], 16)])
                    i += 4
                    continue
                except ValueError:
                    pass
        out += c.encode("utf-8")
        i += 1
    return bytes(out)


def split_lines(data):
    """按 \\n 切行，保留行尾 \\n（末尾残行也保留，便于诊断半包/截断）。"""
    if not data:
        return []
    parts = data.split(b"\n")
    lines = [p + b"\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def echo_kind(rx, expected):
    """回显是否成立。返回 None=失败，否则返回匹配类型。

    按**字节流**比对，不按行：payload 不以 \\n 结尾（或中间有 \\n）时，
    "连发 3 条"的字节会和后面一组串在一起，按行切会把它们切错位、误判成失败。
      exact      - 收到的就是原样 3 条（板子固件这类纯回显）
      contiguous - 3 条原样出现，但前后有别的字节（服务端先问候的场景）
    """
    if rx == expected:
        return "exact"
    if expected in rx:
        return "contiguous"
    return None


def drain(sock, idle=0.4, total=5.0, need=None):
    """读到"静默 idle 秒"或读满 total 秒为止；need 字节到手可提前返回。"""
    buf = b""
    hard_end = time.monotonic() + total
    idle_end = time.monotonic() + idle
    while time.monotonic() < hard_end:
        if need is not None and len(buf) >= need:
            break
        if time.monotonic() > idle_end:
            break
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError as exc:
            log("[tcp-echo] recv 出错: %s" % exc)
            break
        if not chunk:
            break  # 对端关闭
        buf += chunk
        idle_end = time.monotonic() + idle
    return buf


def report(tx_lines, rx, payload, tag):
    """打印收发内容并判定 PASS/FAIL（返回退出码）。"""
    expected = payload * len(tx_lines)
    rx_lines = split_lines(rx)
    log("[%s] tx %d 条 x %dB: %r" % (tag, len(tx_lines), len(payload), payload))
    log("[%s] rx %dB / %d 行（期望 %dB）" % (tag, len(rx), len(rx_lines), len(expected)))
    for idx, line in enumerate(rx_lines[:8]):
        log("[%s]   rx[%d] = %r" % (tag, idx, line))
    if len(rx_lines) > 8:
        log("[%s]   ... 省略 %d 行" % (tag, len(rx_lines) - 8))

    kind = echo_kind(rx, expected)
    if kind == "exact":
        log("[%s] PASS: %d 条 x %dB 原样回显（字节完全一致）"
            % (tag, len(tx_lines), len(payload)))
        return E_OK
    if kind == "contiguous":
        log("[%s] PASS: %d 条原样回显，另有 %dB 非本端数据（服务端问候，正常）"
            % (tag, len(tx_lines), len(rx) - len(expected)))
        return E_OK
    log("[%s] FAIL: 回显与发送不一致" % tag)
    log("[%s]   tx = %s" % (tag, expected.hex()))
    log("[%s]   rx = %s" % (tag, rx.hex()))
    if len(rx) < len(expected):
        log("[%s]   rx 比期望少 %dB —— 先看板子串口有没有丢日志/复位" % (tag, len(expected) - len(rx)))
    return E_FAIL


def client_session(host, port, payload, count, connect_timeout, tag="client"):
    """客户端会话：连上 -> 连发 count 条 -> 读回显 -> 校验。返回退出码。"""
    t0 = time.monotonic()
    try:
        sock = socket.create_connection((host, port), timeout=connect_timeout)
    except OSError as exc:
        log("[%s] FAIL: 连接 %s:%d 失败: %s" % (tag, host, port, exc))
        log("[%s]   排查: 板子跑起来了吗? 串口应打印 IPv4 Address: 192.168.100.10 / 链路 Up; "
            "PC 网卡是否在 192.168.100.0/24 (本机 .11)" % tag)
        return E_NET
    log("[%s] connected -> %s:%d (%.0f ms)" % (tag, host, port, (time.monotonic() - t0) * 1000))

    tx_lines = [payload] * count
    with sock:
        sock.settimeout(0.2)
        try:
            for line in tx_lines:          # 连发 count 条，不做间隔
                sock.sendall(line)
        except OSError as exc:
            log("[%s] FAIL: 发送出错: %s" % (tag, exc))
            return E_NET
        rx = drain(sock, idle=0.4, total=5.0)
    return report(tx_lines, rx, payload, tag)


def server_session(conn, payload, count, idle_timeout, greet=True, tag="server"):
    """服务端会话：可选先问候 -> 收到什么回什么，直到对端关闭/静默/凑够 count 条。"""
    peer = conn.getpeername()
    log("[%s] peer = %s:%d" % (tag, peer[0], peer[1]))
    with conn:
        conn.settimeout(0.2)
        if greet:
            for _ in range(count):
                conn.sendall(payload)
            log("[%s] greet: 先发 %d 条 x %dB: %r" % (tag, count, len(payload), payload))

        need = count * len(payload)
        rx = b""
        idle_end = time.monotonic() + idle_timeout
        while len(rx) < need and time.monotonic() < idle_end:
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                log("[%s] recv 出错: %s" % (tag, exc))
                break
            if not chunk:
                log("[%s] peer 关闭连接" % tag)
                break
            rx += chunk
            conn.sendall(chunk)            # 原样回显
            idle_end = time.monotonic() + idle_timeout

    tx_lines = [payload] * count
    if len(rx) < need:
        log("[%s] 注意: 只收到 %dB，不足 %dB（%d 条），按实际内容判定"
            % (tag, len(rx), need, count))
    return report(tx_lines, rx, payload, tag)


def cmd_client(args):
    watchdog(budget_of(args, "client"), "client")
    log("[tcp-echo] client: %s:%d, payload=%r x%d, 最多 %d 次尝试"
        % (args.host, args.port, args.payload, args.count, args.retries))
    rc = E_NET
    for attempt in range(1, args.retries + 1):
        if attempt > 1:
            log("[tcp-echo] ---- 第 %d/%d 次尝试 ----" % (attempt, args.retries))
        rc = client_session(args.host, args.port, args.payload, args.count,
                            args.connect_timeout)
        if rc == E_OK:
            if attempt > 1:
                log("[tcp-echo] 注意: 第 %d 次才通过（前 %d 次板子没应答 —— "
                    "这块靶子跑的是轮询版 lwIP，实测有秒级抖动）" % (attempt, attempt - 1))
            return E_OK
        if attempt < args.retries:
            log("[tcp-echo] 第 %d/%d 次未通过，%.1fs 后重试 ..."
                % (attempt, args.retries, args.retry_delay))
            time.sleep(args.retry_delay)
    log("[tcp-echo] FAIL: %d 次尝试都没通过" % args.retries)
    return rc


def cmd_server(args):
    watchdog(budget_of(args, "server"), "server")
    try:
        srv = listen_socket(args.bind, args.port)
    except OSError as exc:
        log("[server] FAIL: 绑定 %s:%d 失败: %s" % (args.bind, args.port, exc))
        log("[server]   端口被占用? 先查: netstat -ano | findstr :%d" % args.port)
        log("[server]   或换端口: --port 5002（同时让对端也改）")
        return E_NET
    log("[server] listening on %s:%d，等待对端连接（%.0fs 无连接即退出，Ctrl-C 结束）"
        % (args.bind, args.port, args.accept_timeout))
    srv.settimeout(args.accept_timeout)
    try:
        conn, _ = srv.accept()
    except socket.timeout:
        log("[server] 超时: %.0fs 内没有客户端连上来" % args.accept_timeout)
        srv.close()
        return E_NET
    srv.close()
    return server_session(conn, args.payload, args.count, args.timeout,
                          greet=not args.no_greet)


def cmd_selftest(args):
    """不开硬件：本地起服务端 + 跑客户端，两种配置各来一轮。"""
    watchdog(budget_of(args, "selftest"), "selftest")
    rc_all = E_OK
    for greet in (True, False):
        cases = "server 带问候 + client" if greet else "server 纯回显 + client"
        log("[tcp-echo] ==== selftest: %s ====" % cases)

        srv = listen_socket("127.0.0.1", 0)
        port = srv.getsockname()[1]

        box = {}

        def serve():
            try:
                conn, _ = srv.accept()
                box["rc"] = server_session(conn, args.payload, args.count,
                                           args.timeout, greet=greet, tag="srv")
            except OSError as exc:
                box["rc"] = E_NET
                log("[srv] 出错: %s" % exc)

        th = threading.Thread(target=serve, daemon=True)
        th.start()
        time.sleep(0.05)
        rc_cli = client_session("127.0.0.1", port, args.payload, args.count, 2.0)
        th.join(timeout=args.timeout + 2.0)
        srv.close()
        rc_srv = box.get("rc", E_NET)
        log("[tcp-echo] ==== %s: client=%s server=%s ===="
            % (cases, "PASS" if rc_cli == E_OK else "FAIL",
               "PASS" if rc_srv == E_OK else "FAIL"))
        log("")
        if rc_cli != E_OK or rc_srv != E_OK:
            rc_all = E_FAIL
    log("[tcp-echo] selftest %s" % ("PASS: 脚本自身收发/校验正常" if rc_all == E_OK
                                    else "FAIL"))
    return rc_all


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-n", "--count", type=int, default=DEFAULT_COUNT,
                        help="连发的条数（默认 %d）" % DEFAULT_COUNT)
    common.add_argument("--payload", default=DEFAULT_PAYLOAD.decode(),
                        help="单条内容，支持 \\n \\t \\\\ \\xNN 转义（默认 'hello, echo!\\n'）")
    common.add_argument("-t", "--timeout", type=float, default=3.0,
                        help="服务端等待后续数据的静默超时秒数（默认 3）")
    common.add_argument("--budget", type=float, default=0.0,
                        help="脚本总时长硬上限秒数，超时自杀退出码 9（默认 0 = 按角色自动）")

    ap = argparse.ArgumentParser(
        prog="tcpecho.py",
        description="HPM6800EVK lwIP tcpecho 例程的 TCP 回显测试（server / client 两种角色）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例: python tools/selftest/tcpecho.py client -H 192.168.100.10 -p 5001\n"
               "    python tools/selftest/tcpecho.py server -p 5001\n"
               "    python tools/selftest/tcpecho.py selftest")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_cli = sub.add_parser("client", parents=[common], help="当 TCP 客户端，连板子并校验回显")
    p_cli.add_argument("-H", "--host", default=DEFAULT_HOST,
                       help="服务端 IP（默认 %s，即例程静态 IP）" % DEFAULT_HOST)
    p_cli.add_argument("-p", "--port", type=int, default=DEFAULT_PORT,
                       help="服务端端口（默认 %d，即 TCP_LOCAL_PORT）" % DEFAULT_PORT)
    p_cli.add_argument("--connect-timeout", type=float, default=3.0,
                       help="TCP 连接超时秒数（默认 3）")
    p_cli.add_argument("--retries", type=int, default=3,
                       help="尝试次数，用于吸收靶子的秒级抖动（默认 3，1 = 不重试）")
    p_cli.add_argument("--retry-delay", type=float, default=1.0,
                       help="两次尝试之间的间隔秒数（默认 1）")
    p_cli.set_defaults(func=cmd_client)

    p_srv = sub.add_parser("server", parents=[common], help="当 TCP 服务端（回显对照）")
    p_srv.add_argument("-b", "--bind", default="0.0.0.0",
                       help="监听地址（默认 0.0.0.0）")
    p_srv.add_argument("-p", "--port", type=int, default=DEFAULT_PORT,
                       help="监听端口（默认 %d）" % DEFAULT_PORT)
    p_srv.add_argument("--accept-timeout", type=float, default=60.0,
                       help="等待客户端连接的秒数（默认 60）")
    p_srv.add_argument("--no-greet", action="store_true",
                       help="连接后不先发问候，纯回显（与板子固件行为一致）")
    p_srv.set_defaults(func=cmd_server)

    p_st = sub.add_parser("selftest", parents=[common],
                          help="本地自检（127.0.0.1 起服务端 + 客户端，不需要硬件）")
    p_st.set_defaults(func=cmd_selftest)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.payload = unescape(args.payload)
    if args.count < 1:
        log("--count 必须 >= 1")
        return E_FAIL
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("")
        log("[tcp-echo] Ctrl-C 退出")
        sys.exit(130)
