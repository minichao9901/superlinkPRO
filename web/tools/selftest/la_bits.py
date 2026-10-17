#!/usr/bin/env python3
"""从 kingst_la.py 导出的 CSV 里重建 SWD 位流（在 CLK 上升沿采样 DIO）。

用法：
    python la_bits.py <csv> [--rle] [--lo N] [--hi N]
        --rle   用"值×个数"的游程形式打印（找 64 个 1 + 8 个 0 的激活序列最方便）
        --lo/--hi  只看某个位区间

CSV 格式（KingstVIS 导出）：每行一个跳变点，给出**当时所有通道**的电平：
    Time[s], CH0, CH1
"""
import argparse
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load(path):
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        next(f, None)
        for line in f:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            try:
                rows.append((float(parts[0]) * 1e9, int(parts[1]), int(parts[2])))
            except ValueError:
                continue
    return rows


def sample_bits(rows):
    """CLK 上升沿处的 (时间ns, DIO) 列表"""
    bits = []
    prev_clk = rows[0][1] if rows else 0
    for t, clk, dio in rows[1:]:
        if prev_clk == 0 and clk == 1:
            bits.append((t, dio))
        prev_clk = clk
    return bits


def rle(bits, lo, hi):
    out = []
    for _, v in bits[lo:hi]:
        if out and out[-1][0] == v:
            out[-1][1] += 1
        else:
            out.append([v, 1])
    return " ".join(f"{v}x{n}" for v, n in out)


def dump(bits, lo, hi, period_hint=None):
    print(f"  位序列（共 {len(bits)} 位，展示 {lo}..{min(hi, len(bits))}）：")
    line = ""
    for i, (t, v) in enumerate(bits[lo:hi], start=lo):
        line += str(v)
        if (i + 1) % 8 == 0:
            line += " "
    print("    " + line.strip())
    # 位周期
    if len(bits) > lo + 1:
        d = [bits[i + 1][0] - bits[i][0] for i in range(lo, min(hi, len(bits) - 1))]
        d = [x for x in d if x > 0]
        if d:
            d.sort()
            print(f"  位周期：中位 {d[len(d)//2]:.0f} ns  最小 {d[0]:.0f} ns  最大 {d[-1]:.0f} ns")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--rle", action="store_true")
    ap.add_argument("--lo", type=int, default=0)
    ap.add_argument("--hi", type=int, default=400)
    a = ap.parse_args()

    rows = load(a.csv)
    bits = sample_bits(rows)
    print(f"{a.csv}: {len(rows)} 个跳变点，{len(bits)} 个 CLK 上升沿")
    if a.rle:
        print("  游程(值x个数):")
        # 分段打印，便于找长游程
        seg = 64
        for i in range(0, min(len(bits), a.hi), seg):
            print(f"    [{i:5d}] {rle(bits, i, min(i + seg, a.hi))}")
    else:
        dump(bits, a.lo, a.hi)


if __name__ == "__main__":
    main()
