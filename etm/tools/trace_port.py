#!/usr/bin/env python3
"""Kingst KVDAT / 跳变 CSV → 4 位 DDR trace 字节流候选。

物理取样分两条实现，语义完全一致：
  * 向量化路径（numpy/pandas）：整块解析 CSV，用 searchsorted 取样，实测 4 ns 滤波与
    跳变提取都是 C 级批量操作；
  * 纯 Python 路径（csv.reader + bisect）：numpy 缺失或 CSV 形状不支持批量处理时使用。
两条路径产出同一份 physical 报告与同一对相位字节流，切换不影响解码结果。

KVDAT 直接读取边沿位置与初始电平，CSV 时间列使用 round_trip。两种输入
共用同一套滤波/DDR 取样/相位代码；本模块不执行 ETM 指令解码。
"""
import argparse
import bisect
import csv
import hashlib
import json
import re
import statistics
from pathlib import Path

try:  # 可选加速：缺 numpy 时退回纯 Python 实现（结果相同，只是慢）
    import numpy as _np
except ImportError:  # pragma: no cover - 只在精简环境触发
    _np = None


def frontend_accel():
    """报告当前进程里向量化前端是否可用（numpy + pandas 都能 import 才算 vectorized）。

    便携包曾经出现"文件里带了 numpy、但冻结后 import 失败 → 静默回退纯 Python"，
    所以要有一个可查询的口径：`/api/status` 会带上它，便携包测试据此断言加速真的生效。
    """
    numpy_ok = _np is not None
    pandas_ok = False
    if numpy_ok:
        try:
            import pandas  # noqa: F401  (只探测可导入性)
            pandas_ok = True
        except Exception:
            pandas_ok = False
    return dict(numpy=bool(numpy_ok), pandas=bool(pandas_ok), vectorized=bool(numpy_ok and pandas_ok),kvdat=bool(numpy_ok))


def read_tracks(path):
    initial, tracks = {}, {}
    start = end = None
    with path.open('rb') as stream:
        header_bytes = stream.readline()
    try:
        if header_bytes.startswith((b'\xff\xfe',b'\xfe\xff')):encoding='utf-16'
        else:
            header_bytes.decode('utf-8-sig')
            encoding = 'utf-8-sig'
    except UnicodeDecodeError:
        # 中文 KingstVIS 使用 Windows 本地编码导出“通道 N”表头。
        header_bytes.decode('gb18030')
        encoding = 'gb18030'
    with path.open(newline='', encoding=encoding) as stream:
        reader = csv.reader(stream)
        header = next((r for r in reader if r), None)
        if not header:
            raise ValueError('CSV 为空。')
        channels = []
        for name in header[1:]:
            match = re.search(r'\d+', name)
            if not match:
                raise ValueError(f'不能识别通道表头：{name!r}')
            channels.append(int(match.group()))
        if len(set(channels)) != len(channels):
            raise ValueError('CSV 通道号重复。')
        levels = {}
        for row in reader:
            if not row or not row[0].strip():
                continue
            try:
                timestamp = float(row[0])
            except ValueError:
                raise ValueError(f'不是 Kingst 秒数时间列：{row[0]!r}') from None
            if end is not None and timestamp < end:
                raise ValueError('CSV 时间倒序。')
            if len(row) != len(header):
                raise ValueError('CSV 行的通道数与表头不一致。')
            if start is None:
                start = timestamp
            end = timestamp
            for channel, value in zip(channels, row[1:]):
                value = value.strip()
                if value not in ('0', '1'):
                    raise ValueError(f'CH{channel} 电平不是 0/1：{value!r}')
                level = int(value)
                if channel not in initial:
                    initial[channel] = level
                    tracks[channel] = []
                elif levels[channel] != level:
                    tracks[channel].append((timestamp, level))
                levels[channel] = level
    if start is None:
        raise ValueError('CSV 没有数据行。')
    return initial, tracks, start, end


def pack_nibbles(nibbles, phase):
    return bytes(nibbles[i] | (nibbles[i + 1] << 4)
                 for i in range(phase, len(nibbles) - 1, 2))


