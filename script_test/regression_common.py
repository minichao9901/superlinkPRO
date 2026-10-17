# -*- coding: utf-8 -*-
"""回归编排器的公共层（regression_swd.py / regression_riscv.py 共用）。

职责（对应 docs 的一键回归需求）：
  1. 逐阶段调用现有测试脚本，**流式**把子进程输出回显到控制台 + 日志文件 ——
     每一轮（每个阶段/每个频率档）跑完立刻落一条结果，监控方（人或 AI）实时可读；
  2. 指标判定：吞吐类 >= 基线 × floor（默认 0.8）；错误/丢失类必须为 0；
  3. 任一阶段 FAIL ⇒ 默认**立即停止**（不再跑后续阶段），退出码 2；
     全程预算超时（--budget）⇒ 硬退出（防卡死），退出码 3；
  4. 结果持久化：build/regression/<family>.log（人读）+ .jsonl（机读，逐条 JSON）。

子进程输出按 UTF-8 读；为防"子脚本没 reconfigure stdout 就用控制台代码页写中文"
导致的假失败，启动子进程时统一注入 `PYTHONIOENCODING=utf-8:replace`
（见 child_env()），本模块的 stdout 也 reconfigure 成 UTF-8/replace，
GBK 控制台不会被打崩。
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUTDIR = os.path.join(REPO, "build", "regression")

VID, PID = 0x0D28, 0x0204
SDK_ENV = os.environ.get("HPM_SDK_ENV_DIR", r"E:\sdk_env_v1.11.0")
OPENOCD = os.environ.get("OPENOCD_EXE", os.path.join(SDK_ENV, "tools", "openocd", "openocd.exe"))


def child_env():
    """给子脚本的环境变量：强制它的 stdout/stderr 也用 UTF-8。

    为什么必须在编排层兜住：本模块按 `encoding="utf-8"` 读子进程输出，而子脚本
    `print()` 中文时用的是**控制台代码页**（本机 cp936）—— 中文于是变成乱码，
    脚本里按字面中文写的正则（例如 regression_riscv.py 的
    `sticky 错误事件 = (\\d+)`、`整块重读 = (\\d+)`）匹配不上、取默认值 -1，
    表现为"探针明明回了正确值，却报 sticky_events=-1 FAIL"的假失败。
    （2026-10-03 实测：make regression-riscv 在 P1.sbastat 假红，手工设
    PYTHONUTF8/PYTHONIOENCODING 后立刻全绿。）

    约定本来是子脚本自己 `sys.stdout.reconfigure(encoding="utf-8")`，但 script_test
    下还有十几个脚本没写这行（hpm6800_riscv/probe/selfcheck、sram_speed_test、
    rtt_probe_bridge …），逐个补既琐碎、新脚本还会再忘 —— 干脆在唯一的启动点上
    兜住。只设 PYTHONIOENCODING（管 stdin/stdout/stderr 的编码），**不设
    PYTHONUTF8**：后者会连带改掉子脚本 open() 的默认编码，副作用更大。
    errors 用 replace：个别脚本会 print 无法编码的字符，宁可显示成 ? 也不能让
    子进程抛 UnicodeEncodeError 把整轮回归带崩。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8:replace"
    return env

FAILS = []          # 本进程内累积的失败描述（阶段结束后由调用方决定停不停）
RESULTS = []        # 全部阶段结果（内存 + jsonl）


