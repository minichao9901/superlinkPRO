# -*- coding: utf-8 -*-
"""SWD 目标（以 STM32F103ZE @96MHz 为例）一键回归。

跑三个阶段，逐阶段/逐频率档出结果（控制台 + build/regression/swd.jsonl）：
  P1 SRAM   —— OpenOCD load/dump 20KB，1~60 MHz 全档逐字节校验；60M 档按基线判
  P2 RTT    —— 探针侧桥，SWD 20/45/60 MHz 各一个窗口：交付率 + 字节级误码（必须 0）
  P3 HSS    —— bench（单变量/8 通道，多档 + 拟合公式）+ run（端到端速率、丢拍、u_hi 误码）

前置（脚本会自己调，也可用 --skip-flash 跳过）：
  靶子固件按阶段切换：P2 前刷 stm32f103_rtt_speed -Board ze，P3 前刷
  stm32f103_scope -Board ze（都走探针 OpenOCD，build.ps1 + flash.ps1）。

判定：吞吐 >= 基线 × floor（默认 80%）；丢失/误码必须为 0；任一阶段 FAIL ⇒
立即停止（--keep-going 例外）；全程预算 --budget（默认 3600s）硬超时。

用法：
  python script_test/regression_swd.py                 # 前台全跑
  make regression-swd                                  # 同上
  make regression-swd-bg                               # 后台，结果进 build/regression/
  python script_test/regression_swd.py --list          # 只打印阶段与基线

基线出处（README 实测，F103ZET6 @96MHz；floor 只做**回归**判定，不是天花板）：
  SRAM@60M wall 写 2844 / 读 2443 KB/s；xfer 口径 写 3471 / 读 2958 KB/s
  RTT 交付 20M 1379 / 45M 2527 / 60M 2954 KB/s（字节级零丢包）
  HSS bench 单变量@60M 629 kHz、8 通道@60M 89.3 kHz；端到端 单变量@3µs ~320 kHz、
  8 通道@12µs 76.4 kHz
"""
import argparse
import os
import re
import sys
import time

import regression_common as rc

# ---- 基线（可按机况调整；floor 在命令行给） -------------------------------
BL_SRAM_WALL = {"60 MHz": (2844, 2443)}
BL_SRAM_XFER = {"60 MHz": (3471, 2958)}
BL_RTT = {20: 1300, 30: 1800, 36: 2100, 45: 2350, 60: 2750}   # 交付率 KB/s
BL_BENCH_ONE = {60: 629, 45: 318, 30: 200, 20: 130}           # M0 上限 kHz
BL_BENCH_PACK = 89.3
BL_RUN_ONE = 320.0        # 单变量 u32 @3µs 端到端 kHz（README ~320-329）
BL_RUN_PACK = 76.4        # 8 通道 @12µs 端到端 kHz

F103_RTT_DIR = os.path.join(rc.HERE, "stm32f103_rtt_speed")
F103_SCOPE_DIR = os.path.join(rc.HERE, "stm32f103_scope")


def build_and_flash(reg, psdir, board, tag, skip_flash):
    if not skip_flash:
        cmd, _, _ = reg.run(rc.powershell(os.path.join(psdir, "build.ps1"), "-Board", board),
                            600, tag + ".build")
        if cmd != 0:
            return False
        cmd, _, _ = reg.run(rc.powershell(os.path.join(psdir, "flash.ps1"), "-Board", board),
                            240, tag + ".flash")
        if cmd != 0:
            return False
    return True