def filter_pulses(edges, limit_ns):
    """Remove only complete pulse pairs; preserve all surviving edge timestamps."""
    filtered=[]
    for edge in edges:
        filtered.append(edge)
        if len(filtered)>=2 and (filtered[-1][0]-filtered[-2][0])*1e9<=limit_ns+0.001:
            filtered.pop();filtered.pop()
    return filtered


class _NotVectorizable(Exception):
    """CSV 形状超出向量化路径的处理范围 → 退回逐行实现。"""


def _header_encoding(path):
    """识别 CSV 表头编码，并返回 (编码, 表头文本)。

    UTF-16 必须按文本模式读首行：二进制 `readline()` 只会读到 0x0a 半个换行，行会被截断。
    """
    with path.open('rb') as stream:
        prefix = stream.read(4)
    candidates = ['utf-16'] if prefix.startswith((b'\xff\xfe', b'\xfe\xff')) else ['utf-8-sig', 'gb18030']
    for encoding in candidates:
        try:
            with path.open(encoding=encoding, newline='') as stream:
                return encoding, stream.readline()
        except UnicodeDecodeError:
            continue
    raise ValueError('CSV 表头编码无法识别。')


def _filter_keep(times, limit_ns):
    """filter_pulses 的向量化等价实现：返回 keep 掩码。

    逐条实现是栈式规则：新边沿若与栈顶相距 ≤ 阈值，就成对消去（栈顶+新边沿都不保留），
    否则入栈。等价地，只有"连续过窄"的边沿段会互相配对；跨过一段正常间隔后，栈顶
    不可能再与后面的边沿配对。因此按 **close 的极大连续段** 独立处理每一段即可，
    段内用逐元素栈模拟（段长通常只有几个边沿），段外整段直通。

    🚨 2026-10-18 审查 F01：旧实现用 `top=[stop-2, stop-1]` 初始化，当首个过窄对出现在
    索引 0（stop=1）时会得到 [-1,0]，负下标指向数组末尾，把后续合法边沿也删掉
    （例：[0,2,10,22] ns、阈值 4，旧实现返回空，逐条实现保留 [10,22]）。
    """
    total = len(times)
    keep = _np.ones(total, dtype=bool)
    if total < 2 or not limit_ns:
        return keep
    limit = limit_ns + 0.001
    close = _np.diff(times) * 1e9 <= limit
    if not close.any():
        return keep
    positions = _np.flatnonzero(close)
    previous = _np.zeros_like(close)
    previous[1:] = close[:-1]
    following = _np.zeros_like(close)
    following[:-1] = close[1:]
    # close[i] 为真表示边沿 i 与 i+1 过窄。一段极大连续 close 覆盖边沿 [start, end]。
    starts = positions[~previous[positions]]
    ends = positions[~following[positions]] + 1
    for start, end in zip(starts.tolist(), ends.tolist()):
        stack = []
        for position in range(start, end + 1):
            if stack and (float(times[position]) - float(times[stack[-1]])) * 1e9 <= limit:
                keep[stack.pop()] = False
                keep[position] = False
            else:
                stack.append(position)
    return keep


