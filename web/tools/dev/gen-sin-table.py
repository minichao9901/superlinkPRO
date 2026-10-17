#!/usr/bin/env python3
"""生成 HPM J-Scope 靶子固件的「高速平滑正弦」查表头文件。

背景：契约块 `g_v.f_sin` 是 **10 kHz 更新 + 20 点查表** —— 一个 500 Hz 周期只有 20 个不同值，
每级台阶 100 µs。用 100 kHz 采样时，一个台阶里有 10 个采样点取到同一个值，画出来就是台阶。
想看"连续"的波形，需要**更新率 ≫ 采样率**，所以另加一块 `g_v_hi`：
每 5 µs（200 kHz）更新一次，一个 500 Hz 周期 = 400 拍 = 400 个不同值 ⇒ 采样 100 kHz 时
每两个采样点之间信号都在变。

表就是这一路的"契约"：`f = 更新率 / N`，所以 N 必须和 main.c 里的节拍常量一致。
本脚本生成 `src/sin_hi_table.h`，`--check` 用来校验仓库里那份没被手改（CI / 自测用）。

用法：
    python tools/dev/gen-sin-table.py                 # 生成 src/sin_hi_table.h
    python tools/dev/gen-sin-table.py --check         # 只校验，不一致则退出码 1
    python tools/dev/gen-sin-table.py --n 400 --name kSinHi
"""
import argparse
import math
import pathlib
import sys

# Windows 控制台默认是 GBK，打印 ✓/❌ 这种字符会 UnicodeEncodeError —— 统一改成 UTF-8 兜底
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_OUT = HERE.parent.parent / "tools" / "target-firmware" / "hpm6800evk_scope" / "src" / "sin_hi_table.h"


def render(n: int, name: str, update_hz: int) -> str:
    freq = update_hz / n
    lines = [
        "/* 本文件由 tools/dev/gen-sin-table.py 自动生成 —— 别手改。",
        f" * {n} 点正弦表：f = 更新率 / N = {update_hz} Hz / {n} = {freq:g} Hz。",
        f" * 生成公式：sin(2π·i/{n})，格式 %.9ff。",
        " * 配套：main.c 里 g_v_hi 每 %d µs 更新一拍（%.9g kHz）。" % (round(1e6 / update_hz), update_hz / 1e3),
        " */",
        f"#define SIN_HI_N {n}U",
        f"static const float {name}[SIN_HI_N] = {{",
    ]
    per_line = 5
    for i in range(0, n, per_line):
        chunk = ", ".join(f"{math.sin(2 * math.pi * k / n):.9f}f" for k in range(i, min(i + per_line, n)))
        lines.append("    " + chunk + ("," if i + per_line < n else ","))
    lines.append("};")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=400, help="一个周期多少拍（默认 400 ⇒ 200 kHz/400 = 500 Hz）")
    ap.add_argument("--name", default="kSinHi", help="表名（默认 kSinHi）")
    ap.add_argument("--update-hz", type=int, default=200_000, help="g_v_hi 的更新率（默认 200 kHz）")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--check", action="store_true", help="只校验已存在的文件与公式一致")
    args = ap.parse_args()

    want = render(args.n, args.name, args.update_hz)
    out = pathlib.Path(args.out)
    if args.check:
        if not out.exists():
            print(f"❌ 缺少 {out}")
            return 1
        got = out.read_text(encoding="utf-8")
        if got != want:
            print(f"❌ {out} 与生成公式不一致（被手改了？重跑本脚本）")
            return 1
        print(f"✓ {out.name} 与公式一致（{args.n} 点 / {args.update_hz} Hz ⇒ {args.update_hz / args.n:g} Hz）")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(want, encoding="utf-8", newline="\n")
    print(f"✓ 写出 {out}（{args.n} 点 ⇒ {args.update_hz / args.n:g} Hz，{out.stat().st_size} B）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