# ---------------------------------------------------------------- P1 SRAM --
def phase_sram(reg):
    rcv, tail, secs = reg.run([sys.executable, os.path.join(rc.HERE, "sram_speed_test.py")],
                              420, "P1.sram")
    text = "\n".join(tail)
    m = {"rc": rcv, "secs": round(secs, 1)}
    if rcv != 0:
        reg.emit("P1.sram", "FAIL", m, note="脚本退出码 %s" % rcv)
        return False
    # 逐档成功行：` 20 MHz | write   1355.9 KB/s | read   1287.3 KB/s | verified`
    tiers = re.findall(r"^\s*(\S[^|]*?MHz)\s*\|\s*write\s+([\d.]+)\s*KB/s\s*\|\s*read\s+"
                       r"([\d.]+)\s*KB/s\s*\|\s*verified", text, re.M)
    # 失败行格式不同（没有 write/read 字段）：` 20 MHz | FAILED (err=... data_ok=...)`
    failed = [m.strip() for m in re.findall(r"^\s*(\S[^|]*?MHz)\s*\|\s*FAILED", text, re.M)]
    xfer = re.findall(r"^\s*(\S[^|]*?MHz)\s*\|\s*(\d+\.?\d*)K\s*\|\s*(\d+\.?\d*)K\s*\|\s*"
                      r"(\d+\.?\d*|n/a)\s*\|\s*(\d+\.?\d*|n/a)\s*$", text, re.M)
    m["tiers_verified"] = len(tiers)
    ok = reg.need("P1.sram", bool(tiers) and not failed,
                  "全部档位逐字节校验通过（%d 档，失败 %s）" % (len(tiers), failed or "无"), m)
    x60 = next((x for x in xfer if "60" in x[0]), None)
    if x60:
        m["xfer_w"], m["xfer_r"] = float(x60[3]), float(x60[4])
        bw, br = BL_SRAM_XFER.get("60 MHz", (0, 0))
        ok &= reg.rate_check("P1.sram", "sram_xfer_write@60M", m["xfer_w"], bw)
        ok &= reg.rate_check("P1.sram", "sram_xfer_read@60M", m["xfer_r"], br)
    wall = next((t for t in tiers if "60" in t[0]), None)
    if wall:
        m["wall_w"], m["wall_r"] = float(wall[1]), float(wall[2])
        bw, br = BL_SRAM_WALL.get("60 MHz", (0, 0))
        ok &= reg.rate_check("P1.sram", "sram_wall_write@60M", m["wall_w"], bw)
        ok &= reg.rate_check("P1.sram", "sram_wall_read@60M", m["wall_r"], br)
    reg.emit("P1.sram", "PASS" if ok else "FAIL", m)
    return ok


# ----------------------------------------------------------------- P2 RTT --
def phase_rtt(reg, args):
    if not build_and_flash(reg, F103_RTT_DIR, "ze", "P2.rtt", args.skip_flash):
        reg.emit("P2.rtt", "FAIL", {}, note="靶子固件构建/烧录失败")
        return False
    ok = True
    for clk in args.rtt_clks:
        tag = "P2.rtt@%dM" % clk
        rcv, tail, secs = reg.run([sys.executable, os.path.join(rc.HERE, "rtt_probe_bridge.py"),
                                   args.port, str(args.rtt_secs), "36000", str(clk)],
                                  args.rtt_secs * 10 + 120, tag)
        text = "\n".join(tail)
        m = {"clk": clk, "rc": rcv, "secs": round(secs, 1)}
        if rcv != 0:
            reg.emit(tag, "FAIL", m, note="脚本退出码 %s" % rcv)
            return False
        m["rate"] = rc.grab(r"got \d+ bytes in [\d.]+s -> ([\d.]+) KB/s", text)
        m["verdict"] = (rc.grab(r"(LOSSLESS|LOSSY/GARBLED):", text, cast=str) or "?")
        m["lost"] = rc.grab(r": \d+ gap\(s\), (\d+) bytes lost", text, cast=int, default=-1)
        m["dup"] = rc.grab(r"(\d+) bytes duplicated", text, cast=int, default=-1)
        m["rd_err"] = rc.grab(r"rd_err=(\d+)", text, cast=int, default=-1)
        m["wr_err"] = rc.grab(r"wr_err=(\d+)", text, cast=int, default=-1)
        m["active_clk"] = rc.grab(r"active SWD clock: (\d+) MHz", text, cast=int, default=clk)
        start_rc = rc.grab(r"start rc=(-?\d+)", text, cast=int, default=-99)
        if start_rc != 0:
            reg.emit(tag, "FAIL", m, note="桥启动失败 rc=%d" % start_rc)
            return False
        ok_t = reg.rate_check(tag, "rtt_delivery", m["rate"],
                              BL_RTT.get(m["active_clk"], BL_RTT[45]))
        ok_t &= reg.need(tag, m["verdict"] == "LOSSLESS", "流校验 %s" % m["verdict"], m)
        ok_t &= reg.zero_check(tag, "lost", m["lost"])
        ok_t &= reg.zero_check(tag, "dup", m["dup"])
        # rd_err/wderr 是链路计数：F103@60M 的 wr_err=1 是 README 记录的既有现象，
        # 字节级校验（lost/dup=0）才是误码判据 —— 这里只记录不判死。
        if not ok_t and not reg.keep_going:
            reg.emit(tag, "FAIL", m)
            return False
        reg.emit(tag, "PASS" if ok_t else "FAIL", m)
        ok &= ok_t
    return ok