def _extract_vectorized(path, clock, data, fraction, glitch_ns):
    """向量化 extract：报告字段与逐行实现逐字节一致，返回 (report, streams)。"""
    try:
        import pandas as pd
    except ImportError:  # 精简环境没装 pandas：退回逐行实现
        raise _NotVectorizable('pandas') from None

    encoding, header = _header_encoding(path)
    fields = [field.strip() for field in header.rstrip('\r\n').split(',')]
    if not fields or not fields[0]:
        raise _NotVectorizable('header')
    names = fields[1:]
    if not all(re.search(r'\d+', name) for name in names):
        raise _NotVectorizable('header')
    channels = [int(re.search(r'\d+', name).group()) for name in names]
    if len(set(channels)) != len(channels):
        raise _NotVectorizable('duplicate channel')

    # 时间列用 round_trip 精确解析：与历史实现的 Python float() 逐位一致
    # （pandas 默认解析器对个别小数会差 1 ULP）；通道按 int16 读入 —— 不能像早期实现那样
    # 读成 int8：256/257/-256 会被静默截成 0/1 再通过 0/1 校验（审查 F04）。
    # int16 覆盖常见越界值，且 100 M 行 × 4 通道只比 int8 多约 400 MB。
    dtypes = {index: _np.int16 for index in range(1, len(fields))}
    dtypes[0] = _np.float64
    try:
        frame = pd.read_csv(path, header=None, skiprows=1, dtype=dtypes,
                            encoding=encoding, engine='c', memory_map=True,
                            float_precision='round_trip')
    except (ValueError, OSError, UnicodeError):
        raise _NotVectorizable('parse') from None
    table = frame.to_numpy(copy=False)
    if table.ndim != 2 or table.shape[1] != len(fields) or table.shape[0] < 1:
        raise _NotVectorizable('shape')
    if not _np.isfinite(table).all():
        raise _NotVectorizable('non-finite')
    timestamps = table[:, 0]
    if _np.any(_np.diff(timestamps) < 0):
        raise ValueError('CSV 时间倒序。')
    start, end = float(timestamps[0]), float(timestamps[-1])

    initial, track_times, track_levels = {}, {}, {}
    for column, channel in enumerate(channels):
        values = table[:, column + 1]
        if not _np.all((values == 0) | (values == 1)):
            raise ValueError(f'CH{channel} 电平不是 0/1：{values[(_np.flatnonzero((values != 0) & (values != 1))[0])]!r}')
        initial[channel] = int(values[0])
        changed = _np.flatnonzero(_np.diff(values) != 0) + 1
        track_times[channel] = timestamps[changed]
        track_levels[channel] = values[changed].astype(_np.int8)

    return _extract_tracks(path, initial, track_times, track_levels, start, end,
                           clock, data, fraction, glitch_ns)


