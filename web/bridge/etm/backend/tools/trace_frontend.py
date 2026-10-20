#!/usr/bin/env python3
"""有界分块原生物理前端（P5a 参考实现）。

链路：KVDAT/CSV →（按通道有界读取游标）→ 成对毛刺滤波 → 半周期取样（DDR nibble）
      → 字节相位打包 → TPIU 帧解复用 → ETM 字节流。

语义参照（只读复用，不修改 ``trace_port.py`` / ``tpiu_demux.py`` / ``kingst_record.py``）：

* ``trace_port.extract()``：取样位置、毛刺滤波、DDR nibble、physical 统计；
* ``tpiu_demux.demux()``：FSYNC/HSYNC/帧/未知源/尾部残片；
* ``decode_stream.decode_capture_stream()``：TPIU 字节相位的唯一性判定。

与参考实现的**唯一**差别是内存形状：参考实现为整段记录构造 5 条通道的 float64 时间数组、
全量取样点数组、全量 nibble 数组与全量区间数组；本模块把同样的计算折成有界批次，
跨块只保留 O(1) / O(chunk) 状态，统计量改用精确直方图（值 → 计数）而不是全量数组。
数值运算与参考实现逐条同序（同为 float64 的同样加减乘除），因此输出逐字节一致：

* 滤波判定 ``(后一边沿 - 前一边沿) * 1e9 <= limit_ns + 0.001``；
* 取样点 ``first + (second - first) * fraction``；
* 取样电平 ``searchsorted(times, sample, 'right')`` → 上一条跳变电平 / 初始电平；
* 中位数 ``np.median`` 的算术（偶数个取中间两个的 ``(a + b) / 2``）。

跨块携带的状态（块边界一律不丢，与方案 §7 的清单对应）：

* 每通道毛刺滤波的“待定边沿”：成对消去规则只差 1 个后续边沿即可判定，状态是 1 个边沿；
* 时钟通道：上一块末位时间、末两个时间（半周期 / 整周期）、``clock_runs`` 的 257 边沿窗口、
  区间与周期的精确直方图、gap 候选、滤波前后计数、非单调标志；
* 数据通道：已确认滤波跳变窗口 + searchsorted 基址、取样裕量游标、上批末位取样点、
  原始边沿未消费尾部；
* DDR nibble：相位 0 / 相位 1 各自的半字节进位（未成字节）与已产字节数；
* TPIU：FSYNC 搜索残片、帧内 partial frame、current trace id、未知源计数、尾部残片、
  两个候选相位各自的流与统计、已失败候选的失败原因；
* 会话复位：``reset()`` 一次性清空以上全部状态并倒回输入起点（不得把每块尾部当捕获边界）。

API::

    frontend = Frontend(source, config, chunk_bytes=1 << 20)
    while frontend.feed():      # 每次推进至多 chunk_bytes 的输入增量
        pass
    result = frontend.finish()  # {'phase', 'tpiu', 'streams', 'physical', 'framing', ...}
    frontend.physical()         # 与 trace_port.extract 的 physical 报告逐字段一致
    frontend.framing()          # 与 tpiu_demux.demux 的 stats + phase 逐字段一致
    frontend.tpiu()             # 选定相位的 TPIU 字节（= tpiu.bin）
    frontend.streams()          # {trace id: ETM 字节流}（= id<N>.bin）

内存边界：`chunk_bytes` 是每次读取的上界（`feed()` 每次至多读一块），取样批次另有
`batch_edges` 上界；候选相位字节各自 O(输出)（相位只能在整段末尾唯一确定，两个候选都
必须保留）。三个纯吞吐参数不改变任何数值：`accumulate_edges`（攒够多少原始边沿再走一遍
numpy）、`min_batch_edges`（已确认时钟边沿少于此值时先继续读）、`parse_min_bytes`
（KVDAT 游标攒够多少字节再解析）。本模块**不**宣称实时：它只是把参考语义搬到有界内存里，
原生加速属后续任务。
"""
import argparse
import codecs
import csv
import hashlib
import json
import re
import sys
import time
from pathlib import Path

try:  # 参考实现同样以 numpy 为硬依赖（KVDAT 取样需要它）
    import numpy as np
except ImportError as _numpy_error:  # pragma: no cover - 精简环境
    np = None
    _NP_ERROR = str(_numpy_error)
else:
    _NP_ERROR = None

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kingst_record  # noqa: E402  只读复用已验布局校验

DEFAULT_CHUNK_BYTES = 1 << 20
DEFAULT_BATCH_EDGES = 1 << 16
DEFAULT_MAX_DISTINCT = 1 << 20
MAX_ROUNDS = 1 << 20
FSYNC = b'\xff\xff\xff\x7f'
HSYNC = b'\xff\x7f'
KVDAT_EDGE_BYTES = 5
GAP_CANDIDATE_SECONDS = 1e-6
# 极小分块（1/3/7 字节）时先攒够这么多原始边沿再走一遍 numpy 处理，避免每条边沿都付
# 一次数组运算的固定开销；只影响吞吐，不改变任何数值（滤波/取样都是逐条同序的）。
RAW_ACCUMULATE_EDGES = 1 << 12
# 取样批次下限：已确认时钟边沿太少时先继续读，攒够再成批取样（同样只影响吞吐）。
MIN_BATCH_EDGES = 1 << 10
# KVDAT 游标攒到这么多字节才做一次 numpy 解析（极小分块时避免每条记录一次数组运算）。
KVDAT_PARSE_MIN_BYTES = 1 << 10
_EMPTY_TIMES = np.empty(0, dtype=np.float64) if np is not None else None
_EMPTY_LEVELS = np.empty(0, dtype=np.uint8) if np is not None else None
NOTE = ('Byte phase must be confirmed by TPIU framing and ETM decode; '
        'sync matches alone do not prove valid capture.')
FILTER_BASIS = 'complete pulses <= limit; raw CSV and KVDAT remain unchanged'


class FrontendError(ValueError):
    """前端明确拒绝的输入或调用（不猜、不静默降级）。"""


class PhaseSelectionError(FrontendError):
    """整段数据无法得到唯一有效的 TPIU 字节相位。"""

    def __init__(self, message, failures, valid_count):
        super().__init__(message)
        self.failures = failures
        self.valid_count = valid_count


def select_phase(demuxers, traceid):
    """整段唯一有效候选判定（与 ``decode_stream.decode_capture_stream`` 完全同语义）。

    "有效" = 解复用不报错、帧数 > 0、且期望的 trace id 出现过。两个候选都有效或都无效
    都必须明确失败，绝不凭短前缀猜相位。返回 ``(phase, framing, streams, failures)``，
    ``failures`` 是落选相位的失败原因（诊断用，成功时也可能非空）。
    """
    valid = []
    failures = []
    for phase, demuxer in enumerate(demuxers):
        try:
            framing, streams = demuxer.finish()
        except FrontendError as error:
            failures.append({'phase': phase, 'error': str(error)})
            continue
        if traceid in streams and framing['frames']:
            valid.append((phase, framing, streams))
    if len(valid) != 1:
        message = f'不能唯一确定 TPIU 字节相位：{len(valid)} 个候选；请检查通道和取样位置。'
        if failures:
            message += ' ' + failures[-1]['error']
        raise PhaseSelectionError(message, failures, len(valid))
    phase, framing, streams = valid[0]
    return phase, {**framing, 'phase': phase}, streams, failures


def _require_numpy():
    if np is None:  # pragma: no cover - 精简环境
        raise FrontendError(f'物理前端需要 numpy：{_NP_ERROR}')


# ── 精确统计：值 → 计数直方图（不保存全量区间数组） ────────────────────────────────


class IntervalHistogram:
    """区间的精确多重集合 + ``np.median`` 等价中位数。

    参考实现把整段区间放进 numpy 数组再取中位数；这里只保存“值 → 计数”，
    内存是 O(不同取值)。实测 200 ms 记录 12,000,306 个半周期只有 73 个不同取值
    （采样位置是整数 tick，量化后区间值几乎不重复）。超出 ``max_distinct`` 时明确失败，
    不静默退化成无界数组。
    """

    __slots__ = ('counts', 'total', 'max_distinct')

    def __init__(self, max_distinct=DEFAULT_MAX_DISTINCT):
        self.counts = {}
        self.total = 0
        self.max_distinct = int(max_distinct)

    def add(self, values):
        if not len(values):
            return
        unique, counts = np.unique(values, return_counts=True)
        table = self.counts
        for value, count in zip(unique.tolist(), counts.tolist()):
            table[value] = table.get(value, 0) + count
            self.total += count
        if len(table) > self.max_distinct:
            raise FrontendError(
                f'区间取值离散度 {len(table)} 超过有界直方图上限 {self.max_distinct}；'
                '请提高 max_distinct_intervals 或改用 trace_port 参考实现')

    def median(self):
        """复现 ``float(np.median(array))`` 的算术（偶数个取中间两个的均值）。"""
        if not self.total:
            raise FrontendError('没有可用于统计中位数的区间')
        middle = self.total // 2
        odd = self.total % 2
        seen = 0
        lower = None
        for value, count in sorted(self.counts.items()):
            if odd:
                if seen + count > middle:
                    return value
            else:
                if lower is None and seen + count > middle - 1:
                    lower = value
                if seen + count > middle:
                    return ((lower if lower is not None else value) + value) / 2
            seen += count
        raise FrontendError('区间直方图计数不完整')  # pragma: no cover