# ---------------------------------------------------------------- P3 HSS --
def phase_scope(reg, args):
    if not build_and_flash(reg, F103_SCOPE_DIR, "ze", "P3.scope", args.skip_flash):
        reg.emit("P3.scope", "FAIL", {}, note="靶子固件构建/烧录失败")
        return False
    scope = os.path.join(rc.HERE, "scope_hss_test.py")
    ok = True

    # --- bench：单变量多档 + 拟合 T = a + b/f；8 通道单档 ---
    pts = []
    fitm = {}
    for clk in args.bench_clks:
        tag = "P3.bench-one@%dM" % clk
        rcv, tail, secs = reg.run([sys.executable, scope, "bench", "--set", "one",
                                   "--clock", str(clk * 1000000), "--iters", "300"],
                                  240, tag)
        text = "\n".join(tail)
        m = {"clk": clk, "rc": rcv}
        khz = rc.grab(r"上限 ≈ ([\d.]+) kHz", text)
        m["khz"] = khz
        if rcv != 0 or khz is None:
            reg.emit(tag, "FAIL", m, note="标定失败（err 或 无响应）")
            return False
        pts.append((clk, 1000.0 / khz))          # µs/样本
        ok_t = reg.rate_check(tag, "m0_khz", khz, BL_BENCH_ONE.get(clk, 0))
        reg.emit(tag, "PASS" if ok_t else "FAIL", m)
        ok &= ok_t
        if not ok and not reg.keep_going:
            return False

    fit = rc.linfit([1.0 / c for c, _ in pts], [t for _, t in pts])
    if fit:
        a, b, r2 = fit
        # x = 1/f（f 以 MHz 计）⇒ b 的单位是 µs·MHz：T(60MHz) = a + b/60
        fitm = {"fit": "T(µs) = %.3f + %.1f×(MHz/f_swd)" % (a, b), "r2": round(r2, 4)}
        reg.emit("P3.fit", "INFO", fitm,
                 note="单变量 M0：位翻转贡献 %.1f µs·MHz，固定开销 %.3f µs（例：%.1f MHz → %.2f µs）"
                      % (b, a, pts[0][0], a + b / pts[0][0]))
        for clk, t_us in pts:
            pred = a + b / clk
            if abs(t_us - pred) > 0.25 * pred:
                ok &= reg.need("P3.fit", False,
                               "%d MHz 实测 %.3f µs 偏离拟合 %.3f µs > 25%%" % (clk, t_us, pred), {})
        ok &= reg.need("P3.fit", r2 >= 0.95, "拟合 R²=%.4f (>=0.95)" % r2, {})

    tag = "P3.bench-pack@60M"
    rcv, tail, _ = reg.run([sys.executable, scope, "bench", "--set", "pack",
                            "--clock", "60000000", "--iters", "200"], 240, tag)
    text = "\n".join(tail)
    khz = rc.grab(r"上限 ≈ ([\d.]+) kHz", text)
    m = {"khz": khz, "rc": rcv}
    if rcv != 0 or khz is None:
        reg.emit(tag, "FAIL", m, note="标定失败")
        return False
    ok_t = reg.rate_check(tag, "m0_khz", khz, BL_BENCH_PACK)
    reg.emit(tag, "PASS" if ok_t else "FAIL", m)
    ok &= ok_t
    if not ok and not reg.keep_going:
        return False

    # --- run：端到端 + 丢拍 + 误码（u_hi / tick 跳变） ---
    tag = "P3.run-one@3us"
    rcv, tail, _ = reg.run([sys.executable, scope, "run", "--set", "one", "--clock", "60000000",
                            "--period", "3", "--secs", str(args.scope_secs), "--flags", "0x20",
                            "--readsize", "65536"], 300, tag)
    text = "\n".join(tail)
    m = {"rc": rcv}
    m["e2e"] = rc.grab(r"★ 端到端 ([\d.]+) kHz", text)
    m["drop_pct"] = (lambda mm: float(mm.group(1)) if mm else None)(re.search(r"丢 \d+ 拍 = ([\d.]+)%", text))
    m["jumps"] = rc.grab(r"跳变丢样本合计 (\d+)", text, cast=int, default=-1)
    start_rc = rc.grab(r"启动 rc=(-?\d+)", text, cast=int, default=-99)
    if rcv != 0 or start_rc != 0 or m["e2e"] is None:
        reg.emit(tag, "FAIL", m, note="启动 rc=%s" % start_rc)
        return False
    ok_t = reg.rate_check(tag, "e2e_khz", m["e2e"], BL_RUN_ONE)
    ok_t &= reg.need(tag, m["drop_pct"] is not None and m["drop_pct"] <= 8.0,
                     "探针跳拍 %.1f%% (<=8%%)" % (m["drop_pct"] or -1), m)
    reg.emit(tag, "PASS" if ok_t else "FAIL", m)
    ok &= ok_t
    if not ok and not reg.keep_going:
        return False

    tag = "P3.run-pack@12us"
    rcv, tail, _ = reg.run([sys.executable, scope, "run", "--set", "pack", "--clock", "60000000",
                            "--period", "12", "--secs", str(args.scope_secs), "--flags", "0x20"],
                           300, tag)
    text = "\n".join(tail)
    m = {"rc": rcv}
    m["e2e"] = rc.grab(r"★ 端到端 ([\d.]+) kHz", text)
    m["drop_pct"] = (lambda mm: float(mm.group(1)) if mm else None)(re.search(r"丢 \d+ 拍 = ([\d.]+)%", text))
    start_rc = rc.grab(r"启动 rc=(-?\d+)", text, cast=int, default=-99)
    if rcv != 0 or start_rc != 0 or m["e2e"] is None:
        reg.emit(tag, "FAIL", m, note="启动 rc=%s" % start_rc)
        return False
    ok_t = reg.rate_check(tag, "e2e_khz", m["e2e"], BL_RUN_PACK)
    mm = re.search(r"u_hi 高位校验: (\d+)/(\d+) 正确", text)
    if mm:
        m["u_hi_bad"] = int(mm.group(2)) - int(mm.group(1))
        ok_t &= reg.zero_check(tag, "u_hi_bad", m["u_hi_bad"])
    else:
        ok_t &= reg.need(tag, False, "u_hi 高位校验行没抓到", m)
    reg.emit(tag, "PASS" if ok_t else "FAIL", m)
    ok &= ok_t
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", default=None, help="探针 CDC 口（RTT 用）；缺省自动探测（VID/PID 匹配）")
    ap.add_argument("--floor", type=float, default=0.8, help="吞吐判定下限比例（默认 0.8）")
    ap.add_argument("--budget", type=float, default=3600, help="全程预算秒数（超时硬退出）")
    ap.add_argument("--keep-going", action="store_true", help="阶段失败后继续跑后续阶段")
    ap.add_argument("--skip-flash", action="store_true", help="不构建/烧录靶子固件（假定已在位）")
    ap.add_argument("--rtt-clks", default="20,45,60", help="RTT 阶段的 SWD 档（MHz，逗号分隔）")
    ap.add_argument("--bench-clks", default="60,45,30,20", help="HSS 标定的 SWD 档")
    ap.add_argument("--rtt-secs", type=float, default=10.0, help="RTT 每窗秒数")
    ap.add_argument("--scope-secs", type=float, default=4.0, help="HSS run 每窗秒数")
    ap.add_argument("--list", action="store_true", help="只打印阶段计划与基线，不跑")
    args = ap.parse_args()

    # argparse 进来的是字符串：不拆分的话迭代得到的是**单个字符**，
    # "%d" % "2" 直接把整个回归进程炸掉（实测踩过，且死在两阶段之间连
    # RESULT 都来不及写）。
    args.rtt_clks = [int(x) for x in str(args.rtt_clks).split(",") if x.strip()]
    args.bench_clks = [int(x) for x in str(args.bench_clks).split(",") if x.strip()]

    if args.list:
        print("P1.sram    sram_speed_test.py 1~60MHz 逐字节校验；60M 基线 wall %s xfer %s"
              % (BL_SRAM_WALL["60 MHz"], BL_SRAM_XFER["60 MHz"]))
        print("P2.rtt     rtt_probe_bridge.py 档位 %s KB/s 基线 %s，lost/dup 必须 0"
              % (args.rtt_clks, BL_RTT))
        print("P3.scope   bench one %s kHz / pack %.1f kHz；run one@3µs %.0f、pack@12µs %.1f；"
              "拟合 T=a+b·MHz/f" % (BL_BENCH_ONE, BL_BENCH_PACK, BL_RUN_ONE, BL_RUN_PACK))
        print("floor=%.0f%%  budget=%.0fs  port=%s" % (args.floor * 100, args.budget, args.port))
        return 0

    reg = rc.Regression("swd", floor=args.floor, budget=args.budget, keep_going=args.keep_going)
    if not rc.probe_present():
        reg.emit("P0.preflight", "FAIL", {}, note="探针 HID 不在（VID 0D28/PID 0204）")
        return reg.finish("P0")
    if args.port is None:
        args.port = rc.find_cdc_port()
        if args.port is None:
            reg.emit("P0.preflight", "FAIL", {}, note="探针 CDC 串口没找到（RTT 阶段需要）")
            return reg.finish("P0")
    if not os.path.exists(rc.OPENOCD):
        reg.emit("P0.preflight", "FAIL", {}, note="OpenOCD 不存在: %s" % rc.OPENOCD)
        return reg.finish("P0")
    reg.emit("P0.preflight", "PASS", {"openocd": rc.OPENOCD, "cdc_port": args.port})

    phases = [("P1.sram", lambda: phase_sram(reg)),
              ("P2.rtt", lambda: phase_rtt(reg, args)),
              ("P3.scope", lambda: phase_scope(reg, args))]
    for name, fn in phases:
        try:
            ok = fn()
        except Exception as exc:
            # 阶段内的意外异常也要落一条结果（后台跑时 stdout 丢了就只剩 jsonl）
            import traceback
            reg.emit(name, "FAIL", {"exception": "%s: %s" % (type(exc).__name__, exc)},
                     note=traceback.format_exc(limit=3).replace("\n", " | "))
            ok = False
        if not ok and not args.keep_going:
            return reg.finish(name)
    return reg.finish()


if __name__ == "__main__":
    sys.exit(main())
