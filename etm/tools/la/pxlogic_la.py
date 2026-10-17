#!/usr/bin/env python3
"""PXLogic（PXView）逻辑分析仪采集后端 —— 命令行与 Kingst 后端同构。

为什么这样做：Kingst 那条路是「外部程序采集 + 导出跳变 CSV + Python 解析」，
这里对 PXLogic 采用同样的形态，只是外部程序换成 PXView 自带的 `pxview-cli.cmd`：

    pxview-cli capture --device N --channels 0,1,2,3,4 --rate 500M --time 2ms ...
    pxview-cli export  --format binary --dir <raw>        # channel_N.bin，位打包
    （Python）channel_N.bin → 跳变 CSV（Time[s], 通道 N, ...）→ tools/trace_port.py 起的既有解码链

这样两个 LA 后端在**文件层**收敛：同一份 `Time[s], 通道 N` 跳变 CSV，
`tools/trace_port.py` / `tools/decode_trace.py` 不需要区分数据来自哪台设备。

关键事实（本机实测，PXView 版本见 capture.json）：
  · `export` 不传 `--digital-channels` 时 csv/vcd/hex/bits **什么都不导出**（binary 默认导出全部使能通道）；
  · `channel_N.bin` 是位打包：8 样本/字节、LSB-first，字节 i 的 bit k = 样本 i*8+k；
  · bin 按字节补齐，**末字节的补齐位会产生假跳变**，所以转换/统计必须传真实样本数（见 transitions()）；
  · 采样率/时长被如实执行（500 MSa/s × 2 ms × 5 通道 = 125000 B/通道 = 1000000 样本）；
  · 采集是阻塞式：`capture` 子命令返回即表示采集结束。

已知限制（都是 PXView CLI 本身的能力边界，不是本工具的取舍）：
  · **没有 stop/取消子命令**：正在等待触发的采集无法用 CLI 中止，只能在 PXView 界面里停；
  · `capture --trigger 通道:类型` 目前**不生效**（CLI 把它塞进服务器不接收的 digitalCaptureMode
    字段），触发需要在 PXView 界面或用 MCP 的 configure_trigger 设置；
  · 客户端对未显式指定超时的调用统一钳到 30 s，超大导出可能超时；
  · 所有 CLI 调用共享 PXView 的同一个会话，不能并发跑两条采集/导出。

本工具不启动、不关闭 PXView 进程；采集命令会设置通道、采样率、时长与阈值。
CLI 和 GUI 共用一个会话，使用时须串行操作。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

DEFAULT_CHANNELS = [0, 1, 2, 3, 4]          # CH0=TRACECLK，CH1..4=D0..D3（本工程接线）
CLI_CANDIDATES = (
    r"C:\Program Files\PXView\pxview-cli.cmd",
    r"C:\Program Files (x86)\PXView\pxview-cli.cmd",
    r"C:\PXView\pxview-cli.cmd",
    r"D:\PXView\pxview-cli.cmd",
)


# ======================================================================
#  1. 命令行封装
# ======================================================================
def find_cli(explicit: str | None = None) -> str:
    """定位 pxview-cli.cmd。环境变量 PXVIEW_CLI 优先，其次安装目录默认位置。"""
    import os

    for candidate in filter(None, (explicit, os.environ.get("PXVIEW_CLI"), *CLI_CANDIDATES)):
        if Path(candidate).exists():
            return str(candidate)
    raise SystemExit(
        "找不到 pxview-cli。用 --cli 指定，或设置环境变量 PXVIEW_CLI 指向 pxview-cli.cmd。"
    )


def run_cli(cli: str, args: list[str], timeout: float, json_out: bool = True):
    """跑一条 pxview-cli 子命令，返回 (stdout 文本, 解析后的 JSON 或 None)。

    🚨 两个坑：
      ① 所有外部调用都必须带超时：PXView 在设备忙或触发未命中时会一直挂着，
         而 CLI **没有 stop 子命令**，挂住只能去界面里停；
      ② 子进程 stdout 被管道接走时按控制台编码（中文 Windows = GBK）输出，
         `channels` / `list-decoders` 这类含中文的返回会直接崩，
         所以固定给它 PYTHONIOENCODING=utf-8。
    """
    import os

    command = [cli, *(["--json"] if json_out else []), *args]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        done = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(
            f"pxview-cli 超时（{timeout:g}s）：{' '.join(args)}\n"
            f"提示：CLI 没有 stop 子命令；若卡在等待触发，请在 PXView 界面里停止采集。"
        ) from None
    text = (done.stdout or "").strip()
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()
        raise SystemExit(f"pxview-cli 失败（退出码 {done.returncode}）：{detail or ' '.join(args)}")
    parsed = None
    if json_out and text:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
    return text, parsed


def status(cli: str, timeout: float = 30.0) -> dict:
    _, parsed = run_cli(cli, ["status"], timeout)
    return parsed if isinstance(parsed, dict) else {}


def devices(cli: str, timeout: float = 30.0) -> list[dict]:
    _, parsed = run_cli(cli, ["list-devices"], timeout)
    return parsed if isinstance(parsed, list) else []


def pick_device(cli: str, want: str | None, demo: bool = False, timeout: float = 30.0) -> dict:
    """选设备：--device 给 ID 或名字片段；否则取第一台（非）演示设备。"""
    found = devices(cli, timeout)
    if want:
        for item in found:
            if str(item.get("id")) == str(want) or want.lower() in str(item.get("display_name", "")).lower():
                return item
        raise SystemExit(f"没有匹配的设备：{want}（可用：{[d.get('id') for d in found]}）")
    pool = [d for d in found if bool(d.get("is_demo")) is demo]
    if not pool:
        raise SystemExit("没有可用设备：PXLogic 未连接？用 devices 子命令确认。")
    return pool[0]


# ======================================================================
#  2. 参数解析（与 pxview-cli 的 K/M/G、ms/s 后缀一致）
# ======================================================================
def parse_rate(text) -> int:
    """'500M' / '10k' / '500000000' → Hz。"""
    if isinstance(text, int):
        return text
    match = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([kKmMgG]?)\s*", str(text))
    if not match:
        raise SystemExit(f"采样率无法识别：{text!r}（示例 500M / 100M / 1000000）")
    scale = {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}[match.group(2).lower()]
    return int(float(match.group(1)) * scale)


def parse_seconds(text) -> float:
    """'2ms' / '0.002' / '2s' → 秒。"""
    if isinstance(text, (int, float)):
        return float(text)
    match = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*(ms|us|s|m)?\s*", str(text))
    if not match:
        raise SystemExit(f"时长无法识别：{text!r}（示例 2ms / 0.01 / 10s）")
    scale = {"ms": 1e-3, "us": 1e-6, "s": 1.0, "m": 60.0, None: 1.0}[match.group(2)]
    return float(match.group(1)) * scale


def parse_channels(text) -> list[int]:
    """'0,1,2,3,4' 或 '0-4' → [0,1,2,3,4]。"""
    if isinstance(text, (list, tuple)):
        return [int(item) for item in text]
    out: list[int] = []
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            low, high = part.split("-", 1)
            out.extend(range(int(low), int(high) + 1))
        else:
            out.append(int(part))
    return out


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# ======================================================================
#  3. 位打包样本 → 跳变表
# ======================================================================
def load_bits(path: Path) -> tuple[bytes, int]:
    """读 channel_N.bin，返回 (原始字节, 样本数)。8 样本/字节、LSB-first。"""
    data = path.read_bytes()
    return data, len(data) * 8


def level_at(data: bytes, index: int) -> int:
    return (data[index >> 3] >> (index & 7)) & 1


def transitions(data: bytes, samples: int | None = None) -> list[int]:
    """返回跳变样本序号（该样本的电平与前一个样本不同），升序。

    用大整数做异或，只遍历真实跳变，避免逐样本 Python 循环。
    bit i of (v ^ (v >> 1)) = 样本 i 与 i+1 是否不同；要的是「i 与 i-1 不同」，
    即同一个位图的 bit i-1，所以最后结果 = 位号 + 1。

    🚨 `samples` 必须传**真实样本数**：bin 是按字节补齐的，末字节的补齐 0
    会被当成“电平掉到 0”，凭空多出尾部跳变（8 的倍数时不会暴露）。
    不传就退化为 len(data)*8，并可能带尾部假跳变。
    """
    if samples is None:
        samples = len(data) * 8
    if samples <= 0:
        return []
    out: list[int] = []
    # 分块避免每个跳变都复制整个百万位整数；保留块边界的相邻样本比较。
    for offset in range(0, (samples + 7) // 8, 1024):
        bits = min(8192, samples - offset * 8)
        value = int.from_bytes(data[offset:offset + 1024], "little")
        diff = (value ^ (value << 1)) & ((1 << bits) - 1)
        previous = level_at(data, offset * 8 - 1) if offset else (value & 1)
        diff ^= previous
        while diff:
            low = diff & -diff
            out.append(offset * 8 + low.bit_length() - 1)
            diff ^= low
    return out


def write_transition_csv(directory: Path, out: Path, channels: list[int],
                         rate: int, samples: int | None = None) -> dict:
    """把 channel_N.bin 目录写成与 Kingst 同构的跳变 CSV。

    表头 `Time[s], 通道 0, 通道 1, ...`，只写发生跳变的时刻；
    首行是窗口起点电平（与 Kingst 导出一致，tools/trace_port.py 依赖这个语义）。
    `samples` 传采集的**真实样本数**（用来裁掉末字节的补齐位）。

    🚨 **不能写 `#` 注释行**：tools/trace_port.py 的 read_tracks() 把第一个非空行当表头，
    多一行注释就会把 `Time[s]` 当成时间列而报错。溯源信息一律进 capture.json。
    """
    data: dict[int, bytes] = {}
    padded = None
    for channel in channels:
        path = directory / f"channel_{channel}.bin"
        if not path.exists():
            raise SystemExit(f"缺少 {path}（导出目录不对，或该通道未使能）")
        block, count = load_bits(path)
        if padded is None:
            padded = count
        elif padded != count:
            raise SystemExit(f"通道样本数不一致：channel_{channel} = {count}，其他 = {padded}")
        data[channel] = block
    assert padded is not None

    if samples is None:
        samples = padded
    elif samples > padded:
        raise SystemExit(f"真实样本数 {samples} 超过导出位流容量 {padded}，参数不对")
    padding = padded - samples

    merged: dict[int, None] = {}
    for block in data.values():
        for index in transitions(block, samples):
            merged[index] = None
    indices = sorted(merged)
    strip = 1.0 / rate
    with out.open("w", encoding="utf-8-sig", newline="") as stream:
        stream.write("Time[s], " + ", ".join(f"通道 {c}" for c in channels) + "\n")
        stream.write(f"0.000000000, " + ", ".join(str(level_at(b, 0)) for b in data.values()) + "\n")
        for index in indices:
            row = ", ".join(str(level_at(block, index)) for block in data.values())
            stream.write(f"{index * strip:.9f}, {row}\n")
    return {
        "csv": str(out.resolve()),
        "samples": samples,
        "padded_samples": padded,
        "padding_samples": padding,
        "window_ms": samples / rate * 1000,
        "rows": len(indices) + 1,
        "transitions": {str(c): len(transitions(b, samples)) for c, b in data.items()},
        "csv_sha256": sha256(out),
    }


# ======================================================================
#  4. 统计
# ======================================================================
def channel_stats(directory: Path, channel: int, rate: int, samples: int | None = None) -> dict:
    data, padded = load_bits(directory / f"channel_{channel}.bin")
    if samples is None:
        samples = padded
    elif samples > padded:
        raise SystemExit(f"真实样本数 {samples} 超过导出位流容量 {padded}，参数不对")
    edges = transitions(data, samples)
    valid = data[: (samples + 7) // 8]
    high = sum(bin(byte).count("1") for byte in valid)
    if samples % 8 and valid:                      # 末字节的补齐位不算进占空比
        high -= bin(valid[-1] >> (samples % 8)).count("1")
    rising = [i for i in edges if level_at(data, i) == 1]
    periods = sorted(b - a for a, b in zip(rising, rising[1:]))
    median_period = periods[len(periods) // 2] if periods else None
    half_periods = [b - a for a, b in zip(edges, edges[1:])]
    ordered = sorted(half_periods)
    median_half = ordered[len(ordered) // 2] if ordered else None
    # 🚨 频率用**中位完整周期**算，不要用首末边沿跨度：采集起点常有一个 1 样本毛刺，
    #    同极性沿之间的完整周期也适用于非 50% 占空比参考，避免把高/低宽度当半周期。
    frequency = round(rate / median_period, 3) if median_period else None
    span_frequency = None
    if len(rising) > 1 and rising[-1] > rising[0]:
        span_frequency = round((len(rising) - 1) * rate / (rising[-1] - rising[0]), 3)
    glitches = [h for h in half_periods if median_half and h < median_half * 0.5]
    # 稳定性用**相对**容差：半周期越长，允许的绝对样本抖动越大（采样率越高越明显）。
    slack = max(1.0, median_half * 0.001) if median_half else 1.0
    stable = (sum(1 for h in half_periods if abs(h - median_half) <= slack) / len(half_periods)
              if half_periods and median_half else None)
    return {
        "channel": channel,
        "samples": samples,
        "padded_samples": padded,
        "edges": len(edges),
        "rising": len(rising),
        "duty_percent": round(high / samples * 100, 3) if samples else 0.0,
        "frequency_hz": frequency,
        "frequency_from_span_hz": span_frequency,
        "median_half_period_samples": median_half,
        "median_period_samples": median_period,
        "min_half_period_samples": ordered[0] if ordered else None,
        "max_half_period_samples": ordered[-1] if ordered else None,
        "half_period_stable_percent": round(stable * 100, 2) if stable is not None else None,
        "glitch_count": len(glitches),
        "first_edges": edges[:6],
    }


def print_stats(directory: Path, channels: list[int], rate: int, samples: int | None = None) -> dict:
    print(f"采样率 {rate/1e6:g} MSa/s（按传入采样率换算；是否核验见 capture.json）  目录 {directory}")
    report = {}
    for channel in channels:
        if not (directory / f"channel_{channel}.bin").exists():
            continue
        item = channel_stats(directory, channel, rate, samples)
        report[str(channel)] = item
        frequency = f"{item['frequency_hz']/1e6:.6f} MHz" if item["frequency_hz"] else "—"
        stable = f"{item['half_period_stable_percent']}%" if item["half_period_stable_percent"] is not None else "—"
        half = (item['median_half_period_samples'] if item['median_half_period_samples'] is not None
                else '—')
        print(f"  CH{channel:<2} 样本 {item['samples']:>9}  跳变 {item['edges']:>8}  "
              f"上升沿 {item['rising']:>8}  频率 {frequency:>14}  高占比 {item['duty_percent']:6.2f}%"
              f"  半周期中位 {half}  毛刺 {item['glitch_count']}  稳定 {stable}")
    return report


# ======================================================================
#  5. 子命令
# ======================================================================
def cmd_devices(args) -> int:
    cli = find_cli(args.cli)
    found = devices(cli, args.timeout)
    if args.json:
        print(json.dumps(found, ensure_ascii=False, indent=2))
        return 0
    print(f"pxview-cli: {cli}")
    for item in found:
        kind = "演示" if item.get("is_demo") else ("硬件" if item.get("is_hardware") else "文件")
        print(f"  id={item.get('id'):<4} {kind}  {item.get('display_name')}  driver={item.get('driver_name')}")
    return 0


def cmd_status(args) -> int:
    cli = find_cli(args.cli)
    info = status(cli, args.timeout)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def cmd_export(args) -> int:
    """把 PXView 当前 buffer 导出到目录（不重新采集）。"""
    cli = find_cli(args.cli)
    directory = Path(args.dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    channels = parse_channels(args.channels) if args.channels else None
    run_cli(cli, ["export", "--format", "binary", "--dir", str(directory)], args.timeout, json_out=False)
    if args.format in ("csv", "both"):
        if not channels:
            raise SystemExit("导出 CSV 必须给 --channels（PXView 的 csv 导出不传通道会导出空文件）")
        run_cli(cli, ["export", "--format", "csv", "--dir", str(directory),
                      "--digital-channels", ",".join(str(c) for c in channels)], args.timeout, json_out=False)
    files = sorted(p.name for p in directory.glob("channel_*"))
    print(f"已导出到 {directory}：{', '.join(files) if files else '（没有任何文件）'}")
    return 0 if files else 2


def prepare_raw_dir(directory: Path, reuse: bool) -> list[str]:
    """准备导出目录并**保证里面不会有上一次采集的残留**。

    🚨 为什么必须清：PXView 的 `export` 即使一个字节都没写也会返回 `exported` / 退出码 0，
    所以「目录里已经有 channel_5.bin」这种残留会被误当成本次采集的结果
    （Codex review 2026-10-13 的第一条）。
    返回被删掉的旧文件名，便于写进清单。
    """
    directory.mkdir(parents=True, exist_ok=True)
    stale = sorted(p.name for p in directory.glob("channel_*.bin"))
    if stale:
        if not reuse:
            raise SystemExit(
                f"目录 {directory} 里已有上次导出的 {stale}；不让新采集复用旧位流。\n"
                f"换一个目录（默认每次采集用带时间戳的新目录），或用 --reuse-raw 明确允许覆盖。"
            )
        for name in stale:
            (directory / name).unlink()
    return stale


def validate_export(directory: Path, channels: list[int], started: float) -> int:
    """确认所选通道本次真的导出，返回按字节补齐的容量。"""
    paths = [directory / f"channel_{c}.bin" for c in channels]
    if not paths or any(not p.is_file() for p in paths):
        raise SystemExit("导出缺少所选通道文件，拒绝使用旧数据")
    if any(p.stat().st_mtime < started - 1.0 for p in paths):
        raise SystemExit("位流时间戳早于本次导出，拒绝使用残留数据")
    sizes = {p.stat().st_size for p in paths}
    if len(sizes) != 1 or 0 in sizes:
        raise SystemExit("所选通道位流为空或长度不一致，导出不完整")
    return sizes.pop() * 8


def reconcile_samples(requested: int, padded: int, force: bool) -> int:
    """核对导出容量与请求样本数，返回用于生成时间轴的真实样本数。

    bin 按字节补齐，所以 padding ∈ [0,8) 属正常；超出这个范围说明设备没有按请求采集
    （改过采样率/时长），此时**拒绝**继续：时间轴仍按请求采样率换算，若默默接受会把
    错误的时间轴和溯源写成"已证实"（Codex review 2026-10-13 的第二条）。
    """
    padding = padded - requested
    if 0 <= padding < 8:
        return requested
    if not force:
        raise SystemExit(
            f"导出容量 {padded} 与请求样本数 {requested} 不符（差 {padding}，正常只应有 0~7 个补齐位）。\n"
            f"说明设备没有按请求的采样率/时长采集，此时按请求采样率换算的时间轴不可信。\n"
            f"请核对采样率与时长；确实要用这批数据请加 --force（清单会记下不符）。"
        )
    return padded


def verify_rate_from_reference(directory: Path, channel: int, expected_hz: float,
                               requested_rate: int, samples: int, tolerance: float) -> tuple[float, float]:
    """用采集里的已知频率参考反推实际采样率，返回 (实测采样率, 相对偏差)。

    PXView CLI 不回读采样率，但只要捕获里有频率已知的信号（本机是 LA 自身 PWM0 = 1 kHz，
    ETM 场景是目标板已知的 TRACECLK），就能从半周期样本数反推：
        实测采样率 = 中位完整周期样本数 × 已知频率
    这样「容量与请求不符」是**采样率变了**还是**只是窗口长了一点**就能分开
    （Codex review 2026-10-13 第二条要求区分 requested / actual）。
    """
    item = channel_stats(directory, channel, requested_rate, samples)
    period = item["median_period_samples"]
    if not period or item["rising"] < 4 or expected_hz <= 0:
        raise SystemExit(f"参考通道 CH{channel} 至少需要三个完整周期和有效参考频率")
    measured = period * expected_hz
    return measured, abs(measured - requested_rate) / requested_rate


def cmd_capture(args) -> int:
    cli = find_cli(args.cli)
    channels = parse_channels(args.channels or DEFAULT_CHANNELS)
    rate = parse_rate(args.rate)
    seconds = parse_seconds(args.time) if args.samples is None else None
    window = f"{seconds*1000:g} ms" if seconds is not None else f"{args.samples} 样本"
    # 真实样本数：bin 按字节补齐，转换时必须用它裁掉补齐位。
    expected = args.samples if args.samples is not None else round(rate * seconds)

    device = pick_device(cli, args.device, demo=args.demo, timeout=args.timeout)
    out_path = Path(args.out).resolve()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex
    if args.raw_dir:
        raw = Path(args.raw_dir).resolve()
        stale = prepare_raw_dir(raw, reuse=args.reuse_raw)
    else:
        # 每次采集一个唯一目录，绝不和历史位流混在一起。
        raw = Path(str(out_path.with_suffix("")) + ".pxlogic") / stamp
        stale = prepare_raw_dir(raw, reuse=False)
    if stale:
        print(f"已清掉导出目录里的旧位流：{'、'.join(stale)}")

    command = ["capture", "--device", str(device.get("id")),
               "--channels", ",".join(str(c) for c in channels),
               "--rate", str(rate),
               "--time" if seconds is not None else "--samples",
               str(seconds) if seconds is not None else str(args.samples),
               "--threshold", str(args.threshold)]
    if args.trigger:
        # CLI 会把触发塞进服务器不接收的 digitalCaptureMode 字段，实测不生效，这里明确告警而不是假装成功。
        print(f"⚠ --trigger {args.trigger}：PXView CLI 的触发参数当前不生效，本次采集按自由运行处理；"
              f"需要触发请在 PXView 界面里设置。")
        command += ["--trigger", args.trigger]

    print(f"采集：{device.get('display_name')}  通道 {channels}  {rate/1e6:g} MSa/s  "
          f"{window}  阈值 {args.threshold} V")
    started = time.time()
    text, parsed = run_cli(cli, command, args.timeout)
    elapsed = time.time() - started
    state = (parsed or {}).get("state") if isinstance(parsed, dict) else None
    info = status(cli, args.timeout)
    if state is None:
        state = info.get("state")
    if state != "completed" or not info.get("have_hardware_data"):
        raise SystemExit(f"采集未成功完成：state={state}，硬件数据={info.get('have_hardware_data')}；"
                         f"原始输出：{text[:200]}")

    # 导出：binary（位打包，供解析与统计）＋ csv（可选，供人工查看）
    export_started = time.time()
    run_cli(cli, ["export", "--format", "binary", "--dir", str(raw)], args.timeout, json_out=False)
    if args.with_csv:
        run_cli(cli, ["export", "--format", "csv", "--dir", str(raw),
                      "--digital-channels", ",".join(str(c) for c in channels)], args.timeout, json_out=False)

    padded = validate_export(raw, channels, export_started)
    requested_samples = (args.samples if args.samples is not None else round(rate * seconds))
    rate_verified = False
    measured_rate = None
    if args.verify_hz:
        measured_rate, deviation = verify_rate_from_reference(
            raw, args.verify_channel, args.verify_hz, rate, padded, args.verify_tolerance)
        print(f"采样率核对：参考 {args.verify_hz:g} Hz → 实测采样率 {measured_rate/1e6:.6f} MSa/s"
              f"（请求 {rate/1e6:g} MSa/s，偏差 {deviation*100:.4f}%）")
        if deviation > args.verify_tolerance:
            raise SystemExit(
                f"实测采样率 {measured_rate/1e6:.6f} MSa/s 与请求 {rate/1e6:g} MSa/s 偏差 "
                f"{deviation*100:.4f}% 超过容差 {args.verify_tolerance*100:g}%，拒绝按请求值生成时间轴。"
            )
        rate_verified = True
    expected = reconcile_samples(requested_samples, padded, force=args.force or rate_verified)
    count_mismatch = not 0 <= padded - requested_samples < 8
    if count_mismatch:
        if rate_verified:
            print(f"采集容量与请求不符：实际 {padded} 样本，请求 {requested_samples} 样本"
                  f"（+{(padded - requested_samples) / requested_samples * 100:.3f}%）；"
                  f"按实际容量生成时间轴，采样率已用参考信号核对。")
        else:
            print(f"⚠ 导出容量 {padded} 与请求 {requested_samples} 样本不符，已由 --force 放行；"
                  f"时间轴与溯源按请求采样率换算，不可当已证实（建议加 --verify-hz 用参考信号核对）。")

    csv_path = out_path
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    effective_rate = measured_rate if rate_verified else rate
    summary = write_transition_csv(raw, csv_path, channels, effective_rate, samples=expected)
    files = {p.name: {"bytes": p.stat().st_size, "sha256": sha256(p),
                      "mtime": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")}
             for p in sorted(raw.glob("channel_*.bin"))}
    manifest = {
        "backend": "pxlogic",
        "tool": "tools/la/pxlogic_la.py",
        "cli": cli,
        "created": datetime.now().isoformat(timespec="seconds"),
        "device": {"id": device.get("id"), "name": device.get("display_name"),
                   "driver": device.get("driver_name"), "usb_speed": device.get("usb_speed")},
        "capture": {
            "channels": channels,
            # 请求值与实际值必须分开：PXView CLI 没有回读采样率/样本数的子命令，
            # 只能核对导出容量是否与请求相符，所以实际采样率**未证实**，
            # 由它换算的时间轴与频率也只是「按请求值换算」，不能当已实测。
            "requested": {"sample_rate_hz": rate, "seconds": seconds, "samples": requested_samples},
            "actual": {"samples": summary["samples"], "export_capacity_samples": padded,
                       "sample_rate_hz": measured_rate, "verified": rate_verified,
                       "count_mismatch": count_mismatch,
                       "source": ("参考信号反推：中位完整周期样本数 × 已知频率"
                                  if rate_verified else
                                  "PXView CLI 无采样率回读接口；只在导出容量与请求不符时判定设备未按请求采集")},
            "threshold_volts": args.threshold, "trigger": args.trigger,
            "trigger_effective": False,
            "wall_seconds": round(elapsed, 3),
            "state": state,
            "window_ms": round(summary["window_ms"], 6),
            "rate_basis": "参考信号核验值" if rate_verified else "请求采样率（未核验）",
            "time_axis_rate_hz": effective_rate,
        },
        "files": files,
        "stale_removed": stale,
        "transition_csv": summary,
        "cli_commands": [f"pxview-cli {' '.join(command)}",
                         f"pxview-cli export --format binary --dir {raw}"],
    }
    (raw / "capture.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"完成：{summary['samples']} 样本 / {summary['window_ms']:.3f} ms，跳变 {summary['transitions']}")
    print(f"  跳变 CSV：{csv_path}")
    print(f"  原始位流：{raw}（{'、'.join(files)}）")
    print(f"  采集清单：{raw / 'capture.json'}")
    if count_mismatch:
        if rate_verified:
            print(f"  采集容量与请求不符（{padded} vs {requested_samples} 样本），已按实际容量生成时间轴。")
        else:
            print("⚠ 导出容量与请求样本数不符（--force 已放行）：时间轴按请求采样率换算，不可当已证实。")
    if rate_verified:
        print(f"  采样率已核对：参考 {args.verify_hz:g} Hz → {measured_rate/1e6:.6f} MSa/s"
              f"（清单 actual.verified=true）。")
    else:
        print("  说明：PXView CLI 不回读采样率，时间轴与频率按**请求值**换算"
              "（清单里 actual.verified=false；可加 --verify-hz 用参考信号核对）。")
    if args.stats:
        print()
        print_stats(raw, channels, effective_rate, expected)
    return 0


def cmd_csv(args) -> int:
    directory = Path(args.dir).resolve()
    channels = parse_channels(args.channels or DEFAULT_CHANNELS)
    rate = parse_rate(args.rate)
    samples = args.samples
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    summary = write_transition_csv(directory, out, channels, rate, samples=samples)
    print(json.dumps(summary, ensure_ascii=False, indent=2) if args.json else
          f"已写出 {out}：{summary['samples']} 样本，{summary['rows']} 行，跳变 {summary['transitions']}")
    return 0


def cmd_stats(args) -> int:
    directory = Path(args.dir).resolve()
    channels = parse_channels(args.channels or DEFAULT_CHANNELS)
    rate = parse_rate(args.rate)
    report = print_stats(directory, channels, rate, args.samples)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report else 2


def cmd_selftest(args) -> int:
    """自检：抓一段已知信号，核对频率/占空比是否落在期望范围内。

    本机接线：CH0 接 LA 自身的 PWM0（1 kHz、50%）。用 --expect-hz / --duty-tolerance
    判定，失败返回非 0，便于当验收脚本用。
    """
    cli = find_cli(args.cli)
    channel = parse_channels(args.channel)[0]
    rate = parse_rate(args.rate)
    seconds = parse_seconds(args.time)
    scratch = Path(args.dir).resolve() / uuid4().hex
    prepare_raw_dir(scratch, reuse=False)

    device = pick_device(cli, args.device, demo=args.demo, timeout=args.timeout)
    run_cli(cli, ["capture", "--device", str(device.get("id")), "--channels", str(channel),
                  "--rate", str(rate), "--time", str(seconds), "--threshold", str(args.threshold)],
            args.timeout)
    info = status(cli, args.timeout)
    if info.get("state") != "completed" or not info.get("have_hardware_data"):
        print(f"自检失败：采集状态 {info.get('state')}")
        return 2
    export_started = time.time()
    run_cli(cli, ["export", "--format", "binary", "--dir", str(scratch)], args.timeout, json_out=False)
    capacity = validate_export(scratch, [channel], export_started)
    samples = reconcile_samples(round(rate * seconds), capacity, force=False)

    item = channel_stats(scratch, channel, rate, samples)
    frequency = item["frequency_hz"]
    print(json.dumps({"device": device.get("display_name"), "rate_hz": rate,
                      "channel": channel, "stats": item}, ensure_ascii=False, indent=2))

    checks: list[tuple[str, bool, str]] = []
    checks.append(("有跳变", item["edges"] > 2, f"跳变 {item['edges']}"))
    if args.expect_hz:
        low, high = args.expect_hz * (1 - args.tolerance), args.expect_hz * (1 + args.tolerance)
        ok = frequency is not None and low <= frequency <= high
        checks.append(("频率在容差内（中位完整周期）", ok,
                       f"实测 {frequency} Hz，期望 {args.expect_hz} Hz（±{args.tolerance*100:g}%）；"
                       f"跨度法给 {item['frequency_from_span_hz']} Hz"))
    checks.append(("毛刺数量", item["glitch_count"] <= args.max_glitches,
                   f"{item['glitch_count']} 个（短于中位半周期一半；阈值 {args.max_glitches}）"))
    if item["edges"] >= 9:      # 半周期太少时稳定性没有统计意义，跳过而不是误判失败
        checks.append(("半周期稳定", item["half_period_stable_percent"] is not None
                       and item["half_period_stable_percent"] >= args.min_stable,
                       f"{item['half_period_stable_percent']}% 的半周期在中位值 ±0.1% 内"
                       f"（阈值 {args.min_stable}%）"))
    else:
        print(f"  [跳过] 半周期稳定：只有 {item['edges']} 个跳变（<9），样本太少无法统计；"
              f"窗口太短时请加大 --time")
    if args.expect_duty is not None:
        ok = abs(item["duty_percent"] - args.expect_duty) <= args.duty_tolerance
        checks.append(("占空比在容差内", ok,
                       f"实测 {item['duty_percent']}%，期望 {args.expect_duty}%（±{args.duty_tolerance}）"))
    failed = [name for name, ok, _ in checks if not ok]
    for name, ok, detail in checks:
        print(f"  [{'通过' if ok else '失败'}] {name}：{detail}")
    if failed:
        print(f"自检失败：{failed}")
        return 1
    print("自检通过")
    return 0

def cmd_info(args) -> int:
    cli = find_cli(args.cli)
    print(f"pxview-cli: {cli}")
    found = devices(cli, args.timeout)
    for item in found:
        kind = "演示" if item.get("is_demo") else ("硬件" if item.get("is_hardware") else "文件")
        print(f"  设备 id={item.get('id')} {kind} {item.get('display_name')} driver={item.get('driver_name')}")
    print(f"状态: {json.dumps(status(cli, args.timeout), ensure_ascii=False)}")
    return 0


# ======================================================================
#  6. 命令行
# ======================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PXLogic（PXView）采集后端；输出与 Kingst 同构的跳变 CSV。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--cli", help="pxview-cli.cmd 路径（默认自动查找/环境变量 PXVIEW_CLI）")
    parser.add_argument("--timeout", type=float, default=60.0, help="单条 pxview-cli 超时（秒），默认 60")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("info", help="设备与服务状态一览")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("devices", help="列出设备")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("status", help="打印当前采集状态")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("capture", help="采集并导出：位流 + 跳变 CSV + capture.json")
    p.add_argument("--device", help="设备 ID 或名称片段；默认第一台硬件设备")
    p.add_argument("--demo", action="store_true", help="用演示设备（无硬件自检用）")
    p.add_argument("--channels", default="0,1,2,3,4", help="通道，如 0,1,2,3,4 或 0-4")
    p.add_argument("--rate", default="500M", help="采样率，如 500M / 100M / 1000000")
    p.add_argument("--time", default="2ms", help="时长，如 2ms / 0.01 / 10s")
    p.add_argument("--samples", type=int, help="按样本数采集（与 --time 二选一，给了它就忽略 --time）")
    p.add_argument("--threshold", type=float, default=1.65, help="数字阈值电压（V）")
    p.add_argument("--trigger", help="触发，格式 通道:类型（当前 PXView CLI 不生效，见文件头说明）")
    p.add_argument("--out", required=True, help="跳变 CSV 输出路径")
    p.add_argument("--raw-dir", help="位流目录；默认 <out 同名>.pxlogic/<时间戳>/（每次采集一个新目录）")
    p.add_argument("--reuse-raw", action="store_true",
                   help="允许复用 --raw-dir 并先清掉里面的旧 channel_*.bin；不给就拒绝复用")
    p.add_argument("--force", action="store_true",
                   help="导出容量与请求样本数不符时仍继续（清单会记 count_mismatch，时间轴不可当已证实）")
    p.add_argument("--verify-hz", type=float,
                   help="用采集里频率已知的信号核对实际采样率，如 LA 自身 PWM0=1000 或目标 TRACECLK")
    p.add_argument("--verify-channel", type=int, default=0, help="参考信号所在通道，默认 0")
    p.add_argument("--verify-tolerance", type=float, default=0.01,
                   help="实测采样率与请求值的最大相对偏差，默认 1%%")
    p.add_argument("--with-csv", action="store_true", help="同时导出 PXView 的逐样本 CSV（每通道一个大文件）")
    p.add_argument("--stats", action="store_true", help="采集后打印通道统计")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("export", help="导出当前 buffer（不重新采集）")
    p.add_argument("--dir", required=True)
    p.add_argument("--channels", help="仅 csv 需要；不传则只导出 binary")
    p.add_argument("--format", choices=("binary", "csv", "both"), default="binary")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("csv", help="由 channel_N.bin 目录生成跳变 CSV")
    p.add_argument("dir")
    p.add_argument("--channels", default="0,1,2,3,4")
    p.add_argument("--rate", required=True)
    p.add_argument("--samples", type=int, help="真实样本数（默认按导出容量，可能带末字节补齐位）")
    p.add_argument("--out", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_csv)

    p = sub.add_parser("stats", help="统计已导出位流的每通道频率/占空比")
    p.add_argument("dir")
    p.add_argument("--channels", default="0,1,2,3,4")
    p.add_argument("--rate", required=True)
    p.add_argument("--samples", type=int, help="真实样本数（默认按导出容量）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("selftest", help="已知信号自检（默认 CH0 = LA 自身 PWM0，1 kHz 50%%）")
    p.add_argument("--device")
    p.add_argument("--demo", action="store_true")
    p.add_argument("--channel", default="0")
    p.add_argument("--rate", default="10M")
    p.add_argument("--time", default="20ms")
    p.add_argument("--threshold", type=float, default=1.5)
    p.add_argument("--expect-hz", type=float, default=1000.0)
    p.add_argument("--expect-duty", type=float, default=50.0)
    p.add_argument("--tolerance", type=float, default=0.02, help="频率容差比例，默认 2%%")
    p.add_argument("--duty-tolerance", type=float, default=2.0, help="占空比容差（百分点），默认 2")
    p.add_argument("--max-glitches", type=int, default=2,
                   help="允许的短半周期毛刺数，默认 2（本机 CH0 起点实测有 1 个）")
    p.add_argument("--min-stable", type=float, default=95.0,
                   help="半周期落在中位值 ±1 样本内的最低百分比，默认 95")
    p.add_argument("--dir", default="tmp/pxlogic-selftest")
    p.set_defaults(func=cmd_selftest)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