# ── 成对毛刺滤波（跨块只带 1 个待定边沿） ───────────────────────────────────────


class GlitchFilter:
    """``trace_port.filter_pulses`` / ``_filter_keep`` 的分块等价实现。

    规则：新边沿与上一个**保留**边沿相距 ``<= limit`` 时成对消去，否则保留。
    参考实现的栈式规则里，一次消去总是“最新两个保留边沿”；弹出后与新边沿比较的
    更早保留边沿到新边沿的距离必然大于刚被弹出那一对（否则那一对不会被弹出），
    所以与“只和上一个保留边沿比较”等价。状态因此只有 1 个待定边沿：

    * 待定边沿后的新边沿若与它过窄 → 两者一起丢弃；
    * 否则待定边沿转为确认输出，新边沿成为待定边沿。
    """

    __slots__ = ('limit', 'enabled', 'pending_time', 'pending_level', 'raw_count', 'emitted')

    def __init__(self, limit_ns=0, enabled=True):
        self.limit = float(limit_ns) + 0.001
        self.enabled = bool(enabled) and bool(limit_ns)
        self.pending_time = None
        self.pending_level = 0
        self.raw_count = 0
        self.emitted = 0

    def removed_count(self):
        """被成对消去的原始边沿数（= 参考实现 ``len(keep) - keep.sum()``）。"""
        return self.raw_count - self.emitted - (1 if self.pending_time is not None else 0)

    def push_batch(self, times, levels=None):
        """推入一批原始边沿，返回已确认的保留边沿 ``(times, levels)``。"""
        count = len(times)
        if not count:
            return np.empty(0, dtype=np.float64), np.empty(0, dtype=np.uint8)
        self.raw_count += count
        if not self.enabled:
            self.emitted += count
            return times, (levels if levels is not None else np.zeros(count, dtype=np.uint8))
        if self.pending_time is not None:
            times = np.concatenate(([self.pending_time], times))
            if levels is not None:
                levels = np.concatenate(([self.pending_level], levels))
        close = np.diff(times) * 1e9 <= self.limit
        keep = None
        if close.any():
            keep = np.ones(len(times), dtype=bool)
            positions = np.flatnonzero(close)
            previous = np.zeros_like(close)
            previous[1:] = close[:-1]
            following = np.zeros_like(close)
            following[:-1] = close[1:]
            starts = positions[~previous[positions]]
            ends = positions[~following[positions]]
            for start, end in zip(starts.tolist(), ends.tolist()):
                keep[start:end + 2] = False
                if (end - start + 2) % 2:      # 一段极大过窄段里奇数个边沿 → 末位保留
                    keep[end + 1] = True
        survivors = times if keep is None else times[keep]
        survivor_levels = (levels if keep is None else levels[keep]) if levels is not None else None
        if keep is None or keep[-1]:
            # 末位保留但尚未判定（可能被下一块的第一个边沿成对消去）→ 留作待定边沿
            self.pending_time = float(survivors[-1])
            self.pending_level = int(survivor_levels[-1]) if survivor_levels is not None else 0
            survivors = survivors[:-1]
            survivor_levels = survivor_levels[:-1] if survivor_levels is not None else None
        else:
            self.pending_time = None
        self.emitted += len(survivors)
        return survivors, (survivor_levels if survivor_levels is not None
                           else np.zeros(len(survivors), dtype=np.uint8))

    def flush(self):
        """输入结束：待定边沿按参考实现保留（未成对即保留）。"""
        if self.pending_time is None:
            return None
        edge = (self.pending_time, self.pending_level)
        self.pending_time = None
        self.emitted += 1
        return edge


# ── 时钟通道统计（跨块累积） ───────────────────────────────────────────────────


class ClockStatistics:
    """滤波后时钟边沿的跨块统计：区间/周期直方图、clock_runs、gaps、不规则计数。"""

    __slots__ = ('clock', 'min_edges', 'intervals', 'periods', 'raw_intervals',
                 'edge_count', 'raw_edge_count', 'first_time', 'last_time',
                 '_filtered_tail', '_raw_tail', '_window', 'next_offset',
                 'gap_candidates', 'non_increasing', 'runs')

    def __init__(self, clock, min_edges=4, max_distinct=DEFAULT_MAX_DISTINCT):
        self.clock = clock
        self.min_edges = min_edges
        self.intervals = IntervalHistogram(max_distinct)
        self.periods = IntervalHistogram(max_distinct)
        self.raw_intervals = IntervalHistogram(max_distinct)
        self.edge_count = 0
        self.raw_edge_count = 0
        self.first_time = None
        self.last_time = None
        self._filtered_tail = ()
        self._raw_tail = None
        self._window = np.empty(0, dtype=np.float64)
        self.next_offset = 0
        self.gap_candidates = []
        self.non_increasing = False
        self.runs = []

    def add_raw(self, times):
        """原始时钟边沿（毛刺滤波前）：只用于 raw_clock_edges 与滤波阈值校验。"""
        if not len(times):
            return
        self.raw_edge_count += len(times)
        extended = (np.concatenate(([self._raw_tail], times))
                    if self._raw_tail is not None else times)
        intervals = np.diff(extended)
        if len(intervals):
            self.raw_intervals.add(intervals)
        self._raw_tail = float(times[-1])

    def add(self, times):
        """滤波后时钟边沿（已确认保留）。"""
        count = len(times)
        if not count:
            return
        base = self.edge_count
        if self.first_time is None:
            self.first_time = float(times[0])
        tail = np.asarray(self._filtered_tail, dtype=np.float64)
        extended = np.concatenate((tail, times)) if len(tail) else times
        intervals = np.diff(extended)
        if len(intervals):
            if not self.non_increasing and np.any(intervals <= 0):
                self.non_increasing = True
            self.intervals.add(intervals)
            left = extended[:-1]
            for position in np.flatnonzero(intervals > GAP_CANDIDATE_SECONDS).tolist():
                self.gap_candidates.append((base + position, float(left[position]),
                                            float(intervals[position])))
        if len(extended) >= 3:
            self.periods.add(extended[2:] - extended[:-2])
        self.edge_count = base + count
        self.last_time = float(times[-1])
        merged = np.concatenate((tail, times)) if len(tail) else times
        self._filtered_tail = tuple(merged[-2:].tolist())
        self._window = (np.concatenate((self._window, times)) if len(self._window) else times)
        while len(self._window) >= 257:
            self._run_window(self._window[:257])
            self._window = self._window[256:]
            self.next_offset += 256

    def _run_window(self, chunk):
        duration = float(chunk[-1] - chunk[0])
        frequency = (len(chunk) - 1) / duration / 2
        if self.runs and abs(frequency / self.runs[-1]['clock_hz_average'] - 1) < .05:
            run = self.runs[-1]
            run['end_seconds'] = float(chunk[-1])
            run['half_cycles'] += len(chunk) - 1
            run['clock_hz_average'] = (run['half_cycles']
                                       / (run['end_seconds'] - run['start_seconds']) / 2)
        else:
            self.runs.append(dict(start_seconds=float(chunk[0]), end_seconds=float(chunk[-1]),
                                  half_cycles=len(chunk) - 1, clock_hz_average=frequency))

    def filter_limit_check(self, glitch_ns):
        """复现参考实现“滤波阈值须小于典型时钟半周期的一半”的前置校验。"""
        if not glitch_ns:
            return
        if not self.raw_intervals.total:
            raise FrontendError(
                '软件滤波阈值须小于典型时钟半周期的一半，避免滤掉正常 Trace；请降低阈值')
        if glitch_ns >= self.raw_intervals.median() * 1e9 / 2:
            raise FrontendError(
                '软件滤波阈值须小于典型时钟半周期的一半，避免滤掉正常 Trace；请降低阈值')

    def finish(self):
        """收尾：处理最后一个未满 257 的 clock_runs 窗口并给出统计字段。"""
        if self.edge_count < self.min_edges:
            raise FrontendError('TRACECLK 没有足够跳变。')
        if self.non_increasing:
            raise FrontendError('同一时刻存在多个时钟跳变。')
        if self.next_offset < self.edge_count - 2 and len(self._window) >= 3:
            self._run_window(self._window[:min(257, len(self._window))])
        half_period = self.intervals.median()
        gap_limit = max(half_period * 8, 1e-6)
        gaps = [dict(edge=int(index), time_seconds=time_seconds,
                     duration_ns=float(duration * 1e9))
                for index, time_seconds, duration in self.gap_candidates
                if duration > gap_limit]
        irregular = sum(count for value, count in self.intervals.counts.items()
                        if abs(value / half_period - 1) > 0.25)
        average = (self.edge_count - 1) / (self.last_time - self.first_time) / 2
        return dict(half_period=half_period, gaps=gaps, irregular=int(irregular),
                    average=average, median=1.0 / float(self.periods.median()),
                    runs=self.runs)