def _extract_tracks(path, initial, track_times, track_levels, start, end,
                    clock, data, fraction, glitch_ns):
    """Shared physical sampling; CSV and KVDAT use identical filtering/phases."""
    path = Path(path)
    needed = [clock] + data
    if len(set(needed)) != 5 or len(data) != 4:
        raise ValueError('需要五个不同通道：时钟 + D0/D1/D2/D3。')
    missing = set(needed) - set(initial)
    if missing:
        raise ValueError(f'CSV 缺少通道：{sorted(missing)}')
    if not isinstance(glitch_ns,(int,float)) or not 0<=glitch_ns<=100:
        raise ValueError('软件滤波宽度须为 0～100 ns（0 为关闭）')
    raw_clock_edges = len(track_times[clock])
    removed = {}
    if glitch_ns:
        raw_intervals = _np.diff(track_times[clock])
        if not len(raw_intervals) or glitch_ns >= float(_np.median(raw_intervals)) * 1e9 / 2:
            raise ValueError('软件滤波阈值须小于典型时钟半周期的一半，避免滤掉正常 Trace；请降低阈值')
        for channel in needed:
            keep = _filter_keep(track_times[channel], glitch_ns)
            removed[str(channel)] = int(len(keep) - int(keep.sum()))
            track_times[channel] = track_times[channel][keep]
            track_levels[channel] = track_levels[channel][keep]

    edges = track_times[clock]
    if len(edges) < 4:
        raise ValueError('TRACECLK 没有足够跳变。')
    intervals = _np.diff(edges)
    if _np.any(intervals <= 0):
        raise ValueError('同一时刻存在多个时钟跳变。')
    half_period = float(_np.median(intervals))
    periods = edges[2:] - edges[:-2]
    # Windowed full-cycle estimates tolerate 2 ns quantization. Keep changes
    # and clock gaps visible rather than treating a reset-to-PLL capture as one clock.
    clock_runs = []
    for offset in range(0, len(edges) - 2, 256):
        chunk = edges[offset:min(offset + 257, len(edges))]
        if len(chunk) < 3:
            continue
        duration = float(chunk[-1] - chunk[0])
        frequency = (len(chunk) - 1) / duration / 2
        if clock_runs and abs(frequency / clock_runs[-1]['clock_hz_average'] - 1) < .05:
            run = clock_runs[-1]
            run['end_seconds'] = float(chunk[-1])
            run['half_cycles'] += len(chunk) - 1
            run['clock_hz_average'] = run['half_cycles'] / (run['end_seconds'] - run['start_seconds']) / 2
        else:
            clock_runs.append(dict(start_seconds=float(chunk[0]), end_seconds=float(chunk[-1]),
                                   half_cycles=len(chunk) - 1, clock_hz_average=frequency))
    gap_limit = max(half_period * 8, 1e-6)
    gaps = [dict(edge=int(position), time_seconds=float(edges[position]),
                 duration_ns=float(intervals[position] * 1e9))
            for position in _np.flatnonzero(intervals > gap_limit)]

    first = edges[:-1]
    sample = first + (edges[1:] - first) * fraction
    nibbles = _np.zeros(len(sample), dtype=_np.uint8)
    min_margin = float('inf')
    for bit, channel in enumerate(data):
        times = track_times[channel]
        levels = track_levels[channel]
        if not len(times):
            # 整段没有跳变的常数通道：电平恒为初始值，也不贡献取样裕量
            nibbles |= initial[channel] << bit
            continue
        index = _np.searchsorted(times, sample, side='right')
        level = _np.where(index == 0, initial[channel], levels[_np.maximum(index - 1, 0)])
        nibbles |= level.astype(_np.uint8) << bit
        previous = _np.where(index > 0, sample - times[_np.maximum(index - 1, 0)], _np.inf)
        following = _np.where(index < len(times), times[_np.minimum(index, len(times) - 1)] - sample, _np.inf)
        margin = min(previous.min(), following.min())
        if margin < min_margin:
            min_margin = float(margin)

    streams = []
    packed = _np.ascontiguousarray(nibbles, dtype=_np.uint8)
    for phase in (0, 1):
        pairs = len(range(phase, len(packed) - 1, 2))
        low = packed[phase:phase + 2 * pairs:2]
        high = packed[phase + 1:phase + 1 + 2 * pairs:2]
        streams.append((low | (high << 4)).tobytes())

    sync = b'\xff\xff\xff\x7f'
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    report = {
        'input': str(path.resolve()),
        'input_sha256': digest.hexdigest(),
        'software_filter':dict(enabled=bool(glitch_ns),limit_ns=glitch_ns,
            removed_transitions=removed,raw_clock_edges=raw_clock_edges,
            basis='complete pulses <= limit; raw CSV and KVDAT remain unchanged'),
        'stage': 'physical DDR extraction only; no TPIU demux or ETM instruction decode',
        'clock_channel': clock, 'data_channels_D0_to_D3': data,
        'window_seconds': [start, end], 'clock_edges': len(edges),
        'clock_hz_median': 1.0 / float(_np.median(periods)),
        'clock_hz_average': (len(edges) - 1) / (edges[-1] - edges[0]) / 2,
        'half_period_ns': half_period * 1e9,
        'irregular_half_periods': int(_np.sum(_np.abs(intervals / half_period - 1) > 0.25)),
        'clock_runs':clock_runs,'clock_gaps':gaps,
        'data_transitions': {f'CH{ch}': len(track_times[ch]) for ch in data},
        'sample_fraction_after_each_edge': fraction,
        'min_data_transition_distance_ns': None if min_margin == float('inf') else min_margin * 1e9,
        'nibbles': len(nibbles),
        'candidates': [{'phase': i, 'bytes': len(buf), 'frame_sync_matches': buf.count(sync),
                        'first_32_bytes_hex': buf[:32].hex(' ')} for i, buf in enumerate(streams)],
        'note': 'Byte phase must be confirmed by TPIU framing and ETM decode; sync matches alone do not prove valid capture.',
    }
    return report, streams


