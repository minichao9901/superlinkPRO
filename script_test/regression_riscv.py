# -*- coding: utf-8 -*-
"""RISC-V 目标（以 HPM6800EVK / HPM6880 为例）一键回归。

跑三个阶段，逐阶段出结果（控制台 + build/regression/riscv.jsonl）：
  P1 引擎   —— riscv_jtag open + selfcheck 门禁 + 块读/块写基准 + sbastat（sticky 必须全 0）
  P2 RTT    —— 构建并烧 hpm6800evk_rtt_flood，交付率 + 28MB 级字节流零丢校验
               （JTAG 的时序旋钮是 DMI idle delay，实测下界 8 已是最优 ⇒ 没有
                "不同频率"维度，只跑默认档，见 docs/hpm6800evk-jtag.md）
  P3 HSS    —— 单字流水值完整性（u_hi 契约 0 违例）+ bench（单变量 / 8 通道，
               拟合 T = a + b·字数）+ run（rv 8 通道契约 PASS、单变量端到端）

前置：探针已在 DFU/APP 模式、6800EVK 用 20 针排线接到探针 J5（README 接线表）。
判定与早退规则同 regression_swd.py（吞吐 >= 基线 × floor，错误类必须 0）。

用法：
  python script_test/regression_riscv.py              # 前台全跑
  make regression-riscv                               # 同上
  make regression-riscv-bg                            # 后台，结果进 build/regression/
  python script_test/regression_riscv.py --list       # 只打印阶段与基线

基线出处（README + 2026-09-30 修复后实测；floor 只做回归判定）：
  块读 1504.5 / 块写 1511.8 KB/s；RTT 交付 1385 KB/s（28.5MB 字节级零丢）
  HSS 单变量 bench 315 kHz（流水检查修复后）；8 通道 bench 27.3 kHz；
  端到端 rv@200µs 5.1 kHz、单变量@10µs ~100 kHz（≤ bench 与周期共同封顶）
"""
import argparse
import os
import re
import sys

import regression_common as rc

BL_RBENCH = 1504.5          # KB/s，块读（README）
BL_WBENCH = 1511.8          # KB/s，块写
BL_RTT = 1385.0             # KB/s，交付率（README，字节级零丢）
BL_BENCH_ONE = 315.0        # kHz，单变量 M0（2026-09-30 修复流水检查后实测 315.6）
BL_BENCH_RV = 27.3          # kHz，8 通道 32B span（README）
BL_RUN_RV = 5.1             # kHz，rv @200µs 端到端
RV_SCOPE_DIR = os.path.join(rc.HERE, "hpm6800evk_scope")
RV_FLOOD_DIR = os.path.join(rc.HERE, "hpm6800evk_rtt_flood")
RV_SCOPE_ELF = os.path.join(RV_SCOPE_DIR, "build", "flash_xip", "output", "demo.elf")
RV_FLOOD_ELF = os.path.join(RV_FLOOD_DIR, "build", "flash_xip", "output", "demo.elf")
RV_BASE = 0x01240000        # 靶子契约变量块 g_v（构建后 nm 复核）


def build_and_flash(reg, psdir, elf, tag, skip_flash):
    if skip_flash:
        return True
    cmd, _, _ = reg.run(rc.powershell(os.path.join(psdir, "build.ps1")), 900, tag + ".build")
    if cmd != 0:
        return False
    cmd, tail, _ = reg.run([sys.executable, os.path.join(rc.HERE, "hpm6800_flash_target.py"), elf],
                           300, tag + ".flash")
    return cmd == 0