class Regression:
    def __init__(self, family, floor=0.8, budget=3600.0, keep_going=False):
        self.family = family
        self.floor = floor
        self.keep_going = keep_going
        os.makedirs(OUTDIR, exist_ok=True)
        self.log_path = os.path.join(OUTDIR, "%s.log" % family)
        self.jsonl_path = os.path.join(OUTDIR, "%s.jsonl" % family)
        self.log = open(self.log_path, "a", encoding="utf-8", errors="replace")
        self.t0 = time.time()
        self._budget = budget
        self._budget_hit = False
        if budget > 0:
            t = threading.Thread(target=self._watchdog, daemon=True)
            t.start()
        self.line("=" * 74)
        self.line("[%s] 回归开始 %s  (floor=%.0f%%, budget=%.0fs)" %
                  (family, time.strftime("%F %T"), floor * 100, budget))

    # ---- 基础设施 ---------------------------------------------------------
    def _watchdog(self):
        time.sleep(self._budget)
        self._budget_hit = True
        self.line("!! 全程预算超时（%.0fs）—— 硬退出，避免卡死" % self._budget)
        try:
            self.emit("BUDGET", "FAIL", {}, note="total budget %.0fs exceeded" % self._budget)
        except Exception:
            pass
        os._exit(3)

    def line(self, msg):
        stamp = time.strftime("%H:%M:%S")
        text = "%s  %s" % (stamp, msg)
        print(text, flush=True)
        self.log.write(text + "\n")
        self.log.flush()

    def emit(self, phase, verdict, metrics, note=""):
        """一条阶段结果：控制台 RESULT 行 + jsonl 一条。verdict: PASS/FAIL/SKIP/INFO"""
        rec = {"ts": time.strftime("%F %T"), "family": self.family, "phase": phase,
               "verdict": verdict, "metrics": metrics, "floor": self.floor, "note": note}
        RESULTS.append(rec)
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        # FAIL 必须进失败累积（finish() 按 FAILS 判定整体结论）——否则"阶段 FAIL
        # 但 SUMMARY PASS"的自相矛盾（首跑实测踩过：烧录失败后总结仍然 PASS）。
        if verdict == "FAIL" and phase != "SUMMARY":
            FAILS.append(phase)
        mtext = " ".join("%s=%s" % (k, v) for k, v in metrics.items())
        self.line("RESULT [%s] %s %s %s" % (phase, verdict, mtext, ("| " + note) if note else ""))

    # ---- 子进程（流式回显 + 硬超时） --------------------------------------
    def run(self, argv, timeout, tag, cwd=None):
        """跑一个子脚本，逐行回显；返回 (rc, tail_lines, secs)。超时 kill ⇒ rc=None。"""
        self.line("---- run [%s]: %s" % (tag, " ".join(os.path.basename(a) if i == 1 else a
                                                         for i, a in enumerate(argv))))
        t0 = time.time()
        p = subprocess.Popen(argv, cwd=cwd or REPO, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", bufsize=1,
                             env=child_env())
        tail = []
        try:
            for ln in p.stdout:
                ln = ln.rstrip()
                if not ln:
                    continue
                tail.append(ln)
                if len(tail) > 200:
                    tail.pop(0)
                print("    " + ln, flush=True)
                self.log.write("    " + ln + "\n")
                self.log.flush()
                if time.time() - t0 > timeout:
                    raise subprocess.TimeoutExpired(argv, timeout)
            rc = p.wait(timeout=max(5, timeout - (time.time() - t0)))
        except subprocess.TimeoutExpired:
            p.kill()
            try:
                p.wait(timeout=5)
            except Exception:
                pass
            self.line("!! [%s] 子进程超时（%ds）被终止" % (tag, timeout))
            return None, tail, time.time() - t0
        return rc, tail, time.time() - t0

    # ---- 判定 -------------------------------------------------------------
    def need(self, phase, ok, desc, metrics):
        """单条判定：ok=False 记 FAIL 并返回 False（由阶段聚合决定停不停）。"""
        if ok:
            self.line("  ok   [%s] %s" % (phase, desc))
        else:
            FAILS.append("[%s] %s" % (phase, desc))
            self.line("  FAIL [%s] %s" % (phase, desc))
        return ok

    def rate_check(self, phase, name, value, baseline, floor=None):
        """吞吐类：value >= baseline × floor 才算过。返回布尔。"""
        f = self.floor if floor is None else floor
        ok = (value is not None) and (value >= baseline * f)
        return self.need(phase, ok, "%s=%.1f (基线 %.1f × %.0f%% = %.1f)" %
                         (name, value if value is not None else -1, baseline, f * 100, baseline * f), {})

    def zero_check(self, phase, name, value):
        ok = (value == 0)
        return self.need(phase, ok, "%s=%s (必须为 0)" % (name, value), {})

    # ---- 收尾 -------------------------------------------------------------
    def finish(self, stopped_phase=None):
        total = time.time() - self.t0
        verdict = "PASS" if not FAILS else "FAIL"
        self.line("=" * 74)
        self.line("[%s] 回归结束: %s, %.0fs, 失败 %d 项" % (self.family, verdict, total, len(FAILS)))
        for f in FAILS:
            self.line("  - " + f)
        if stopped_phase:
            self.line("  （在 %s 之后提前停止）" % stopped_phase)
        self.emit("SUMMARY", verdict, {"fails": len(FAILS), "secs": round(total, 1),
                                       "budget_hit": self._budget_hit})
        self.log.close()
        return 0 if not FAILS else 2


# ---- 解析辅助（两个回归共用的小正则） --------------------------------------
def grab(pattern, text, cast=float, default=None):
    m = re.search(pattern, text, re.M)
    if not m:
        return default
    try:
        return cast(m.group(1))
    except (ValueError, IndexError):
        return default


def probe_present():
    try:
        import hid
        return any(i.get("usage_page") == 0xFF00 for i in hid.enumerate(VID, PID))
    except Exception:
        return False


def find_cdc_port():
    """探针的 CDC 口不是固定的（换 USB 口/重枚举都会变，本机实测 COM5→COM43）。
    按 USB VID/PID 枚举串口，找到探针的虚拟串口；找不到返回 None。"""
    try:
        import serial.tools.list_ports
        for p in serial.tools.list_ports.comports():
            if p.vid == VID and p.pid == PID:
                return p.device
    except Exception:
        pass
    return None


def linfit(xs, ys):
    """最小二乘 y = a + b·x，返回 (a, b, r2)。点数 < 2 返回 None。"""
    n = len(xs)
    if n < 2:
        return None
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    den = n * sxx - sx * sx
    if den == 0:
        return None
    b = (n * sxy - sx * sy) / den
    a = (sy - b * sx) / n
    ym = sy / n
    ss_tot = sum((y - ym) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return a, b, r2


def powershell(script, *args):
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script] + list(args)