def _extract_reference(path, clock, data, fraction=0.25, glitch_ns=0):
    """逐行实现（历史基线，向量化路径不可用时使用）。"""
    initial, tracks, start, end = read_tracks(path)
    needed = [clock] + data
    if len(set(needed)) != 5 or len(data) != 4:
        raise ValueError('需要五个不同通道：时钟 + D0/D1/D2/D3。')
    missing = set(needed) - set(initial)
    if missing:
        raise ValueError(f'CSV 缺少通道：{sorted(missing)}')
    if not isinstance(glitch_ns,(int,float)) or not 0<=glitch_ns<=100:
        raise ValueError('软件滤波宽度须为 0～100 ns（0 为关闭）')
    raw_clock_edges=len(tracks[clock]);removed={}
    if glitch_ns:
        raw_intervals=[b[0]-a[0] for a,b in zip(tracks[clock],tracks[clock][1:])]
        if not raw_intervals or glitch_ns>=statistics.median(raw_intervals)*1e9/2:
            raise ValueError('软件滤波阈值须小于典型时钟半周期的一半，避免滤掉正常 Trace；请降低阈值')
        for channel in needed:
            filtered=filter_pulses(tracks[channel],glitch_ns)
            removed[str(channel)]=len(tracks[channel])-len(filtered)
            tracks[channel]=filtered
    edges = tracks[clock]
    if len(edges) < 4:
        raise ValueError('TRACECLK 没有足够跳变。')
    intervals = [b[0] - a[0] for a, b in zip(edges, edges[1:])]
    if any(dt <= 0 for dt in intervals):
        raise ValueError('同一时刻存在多个时钟跳变。')
    half_period = statistics.median(intervals)
    periods = [edges[i + 2][0] - edges[i][0] for i in range(len(edges) - 2)]
    # Windowed full-cycle estimates tolerate 2 ns quantization. Keep changes
    # and clock gaps visible rather than treating a reset-to-PLL capture as one clock.
    clock_runs = []
    for offset in range(0, len(edges)-2, 256):
        chunk=edges[offset:min(offset+257,len(edges))]
        if len(chunk)<3:continue
        duration=chunk[-1][0]-chunk[0][0]
        frequency=(len(chunk)-1)/duration/2
        if clock_runs and abs(frequency/clock_runs[-1]['clock_hz_average']-1)<.05:
            run=clock_runs[-1];run['end_seconds']=chunk[-1][0];run['half_cycles']+=len(chunk)-1
            run['clock_hz_average']=run['half_cycles']/(run['end_seconds']-run['start_seconds'])/2
        else:clock_runs.append(dict(start_seconds=chunk[0][0],end_seconds=chunk[-1][0],
                                  half_cycles=len(chunk)-1,clock_hz_average=frequency))
    gaps=[dict(edge=i,time_seconds=edges[i][0],duration_ns=dt*1e9)
          for i,dt in enumerate(intervals) if dt>max(half_period*8,1e-6)]
    nibbles = []
    prepared = [(ch, [t for t, _ in tracks[ch]], [lv for _, lv in tracks[ch]])
                for ch in data]
    min_margin = float('inf')
    for a, b in zip(edges, edges[1:]):
        sample = a[0] + (b[0] - a[0]) * fraction
        value = 0
        for bit, (ch, times, levels) in enumerate(prepared):
            index = bisect.bisect_right(times, sample)
            level = initial[ch] if index == 0 else levels[index - 1]
            value |= level << bit
            if index:
                min_margin = min(min_margin, sample - times[index - 1])
            if index < len(times):
                min_margin = min(min_margin, times[index] - sample)
        nibbles.append(value)
    streams = [pack_nibbles(nibbles, phase) for phase in (0, 1)]
    sync = b'\xff\xff\xff\x7f'
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    report = {
        'input': str(path.resolve()),
        'input_sha256': digest.hexdigest(),
        'software_filter':dict(enabled=bool(glitch_ns),limit_ns=glitch_ns,
            removed_transitions=removed,raw_clock_edges=raw_clock_edges,
            basis='complete pulses <= limit; raw CSV and KVDAT remain unchanged'),
        'stage': 'physical DDR extraction only; no TPIU demux or ETM instruction decode',
        'clock_channel': clock, 'data_channels_D0_to_D3': data,
        'window_seconds': [start, end], 'clock_edges': len(edges),
        'clock_hz_median': 1.0 / statistics.median(periods),
        'clock_hz_average': (len(edges) - 1) / (edges[-1][0] - edges[0][0]) / 2,
        'half_period_ns': half_period * 1e9,
        'irregular_half_periods': sum(abs(dt / half_period - 1) > 0.25 for dt in intervals),
        'clock_runs':clock_runs,'clock_gaps':gaps,
        'data_transitions': {f'CH{ch}': len(tracks[ch]) for ch in data},
        'sample_fraction_after_each_edge': fraction,
        'min_data_transition_distance_ns': None if min_margin == float('inf') else min_margin * 1e9,
        'nibbles': len(nibbles),
        'candidates': [{'phase': i, 'bytes': len(buf), 'frame_sync_matches': buf.count(sync),
                        'first_32_bytes_hex': buf[:32].hex(' ')} for i, buf in enumerate(streams)],
        'note': 'Byte phase must be confirmed by TPIU framing and ETM decode; sync matches alone do not prove valid capture.',
    }
    return report, streams