# --------------------------------------------------------------- P1 引擎 --
def phase_engine(reg):
    rcv, tail, _ = reg.run([sys.executable, os.path.join(rc.HERE, "hpm6800_probe.py"), "set-mode", "1"],
                           60, "P1.set-mode")
    if rcv != 0:
        reg.emit("P1.engine", "FAIL", {"rc": rcv}, note="探针切 SWD+JTAG 模式失败")
        return False
    rcv, tail, _ = reg.run([sys.executable, os.path.join(rc.HERE, "hpm6800_riscv.py"), "open"],
                           60, "P1.open")
    if rcv != 0:
        reg.emit("P1.engine", "FAIL", {"rc": rcv}, note="RISC-V 引擎打不开（TAP 没应答？）")
        return False
    ok = True
    # ⚠️ 顺序：selfcheck 结束时会把引擎关掉，所以基准在它**前面**跑，
    # selfcheck 放最后当值完整性的门禁（它内部自带 open + 写读回 + stop）。
    for name, act, bl in (("rbench", "rbench", BL_RBENCH), ("wbench", "wbench", BL_WBENCH)):
        rcv, tail, _ = reg.run([sys.executable, os.path.join(rc.HERE, "hpm6800_riscv.py"),
                                act, "0x1200000", "1024", "50"], 180, "P1." + act)
        text = "\n".join(tail)
        m = {name: rc.grab(r"rate = ([\d.]+) KB/s", text), "rc": rcv}
        if rcv != 0 or m[name] is None:
            reg.emit("P1." + act, "FAIL", m, note="没有 rate 行（引擎中途死了？）")
            return False
        ok_t = reg.rate_check("P1." + act, name, m[name], bl)
        reg.emit("P1." + act, "PASS" if ok_t else "FAIL", m)
        ok &= ok_t
    if not ok and not reg.keep_going:
        return False

    rcv, tail, _ = reg.run([sys.executable, os.path.join(rc.HERE, "hpm6800_riscv.py"), "sbastat"],
                           60, "P1.sbastat")
    text = "\n".join(tail)
    m = {"sticky": rc.grab(r"sticky 错误事件 = (\d+)", text, cast=int, default=-1),
         "retries": rc.grab(r"整块重读 = (\d+)", text, cast=int, default=-1)}
    ok_t = reg.zero_check("P1.sbastat", "sticky_events", m["sticky"])
    ok_t &= reg.zero_check("P1.sbastat", "block_retries", m["retries"])
    reg.emit("P1.sbastat", "PASS" if ok_t else "FAIL", m)
    ok &= ok_t
    if not ok and not reg.keep_going:
        return False

    rcv, tail, _ = reg.run([sys.executable, os.path.join(rc.HERE, "hpm6800_selfcheck.py")],
                           180, "P1.selfcheck")
    text = "\n".join(tail)
    passed = "RESULT: PASS" in text
    m = {"rc": rcv}
    ok &= reg.need("P1.engine", passed, "selfcheck 写读回校验 %s" % ("PASS" if passed else "FAIL"), m)
    if not passed:
        reg.emit("P1.engine", "FAIL", m, note="selfcheck 未过 —— 值完整性存疑")
        return False
    return ok


# ----------------------------------------------------------------- P2 RTT --
def phase_rtt(reg, args):
    if not build_and_flash(reg, RV_FLOOD_DIR, RV_FLOOD_ELF, "P2.rtt", args.skip_flash):
        reg.emit("P2.rtt", "FAIL", {}, note="狂发固件构建/烧录失败")
        return False
    rcv, tail, _ = reg.run([sys.executable, os.path.join(rc.HERE, "hpm6800_rtt_delivery.py"),
                            args.port, str(args.rtt_secs)], 300, "P2.delivery")
    text = "\n".join(tail)
    m = {"rate": rc.grab(r"host read \d+ bytes in [\d.]+s -> ([\d.]+) KB/s", text),
         "rderr": rc.grab(r"rderr=(\d+)", text, cast=int, default=-1),
         "wderr": rc.grab(r"wderr=(\d+)", text, cast=int, default=-1), "rc": rcv}
    start_rc = rc.grab(r"rc=(-?\d+)\s+\(0 = ok", text, cast=int, default=-99)
    if rcv != 0 or start_rc != 0 or m["rate"] is None:
        reg.emit("P2.delivery", "FAIL", m, note="桥启动 rc=%s 或没有速率行" % start_rc)
        return False
    ok = reg.rate_check("P2.delivery", "rtt_delivery", m["rate"], BL_RTT)
    reg.emit("P2.delivery", "PASS" if ok else "FAIL", m)
    if not ok and not reg.keep_going:
        return False

    rcv, tail, _ = reg.run([sys.executable, os.path.join(rc.HERE, "hpm6800_rtt_loss.py"),
                            args.port, str(args.loss_secs)], 300 + args.loss_secs, "P2.loss")
    text = "\n".join(tail)
    m = {"rc": rcv,
         "lost": rc.grab(r"records=\d+ lost=(\d+)", text, cast=int, default=-1),
         "dup": rc.grab(r"lost=\d+ dup=(\d+)", text, cast=int, default=-1),
         "bytes": rc.grab(r"host received\s+(\d+) bytes", text, cast=int, default=0)}
    ok_t = reg.need("P2.loss", "stream check OK" in text, "字节流校验", m)
    ok_t &= reg.zero_check("P2.loss", "lost", m["lost"])
    ok_t &= reg.zero_check("P2.loss", "dup", m["dup"])
    reg.emit("P2.loss", "PASS" if ok_t else "FAIL", m)
    ok &= ok_t
    return ok