# ── 数据通道（跨块取样状态） ───────────────────────────────────────────────────


class DataChannel:
    """单条数据通道的跨块取样状态。

    * ``raw_*``：已读入但尚未推入滤波器的原始跳变（有界，来自游标分块）；
    * ``win_*``：已确认的滤波后跳变窗口（含 searchsorted 基址），只留取样/裕量还需要的；
    * ``margin_cursor``：``min_data_transition_distance_ns`` 处理到哪一条跳变；
    * ``prev_sample``：上一取样批的最后一个取样点，用于跨块“相邻样本”判定。
    """

    __slots__ = ('channel', 'initial', 'filter', 'raw_times', 'raw_levels', 'raw_index',
                 'win_times', 'win_levels', 'win_base', 'filtered_count', 'raw_time_last',
                 'margin_cursor', 'prev_sample', 'last_sample', 'margin_min', 'source_done',
                 'sampling_done', 'tail_time', 'ended', 'pending_times', 'pending_levels',
                 'pending_count', 'accumulate_edges')

    def __init__(self, channel, initial, glitch_ns, accumulate_edges=RAW_ACCUMULATE_EDGES):
        self.channel = channel
        self.initial = int(initial)
        self.accumulate_edges = max(1, int(accumulate_edges))
        self.filter = GlitchFilter(glitch_ns, enabled=bool(glitch_ns))
        self.raw_times = np.empty(0, dtype=np.float64)
        self.raw_levels = np.empty(0, dtype=np.uint8)
        self.raw_index = 0
        self.win_times = np.empty(0, dtype=np.float64)
        self.win_levels = np.empty(0, dtype=np.uint8)
        self.win_base = 0
        self.filtered_count = 0
        self.raw_time_last = None
        self.margin_cursor = 0
        self.prev_sample = None
        self.last_sample = None
        self.margin_min = None
        self.source_done = False
        self.sampling_done = False
        self.tail_time = None
        self.ended = False
        self.pending_times = []
        self.pending_levels = []
        self.pending_count = 0

    # — 原始边沿 —
    def offer(self, times, levels):
        """收下新读入的原始跳变；极小分块时先攒着，避免每条都走一遍 numpy 合并。"""
        if not len(times):
            return
        self.pending_times.append(times)
        if levels is not None:
            self.pending_levels.append(levels)
        self.pending_count += len(times)
        self.raw_time_last = float(times[-1])
        if self.pending_count >= self.accumulate_edges:
            self.merge_raw()

    def merge_raw(self):
        """把攒下的原始跳变合并进 ``raw_times``/``raw_levels``（保留未消费尾部）。"""
        if not self.pending_count:
            return
        tail = self.raw_times[self.raw_index:]
        new_times = (np.concatenate(self.pending_times) if len(self.pending_times) > 1
                     else self.pending_times[0])
        self.raw_times = np.concatenate((tail, new_times)) if len(tail) else new_times
        if self.pending_levels:
            tail_levels = self.raw_levels[self.raw_index:] if len(self.raw_levels) else None
            new_levels = (np.concatenate(self.pending_levels) if len(self.pending_levels) > 1
                          else self.pending_levels[0])
            self.raw_levels = (np.concatenate((tail_levels, new_levels))
                               if tail_levels is not None and len(tail_levels) else new_levels)
        self.pending_times = []
        self.pending_levels = []
        self.pending_count = 0
        self.raw_index = 0

    def raw_pending(self):
        if self.pending_count:
            self.merge_raw()
        return len(self.raw_times) - self.raw_index

    def push_until(self, sample_max):
        """把原始跳变推进滤波器，直到“待定边沿”晚于 ``sample_max``（或原始数据用完）。"""
        if self.pending_count:
            self.merge_raw()
        remaining = self.raw_times[self.raw_index:]
        if not len(remaining):
            return
        cut = int(np.searchsorted(remaining, sample_max, side='right'))
        if self.filter.enabled:
            # 多推 1 条：让最后一条 <= sample_max 的边沿完成“是否成对消去”的判定
            stop = cut + 1 if cut < len(remaining) else cut
        else:
            stop = cut
        if stop <= 0:
            return
        times = remaining[:stop]
        levels = (self.raw_levels[self.raw_index:self.raw_index + stop]
                  if len(self.raw_levels) else None)
        self.raw_index += stop
        survivors, survivor_levels = self.filter.push_batch(times, levels)
        if len(survivors):
            self._append_window(survivors, survivor_levels)

    def push_all(self):
        """把剩余原始跳变全部推进滤波器（不再取样时只要计数与首条时刻）。"""
        if self.pending_count:
            self.merge_raw()
        times = self.raw_times[self.raw_index:]
        if not len(times):
            return
        levels = self.raw_levels[self.raw_index:] if len(self.raw_levels) else None
        self.raw_index = len(self.raw_times)
        survivors, survivor_levels = self.filter.push_batch(times, levels)
        if len(survivors):
            self._append_window(survivors, survivor_levels)

    def _append_window(self, times, levels):
        self.filtered_count += len(times)
        if self.sampling_done:
            # 取样已结束：只需要“最后一个取样点之后的首条跳变”来收尾裕量
            if self.tail_time is None:
                self.tail_time = float(times[0])
            return
        self.win_times = (np.concatenate((self.win_times, times))
                          if len(self.win_times) else times)
        self.win_levels = (np.concatenate((self.win_levels, levels))
                           if len(self.win_levels) else levels)

    def flush(self):
        """输入结束：冲出待定边沿（参考实现里未成对的边沿保留）。"""
        edge = self.filter.flush()
        if edge is None:
            return
        if self.sampling_done:
            self.filtered_count += 1
            if self.tail_time is None:
                self.tail_time = float(edge[0])
            return
        self._append_window(np.asarray([edge[0]], dtype=np.float64),
                            np.asarray([edge[1]], dtype=np.uint8))

    def covers(self, sample_max):
        """是否已能给出 ``<= sample_max`` 的全部滤波后跳变。

        三个条件缺一不可：缓冲里没有 ``<= sample_max`` 的待推边沿；待定边沿已晚于
        ``sample_max``（否则它是否被成对消去还未知）；缓冲已空时必须已读到通道末尾
        （否则后面还可能出现 ``<= sample_max`` 的跳变）。
        """
        if self.sampling_done:
            return True
        if self.raw_pending() and float(self.raw_times[self.raw_index]) <= sample_max:
            return False
        if (self.filter.enabled and self.filter.pending_time is not None
                and self.filter.pending_time <= sample_max):
            return False
        if self.raw_pending():
            return True
        return self.source_done

    # — 取样 —
    def sample(self, samples, prev_sample):
        """返回本批取样点的数据电平（uint8 数组）；同时更新裕量与窗口裁剪。"""
        self.last_sample = float(samples[-1])
        times = self.win_times
        if not len(times):
            self.prev_sample = self.last_sample
            return np.full(len(samples), self.initial, dtype=np.uint8)
        index = np.searchsorted(times, samples, side='right') + self.win_base
        local = index - 1 - self.win_base
        levels = np.where(index == 0, self.initial,
                          self.win_levels[np.maximum(local, 0)]).astype(np.uint8)
        self._margin(samples, prev_sample)
        self._trim(int(index.max()))
        self.prev_sample = self.last_sample
        return levels

    def _margin(self, samples, prev_sample):
        """跨块精确的取样裕量：只统计“跳变两侧的相邻取样点”。

        参考实现整段计算 ``min(previous.min(), following.min())``：对每个取样点 s，
        ``previous`` 是 s 到“不晚于 s 的最后一条跳变”的距离，``following`` 是 s 到
        “晚于 s 的第一条跳变”的距离。等价改写为按跳变 t 统计：

        * ``following`` 的最小值出现在“严格早于 t 的最后一个取样点” → ``t - before``；
        * ``previous`` 的最小值出现在“不早于 t 的第一个取样点” → ``after - t``。

        因此每条跳变只需看它两侧的相邻取样点，既不必保存全量取样点数组，
        每条跳变也只处理一次。
        """
        if not len(samples) or not len(self.win_times):
            return
        times = self.win_times
        # 只处理“不早于 t 的取样点已经存在”的跳变（t <= 最后取样点）
        upto = int(np.searchsorted(times, self.last_sample, side='right'))
        start = self.margin_cursor - self.win_base
        if start < 0:  # pragma: no cover - 窗口裁剪保证不会发生
            start = 0
        if upto <= start:
            return
        selected = times[start:upto]
        left = np.searchsorted(samples, selected, side='left')
        after = samples[np.minimum(left, len(samples) - 1)]
        before = np.where(left > 0, samples[np.maximum(left - 1, 0)], np.nan)
        if prev_sample is not None:
            before = np.where(left > 0, before, float(prev_sample))
        best = float((after - selected).min())
        distance = selected - before
        valid = ~np.isnan(before)
        if valid.any():
            best = min(best, float(distance[valid].min()))
        if self.margin_min is None or best < self.margin_min:
            self.margin_min = best
        self.margin_cursor = self.win_base + upto

    def finish_margin(self):
        """收尾裕量：最后一个取样点之后（没有更晚的取样点）的跳变只贡献 ``following``。"""
        reference = self.last_sample
        if reference is None:
            return
        best = None
        start = self.margin_cursor - self.win_base
        if 0 <= start < len(self.win_times):
            best = float(self.win_times[start] - reference)
        if self.tail_time is not None:
            value = float(self.tail_time - reference)
            best = value if best is None else min(best, value)
        if best is None:
            return
        if best < 0:  # pragma: no cover - 顺序保证不会发生
            best = 0.0
        if self.margin_min is None or best < self.margin_min:
            self.margin_min = best

    def _trim(self, last_index):
        """裁掉取样与裕量都不再需要的旧跳变（保留跨块必需的 1 条）。"""
        keep_from = max(0, min(self.margin_cursor, last_index - 1))
        drop = keep_from - self.win_base
        if drop > 0:
            self.win_times = self.win_times[drop:]
            self.win_levels = self.win_levels[drop:]
            self.win_base += drop