def extract(path, clock, data, fraction=0.25, glitch_ns=0):
    if not 0 < fraction < 1:
        raise ValueError('取样位置必须介于 0 和 1 之间。')
    if Path(path).suffix.lower() == '.kvdat':
        if _np is None:
            raise ValueError('KVDAT 取样需要 numpy；请安装依赖或使用完整工作台包。')
        from kingst_record import read_tracks as read_kvdat
        info, initial, times, levels = read_kvdat(path)
        selected = [clock] + list(data)
        start = -info['trigger'] / info['rate']
        end = max([start] + [float(times[ch][-1]) for ch in selected if ch in times and len(times[ch])])
        physical, streams = _extract_tracks(path, initial, times, levels, start, end,
                                           clock, list(data), fraction, glitch_ns)
        physical['input_format'] = 'Kingst KVDAT'
        physical['sample_rate'] = info['rate']
        physical['sample_depth'] = info['depth']
        return physical, streams
    if _np is not None:
        try:
            return _extract_vectorized(path, clock, data, fraction, glitch_ns)
        except _NotVectorizable:
            pass
    return _extract_reference(path, clock, data, fraction, glitch_ns)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv', type=Path)
    parser.add_argument('--clock', type=int, default=0)
    parser.add_argument('--data', nargs=4, type=int, default=[1, 2, 3, 4], metavar='CH')
    parser.add_argument('--sample-fraction', type=float, default=0.25,
                        help='每个半周期内的取样位置，默认 0.25；实测 H7B0 中点为数据跳变区')
    parser.add_argument('--out', type=Path, help='输出路径前缀，默认 CSV 同名')
    args = parser.parse_args()
    if not 0 < args.sample_fraction < 1:
        parser.error('--sample-fraction 必须介于 0 和 1 之间。')
    try:
        report, streams = extract(args.csv, args.clock, args.data, args.sample_fraction)
    except (ValueError, OSError) as error:
        parser.exit(1, f'分析失败：{error}\n')
    prefix = args.out or args.csv.with_suffix('')
    prefix.parent.mkdir(parents=True, exist_ok=True)
    for i, stream in enumerate(streams):
        out = Path(str(prefix) + f'.phase{i}.bin')
        out.write_bytes(stream)
        report['candidates'][i]['file'] = str(out.resolve())
    out = Path(str(prefix) + '.trace.json')
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"TRACECLK 均值 = {report['clock_hz_average'] / 1e6:.6f} MHz，中位 = {report['clock_hz_median'] / 1e6:.4f} MHz；半周期 = {report['half_period_ns']:.1f} ns")
    print(f"时钟跳变 {report['clock_edges']}，异常半周期 {report['irregular_half_periods']}")
    print(f"数据跳变：{report['data_transitions']}")
    print(f"取样点至最近数据跳变的最小距离：{report['min_data_transition_distance_ns']} ns")
    margin = report['min_data_transition_distance_ns']
    if margin is not None and margin < 5:
        print('注意：取样点接近数据跳变，请调整 --sample-fraction 后重新核对。')
    for candidate in report['candidates']:
        print(f"phase{candidate['phase']}: {candidate['bytes']} bytes，FSYNC 候选 {candidate['frame_sync_matches']}")
    print(f'报告：{out}')
    print('当前只恢复物理字节流；实际执行流需要后续 TPIU + ETM 解码。')


if __name__ == '__main__':
    main()