# ---------------------------------------------------------------- P3 HSS --
def phase_scope(reg, args):
    if not build_and_flash(reg, RV_SCOPE_DIR, RV_SCOPE_ELF, "P3.scope", args.skip_flash):
        reg.emit("P3.scope", "FAIL", {}, note="scope 靶子构建/烧录失败")
        return False
    scope = os.path.join(rc.HERE, "scope_hss_test.py")
    ok = True

    # --- 单字流水值完整性（误码率判据）：u_hi 契约，违例必须 0 ---
    tag = "P3.integrity@100us"
    rcv, tail, _ = reg.run([sys.executable, os.path.join(rc.HERE, "riscv_pipe_integrity_test.py"),
                            "--period", "100", "--secs", "3",
                            "--base", "0x%X" % RV_BASE], 240, tag)
    text = "\n".join(tail)
    m = {"rc": rcv,
         "samples": rc.grab(r"样本 (\d+) 个", text, cast=int, default=0),
         "bad": rc.grab(r"违例 (\d+) 个", text, cast=int, default=-1)}
    ok_t = reg.need(tag, "PASS" in text, "u_hi 契约（违例必须 0）", m)
    reg.emit(tag, "PASS" if ok_t else "FAIL", m)
    if not ok_t and not reg.keep_going:
        return False
    ok &= ok_t

    # --- bench：单变量 + 8 通道，拟合 T = a + b·字数 ---
    pts = []
    for name, vset, extra, bl in (("one", "one", ["--addr", "0x%X" % RV_BASE], BL_BENCH_ONE),
                                  ("rv", "rv", ["--base", "0x%X" % RV_BASE], BL_BENCH_RV)):
        tag = "P3.bench-%s" % name
        rcv, tail, _ = reg.run([sys.executable, scope, "bench", "--riscv", "--set", vset,
                                "--iters", "4000" if name == "one" else "2000"] + extra,
                               240, tag)
        text = "\n".join(tail)
        m = {"khz": rc.grab(r"上限 ≈ ([\d.]+) kHz", text), "rc": rcv}
        if rcv != 0 or m["khz"] is None:
            reg.emit(tag, "FAIL", m, note="标定失败")
            return False
        pts.append((1 if name == "one" else 8, 1000.0 / m["khz"]))
        ok_t = reg.rate_check(tag, "m0_khz", m["khz"], bl)
        reg.emit(tag, "PASS" if ok_t else "FAIL", m)
        ok &= ok_t
        if not ok and not reg.keep_going:
            return False

    fit = rc.linfit([float(x) for x, _ in pts], [t for _, t in pts])
    if fit:
        a, b, r2 = fit
        reg.emit("P3.fit", "INFO",
                 {"fit": "边际 ≈ %.2f µs/字（T_one=%.2f µs, T_8ch=%.2f µs）" % (b, pts[0][1], pts[1][1]),
                  "r2": round(r2, 4),
                  "note": "两点线性：单字走 1 次/拍的流水、8 字走 N+2 块读，两条路径不同 —— "
                          "这个\"公式\"只给量级参考，不作判定"})

    # --- run：rv 8 通道契约 + 单变量端到端 ---
    tag = "P3.run-rv@200us"
    rcv, tail, _ = reg.run([sys.executable, scope, "run", "--riscv", "--set", "rv",
                            "--base", "0x%X" % RV_BASE, "--period", "200",
                            "--secs", str(args.scope_secs)], 300, tag)
    text = "\n".join(tail)
    m = {"rc": rcv,
         "e2e": rc.grab(r"★ 端到端 ([\d.]+) kHz", text),
         "contract": ("契约核对：PASS" in text)}
    start_rc = rc.grab(r"启动 rc=(-?\d+)", text, cast=int, default=-99)
    if rcv != 0 or start_rc != 0 or m["e2e"] is None:
        reg.emit(tag, "FAIL", m, note="启动 rc=%s" % start_rc)
        return False
    ok_t = reg.need(tag, m["contract"], "rv 8 字段契约核对 PASS", m)
    ok_t &= reg.rate_check(tag, "e2e_khz", m["e2e"], BL_RUN_RV)
    reg.emit(tag, "PASS" if ok_t else "FAIL", m)
    ok &= ok_t
    if not ok and not reg.keep_going:
        return False

    tag = "P3.run-one@10us"
    rcv, tail, _ = reg.run([sys.executable, scope, "run", "--riscv", "--set", "one",
                            "--addr", "0x%X" % RV_BASE, "--period", "10",
                            "--secs", str(args.scope_secs), "--flags", "0x20"],
                           300, tag)
    text = "\n".join(tail)
    m = {"rc": rcv, "e2e": rc.grab(r"★ 端到端 ([\d.]+) kHz", text),
         "drop_pct": (lambda mm: float(mm.group(1)) if mm else None)(re.search(r"丢 \d+ 拍 = ([\d.]+)%", text))}
    start_rc = rc.grab(r"启动 rc=(-?\d+)", text, cast=int, default=-99)
    if rcv != 0 or start_rc != 0 or m["e2e"] is None:
        reg.emit(tag, "FAIL", m, note="启动 rc=%s" % start_rc)
        return False
    # 阈值动态：端到端受 bench 能力与周期共同封顶，取两者较小值的 70%
    # （pyusb 同步读的主机侧损耗，README 记录过 ~1/3 是主机读法所致）
    cap = min(BL_BENCH_ONE * reg.floor, 1000.0 / 10)
    ok_t = reg.rate_check(tag, "e2e_khz", m["e2e"], cap, floor=0.7)
    reg.emit(tag, "PASS" if ok_t else "FAIL", m)
    ok &= ok_t
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", default=None, help="探针 CDC 口（RTT 交付用）；缺省自动探测（VID/PID 匹配）")
    ap.add_argument("--floor", type=float, default=0.8, help="吞吐判定下限比例（默认 0.8）")
    ap.add_argument("--budget", type=float, default=2700, help="全程预算秒数（超时硬退出）")
    ap.add_argument("--keep-going", action="store_true", help="阶段失败后继续跑后续阶段")
    ap.add_argument("--skip-flash", action="store_true", help="不构建/烧录靶子固件（假定已在位）")
    ap.add_argument("--rtt-secs", type=float, default=5.0, help="RTT 交付窗口秒数")
    ap.add_argument("--loss-secs", type=float, default=10.0, help="RTT 零丢校验窗口秒数")
    ap.add_argument("--scope-secs", type=float, default=3.0, help="HSS run 每窗秒数")
    ap.add_argument("--list", action="store_true", help="只打印阶段计划与基线，不跑")
    args = ap.parse_args()

    if args.list:
        print("P1.engine  open + selfcheck + rbench %.1f / wbench %.1f KB/s + sbastat(全 0)"
              % (BL_RBENCH, BL_WBENCH))
        print("P2.rtt     烧 flood 固件 → delivery %.1f KB/s + 零丢校验（lost/dup 必须 0）" % BL_RTT)
        print("P3.scope   烧 scope 固件 → integrity 0 违例 / bench one %.0f rv %.1f kHz / "
              "run 契约 PASS / 拟合 T=a+b·字数" % (BL_BENCH_ONE, BL_BENCH_RV))
        print("floor=%.0f%%  budget=%.0fs  port=%s" % (args.floor * 100, args.budget, args.port))
        return 0

    reg = rc.Regression("riscv", floor=args.floor, budget=args.budget, keep_going=args.keep_going)
    if not rc.probe_present():
        reg.emit("P0.preflight", "FAIL", {}, note="探针 HID 不在（VID 0D28/PID 0204）")
        return reg.finish("P0")
    if args.port is None:
        args.port = rc.find_cdc_port()
        if args.port is None:
            reg.emit("P0.preflight", "FAIL", {}, note="探针 CDC 串口没找到（RTT 阶段需要）")
            return reg.finish("P0")
    reg.emit("P0.preflight", "PASS", {"cdc_port": args.port})

    for name, fn in (("P1.engine", lambda: phase_engine(reg)),
                     ("P2.rtt", lambda: phase_rtt(reg, args)),
                     ("P3.scope", lambda: phase_scope(reg, args))):
        ok = fn()
        if not ok and not args.keep_going:
            return reg.finish(name)
    return reg.finish()


if __name__ == "__main__":
    sys.exit(main())