# ── KVDAT / CSV 有界读取游标 ──────────────────────────────────────────────────


class _KvdatCursor:
    """单通道边沿记录的有界读取游标（5 字节/条，末条为范围哨兵）。"""

    __slots__ = ('name', 'stream', 'remaining', 'buffer', 'records_left', 'depth', 'rate',
                 'trigger', 'initial', 'last_position', 'last_time', 'edge_index',
                 'parse_min_bytes')

    def __init__(self, path, block, depth, rate, trigger, name, parse_min_bytes=KVDAT_PARSE_MIN_BYTES):
        self.name = name
        self.stream = path.open('rb')
        self.stream.seek(block['offset'])
        self.remaining = block['count'] * KVDAT_EDGE_BYTES
        self.buffer = bytearray()
        self.records_left = block['count']   # 含末条哨兵
        self.depth = depth
        self.rate = rate
        self.trigger = trigger
        self.initial = block['initial']
        self.last_position = None
        self.last_time = None
        self.edge_index = 0
        self.parse_min_bytes = max(KVDAT_EDGE_BYTES, int(parse_min_bytes))

    def close(self):
        try:
            self.stream.close()
        except Exception:  # pragma: no cover
            pass

    def done(self):
        return self.remaining <= 0 and not self.buffer

    def read(self, chunk_bytes):
        """读入至多 ``chunk_bytes`` 字节，返回新增的 (times, levels)；None=读完。"""
        if self.remaining <= 0 and not self.buffer:
            return None
        want = min(int(chunk_bytes), self.remaining)
        data = self.stream.read(want)
        if len(data) != want:
            raise FrontendError('KVDAT 读取时发生截断')
        self.remaining -= len(data)
        self.buffer.extend(data)
        if len(self.buffer) < self.parse_min_bytes and self.remaining > 0:
            # 极小分块（1/3/7 字节）下先攒够记录再解析：只影响吞吐，不改变结果
            return _EMPTY_TIMES, _EMPTY_LEVELS
        usable = (len(self.buffer) // KVDAT_EDGE_BYTES) * KVDAT_EDGE_BYTES
        if not usable:
            return _EMPTY_TIMES, _EMPTY_LEVELS
        raw = bytes(self.buffer[:usable])
        del self.buffer[:usable]
        rows = np.frombuffer(raw, dtype=np.dtype([('position', '<u4'), ('flag', 'u1')]))
        positions = rows['position'].astype(np.uint32)
        flags = rows['flag']
        count = len(positions)
        if count > self.records_left:
            raise FrontendError('KVDAT 边沿位置或标记无效')
        self.records_left -= count
        if np.any(flags) or np.any(positions > self.depth):
            raise FrontendError('KVDAT 边沿位置或标记无效')
        if self.last_position is not None and positions[0] <= self.last_position:
            raise FrontendError('KVDAT 边沿位置或标记无效')
        if count > 1 and np.any(positions[1:] <= positions[:-1]):
            raise FrontendError('KVDAT 边沿位置或标记无效')
        if self.records_left == 0:
            # 末条是范围哨兵：不进入取样，只确认结束标记（layout 已校验，这里再核一次）
            last = count - 1
            if int(positions[last]) != self.depth or int(flags[last]) != 0:
                raise FrontendError('KVDAT 结束标记无效')
            count = last
        if count <= 0:
            return _EMPTY_TIMES, _EMPTY_LEVELS
        positions = positions[:count]
        times = (positions.astype(np.float64) - self.trigger) / self.rate
        order = np.arange(self.edge_index, self.edge_index + count, dtype=np.uint32) + 1
        levels = (self.initial ^ (order & 1)).astype(np.uint8)
        self.edge_index += count
        self.last_position = int(positions[-1])
        self.last_time = float(times[-1])
        return times, levels


class _KvdatSource:
    """KVDAT 容器：布局校验一次（有界 256 KiB），边沿数据按通道有界读取。"""

    def __init__(self, path, config, parse_min_bytes=KVDAT_PARSE_MIN_BYTES):
        self.path = Path(path)
        self.info = kingst_record.layout(self.path)
        self.depth = self.info['depth']
        self.rate = self.info['rate']
        self.trigger = self.info['trigger']
        self.channels = self.info['channels']
        self.selected = [config['clock']] + list(config['data'])
        self.initial = {channel: self.channels[channel]['initial']
                        for channel in self.selected if channel in self.channels}
        self._cursors = {channel: _KvdatCursor(self.path, self.channels[channel], self.depth,
                                               self.rate, self.trigger, channel,
                                               parse_min_bytes)
                         for channel in self.selected if channel in self.channels}

    def start_seconds(self):
        return -self.trigger / self.rate

    def window(self):
        end = self.start_seconds()
        for channel in self.selected:
            cursor = self._cursors.get(channel)
            if cursor is not None and cursor.last_time is not None:
                end = max(end, cursor.last_time)
        return [self.start_seconds(), end]

    def read(self, channel, chunk_bytes):
        cursor = self._cursors[channel]
        before = cursor.remaining
        result = cursor.read(chunk_bytes)
        consumed = before - cursor.remaining
        if result is None:
            return None
        if not len(result[0]):
            return { }, consumed
        return {channel: result}, consumed

    def done(self, channel):
        return self._cursors[channel].done()

    def missing_channels(self, selected):
        return [channel for channel in selected if channel not in self.channels]

    def close(self):
        for cursor in self._cursors.values():
            cursor.close()

    def extra_report(self):
        return {'input_format': 'Kingst KVDAT', 'sample_rate': self.rate,
                'sample_depth': self.depth}


class _CsvSource:
    """CSV：字节分块 + 增量解码 + 行缓冲；所有通道共享同一条行流。"""

    __slots__ = ('path', 'selected', 'encoding', 'stream', 'decoder', 'pending', 'lines',
                 'fields', 'channels', 'initial', 'levels', 'start', 'end', 'rows',
                 '_buffer', '_eof', 'bytes_read', '_header_pending')

    def __init__(self, path, config):
        self.path = Path(path)
        self.selected = [config['clock']] + list(config['data'])
        self.encoding = self._detect_encoding()
        self.stream = self.path.open('rb')
        self.decoder = codecs.getincrementaldecoder(self.encoding)(errors='strict')
        self.pending = ''
        self.lines = []
        self.fields = []
        self.channels = []
        self.initial = {}
        self.levels = {}
        self.start = None
        self.end = None
        self.rows = 0
        self._buffer = {}
        self._eof = False
        self.bytes_read = 0
        self._header_pending = True
        self._parse_header()

    def _detect_encoding(self):
        with self.path.open('rb') as stream:
            prefix = stream.read(4)
        candidates = (['utf-16'] if prefix.startswith((b'\xff\xfe', b'\xfe\xff'))
                      else ['utf-8-sig', 'gb18030'])
        for encoding in candidates:
            try:
                with self.path.open(encoding=encoding, newline='') as stream:
                    stream.readline()
                return encoding
            except UnicodeDecodeError:
                continue
        raise FrontendError('CSV 表头编码无法识别。')

    def _parse_header(self):
        with self.path.open(encoding=self.encoding, newline='') as stream:
            reader = csv.reader(stream)
            header = next((row for row in reader if row), None)
        if not header:
            raise FrontendError('CSV 为空。')
        self.fields = [field.strip() for field in header[1:]]
        channels = []
        for name in self.fields:
            match = re.search(r'\d+', name)
            if not match:
                raise FrontendError(f'不能识别通道表头：{name!r}')
            channels.append(int(match.group()))
        if len(set(channels)) != len(channels):
            raise FrontendError('CSV 通道号重复。')
        self.channels = channels

    def start_seconds(self):
        return self.start if self.start is not None else 0.0

    def window(self):
        return [self.start, self.end]

    def read(self, channel, chunk_bytes):
        """读一段 CSV 字节 → 解析成行 → 分发到各通道原始跳变缓冲。"""
        del channel   # 行流对所有通道共享
        while True:
            if self.lines:
                return self._consume_lines()
            if self._eof:
                return None
            data = self.stream.read(int(chunk_bytes))
            if not data:
                self._eof = True
                tail = self.decoder.decode(b'', True)
                if tail:
                    self.pending += tail
                if self.pending:
                    self.lines.append(self.pending)
                    self.pending = ''
                continue
            self.bytes_read += len(data)
            text = self.decoder.decode(data)
            if not text:
                continue
            self.pending += text
            parts = self.pending.split('\n')
            self.pending = parts.pop()
            if parts:
                self.lines.extend(parts)
            if not self.lines:
                continue
            return self._consume_lines()

    def _consume_lines(self):
        lines, self.lines = self.lines, []
        for line in lines:
            if line.endswith('\r'):
                line = line[:-1]
            if not line.strip():
                continue
            if self._header_pending:
                self._header_pending = False      # 与 pandas skiprows=1 一致：跳过表头行
                continue
            row = next(csv.reader([line]))
            if not row or not row[0].strip():
                continue
            try:
                timestamp = float(row[0])
            except ValueError:
                raise FrontendError(f'不是 Kingst 秒数时间列：{row[0]!r}') from None
            if self.end is not None and timestamp < self.end:
                raise FrontendError('CSV 时间倒序。')
            if len(row) != len(self.fields) + 1:
                raise FrontendError('CSV 行的通道数与表头不一致。')
            if self.start is None:
                self.start = timestamp
            self.end = timestamp
            for position, channel in enumerate(self.channels):
                value = row[position + 1].strip()
                if value not in ('0', '1'):
                    raise FrontendError(f'CH{channel} 电平不是 0/1：{value!r}')
                level = int(value)
                if channel not in self.initial:
                    self.initial[channel] = level
                    self.levels[channel] = level
                    continue
                if self.levels[channel] != level:
                    self._buffer.setdefault(channel, ([], []))
                    times, levels = self._buffer[channel]
                    times.append(timestamp)
                    levels.append(level)
                    self.levels[channel] = level
            self.rows += 1
        updates = {}
        for channel in list(self._buffer):
            times, levels = self._buffer.pop(channel)
            if times:
                updates[channel] = (np.asarray(times, dtype=np.float64),
                                    np.asarray(levels, dtype=np.uint8))
        if not updates:
            return ({}, 0)
        return updates, 0

    def done(self, channel):
        del channel
        if self.lines:
            return False
        return self._eof and not self._buffer

    def missing_channels(self, selected):
        return [channel for channel in selected if channel not in self.channels]

    def close(self):
        try:
            self.stream.close()
        except Exception:  # pragma: no cover
            pass

    def extra_report(self):
        return {}


# ── TPIU 帧解复用（流式，逐字节等价 tpiu_demux.demux） ─────────────────────────


class TpuFraming:
    """流式 TPIU 解复用；状态在块边界完整保留。

    与 ``tpiu_demux.demux(raw)`` 的逐字节等价由三件事保证：

    1. FSYNC 搜索只在“帧边界（partial frame 为空）”进行，搜索残片最多 3 字节；
    2. 帧内 partial frame（0..15 字节）跨块保留，满 16 字节才输出；
    3. 尾部残片规则（trailing_sync_bytes / trailing_frame_bytes / trailing_raw_bytes）
       只在输入真正结束时判定，块尾不会被当作捕获边界。
    """

    __slots__ = ('mode', 'carry', 'scanned', 'buffer', 'offset', 'frame', 'current',
                 'streams', 'stats', 'error')

    def __init__(self):
        self.mode = 'search'
        self.carry = b''
        self.scanned = 0
        self.buffer = bytearray()
        self.offset = 0
        self.frame = bytearray()
        self.current = None
        self.streams = {}
        self.stats = dict(frames=0, fsyncs=0, hsyncs=0, errors=0, unknown_bytes=0,
                          initial_skipped_bytes=0, id_changes=[], trailing_sync_bytes=0,
                          idle_resyncs=0, dropped_partial_frame_bytes=0, mid_frame_fsyncs=0)
        self.error = None

    def state(self):
        return dict(mode=self.mode, scanned=self.scanned, offset=self.offset,
                    buffered=len(self.buffer), frame_bytes=len(self.frame),
                    current=self.current, error=self.error)

    def feed(self, data):
        if self.mode in ('failed', 'done') or not data:
            return
        if self.mode == 'search':
            window = self.carry + data
            position = window.find(FSYNC)
            if position < 0:
                keep = window[-3:]
                # scanned = 已丢弃字节数 = carry[0] 在候选流里的绝对偏移
                self.scanned += len(window) - len(keep)
                self.carry = keep
                return
            self.stats['initial_skipped_bytes'] = self.scanned + position
            self.mode = 'parse'
            self.carry = b''
            self.buffer += window[position:]
            self._parse(False)
            return
        self.buffer += data
        self._parse(False)

    def finish(self):
        """输入结束：处理尾部残片并返回 (stats, streams)；没有 FSYNC 时抛 FrontendError。"""
        if self.mode == 'failed':
            raise FrontendError(self.error)
        if self.mode == 'search':
            raise FrontendError('没有 FSYNC，无法确认 TPIU 帧起点。')
        self._parse(True)
        self.mode = 'done'
        stats = dict(self.stats)
        stats['trailing_frame_bytes'] = len(self.frame)
        stats['trailing_raw_bytes'] = len(self.buffer)
        stats['stream_bytes'] = {str(source): len(buffer)
                                 for source, buffer in self.streams.items()}
        return stats, {source: bytes(buffer) for source, buffer in self.streams.items()}

    # 内部
    def _emit(self, source, value):
        if source is None:
            self.stats['unknown_bytes'] += 1
            return
        buffer = self.streams.get(source)
        if buffer is None:
            self.streams[source] = bytearray((value,))
        else:
            buffer.append(value)

    def _flush_frame(self):
        frame = self.frame
        stats = self.stats
        flags = frame[15]
        for position in range(0, 15, 2):
            flag = (flags >> (position // 2)) & 1
            value = frame[position]
            if value & 1:
                new = value >> 1
                previous = self.current
                if new != previous:
                    stats['id_changes'].append(new)
                self.current = new
                if position < 14:
                    self._emit(previous if flag and new != previous else self.current,
                               frame[position + 1])
            else:
                self._emit(self.current, value | flag)
                if position < 14:
                    self._emit(self.current, frame[position + 1])
        self.frame = bytearray()
        stats['frames'] += 1

    def _parse(self, eof):
        buffer = self.buffer
        pos = 0
        stop = len(buffer)
        stats = self.stats
        try:
            while True:
                remaining = stop - pos
                if remaining <= 0:
                    break
                if not self.frame and remaining < 4:
                    if not eof:
                        break
                    if buffer[pos:stop] == FSYNC[:remaining]:
                        stats['trailing_sync_bytes'] = remaining
                        break
                if remaining >= 4 and not self.frame and buffer[pos:pos + 4] == FSYNC:
                    stats['fsyncs'] += 1
                    pos += 4
                    continue
                if remaining < 2:
                    break
                pair = bytes(buffer[pos:pos + 2])
                if pair == HSYNC:
                    stats['hsyncs'] += 1
                    pos += 2
                    continue
                if pair == b'\xff\xff':
                    raise FrontendError(
                        f'TPIU 帧内完整同步或非法源 ID 0x7f（物理偏移 {self.offset + pos}，'
                        f'帧内 {len(self.frame)} 字节）；拒绝拼接损坏帧')
                self.frame += pair
                pos += 2
                if len(self.frame) != 16:
                    continue
                self._flush_frame()
        except FrontendError as error:
            self.mode = 'failed'
            self.error = str(error)
            raise
        finally:
            if pos:
                del buffer[:pos]
                self.offset += pos


# ── 前端主体 ─────────────────────────────────────────────────────────────────


class Frontend:
    """有界分块物理前端（见模块 docstring）。"""

    def __init__(self, source, config=None, chunk_bytes=DEFAULT_CHUNK_BYTES,
                 batch_edges=DEFAULT_BATCH_EDGES, max_distinct_intervals=DEFAULT_MAX_DISTINCT,
                 accumulate_edges=RAW_ACCUMULATE_EDGES,
                 min_batch_edges=MIN_BATCH_EDGES, parse_min_bytes=KVDAT_PARSE_MIN_BYTES):
        _require_numpy()
        self.source_path = Path(source).resolve()
        if not self.source_path.exists():
            raise FrontendError(f'输入不存在：{self.source_path}')
        self.config = self._normalize_config(config)
        plan = self._check_chunk(chunk_bytes)
        self._chunk_plan = plan if isinstance(plan, list) else None
        self.chunk_bytes = plan[0] if isinstance(plan, list) else plan
        self.batch_edges = max(2, int(batch_edges))
        self.accumulate_edges = max(1, int(accumulate_edges))
        self.min_batch_edges = max(2, min(int(min_batch_edges), self.batch_edges))
        self.parse_min_bytes = max(KVDAT_EDGE_BYTES, int(parse_min_bytes))
        self.max_distinct = int(max_distinct_intervals)
        self._finished = False
        self._result = None
        self._counter = dict(feeds=0, reads=0, bytes_read=0, batches=0, samples=0,
                             phase0_bytes=0, phase1_bytes=0, demux_bytes=0)
        self._timing = dict(read=0.0, filter=0.0, sample=0.0, pack=0.0, demux=0.0)
        self._build()

    # — 初始化 / 会话复位 —
    @staticmethod
    def _normalize_config(config):
        config = dict(config or {})
        clock = int(config.get('clock', 0))
        data = [int(channel) for channel in config.get('data', [1, 2, 3, 4])]
        fraction = config.get('fraction', 0.25)
        glitch = config.get('glitch_ns', 0)
        traceid = int(config.get('traceid', 2))
        if not 0 < fraction < 1:
            raise FrontendError('取样位置必须介于 0 和 1 之间。')
        if isinstance(glitch, bool) or not isinstance(glitch, (int, float)) or not 0 <= glitch <= 100:
            raise FrontendError('软件滤波宽度须为 0～100 ns（0 为关闭）')
        if len(data) != 4 or len(set([clock] + data)) != 5:
            raise FrontendError('需要五个不同通道：时钟 + D0/D1/D2/D3。')
        return dict(clock=clock, data=data, fraction=float(fraction),
                    glitch_ns=float(glitch), traceid=traceid)

    @staticmethod
    def _check_chunk(chunk_bytes):
        if isinstance(chunk_bytes, int) and not isinstance(chunk_bytes, bool):
            if chunk_bytes <= 0:
                raise FrontendError('分块大小必须是正整数')
            return chunk_bytes
        if isinstance(chunk_bytes, (list, tuple)):
            if not chunk_bytes:
                raise FrontendError('分块计划不能为空')
            for size in chunk_bytes:
                if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                    raise FrontendError('分块大小必须是正整数')
            return list(chunk_bytes)
        raise FrontendError('分块大小必须是正整数或正整数序列')

    def _build(self):
        glitch = self.config['glitch_ns']
        suffix = self.source_path.suffix.lower()
        if suffix == '.kvdat':
            self.source = _KvdatSource(self.source_path, self.config, self.parse_min_bytes)
        elif suffix == '.csv':
            self.source = _CsvSource(self.source_path, self.config)
        else:
            raise FrontendError(f'只支持 .kvdat 或 .csv 输入：{self.source_path.name}')
        selected = [self.config['clock']] + self.config['data']
        missing = self.source.missing_channels(selected)
        if missing:
            raise FrontendError(f'CSV 缺少通道：{sorted(missing)}')
        initial = self.source.initial
        self._initial_pending = len(self.source.initial) < 5   # CSV 的首行电平要读到第一行才知道
        self.clock_stats = ClockStatistics(self.config['clock'],
                                           max_distinct=self.max_distinct)
        self.clock_filter = GlitchFilter(glitch, enabled=bool(glitch))
        self.clock_confirmed = np.empty(0, dtype=np.float64)
        self.clock_raw = []
        self.clock_raw_count = 0
        self.clock_prev = None
        self.clock_done = False
        self.clock_flushed = False
        self.sampling_done = False
        self.data_channels = [DataChannel(channel, initial.get(channel, 0), glitch,
                                         self.accumulate_edges)
                              for channel in self.config['data']]
        self._data_by_channel = {channel.channel: channel for channel in self.data_channels}
        self.nibble_carry = np.empty(0, dtype=np.uint8)
        self.nibble_base = 0
        self.phase_counts = [0, 0]
        self.candidates = [bytearray(), bytearray()]
        self.candidate_syncs = [0, 0]
        self.candidate_tails = [b'', b'']
        self.candidate_heads = [b'', b'']
        self.demuxers = [TpuFraming(), TpuFraming()]
        self.demux_failures = []
        self.sample_prev = None
        self._plan_pos = 0
        self._need = None

    def reset(self):
        """显式会话复位：清空全部跨块状态并倒回输入起点（不继承上一次会话的任何残片）。"""
        try:
            self.source.close()
        except Exception:  # pragma: no cover
            pass
        self._finished = False
        self._result = None
        self._counter = {key: 0 for key in self._counter}
        self._timing = {key: 0.0 for key in self._timing}
        self._build()

    # — 分块尺寸 —
    def _next_chunk(self, override=None):
        if override is not None:
            return self._check_chunk(override)
        if self._chunk_plan is None:
            return self.chunk_bytes
        size = self._chunk_plan[min(self._plan_pos, len(self._chunk_plan) - 1)]
        self._plan_pos += 1
        return size

    # — 主循环 —
    def feed(self, chunk_bytes=None):
        """推进一轮：尽量取样，并**至多**读入一块输入（``chunk_bytes``）。返回是否有进展。

        “每次只读一块”是为了让跨块状态可观察、也可用于有界队列的实时输入；``run()``
        会一直 feed 到没有进展。
        """
        if self._finished:
            raise FrontendError('会话已结束：finish() 之后不能再 feed()')
        size = self._next_chunk(chunk_bytes)
        self._counter['feeds'] += 1
        progressed = False
        settled = False
        for _ in range(MAX_ROUNDS):
            if self._sample_once():
                progressed = True
                settled = False
                continue
            target = self._need
            if target is None or self.source.done(target):
                if settled:
                    break
                # 可能是某个通道刚好读完：收尾后再试一轮取样
                settled = True
                self._settle_ends()
                continue
            started = time.perf_counter()
            data = self.source.read(target, size)
            self._timing['read'] += time.perf_counter() - started
            if data is None:
                self._mark_done(target)
                progressed = True
                continue
            updates, consumed = data
            self._counter['reads'] += 1
            self._counter['bytes_read'] += consumed
            self._offer(updates)
            progressed = True
            break                      # 本次 feed 只读一块，剩余留给下一次 feed
        return progressed

    def _settle_ends(self):
        """把“源已读完”的通道收尾：推完剩余原始边沿并冲出“待定边沿”。

        参考实现在整段数组上一次性过滤，最后一条边沿若没成对就被保留；分块实现里它一直
        挂在“待定边沿”上，必须等通道确实读完才冲出来，否则会永久挡住后续取样。
        ``source.done()`` 可能在最后一次 read() 里就变真（不会再有一次返回 None），
        所以这里主动同步“已读完”标记。
        """
        if not self.clock_done and self.source.done(self.config['clock']):
            self.clock_done = True
        if self.clock_done and not self.clock_flushed:
            self._flush_clock_raw()
            self.clock_flushed = True
            edge = self.clock_filter.flush()
            if edge is not None:
                survivors = np.asarray([edge[0]], dtype=np.float64)
                self.clock_stats.add(survivors)
                if not self.sampling_done:
                    self.clock_confirmed = np.concatenate((self.clock_confirmed, survivors))
        for channel in self.data_channels:
            if not channel.source_done and self.source.done(channel.channel):
                channel.source_done = True
            if channel.source_done and not channel.ended:
                channel.ended = True
                channel.push_all()
                channel.flush()

    def _mark_done(self, channel):
        if channel == self.config['clock']:
            self.clock_done = True
        else:
            self._data_by_channel[channel].source_done = True

    def _offer(self, updates):
        clock = self.config['clock']
        for channel, (times, levels) in updates.items():
            if not len(times):
                continue
            if channel == clock:
                self.clock_raw.append(times)
                self.clock_raw_count += len(times)
                if self.clock_raw_count >= self.accumulate_edges:
                    self._flush_clock_raw()
                continue
            self._data_by_channel[channel].offer(times, levels)

    def _flush_clock_raw(self):
        """把攒下的原始时钟边沿一次性推进滤波与统计（顺序与逐条处理完全一致）。"""
        if not self.clock_raw_count:
            return
        times = (np.concatenate(self.clock_raw) if len(self.clock_raw) > 1
                 else self.clock_raw[0])
        self.clock_raw = []
        self.clock_raw_count = 0
        started = time.perf_counter()
        self.clock_stats.add_raw(times)
        self._timing['filter'] += time.perf_counter() - started
        survivors, _ = self.clock_filter.push_batch(times, None)
        if len(survivors):
            self.clock_stats.add(survivors)
            if not self.sampling_done:
                self.clock_confirmed = np.concatenate((self.clock_confirmed, survivors))

    def _sample_once(self):
        """取样一批（<= batch_edges 个时钟边沿）。无法取样时设置 ``self._need``。"""
        self._need = None
        if self._initial_pending:
            # CSV 的初始电平来自第一行；没读到第一行就不能取样
            initial = self.source.initial
            if any(channel.channel not in initial for channel in self.data_channels):
                self._need = self.config['clock'] if not self.clock_done else self.config['data'][0]
                return False
            for channel in self.data_channels:
                channel.initial = int(initial[channel.channel])
            self._initial_pending = False
        confirmed = self.clock_confirmed
        if (len(confirmed) < self.min_batch_edges and not self.clock_done
                and self.clock_raw_count >= self.accumulate_edges):
            # 攒够一批原始边沿后先推完，再看已确认边沿是否够成批取样
            self._flush_clock_raw()
            confirmed = self.clock_confirmed
        if self.clock_prev is None:
            if len(confirmed) < 2:
                if not self.clock_done:
                    self._need = self.config['clock']
                return False
            ext = confirmed[:self.batch_edges + 1]
            take = len(ext)
        else:
            if not len(confirmed):
                if not self.clock_done:
                    self._need = self.config['clock']
                return False
            ext = np.concatenate(([self.clock_prev], confirmed[:self.batch_edges]))
            take = len(ext) - 1
        samples = ext[:-1] + (ext[1:] - ext[:-1]) * self.config['fraction']
        if not len(samples):  # pragma: no cover - take>=2 保证 samples 非空
            return False
        sample_max = float(samples[-1])
        started = time.perf_counter()
        for channel in self.data_channels:
            channel.push_until(sample_max)
            if not channel.covers(sample_max):
                self._timing['sample'] += time.perf_counter() - started
                if not channel.source_done:
                    self._need = channel.channel
                return False
        nibbles = np.zeros(len(samples), dtype=np.uint8)
        for bit, channel in enumerate(self.data_channels):
            nibbles |= channel.sample(samples, self.sample_prev).astype(np.uint8) << bit
        self._timing['sample'] += time.perf_counter() - started
        self.clock_prev = float(ext[-1])
        self.clock_confirmed = confirmed[take:]
        self.sample_prev = float(samples[-1])
        self._counter['batches'] += 1
        self._counter['samples'] += len(samples)
        self._pack(nibbles)
        return True

    # — nibble → 字节 → 两个候选相位 —
    def _pack(self, nibbles):
        started = time.perf_counter()
        base = self.nibble_base
        extended = (np.concatenate((self.nibble_carry, nibbles))
                    if len(self.nibble_carry) else nibbles)
        total = base + len(extended)
        chunks = [b'', b'']
        for phase in (0, 1):
            count = total // 2 if phase == 0 else (total - 1) // 2
            produced = self.phase_counts[phase]
            if count <= produced:
                continue
            local = 2 * produced + phase - base
            low = extended[local::2][:count - produced]
            high = extended[local + 1::2][:count - produced]
            chunks[phase] = (low | (high << 4)).tobytes()
            self.phase_counts[phase] = count
        next_need = min(2 * self.phase_counts[0], 2 * self.phase_counts[1] + 1)
        self.nibble_carry = extended[next_need - base:].copy()
        self.nibble_base = next_need
        self._timing['pack'] += time.perf_counter() - started
        for phase in (0, 1):
            chunk = chunks[phase]
            if not chunk:
                continue
            self._counter['phase0_bytes' if phase == 0 else 'phase1_bytes'] += len(chunk)
            self.candidates[phase] += chunk
            if len(self.candidate_heads[phase]) < 32:
                self.candidate_heads[phase] = bytes(self.candidates[phase][:32])
            window = self.candidate_tails[phase] + chunk
            self.candidate_syncs[phase] += window.count(FSYNC)
            self.candidate_tails[phase] = window[-3:]
            started = time.perf_counter()
            self.demuxers[phase].feed(chunk)
            self._counter['demux_bytes'] += len(chunk)
            self._timing['demux'] += time.perf_counter() - started

    # — 收尾 —
    def finish(self):
        if self._finished:
            raise FrontendError('会话已结束：finish() 只能调用一次')
        if self._result is not None:  # pragma: no cover - 防御
            return self._result
        # 1) 取样到底（读完的通道先冲出待定边沿，否则最后一条边沿会永久挡住取样）
        while True:
            progressed = False
            settled = False
            for _ in range(MAX_ROUNDS):
                if self._sample_once():
                    progressed = True
                    settled = False
                    continue
                target = self._need
                if target is None or self.source.done(target):
                    if settled:
                        break
                    settled = True
                    self._settle_ends()
                    continue
                data = self.source.read(target, self._next_chunk())
                if data is None:
                    self._mark_done(target)
                    progressed = True
                    continue
                updates, consumed = data
                self._counter['reads'] += 1
                self._counter['bytes_read'] += consumed
                self._offer(updates)
                progressed = True
                settled = False
            if not progressed:
                break
        # 2) 时钟边沿也用尽后取样到底
        self._settle_ends()
        while self._sample_once():
            pass
        self.sampling_done = True
        # 3) 读完剩余输入（统计必须覆盖整段记录）
        for channel in self.data_channels:
            while True:
                if self.source.done(channel.channel) and channel.raw_pending() == 0:
                    break
                data = self.source.read(channel.channel, self._next_chunk())
                if data is None:
                    channel.source_done = True
                    channel.push_all()
                    channel.flush()
                    break
                updates, consumed = data
                self._counter['reads'] += 1
                self._counter['bytes_read'] += consumed
                self._offer(updates)
                channel.push_all()
        while not self.clock_done:
            data = self.source.read(self.config['clock'], self._next_chunk())
            if data is None:
                self.clock_done = True
                break
            updates, consumed = data
            self._counter['reads'] += 1
            self._counter['bytes_read'] += consumed
            self._offer(updates)
        self.clock_done = True
        self._settle_ends()
        for channel in self.data_channels:
            channel.finish_margin()
        # 4) 时钟统计与滤波阈值校验
        try:
            self.clock_stats.filter_limit_check(self.config['glitch_ns'])
            stats = self.clock_stats.finish()
        except FrontendError:
            self.source.close()
            raise
        physical = self._physical(stats)
        # 5) TPIU 解复用收尾与相位唯一性判定（整段只能有一个有效候选）
        try:
            phase, framing, streams, failures = select_phase(self.demuxers,
                                                             self.config['traceid'])
        except PhaseSelectionError as error:
            self.demux_failures = error.failures
            self.source.close()
            raise
        self.demux_failures = failures
        self._finished = True
        self._result = dict(phase=phase, tpiu=bytes(self.candidates[phase]),
                            streams=streams, physical=physical, framing=framing,
                            input=str(self.source_path), chunk_bytes=self.chunk_bytes,
                            counters=dict(self._counter), timing=dict(self._timing))
        self.source.close()
        return self._result

    def _physical(self, stats):
        glitch = self.config['glitch_ns']
        removed = {}
        if glitch:
            removed[str(self.config['clock'])] = int(self.clock_filter.removed_count())
            for channel in self.data_channels:
                removed[str(channel.channel)] = int(channel.filter.removed_count())
        margins = [channel.margin_min for channel in self.data_channels
                   if channel.filtered_count > 0 and channel.margin_min is not None]
        transitions = {f'CH{channel.channel}': int(channel.filtered_count)
                       for channel in self.data_channels}
        report = {
            'input': str(self.source_path),
            'input_sha256': self._sha256(),
            'software_filter': dict(enabled=bool(glitch), limit_ns=glitch,
                                    removed_transitions=removed,
                                    raw_clock_edges=self.clock_stats.raw_edge_count,
                                    basis=FILTER_BASIS),
            'stage': 'physical DDR extraction only; no TPIU demux or ETM instruction decode',
            'clock_channel': self.config['clock'],
            'data_channels_D0_to_D3': list(self.config['data']),
            'window_seconds': [float(value) for value in self.source.window()],
            'clock_edges': self.clock_stats.edge_count,
            'clock_hz_median': stats['median'],
            'clock_hz_average': stats['average'],
            'half_period_ns': stats['half_period'] * 1e9,
            'irregular_half_periods': stats['irregular'],
            'clock_runs': stats['runs'],
            'clock_gaps': stats['gaps'],
            'data_transitions': transitions,
            'sample_fraction_after_each_edge': self.config['fraction'],
            'min_data_transition_distance_ns': (min(margins) * 1e9 if margins else None),
            'nibbles': int(self._counter['samples']),
            'candidates': [{'phase': phase, 'bytes': len(self.candidates[phase]),
                            'frame_sync_matches': int(self.candidate_syncs[phase]),
                            'first_32_bytes_hex': self.candidate_heads[phase].hex(' ')}
                           for phase in (0, 1)],
            'note': NOTE,
        }
        report.update(self.source.extra_report())
        return report

    def _sha256(self):
        digest = hashlib.sha256()
        with self.source_path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b''):
                digest.update(chunk)
        return digest.hexdigest()

    # — 结果访问器 —
    def run(self, max_feeds=None):
        try:
            feeds = 0
            while self.feed():
                feeds += 1
                if max_feeds is not None and feeds >= max_feeds:
                    break
            return self.finish()
        except BaseException:
            self.close()
            raise

    def close(self):
        """关闭底层输入（可重复调用；用于异常/取消后立刻释放文件句柄）。"""
        source = getattr(self, 'source', None)
        if source is not None:
            try:
                source.close()
            except Exception:  # pragma: no cover
                pass

    def physical(self):
        return self._require()['physical']

    def framing(self):
        return self._require()['framing']

    def tpiu(self):
        return self._require()['tpiu']

    def streams(self):
        return self._require()['streams']

    def phase(self):
        return self._require()['phase']

    def candidate_bytes(self, phase):
        return bytes(self.candidates[phase])

    def counters(self):
        return dict(self._counter, timing=dict(self._timing))

    def demux_errors(self):
        return [dict(item) for item in self.demux_failures]

    def _require(self):
        if self._result is None:
            raise FrontendError('会话尚未结束：请先调用 finish()')
        return self._result

    def state(self):
        """跨块状态快照（诊断/测试用；用于证明状态确实在块边界保留）。"""
        return {
            'source': {'bytes_read': self._counter['bytes_read'],
                       'reads': self._counter['reads']},
            'clock': {'raw_edges': self.clock_stats.raw_edge_count,
                      'edges': self.clock_stats.edge_count,
                      'pending_edge': self.clock_filter.pending_time,
                      'prev_time': self.clock_prev,
                      'confirmed_buffered': int(len(self.clock_confirmed)),
                      'distinct_intervals': len(self.clock_stats.intervals.counts)},
            'data': {channel.channel: {'raw_pending': channel.raw_pending(),
                                       'pending_edge': channel.filter.pending_time,
                                       'window_base': channel.win_base,
                                       'window_len': int(len(channel.win_times)),
                                       'margin_cursor': channel.margin_cursor,
                                       'filtered': channel.filtered_count,
                                       'source_done': channel.source_done}
                     for channel in self.data_channels},
            'nibbles': {'base': self.nibble_base, 'carry': self.nibble_carry.tolist(),
                        'phase0': self.phase_counts[0], 'phase1': self.phase_counts[1]},
            'tpiu': [demux.state() for demux in self.demuxers],
            'phases': {'candidate_bytes': [len(item) for item in self.candidates],
                       'syncs': list(self.candidate_syncs)},
        }


# ── 便利函数与 CLI ───────────────────────────────────────────────────────────


def run_file(source, config=None, chunk_bytes=DEFAULT_CHUNK_BYTES,
             batch_edges=DEFAULT_BATCH_EDGES):
    """一次性跑完整条链路（等价 ``Frontend(...).run()``）。"""
    frontend = Frontend(source, config, chunk_bytes=chunk_bytes, batch_edges=batch_edges)
    return frontend, frontend.run()


def main():
    parser = argparse.ArgumentParser(
        description='有界分块物理前端：KVDAT/CSV → 滤波/DDR → TPIU 解复用（P5a 参考实现）')
    parser.add_argument('source', type=Path, help='capture.kvdat（优先）或 capture.csv')
    parser.add_argument('--clock', type=int, default=0, help='时钟通道（默认 0）')
    parser.add_argument('--data', nargs=4, type=int, default=[1, 2, 3, 4], metavar='CH',
                        help='D0..D3 通道（默认 1 2 3 4）')
    parser.add_argument('--sample-fraction', type=float, default=0.25,
                        help='每个半周期内的取样位置（默认 0.25）')
    parser.add_argument('--glitch-ns', type=float, default=0,
                        help='毛刺滤波宽度（0～100 ns，0 为关闭）')
    parser.add_argument('--traceid', type=int, default=2, help='期望的 TPIU 数据源 ID（默认 2）')
    parser.add_argument('--chunk-bytes', type=int, default=DEFAULT_CHUNK_BYTES,
                        help=f'每次读取的输入字节数（默认 {DEFAULT_CHUNK_BYTES}）')
    parser.add_argument('--batch-edges', type=int, default=DEFAULT_BATCH_EDGES,
                        help=f'每个取样批的时钟边沿数上限（默认 {DEFAULT_BATCH_EDGES}）')
    parser.add_argument('--out', type=Path, help='输出路径前缀，默认与输入同名')
    args = parser.parse_args()
    config = dict(clock=args.clock, data=list(args.data), fraction=args.sample_fraction,
                  glitch_ns=args.glitch_ns, traceid=args.traceid)
    try:
        frontend = Frontend(args.source, config, chunk_bytes=args.chunk_bytes,
                            batch_edges=args.batch_edges)
        started = time.perf_counter()
        result = frontend.run()
        elapsed = time.perf_counter() - started
    except (OSError, ValueError) as error:
        parser.exit(1, f'前端失败：{error}\n')
    prefix = args.out or args.source.with_suffix('')
    prefix.parent.mkdir(parents=True, exist_ok=True)
    Path(str(prefix) + '.tpiu.bin').write_bytes(result['tpiu'])
    for source_id, buffer in result['streams'].items():
        Path(str(prefix) + f'.id{source_id}.bin').write_bytes(buffer)
    Path(str(prefix) + '.physical.json').write_text(
        json.dumps(result['physical'], ensure_ascii=False, indent=2), encoding='utf-8')
    Path(str(prefix) + '.framing.json').write_text(
        json.dumps(result['framing'], ensure_ascii=False, indent=2), encoding='utf-8')
    Path(str(prefix) + '.frontend.json').write_text(
        json.dumps(dict(chunkBytes=result['chunk_bytes'], phase=result['phase'],
                        counters=result['counters'], timing=result['timing']),
                   ensure_ascii=False, indent=2), encoding='utf-8')
    physical = result['physical']
    print(f"TRACECLK 中位 = {physical['clock_hz_median'] / 1e6:.6f} MHz，"
          f"均值 = {physical['clock_hz_average'] / 1e6:.6f} MHz")
    print(f"时钟跳变 {physical['clock_edges']}"
          f"（原始 {physical['software_filter']['raw_clock_edges']}），"
          f"nibble {physical['nibbles']}，数据跳变 {physical['data_transitions']}")
    print(f"TPIU 相位 {result['phase']}：{result['framing']['frames']} 帧，"
          f"FSYNC {result['framing']['fsyncs']}，HSYNC {result['framing']['hsyncs']}，"
          f"数据源 {result['framing']['stream_bytes']}")
    print(f'分块 {result["chunk_bytes"]} 字节，feed {result["counters"]["feeds"]} 次，'
          f'用时 {elapsed:.2f} s；输出前缀 {prefix}')


if __name__ == '__main__':
    main()
