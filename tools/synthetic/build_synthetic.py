#!/usr/bin/env python3
"""构造可复现的合成 1 s 数据集（非真机）：物理输入拼接 + 1 s 规模事件流。

方案依据：``docs/etm-1s-native-pipeline-plan-20261019.md`` §9.1/§9.2（固定输入与比较层次）、
§3.2（拼接多个独立 capture 只能用于带显式会话复位的压力测试）、§8 P6。

**合成数据不是连续真机 trace**：物理 1 s 输入是把一份 10 ms / 200 ms KVDAT/CSV 记录按段
重复拼接（段边界=显式会话复位），规模事件流是把单元事件流重复拼接并插入复位标记。它只用于
store 构建、查询与吞吐的规模化压力测试，不能用于 1 s 硬件验收或性能达标宣称。

用法
----
  # 物理 1 s：10 ms KVDAT 记录 × 100 段（0.01 s × 100 = 1 s）
  python -X utf8 tools/synthetic/build_synthetic.py --mode physical \\
      --record etm/records/capture-4395612c313f47ce97af1e1c17e0a0d5 \\
      --kind kvdat --segments 100 --out tmp/p6a-1s --verify all

  # 1 s 规模事件流：同一条 10 ms 记录 × 100 段（≈2,107 万条指令），落 events.jsonl
  python -X utf8 tools/synthetic/build_synthetic.py --mode events \\
      --record etm/records/capture-4395612c313f47ce97af1e1c17e0a0d5 \\
      --segments 100 --out tmp/p6a-1s --event-sink jsonl --verify all

  # 按 synthetic.json 把合成物理输入切成逐段文件（供分块前端跨块等价验证）
  python -X utf8 tools/synthetic/build_synthetic.py --mode split \\
      --meta tmp/p6a-1s/synthetic.json --out tmp/p6a-1s/physical/segments

产物（都在 --out 下；大文件建议放 tmp，证据目录只留 report/JSON/日志/指纹）
------
  synthetic.json                       合成说明：来源指纹、段边界、复位标记、声明、命令
  physical/capture-synthetic-1s.kvdat  物理 1 s 合成输入（或 .csv）
  physical/segments.json               物理段表（含采样位置/时间/复位）
  physical/session-synthetic.json      合成输入的取样配置（给分块前端/解码器）
  unit/unit.events.jsonl               单元事件 spool（canonical JSONL）
  events-record/                       streamed 记录目录（decoded.json+events.jsonl+ELF 等）
  verify/*.json                        校验报告
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from synthetic_events import (                            # noqa: E402
    SyntheticError, build_unit_spool, expected_reset_table, file_fingerprint,
    iter_event_lines, load_unit, peak_rss_bytes, sha256_file, stream_stats, unit_paths,
    verify_event_stream, write_event_shards, write_event_stream, write_events_record_dir,
)

SCHEMA = 'etm-synthetic-1s/1'
DECLARATION = dict(
    synthetic=True,
    continuousRealTrace=False,
    notForHardwareAcceptance=True,
    segmentsAreIndependentCaptures=True,
    resetsBetweenSegments='explicit',
    note=('本数据由既有 10 ms / 200 ms 记录按段重复拼接而成。段与段之间是显式会话复位，'
          '时间/电平/调用栈/周期都不保证连续；它不是一段连续真机 trace，不能冒充 1 s 采集，'
          '不能用于 1 s 硬件验收或性能达标宣称。真机验收必须使用真实连续 1 s 原始记录。'),
    realHardwareAcceptanceRequired=True,
)

KVDAT_HEADER_LIMIT = 256 * 1024
FILE_HEADER = struct.Struct('<4Q')
CHANNEL_HEADER = struct.Struct('<IBBHQ')
KVDAT_CHANNEL_MAGIC = 0x442323
KVDAT_MAGIC = b'kvdat\0\0\0'
_ROWS_DTYPE = None


class BuildError(SyntheticError):
    """构造失败（参数/来源/校验）。"""


# ---------------------------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------------------------

def _ensure_etm_tools():
    tools = REPO_ROOT / 'etm' / 'tools'
    if not tools.is_dir():
        raise BuildError('找不到 etm/tools（本工具只读引用 kingst_record/trace_port）：%s' % tools)
    text = str(tools)
    if text not in sys.path:
        sys.path.insert(0, text)
    return tools


def _import_kingst_record():
    _ensure_etm_tools()
    import kingst_record
    return kingst_record


def _import_trace_port():
    _ensure_etm_tools()
    import trace_port
    return trace_port


def _import_numpy():
    try:
        import numpy
        return numpy
    except ImportError:
        raise BuildError('本工具需要 numpy（KVDAT 边沿数组处理）；请安装依赖') from None


def _rows_dtype():
    global _ROWS_DTYPE
    if _ROWS_DTYPE is None:
        numpy = _import_numpy()
        _ROWS_DTYPE = numpy.dtype([('position', '<u4'), ('flag', 'u1')])
    return _ROWS_DTYPE


def _write_json(path, value, *, indent=2, sort_keys=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=indent, sort_keys=sort_keys)
    path.write_text(text + '\n', encoding='utf-8')
    return dict(path=str(path.resolve()), bytes=path.stat().st_size)


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BuildError('无法读取 JSON %s：%s' % (path, error))


def _read_settings_prefix(path):
    """读取 KVDAT 头部（到 ``kvdat`` 采样头标记之前），拼接时原样复制。

    源文件在 ``</settings>`` 与 ``kvdat\\0\\0\\0`` 之间可能有 CRLF 等字节；这里连这些字节一起
    复制，保证合成文件的头部与来源一致（逐段切分后可与来源逐字节比较）。
    """
    path = Path(path)
    with path.open('rb') as handle:
        head = handle.read(KVDAT_HEADER_LIMIT)
    end = head.find(b'</settings>')
    if end < 0:
        raise BuildError('来源 KVDAT 设置头不完整或超过 %.0f KiB：%s'
                         % (KVDAT_HEADER_LIMIT / 1024, path))
    marker = head.find(KVDAT_MAGIC, end)
    if marker < 0:
        raise BuildError('来源 KVDAT 缺少 kvdat 采样头标记：%s' % path)
    return head[:marker]


def _print(args, message):
    if not getattr(args, 'quiet', False):
        print(message, flush=True)


def _printer(args):
    if getattr(args, 'quiet', False):
        return None

    def note(message):
        print(message, flush=True)
    return note


# ---------------------------------------------------------------------------------------------
# 来源解析
# ---------------------------------------------------------------------------------------------

def resolve_physical_source(record, kind='auto'):
    """返回 dict(path, format, recordDir)；kind=auto 时优先 KVDAT。"""
    record = Path(record)
    if record.is_file():
        suffix = record.suffix.lower()
        if suffix not in ('.kvdat', '.csv'):
            raise BuildError('--record 指向的文件必须是 .kvdat 或 .csv：%s' % record)
        fmt = 'kvdat' if suffix == '.kvdat' else 'csv'
        if kind not in ('auto', fmt):
            raise BuildError('--kind=%s 与来源文件后缀（%s）不符' % (kind, suffix))
        return dict(path=record.resolve(), format=fmt, recordDir=record.parent.resolve())
    if not record.is_dir():
        raise BuildError('来源不存在（既不是记录目录也不是文件）：%s' % record)
    kvdat = record / 'capture.kvdat'
    csv = record / 'capture.csv'
    if kind == 'kvdat':
        if not kvdat.is_file():
            raise BuildError('记录目录没有 capture.kvdat：%s' % record)
        return dict(path=kvdat.resolve(), format='kvdat', recordDir=record.resolve())
    if kind == 'csv':
        if not csv.is_file():
            raise BuildError('记录目录没有 capture.csv：%s' % record)
        return dict(path=csv.resolve(), format='csv', recordDir=record.resolve())
    if kvdat.is_file():
        return dict(path=kvdat.resolve(), format='kvdat', recordDir=record.resolve())
    if csv.is_file():
        return dict(path=csv.resolve(), format='csv', recordDir=record.resolve())
    raise BuildError('记录目录既没有 capture.kvdat 也没有 capture.csv：%s' % record)


def load_session(record_dir):
    path = Path(record_dir) / 'session.json'
    if not path.is_file():
        return {}
    return _load_json(path)


def resolve_sampling(record_dir, session, args):
    """取样参数：优先命令行，其次 session.json，最后默认值。"""
    fraction = args.fraction if args.fraction is not None else session.get('fraction', 0.25)
    glitch = args.glitch_ns if args.glitch_ns is not None else session.get('glitchNs', 0.0)
    clock = args.clock if args.clock is not None else session.get('clock', 0)
    data = list(args.data) if args.data else list(session.get('data') or [1, 2, 3, 4])
    try:
        fraction = float(fraction)
        glitch = float(glitch)
    except (TypeError, ValueError):
        raise BuildError('取样参数非法：fraction=%r glitchNs=%r'
                         % (args.fraction, args.glitch_ns)) from None
    if not 0 < fraction < 1:
        raise BuildError('--fraction 必须介于 0 和 1 之间：%r' % (fraction,))
    if not 0 <= glitch <= 100:
        raise BuildError('--glitch-ns 必须在 0～100 ns：%r' % (glitch,))
    if len(set([clock] + data)) != 5:
        raise BuildError('时钟+数据通道必须是 5 个不同通道：clock=%r data=%r' % (clock, data))
    return dict(fraction=fraction, glitchNs=glitch, clock=clock, data=data)


# ---------------------------------------------------------------------------------------------
# KVDAT：来源读取 / 拼接
# ---------------------------------------------------------------------------------------------

def load_kvdat_source(path):
    """读取来源 KVDAT 布局与逐通道边沿（去掉哨兵），供拼接使用。"""
    kingst_record = _import_kingst_record()
    numpy = _import_numpy()
    path = Path(path)
    info = kingst_record.layout(path)
    prefix = _read_settings_prefix(path)
    dtype = _rows_dtype()
    channels = {}
    with path.open('rb') as handle:
        for channel, block in sorted(info['channels'].items()):
            handle.seek(block['offset'])
            rows = numpy.fromfile(handle, dtype=dtype, count=block['count'])
            if len(rows) != block['count']:
                raise BuildError('KVDAT 读取截断：通道 %s' % channel)
            positions = rows['position']
            if numpy.any(rows['flag']) or numpy.any(numpy.diff(positions) <= 0) \
                    or numpy.any(positions > info['depth']):
                raise BuildError('KVDAT 边沿非法：通道 %s' % channel)
            if int(positions[-1]) != info['depth'] or int(rows['flag'][-1]) != 0:
                raise BuildError('KVDAT 结束哨兵非法：通道 %s' % channel)
            channels[channel] = dict(initial=int(block['initial']),
                                     positions=positions[:-1].astype(numpy.uint32).copy(),
                                     rows=int(block['count']))
    return dict(path=path.resolve(), bytes=path.stat().st_size, sha256=sha256_file(path),
                info=dict(depth=int(info['depth']), rate=int(info['rate']),
                          trigger=int(info['trigger']), version=info.get('version'),
                          channels=sorted(info['channels'])),
                prefix=prefix, channels=channels)


def _max_sample_position(channel, fraction):
    """该通道全部取样点（相邻边沿之间按 fraction 取样）的最大采样位置。"""
    numpy = _import_numpy()
    positions = channel['positions']
    if len(positions) < 2:
        return float(positions[0]) if len(positions) else 0.0
    gaps = positions[1:].astype(numpy.float64) - positions[:-1].astype(numpy.float64)
    samples = positions[:-1].astype(numpy.float64) + gaps * fraction
    return float(samples.max())


def padding_plan(channel, depth, segments, *, max_sample_position):
    """需要补边界边沿的段索引 → 位置。

    KVDAT 每个通道块只有一个首电平，通道电平由「首电平 ^ 边沿计数奇偶」决定。把同一段重复
    拼接时，若某通道累计真实边沿数是奇数，下一段的首电平就会被整体翻转；因此在段首补 1 条
    边界边沿（早于该段所有取样点）把奇偶补回。补边只影响电平奇偶，不改变该段任何取样值。
    """
    positions = channel['positions']
    real = len(positions)
    plan = {}
    if real == 0:
        return plan
    first = int(positions[0])
    written = 0
    for index in range(segments):
        if index and written % 2 == 1:
            if first == 0:
                position = index * depth - 1
                if position <= (index - 1) * depth + max_sample_position:
                    raise BuildError(
                        '无法在不影响前段取样处插入边界边沿（通道首边沿在 0，段深 %d 太浅）：'
                        '请换更长的单元或减少段数' % depth)
            else:
                position = index * depth
            previous_max = (index - 1) * depth + int(positions[-1])
            if position <= previous_max:
                raise BuildError('边界补边位置与上一段末边沿冲突：%d <= %d'
                                 % (position, previous_max))
            plan[index] = position
            written += 1
        written += real
    return plan


def channel_padding_plans(src, segments, fraction):
    max_sample = _max_sample_position(src['channels'][0], fraction) if 0 in src['channels'] \
        else 0.0
    return {channel: padding_plan(channel_info, src['info']['depth'], segments,
                                  max_sample_position=max_sample)
            for channel, channel_info in src['channels'].items()}


def build_physical_kvdat(src, segments, out_path, *, fraction=0.25, progress=None):
    """把来源 KVDAT 按段拼接成 segments×depth 的物理合成输入。"""
    numpy = _import_numpy()
    info = src['info']
    depth = info['depth']
    total_depth = depth * segments
    if total_depth >= 2 ** 32:
        raise BuildError('拼接后采样深度 %d 超过 KVDAT 32 位位置上限；请减少段数' % total_depth)
    if not 0 < depth < 2 ** 32:
        raise BuildError('来源采样深度非法：%d' % depth)
    if info['trigger'] > total_depth:
        raise BuildError('来源触发位置 %d 超过拼接后深度 %d' % (info['trigger'], total_depth))
    plans = channel_padding_plans(src, segments, fraction)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dtype = _rows_dtype()
    started = time.time()
    written_rows = 0
    with out_path.open('wb') as out:
        out.write(src['prefix'])
        out.write(KVDAT_MAGIC)
        out.write(FILE_HEADER.pack(total_depth, info['rate'], info['trigger'],
                                   len(src['channels'])))
        for channel in sorted(src['channels']):
            channel_info = src['channels'][channel]
            positions = channel_info['positions']
            real = len(positions)
            plan = plans[channel]
            rows = real * segments + len(plan) + 1
            out.write(CHANNEL_HEADER.pack(KVDAT_CHANNEL_MAGIC, channel,
                                          channel_info['initial'], 0, rows))
            for index in range(segments):
                if index in plan:
                    padding = numpy.zeros(1, dtype=dtype)
                    padding['position'][0] = plan[index]
                    padding['flag'][0] = 0
                    out.write(padding.tobytes())
                    written_rows += 1
                if real:
                    chunk = numpy.empty(real, dtype=dtype)
                    chunk['position'] = positions + numpy.uint32(index * depth)
                    chunk['flag'] = 0
                    out.write(chunk.tobytes())
                    written_rows += real
            sentinel = numpy.zeros(1, dtype=dtype)
            sentinel['position'][0] = total_depth
            sentinel['flag'][0] = 0
            out.write(sentinel.tobytes())
            written_rows += 1
            if progress is not None:
                progress('  KVDAT：通道 %d 完成（%d 行）' % (channel, rows))
    stat = out_path.stat()
    return dict(format='kvdat', path=str(out_path.resolve()), bytes=stat.st_size,
                sha256=sha256_file(out_path),
                layout=dict(depth=total_depth, unitDepth=depth, rate=info['rate'],
                            trigger=info['trigger'], channels=len(src['channels'])),
                padding={str(channel): sorted(plan) for channel, plan in plans.items()},
                rowsWritten=written_rows,
                elapsedSeconds=round(time.time() - started, 3),
                note='逐段拼接的物理输入；段边界为显式会话复位，非连续真机 trace')


# ---------------------------------------------------------------------------------------------
# CSV：来源读取 / 拼接
# ---------------------------------------------------------------------------------------------

def detect_csv_encoding(path):
    path = Path(path)
    with path.open('rb') as handle:
        prefix = handle.read(4)
    candidates = ['utf-16'] if prefix.startswith((b'\xff\xfe', b'\xfe\xff')) \
        else ['utf-8-sig', 'gb18030']
    for encoding in candidates:
        try:
            with path.open(encoding=encoding, newline='') as handle:
                header = handle.readline()
            return encoding, header
        except UnicodeDecodeError:
            continue
    raise BuildError('CSV 表头编码无法识别：%s' % path)


def csv_geometry(src, record_dir, session, *, seconds=None):
    """CSV 拼接需要 depth/rate/trigger；优先同记录目录 KVDAT 布局，其次 session/--seconds。"""
    kvdat = Path(record_dir) / 'capture.kvdat'
    if kvdat.is_file():
        kingst_record = _import_kingst_record()
        info = kingst_record.layout(kvdat)
        return dict(depth=int(info['depth']), rate=int(info['rate']),
                    trigger=int(info['trigger']), basis='capture.kvdat layout')
    rate = session.get('rate')
    span = seconds if seconds is not None else session.get('seconds')
    if not rate or not span:
        raise BuildError('CSV 来源缺少 KVDAT 布局与 session.json（rate/seconds）；'
                         '请用 --seconds 显式指定段长')
    return dict(depth=int(round(float(rate) * float(span))), rate=int(rate), trigger=0,
                basis='session.json' if seconds is None else '--seconds')


def _csv_rows(path, encoding, *, validate=True):
    """逐行产出 (时间列, 电平列)；位置 = 时间列 × 采样率 + 触发位置（须落在采样网格上）。

    ``validate=False`` 用于重复的写盘遍：跳过 0/1 校验与表头列数校验（第一遍已经校验过），
    用一次 ``replace`` 去空格换取约 2～3 倍吞吐。
    """
    with path.open(encoding=encoding, newline='') as handle:
        header = handle.readline()
        if not header.strip():
            raise BuildError('CSV 没有表头：%s' % path)
        fields = [field.strip() for field in header.rstrip('\r\n').split(',')]
        if len(fields) < 2:
            raise BuildError('CSV 表头列数不足：%r' % header[:80])
        for line in handle:
            text = line.rstrip('\r\n')
            if not text:
                raise BuildError('CSV 出现空行，拒绝静默跳过：%s' % path)
            if not validate:
                parts = text.replace(' ', '').split(',')
                if len(parts) != len(fields):
                    raise BuildError('CSV 行列数与表头不一致：%r' % line[:80])
                try:
                    timestamp = float(parts[0])
                except ValueError:
                    raise BuildError('CSV 首列不是秒数：%r' % parts[0]) from None
                yield timestamp, parts[1:]
                continue
            parts = text.split(',')
            if len(parts) != len(fields):
                raise BuildError('CSV 行列数与表头不一致：%r' % line[:80])
            try:
                timestamp = float(parts[0])
            except ValueError:
                raise BuildError('CSV 首列不是秒数：%r' % parts[0]) from None
            levels = [value.strip() for value in parts[1:]]
            if any(value not in ('0', '1') for value in levels):
                raise BuildError('CSV 电平不是 0/1：%r' % line[:80])
            yield timestamp, levels


def csv_write_codec(path, encoding):
    """返回 (逐行编码, 文件头 BOM 字节)。

    ``utf-8-sig``/``utf-16`` 这类编码名在**每次** ``str.encode()`` 时都会写 BOM，逐行拼接会得到
    每行一个 BOM 的坏文件；这里拆成「基础编码 + 只写一次的文件头 BOM」。
    """
    with Path(path).open('rb') as handle:
        prefix = handle.read(4)
    if encoding == 'utf-8-sig':
        return 'utf-8', b'\xef\xbb\xbf'
    if encoding == 'utf-16':
        if prefix.startswith(b'\xff\xfe'):
            return 'utf-16-le', b'\xff\xfe'
        if prefix.startswith(b'\xfe\xff'):
            return 'utf-16-be', b'\xfe\xff'
        raise BuildError('UTF-16 CSV 缺少 BOM，无法确定字节序：%s' % path)
    return encoding, b''


def build_physical_csv(src, segments, out_path, *, geometry, progress=None):
    """把来源 CSV（跳变表）按段拼接：时间列按整数采样网格重算，保证跨段可复现。"""
    src_path = Path(src['path'])
    encoding, header = detect_csv_encoding(src_path)
    depth = geometry['depth']
    rate = geometry['rate']
    trigger = geometry['trigger']
    total_depth = depth * segments
    if total_depth >= 2 ** 32:
        raise BuildError('拼接后采样深度超过 32 位上限；请减少段数')
    # 第一遍：只校验网格与行数，不驻留整表（200 ms CSV 有近两千万行）
    first_position = None
    last_position = None
    rows = 0
    for timestamp, _levels in _csv_rows(src_path, encoding):
        scaled = timestamp * rate + trigger
        position = int(round(scaled))
        if abs(scaled - position) > 0.25:
            raise BuildError('CSV 时间不在采样网格上（%.6f 采样偏移）：%r'
                             % (scaled - position, timestamp))
        if last_position is not None and position < last_position:
            raise BuildError('CSV 时间倒序：%r' % timestamp)
        if first_position is None:
            first_position = position
        last_position = position
        rows += 1
    if not rows:
        raise BuildError('CSV 没有数据行：%s' % src_path)
    if depth - 1 < last_position - first_position:
        raise BuildError('CSV 跨度 %d 超过段深 %d' % (last_position - first_position, depth))
    if first_position > last_position:
        raise BuildError('CSV 时间倒序：%s' % src_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    segments_meta = []
    total_rows = 0
    line_codec, bom = csv_write_codec(src_path, encoding)
    header_bytes = bom + header.encode(line_codec)
    if not header_bytes.endswith(b'\n'):
        header_bytes += b'\n'
    last_bytes = None
    with out_path.open('wb') as out:
        out.write(header_bytes)
        for index in range(segments):
            byte_offset = out.tell()
            base = index * depth
            written = 0
            for timestamp, levels in _csv_rows(src_path, encoding, validate=False):
                position = int(round(timestamp * rate + trigger)) + base
                seconds = (position - trigger) / rate
                encoded = (', '.join((repr(seconds), *levels)) + '\n').encode(line_codec)
                out.write(encoded)
                last_bytes = encoded
                written += 1
            if written != rows:
                raise BuildError('CSV 段 %d 行数变化：%d≠%d（来源在构造期间被修改）'
                                 % (index, written, rows))
            segments_meta.append(dict(index=index, byteOffset=byte_offset, rows=written,
                                      sampleOffset=base, lastLineBytes=len(last_bytes),
                                      lastLineSha256=hashlib.sha256(last_bytes).hexdigest()))
            total_rows += written
            if progress is not None:
                progress('  CSV：段 %d/%d 完成（累计 %d 行）' % (index + 1, segments, total_rows))
            if index + 1 < segments:
                gap_ns = (((base + depth + first_position) - trigger) / rate
                          - ((base + last_position) - trigger) / rate) * 1e9
                if gap_ns <= 0:
                    raise BuildError('段 %d→%d 拼接后时间不递增（%.3f ns）'
                                     % (index, index + 1, gap_ns))
    stat = out_path.stat()
    for position, segment in enumerate(segments_meta):
        next_offset = segments_meta[position + 1]['byteOffset'] \
            if position + 1 < len(segments_meta) else stat.st_size
        segment['blockBytes'] = next_offset - segment['byteOffset']
    return dict(format='csv', path=str(out_path.resolve()), bytes=stat.st_size,
                sha256=sha256_file(out_path), encoding=encoding,
                layout=dict(depth=total_depth, unitDepth=depth, rate=rate, trigger=trigger,
                            firstSample=first_position, lastSample=last_position),
                segments=segments_meta, rowsWritten=total_rows,
                elapsedSeconds=round(time.time() - started, 3),
                note='逐段拼接的物理输入；段边界为显式会话复位，非连续真机 trace')


# ---------------------------------------------------------------------------------------------
# 物理校验
# ---------------------------------------------------------------------------------------------

def verify_kvdat_rows(synth_path, src, segments, *, fraction, progress=None):
    """逐段核对合成 KVDAT 的边沿行：位置=源位置+段偏移，补边符合计划，末哨兵=总深度。"""
    numpy = _import_numpy()
    kingst_record = _import_kingst_record()
    synth_path = Path(synth_path)
    info = kingst_record.layout(synth_path)
    depth = src['info']['depth']
    total_depth = depth * segments
    if info['depth'] != total_depth:
        raise BuildError('合成 KVDAT 深度不符：%d≠%d' % (info['depth'], total_depth))
    if info['rate'] != src['info']['rate'] or info['trigger'] != src['info']['trigger']:
        raise BuildError('合成 KVDAT 采样率/触发位置与来源不符')
    dtype = _rows_dtype()
    plans = channel_padding_plans(src, segments, fraction)
    checked_rows = 0
    channels_report = {}
    with synth_path.open('rb') as handle:
        for channel in sorted(src['channels']):
            block = info['channels'][channel]
            channel_info = src['channels'][channel]
            positions = channel_info['positions']
            plan = plans[channel]
            expected_rows = len(positions) * segments + len(plan) + 1
            if block['count'] != expected_rows:
                raise BuildError('通道 %d 行数不符：%d≠%d'
                                 % (channel, block['count'], expected_rows))
            if block['initial'] != channel_info['initial']:
                raise BuildError('通道 %d 首电平不符：%d≠%d'
                                 % (channel, block['initial'], channel_info['initial']))
            cursor = block['offset']
            for index in range(segments):
                if index in plan:
                    handle.seek(cursor)
                    rows = numpy.fromfile(handle, dtype=dtype, count=1)
                    if int(rows['position'][0]) != plan[index] or int(rows['flag'][0]) != 0:
                        raise BuildError('通道 %d 段 %d 边界补边不符' % (channel, index))
                    cursor += 5
                    checked_rows += 1
                if len(positions):
                    handle.seek(cursor)
                    rows = numpy.fromfile(handle, dtype=dtype, count=len(positions))
                    expected = positions + numpy.uint32(index * depth)
                    if len(rows) != len(positions) \
                            or not numpy.array_equal(rows['position'], expected) \
                            or numpy.any(rows['flag']):
                        raise BuildError('通道 %d 段 %d 边沿与来源不符' % (channel, index))
                    cursor += len(positions) * 5
                    checked_rows += len(positions)
            handle.seek(cursor)
            rows = numpy.fromfile(handle, dtype=dtype, count=1)
            if len(rows) != 1 or int(rows['position'][0]) != total_depth \
                    or int(rows['flag'][0]) != 0:
                raise BuildError('通道 %d 结束哨兵不符（应为深度 %d）' % (channel, total_depth))
            checked_rows += 1
            channels_report[str(channel)] = dict(rows=int(block['count']),
                                                 padding=len(plan),
                                                 realEdges=len(positions))
            if progress is not None:
                progress('  逐段行核对：通道 %d 完成（%d 行）' % (channel, block['count']))
    return dict(ok=True, format='kvdat', depth=total_depth, unitDepth=depth, segments=segments,
                channels=channels_report, rowsChecked=checked_rows, sentinel=total_depth,
                padding={str(key): value for key, value in plans.items()},
                note='逐段行比对：每段边沿位置=源边沿+段偏移；段首电平由补边保持')


def _write_kvdat_segment(src, synth_path, info, segment_index, out_path, *, padding):
    """从合成 KVDAT 抽取一段，重排成独立 KVDAT（回卷到源采样坐标系）。"""
    numpy = _import_numpy()
    depth = src['info']['depth']
    dtype = _rows_dtype()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    channels = {}
    synth_layout = info['channels']
    base = segment_index * depth
    with Path(synth_path).open('rb') as source, out_path.open('wb') as out:
        out.write(src['prefix'])
        out.write(KVDAT_MAGIC)
        out.write(FILE_HEADER.pack(depth, src['info']['rate'], src['info']['trigger'],
                                   len(src['channels'])))
        for channel in sorted(src['channels']):
            channel_info = src['channels'][channel]
            block = synth_layout[channel]
            positions = channel_info['positions']
            real = len(positions)
            plan = padding.get(channel, {}) or {}
            out.write(CHANNEL_HEADER.pack(KVDAT_CHANNEL_MAGIC, channel,
                                          channel_info['initial'], 0, real + 1))
            padding_before = sum(1 for index in plan if index < segment_index)
            cursor = block['offset'] + (segment_index * real + padding_before) * 5
            if segment_index in plan:
                cursor += 5                           # 跳过段首补边
            source.seek(cursor)
            chunk = numpy.fromfile(source, dtype=dtype, count=real)
            if len(chunk) != real:
                raise BuildError('抽取段 %d 通道 %d 时截断' % (segment_index, channel))
            expected = positions + numpy.uint32(base)
            if not numpy.array_equal(chunk['position'], expected):
                raise BuildError('抽取段 %d 通道 %d 边沿与来源不符' % (segment_index, channel))
            out_rows = numpy.empty(len(positions), dtype=dtype)
            out_rows['position'] = positions
            out_rows['flag'] = 0
            out.write(out_rows.tobytes())
            sentinel = numpy.zeros(1, dtype=dtype)
            sentinel['position'][0] = depth
            sentinel['flag'][0] = 0
            out.write(sentinel.tobytes())
            channels[str(channel)] = dict(edges=len(positions), initial=channel_info['initial'])
    return dict(path=str(out_path.resolve()), bytes=out_path.stat().st_size,
                sha256=sha256_file(out_path), segment=segment_index,
                sampleOffset=base,
                timeSeconds=(base - src['info']['trigger']) / src['info']['rate'],
                channels=channels)


def _write_csv_segment(src, synth_path, out_path, *, segment_meta, encoding, header,
                       line_codec, bom, rate, trigger):
    """从合成 CSV 抽取一段，时间轴回卷到源采样坐标系，写成独立 CSV。

    回卷后段文件的时间列与来源段逐行同值（采样位置相同），因此现前端取样报告可与来源逐项比较。
    """
    synth_path = Path(synth_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    start = int(segment_meta['byteOffset'])
    count = int(segment_meta['rows'])
    base = int(segment_meta['sampleOffset'])
    raw = synth_path.open('rb')
    try:
        raw.seek(start)
        payload = raw.read(int(segment_meta['blockBytes']))
    finally:
        raw.close()
    lines = payload.splitlines(keepends=True)
    if len(lines) < count:
        raise BuildError('CSV 段 %d 行数不足：%d≠%d' % (segment_meta['index'], len(lines), count))
    header_bytes = bom + header.encode(line_codec)
    if not header_bytes.endswith(b'\n'):
        header_bytes += b'\n'
    out_lines = []
    for line in lines[:count]:
        parts = line.decode(encoding).rstrip('\r\n').split(',')
        if len(parts) < 2:
            raise BuildError('CSV 行列数不足：%r' % line[:80])
        position = int(round(float(parts[0]) * rate + trigger)) - base
        if position < 0:
            raise BuildError('CSV 段 %d 行位置回卷为负：%d' % (segment_meta['index'], position))
        seconds = (position - trigger) / rate
        levels = [value.strip() for value in parts[1:]]
        out_lines.append((', '.join((repr(seconds), *levels)) + '\n').encode(line_codec))
    out_path.write_bytes(header_bytes + b''.join(out_lines))
    return dict(path=str(out_path.resolve()), bytes=out_path.stat().st_size,
                sha256=sha256_file(out_path), segment=segment_meta['index'], rows=count,
                sampleOffset=base)


_SKIPPED_REPORT_KEYS = ('input', 'input_sha256', 'candidates')


def _compare_physical_reports(reference, candidate, *, where):
    """逐项比较物理取样报告。

    段文件已把时间轴回卷到源采样坐标系（KVDAT/CSV 都回卷），因此除输入路径/指纹与
    ``candidates[].file`` 外，全部字段都必须逐项相等；不做"只比几个字段"的宽松比较。
    """
    differences = {}
    keys = sorted(set(reference) | set(candidate))
    for key in keys:
        if key in _SKIPPED_REPORT_KEYS:
            continue
        if reference.get(key) != candidate.get(key):
            differences[key] = dict(reference=reference.get(key), candidate=candidate.get(key))
    left = [{key: value for key, value in item.items() if key not in ('file', 'path')}
            for item in reference.get('candidates') or []]
    right = [{key: value for key, value in item.items() if key not in ('file', 'path')}
             for item in candidate.get('candidates') or []]
    if left != right:
        differences['candidates'] = dict(reference=left, candidate=right)
    if differences:
        raise BuildError('物理取样报告不一致（%s）：%r' % (where, differences))
    return dict(compared=[key for key in keys if key not in _SKIPPED_REPORT_KEYS],
                skipped=list(_SKIPPED_REPORT_KEYS))


def verify_physical_segments(src, synth_path, segments, work_dir, *, sampling, physical,
                             selection=None, progress=None):
    """逐段等价：把合成物理输入按复位段切分，与来源逐段比较取样字节流。

    方法：从**合成文件**里抽取该段边沿/行，重排成独立段文件（KVDAT 回卷到源采样坐标），再用
    现前端 ``trace_port.extract`` 对来源与段文件各取样一次，比较两路相位字节流与平移不变的
    物理统计。KVDAT 段文件额外与来源文件做整文件 SHA256 比较（应逐字节一致）。
    """
    trace_port = _import_trace_port()
    synth_path = Path(synth_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    reference_report, reference_streams = trace_port.extract(
        src['path'], sampling['clock'], sampling['data'], sampling['fraction'],
        sampling['glitchNs'])
    if selection is None:
        selection = list(range(segments))
    kingst_record = _import_kingst_record()
    info = kingst_record.layout(synth_path) if physical['format'] == 'kvdat' else None
    plans = channel_padding_plans(src, segments, sampling['fraction']) \
        if physical['format'] == 'kvdat' else None
    encoding = header = None
    line_codec = ''
    bom = b''
    if physical['format'] == 'csv':
        encoding, header = detect_csv_encoding(src['path'])
        line_codec, bom = csv_write_codec(src['path'], encoding)
    results = []
    compared_keys = []
    skipped_keys = []
    started = time.time()
    for index in selection:
        if physical['format'] == 'kvdat':
            segment_path = work_dir / ('segment-%04d.kvdat' % index)
            meta = _write_kvdat_segment(src, synth_path, info, index, segment_path,
                                        padding=plans)
            byte_identical = meta['sha256'] == src['sha256'] and meta['bytes'] == src['bytes']
        else:
            segment_meta = physical['segments'][index]
            segment_path = work_dir / ('segment-%04d.csv' % index)
            meta = _write_csv_segment(src, synth_path, segment_path,
                                      segment_meta=segment_meta, encoding=encoding,
                                      header=header, line_codec=line_codec, bom=bom,
                                      rate=physical['layout']['rate'],
                                      trigger=physical['layout']['trigger'])
            byte_identical = None
        report, streams = trace_port.extract(segment_path, sampling['clock'], sampling['data'],
                                             sampling['fraction'], sampling['glitchNs'])
        compared = _compare_physical_reports(reference_report, report, where='段 %d' % index)
        if not compared_keys:
            compared_keys = compared['compared']
            skipped_keys = compared['skipped']
        for phase in (0, 1):
            if reference_streams[phase] != streams[phase]:
                raise BuildError('段 %d 的 phase%d 字节流与来源不一致（%d vs %d 字节）'
                                 % (index, phase, len(reference_streams[phase]),
                                    len(streams[phase])))
        results.append(dict(index=index, sampleOffset=meta['sampleOffset'],
                            segmentSha256=meta['sha256'], segmentBytes=meta['bytes'],
                            byteIdenticalToSource=byte_identical,
                            phaseSha256=[hashlib.sha256(item).hexdigest() for item in streams],
                            phaseBytes=[len(item) for item in streams]))
        segment_path.unlink()
        if progress is not None and len(results) % 10 == 0:
            progress('  逐段等价：已校验 %d/%d 段' % (len(results), len(selection)))
    if physical['format'] == 'kvdat' and any(item['byteIdenticalToSource'] is not True
                                             for item in results):
        raise BuildError('KVDAT 段文件与来源文件不是逐字节一致')
    return dict(ok=True, format=physical['format'], segments=segments,
                verified=[item['index'] for item in results], segmentCount=len(results),
                referencePhaseSha256=[hashlib.sha256(item).hexdigest()
                                      for item in reference_streams],
                referencePhaseBytes=[len(item) for item in reference_streams],
                perSegment=results, comparedKeys=compared_keys, skippedKeys=skipped_keys,
                method=('从合成物理文件按复位段抽取独立段文件（时间轴回卷到源采样坐标），'
                        '用 trace_port.extract 逐段取样，逐项比较物理报告与 phase0/phase1 字节'),
                elapsedSeconds=round(time.time() - started, 3), peakRssBytes=peak_rss_bytes())


def verify_csv_rows(physical, segments):
    """CSV 逐段核对：段首行位置、段末行哈希/行数、块字节边界（每段 2 次定位读取）。"""
    path = Path(physical['path'])
    encoding, header = detect_csv_encoding(path)
    rate = physical['layout']['rate']
    trigger = physical['layout']['trigger']
    size = path.stat().st_size
    rows_total = 0
    with path.open('rb') as handle:
        for segment in physical['segments']:
            start = int(segment['byteOffset'])
            block = int(segment['blockBytes'])
            if start + block > size:
                raise BuildError('CSV 段 %d 越过文件末尾' % segment['index'])
            handle.seek(start)
            first = handle.readline().decode(encoding).strip()
            if not first:
                raise BuildError('CSV 段 %d 首行为空' % segment['index'])
            position = int(round(float(first.split(',')[0]) * rate)) + trigger
            if position != segment['sampleOffset']:
                raise BuildError('CSV 段 %d 首行位置不符：%d≠%d'
                                 % (segment['index'], position, segment['sampleOffset']))
            handle.seek(start + block - int(segment['lastLineBytes']))
            last = handle.readline()
            if hashlib.sha256(last).hexdigest() != segment['lastLineSha256']:
                raise BuildError('CSV 段 %d 末行哈希不符' % segment['index'])
            rows_total += int(segment['rows'])
    return dict(ok=True, format='csv', segments=segments, rows=rows_total, encoding=encoding,
                note='按段核对首行位置、末行哈希与块字节边界；逐段取样等价见 sampling 报告')


# ---------------------------------------------------------------------------------------------
# 元数据
# ---------------------------------------------------------------------------------------------

def _merge_metadata(path, section, value):
    path = Path(path)
    if path.is_file():
        meta = _load_json(path)
    else:
        meta = dict(schema=SCHEMA, declaration=DECLARATION,
                    generator='tools/synthetic/build_synthetic.py', generatedAt=None,
                    command=None, source=None, physical=None, events=None)
    meta[section] = value
    meta['generatedAt'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    meta['command'] = ' '.join(sys.argv)
    _write_json(path, meta)
    return meta


def _source_fingerprints(record_dir):
    record_dir = Path(record_dir)
    files = {}
    for name in ('capture.kvdat', 'capture.csv', 'capture.csv.la.json', 'etm.bin',
                 'firmware.elf', 'native-config.txt', 'session.json', 'sources.json',
                 'decoded.json', 'events.jsonl', 'physical.json', 'framing.json'):
        path = record_dir / name
        if path.is_file():
            files[name] = file_fingerprint(path)
    return files


def _physical_segments(physical, segments, src):
    if physical['format'] == 'kvdat':
        depth = src['info']['depth']
        rate = src['info']['rate']
        trigger = src['info']['trigger']
        return [dict(index=index, format='kvdat', sampleOffset=index * depth,
                     timeSeconds=(index * depth - trigger) / rate, seconds=depth / rate,
                     reset=index > 0,
                     resetSampleOffset=index * depth if index else None,
                     sourceSampleSpan=[0, depth])
                for index in range(segments)]
    rate = physical['layout']['rate']
    trigger = physical['layout']['trigger']
    unit = physical['layout']['unitDepth']
    rows = []
    for item in physical['segments']:
        row = dict(item)
        row.update(format='csv',
                   timeSeconds=(item['sampleOffset'] - trigger) / rate,
                   seconds=unit / rate, reset=item['index'] > 0,
                   resetSampleOffset=item['sampleOffset'] if item['index'] else None,
                   sourceSampleSpan=[0, unit])
        rows.append(row)
    return rows


def _session_record(record_dir, session, physical, segments, sampling):
    session_path = Path(record_dir) / 'session.json'
    return dict(
        schema=SCHEMA, synthetic=True, continuousRealTrace=False,
        sourceSession=str(session_path.resolve()) if session_path.is_file() else None,
        name=(session.get('name') or '') + '（合成拼接，非真机连续 trace）',
        input=Path(physical['path']).name,
        rate=physical['layout']['rate'],
        seconds=physical['layout']['depth'] / physical['layout']['rate'],
        segments=segments, actualDepth=physical['layout']['depth'],
        actualTime=physical['layout']['depth'] / physical['layout']['rate'],
        clock=sampling['clock'], data=sampling['data'], fraction=sampling['fraction'],
        glitchNs=sampling['glitchNs'], declaration=DECLARATION,
    )


def _parse_selection(value, segments):
    if value in (None, 'all'):
        return list(range(segments))
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise BuildError('--verify-segments 只能是 all 或正整数：%r' % (value,))
    if count < 1:
        raise BuildError('--verify-segments 必须 ≥1')
    if count >= segments:
        return list(range(segments))
    picks = {0, segments - 1}
    step = (segments - 1) / (count - 1) if count > 1 else 0
    for index in range(count):
        picks.add(int(round(index * step)))
    return sorted(picks)


# ---------------------------------------------------------------------------------------------
# 子命令：physical / events / split
# ---------------------------------------------------------------------------------------------

def run_physical(args, out_dir, result):
    source = resolve_physical_source(args.record, args.kind)
    record_dir = source['recordDir']
    if not record_dir.is_dir():
        raise BuildError('--record 的父目录不是记录目录：%s' % record_dir)
    session = load_session(record_dir)
    sampling = resolve_sampling(record_dir, session, args)
    segments = int(args.segments)
    physical_dir = out_dir / 'physical'
    if args.force and physical_dir.is_dir():
        shutil.rmtree(physical_dir)
    physical_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    if source['format'] == 'kvdat':
        src = load_kvdat_source(source['path'])
        out_path = physical_dir / 'capture-synthetic-1s.kvdat'
        physical = build_physical_kvdat(src, segments, out_path,
                                        fraction=sampling['fraction'],
                                        progress=_printer(args))
    else:
        geometry = csv_geometry(source, record_dir, session, seconds=args.seconds)
        src = dict(path=source['path'], bytes=Path(source['path']).stat().st_size,
                   sha256=sha256_file(source['path']),
                   info=dict(depth=geometry['depth'], rate=geometry['rate'],
                             trigger=geometry['trigger']),
                   channels={}, prefix=None)
        out_path = physical_dir / 'capture-synthetic-1s.csv'
        physical = build_physical_csv(src, segments, out_path, geometry=geometry,
                                      progress=_printer(args))
    physical['buildSeconds'] = round(time.time() - started, 3)
    physical['segments'] = _physical_segments(physical, segments, src)
    physical['sampling'] = sampling
    _write_json(physical_dir / 'segments.json',
                dict(schema=SCHEMA, declaration=DECLARATION, synthetic=True,
                     physical=dict(path=physical['path'], bytes=physical['bytes'],
                                   sha256=physical['sha256'], format=physical['format'],
                                   layout=physical['layout']),
                     segments=physical['segments'],
                     resets=[row for row in physical['segments'] if row['reset']]))
    _write_json(physical_dir / 'session-synthetic.json',
                _session_record(record_dir, session, physical, segments, sampling))
    result['physical'] = physical
    verify_dir = out_dir / 'verify'
    if args.verify in ('layout', 'all') and physical['format'] == 'kvdat':
        kingst_record = _import_kingst_record()
        layout = kingst_record.layout(physical['path'])
        if layout['depth'] != physical['layout']['depth']:
            raise BuildError('合成 KVDAT 深度与声明不符：%d≠%d'
                             % (layout['depth'], physical['layout']['depth']))
        report = dict(ok=True, layoutAccepted=True,
                      layout=dict(depth=layout['depth'], rate=layout['rate'],
                                  trigger=layout['trigger'],
                                  channels={str(key): value['count']
                                            for key, value in layout['channels'].items()}),
                      note='kingst_record.layout() 接受合成 KVDAT')
        verify_dir.mkdir(parents=True, exist_ok=True)
        _write_json(verify_dir / 'physical-layout.json', report)
        result['physicalLayout'] = report
    if args.verify in ('segments', 'all'):
        if physical['format'] == 'kvdat':
            rows = verify_kvdat_rows(physical['path'], src, segments,
                                     fraction=sampling['fraction'], progress=_printer(args))
        else:
            rows = verify_csv_rows(physical, segments)
        selection = _parse_selection(args.verify_segments, segments)
        compare = verify_physical_segments(src, physical['path'], segments,
                                           verify_dir / 'segment-files', sampling=sampling,
                                           physical=physical, selection=selection,
                                           progress=_printer(args))
        verify_dir.mkdir(parents=True, exist_ok=True)
        report = dict(ok=True, declaration=DECLARATION, rows=rows, sampling=compare)
        _write_json(verify_dir / 'physical-verify.json', report)
        result['physicalVerify'] = report
    meta_physical = dict(format=physical['format'], path=physical['path'],
                         bytes=physical['bytes'], sha256=physical['sha256'],
                         encoding=physical.get('encoding'),
                         layout=physical['layout'], sampling=sampling,
                         segments=physical['segments'],
                         resets=[row for row in physical['segments'] if row['reset']],
                         padding=physical.get('padding'),
                         buildSeconds=physical.get('buildSeconds'),
                         elapsedSeconds=physical.get('elapsedSeconds'),
                         rowsWritten=physical.get('rowsWritten'))
    _merge_metadata(out_dir / 'synthetic.json', 'physical', meta_physical)
    source_meta = dict(record=str(record_dir.resolve()),
                       physical=dict(path=str(source['path']),
                                     bytes=Path(source['path']).stat().st_size,
                                     sha256=sha256_file(source['path'])),
                       files=_source_fingerprints(record_dir))
    _merge_metadata(out_dir / 'synthetic.json', 'source', source_meta)
    return result


def run_events(args, out_dir, result):
    record_dir = Path(args.record)
    if not record_dir.is_dir():
        raise BuildError('--mode events 的 --record 必须是记录目录（含 decoded.json/'
                         'events.jsonl）：%s' % record_dir)
    segments = int(args.segments)
    paths = unit_paths(out_dir)
    Path(paths['dir']).mkdir(parents=True, exist_ok=True)
    try:
        physical_source = resolve_physical_source(record_dir, args.kind)
    except BuildError:
        physical_source = None
    unit = None
    if not args.rebuild_unit and Path(paths['meta']).is_file():
        try:
            unit = load_unit(paths['spool'], paths['meta'])
            if unit.get('sourceSha256') != sha256_file(unit['sourceFile']):
                raise BuildError('单元 spool 与来源 sha256 不一致')
            _print(args, '复用单元 spool：%s' % paths['spool'])
        except BuildError:
            unit = None
    started = time.time()
    if unit is None:
        _print(args, '抽取单元事件流（来源记录：%s）…' % record_dir)
        unit = build_unit_spool(record_dir, paths['spool'], source=args.events_source,
                                meta_path=paths['meta'],
                                decoded_meta_path=paths['decodedMeta'],
                                progress=_printer(args))
        result['unitBuildSeconds'] = round(time.time() - started, 3)
    unit['physicalSource'] = dict(path=str(physical_source['path']),
                                  format=physical_source['format']) \
        if physical_source else None
    stats = stream_stats(unit, segments, markers=args.markers, offset_span=args.offset_span)
    result['unit'] = unit
    result['streamStats'] = stats

    events_dir = out_dir / 'events-record'
    if args.force and events_dir.is_dir():
        shutil.rmtree(events_dir)
    events_dir.mkdir(parents=True, exist_ok=True)
    record = None
    stream = None
    if args.event_sink == 'jsonl':
        out_path = events_dir / 'events.jsonl'
        if out_path.exists():
            out_path.unlink()
        _print(args, '生成 1 s 规模事件流（%d 段：%d 条事件 / %d 条指令 / %d 个复位标记）…'
               % (segments, stats['totalEvents'], stats['totalInstructions'],
                  stats['resetCount']))
        stream = write_event_stream(unit, segments, out_path, markers=args.markers,
                                    offset_span=args.offset_span, progress=_printer(args))
        record = write_events_record_dir(unit, segments, events_dir, events_path=out_path,
                                         markers=args.markers, offset_span=args.offset_span)
    elif args.event_sink == 'shards':
        _print(args, '生成 1 s 规模事件流分片（%d 段：%d 行）…'
               % (segments, stats['totalEvents']))
        stream = write_event_shards(unit, segments, out_dir / 'events-shards',
                                    shard_events=args.event_shard_events,
                                    markers=args.markers, offset_span=args.offset_span,
                                    progress=_printer(args))
    elif args.event_sink == 'none':
        _print(args, '流式生成但不落盘（只统计条数与哈希）…')
        digest = hashlib.sha256()
        lines = 0
        for line in iter_event_lines(unit, segments, offset_span=args.offset_span,
                                     markers=args.markers):
            digest.update(line)
            lines += 1
        stream = dict(path=None, bytes=None, sha256=digest.hexdigest(), lines=lines)
    result['events'] = dict(unit=unit, stats=stats, stream=stream, record=record,
                            sink=args.event_sink)

    verify_dir = out_dir / 'verify'
    if args.verify in ('events', 'segments', 'all') and args.event_sink != 'none':
        _print(args, '自检事件流（seq 连续性 / 段边界 / 复位标记 / 抽样 JSON 比对）…')
        target = stream if args.event_sink == 'shards' else stream['path']
        report = verify_event_stream(target, unit, segments, offset_span=args.offset_span,
                                     markers=args.markers, sample_every=args.sample_every,
                                     progress=_printer(args))
        if record:
            report['record'] = record
        verify_dir.mkdir(parents=True, exist_ok=True)
        _write_json(verify_dir / 'events-verify.json', report)
        result['eventsVerify'] = report

    reset_table = expected_reset_table(unit, segments, stats['offsetSpan'])
    meta_events = dict(
        unit=unit, stats=stats, sink=args.event_sink,
        stream=dict(path=stream.get('path'), bytes=stream.get('bytes'),
                    sha256=stream.get('sha256'), lines=stream.get('lines'),
                    elapsedSeconds=stream.get('elapsedSeconds'),
                    shards=[dict(index=item['index'], name=item['name'],
                                 bytes=item['bytes'], sha256=item['sha256'],
                                 lines=item['lines'], firstSeq=item['firstSeq'],
                                 lastSeq=item['lastSeq'])
                            for item in stream.get('shards', [])]),
        record=record,
        resets=[dict(segmentIndex=row['segmentIndex'], seq=row['seq'], offset=row['offset'],
                     sampleOffset=row['sampleOffset'], type=row['type'], payload=row['payload'],
                     text=row['text'], reason=row['reason'])
                for row in reset_table],
    )
    _merge_metadata(out_dir / 'synthetic.json', 'events', meta_events)
    meta_path = out_dir / 'synthetic.json'
    if not meta_path.is_file() or not meta_path.stat().st_size:
        raise BuildError('内部错误：synthetic.json 未生成')
    source_meta = dict(record=str(record_dir.resolve()), files=_source_fingerprints(record_dir))
    if physical_source is not None:
        source_meta['physical'] = dict(path=str(physical_source['path']),
                                       bytes=Path(physical_source['path']).stat().st_size,
                                       sha256=sha256_file(physical_source['path']))
    _merge_metadata(out_dir / 'synthetic.json', 'source', source_meta)
    return result


def run_store_proof(args, out_dir):
    """兼容性证明：用 trace-store v1 参考写入器直接消费合成记录目录。"""
    record_dir = Path(args.record) if args.record else None
    if record_dir is None or not record_dir.is_dir():
        raise BuildError('--mode store-proof 需要 --record 指向合成的 streamed 记录目录')
    events_path = record_dir / 'events.jsonl'
    if not events_path.is_file():
        raise BuildError('记录目录没有 events.jsonl（--event-sink 必须是 jsonl）：%s' % record_dir)
    _ensure_etm_tools()
    import trace_store
    store_dir = out_dir / 'store'
    if store_dir.is_dir():
        shutil.rmtree(store_dir)
    started = time.time()
    report = dict(mode='store-proof', recordDir=str(record_dir.resolve()),
                  writer='etm/tools/trace_store.py（trace-store v1 参考写入器，只读引用）',
                  note='证明合成事件流可被现有 store 写入器直接消费；本步不评价 P3 性能')
    written = trace_store.write(record_dir, store_dir, progress=_printer(args))
    report['elapsedSeconds'] = round(time.time() - started, 3)
    report['storeBytes'] = written.get('storeBytes') or sum(
        path.stat().st_size for path in store_dir.rglob('*') if path.is_file())
    report['storeSha256'] = written.get('storeSha256')
    report['generation'] = written.get('generation')
    report['peakRssBytes'] = written.get('peakRssBytes') or peak_rss_bytes()
    report['counts'] = written.get('counts')
    report['issues'] = written.get('issues')
    report['elementsVerified'] = written.get('elementsVerified')
    report['ok'] = True
    _write_json(out_dir / 'store-proof.json', report)
    return report


def run_split(args, out_dir):
    meta_path = Path(args.meta) if args.meta else out_dir / 'synthetic.json'
    if not meta_path.is_file():
        raise BuildError('缺少 synthetic.json：%s' % meta_path)
    meta = _load_json(meta_path)
    physical = meta.get('physical') or {}
    if not physical.get('path') or not Path(physical['path']).is_file():
        raise BuildError('synthetic.json 里没有可用的物理输入')
    if args.segments is not None:
        raise BuildError('--mode split 不需要 --segments（段表来自 synthetic.json）')
    source_record = Path((meta.get('source') or {}).get('record') or '')
    if not source_record.is_dir():
        raise BuildError('synthetic.json 的来源记录目录不存在：%s' % source_record)
    session = load_session(source_record)
    sampling = physical.get('sampling') or resolve_sampling(source_record, session, args)
    segments = len(physical['segments'])
    out_dir.mkdir(parents=True, exist_ok=True)
    if physical['format'] == 'kvdat':
        src = load_kvdat_source(source_record / 'capture.kvdat')
        synth_path = Path(physical['path'])
        info = _import_kingst_record().layout(synth_path)
        plans = channel_padding_plans(src, segments, sampling['fraction'])
        written = []
        for index in range(segments):
            path = out_dir / ('segment-%04d.kvdat' % index)
            written.append(_write_kvdat_segment(src, synth_path, info, index, path,
                                                padding=plans))
        return dict(mode='split', format='kvdat', segments=written,
                    identicalToSource=all(item['sha256'] == src['sha256']
                                          for item in written))
    encoding, header = detect_csv_encoding(source_record / 'capture.csv')
    line_codec, bom = csv_write_codec(source_record / 'capture.csv', encoding)
    src = dict(path=source_record / 'capture.csv')
    written = []
    for item in physical['segments']:
        path = out_dir / ('segment-%04d.csv' % item['index'])
        written.append(_write_csv_segment(src, physical['path'], path, segment_meta=item,
                                          encoding=encoding, header=header,
                                          line_codec=line_codec, bom=bom,
                                          rate=physical['layout']['rate'],
                                          trigger=physical['layout']['trigger']))
    return dict(mode='split', format='csv', segments=written)


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog='build_synthetic.py',
        description=('构造可复现的合成 1 s 数据集（非真机）：把既有 10 ms / 200 ms 记录按段'
                     '拼接成物理 1 s 输入与 1 s 规模事件流，段边界是显式会话复位。'
                     '合成数据不是连续真机 trace，不能用于 1 s 硬件验收。'),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=('示例：\n'
                '  python -X utf8 tools/synthetic/build_synthetic.py --mode physical '
                '--record <记录目录> --kind kvdat --segments 100 --out tmp/p6a-1s --verify all\n'
                '  python -X utf8 tools/synthetic/build_synthetic.py --mode events '
                '--record <记录目录> --segments 100 --out tmp/p6a-1s --event-sink jsonl --verify all\n'
                '  python -X utf8 tools/synthetic/build_synthetic.py --mode split '
                '--meta tmp/p6a-1s/synthetic.json --out tmp/p6a-1s/physical/segments\n'))
    parser.add_argument('--record', type=str,
                        help='来源：记录目录（含 capture.kvdat/capture.csv）或直接给 .kvdat/.csv 文件')
    parser.add_argument('--kind', choices=('auto', 'kvdat', 'csv'), default='auto',
                        help='物理来源格式；auto=优先 KVDAT（默认）')
    parser.add_argument('--segments', type=int,
                        help='拼接段数（物理：段长×段数；事件：单元事件×段数）。'
                             '1 s 通常 10 ms×100 或 200 ms×5')
    parser.add_argument('--out', type=str, required=True, help='输出目录（大文件建议放 tmp/）')
    parser.add_argument('--mode', choices=('physical', 'events', 'both', 'split', 'store-proof'),
                        default='both',
                        help='构造哪一部分（split=按 synthetic.json 切逐段文件；'
                             'store-proof=证明 store 写入器可消费合成记录）')
    parser.add_argument('--meta', type=str,
                        help='--mode split 时的 synthetic.json（默认 <out>/synthetic.json）')
    parser.add_argument('--events-source', choices=('auto', 'decoded', 'jsonl'), default='auto',
                        help='事件来源：auto=有 events.jsonl 用它，否则读 decoded.json 内联 events')
    parser.add_argument('--event-sink', choices=('jsonl', 'shards', 'none'), default='jsonl',
                        help='事件流落地方式：events.jsonl（默认，store 可直接消费）/ '
                             'gzip 分片 / 不落盘')
    parser.add_argument('--event-shard-events', type=int, default=2000000,
                        help='--event-sink shards 时每片行数（默认 2000000）')
    parser.add_argument('--markers', choices=('emit', 'skip'), default='emit',
                        help='段边界是否插入显式复位标记（默认 emit）')
    parser.add_argument('--offset-span', type=int,
                        help='每段 trace 字节偏移跨度；默认=来源单元最大 offset+1（可用 etm.bin 字节数）')
    parser.add_argument('--fraction', type=float, help='取样位置（默认读 session.json，否则 0.25）')
    parser.add_argument('--glitch-ns', type=float, help='软件滤波宽度 ns（默认读 session.json）')
    parser.add_argument('--clock', type=int, help='时钟通道（默认读 session.json，否则 0）')
    parser.add_argument('--data', type=int, nargs=4, metavar='CH',
                        help='数据通道 D0..D3（默认读 session.json，否则 1 2 3 4）')
    parser.add_argument('--seconds', type=float,
                        help='CSV 来源缺少布局信息时显式指定段长（秒）')
    parser.add_argument('--verify', choices=('none', 'layout', 'segments', 'events', 'all'),
                        default='all',
                        help='校验级别：layout=布局；segments=逐段等价；events=事件流自检；all=全部')
    parser.add_argument('--verify-segments', default='all',
                        help='逐段等价抽样：all 或正整数（默认 all；CSV 建议 6）')
    parser.add_argument('--sample-every', type=int, default=1000,
                        help='事件流自检中每 N 行做一次完整 JSON 比对（默认 1000）')
    parser.add_argument('--rebuild-unit', action='store_true',
                        help='强制重建单元 spool（默认复用同目录已校验的 unit/unit.json）')
    parser.add_argument('--force', action='store_true', help='覆盖 <out> 下本工具已知产物')
    parser.add_argument('--quiet', action='store_true', help='不打印进度')
    parser.add_argument('--json', type=str, help='把结果摘要写成 JSON（证据目录用）')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.mode == 'split':
        out_dir = Path(args.out).resolve()
        try:
            result = run_split(args, out_dir)
        except (BuildError, SyntheticError, OSError) as error:
            print('失败：%s' % error, file=sys.stderr)
            return 1
        print('切分完成：%d 段（格式 %s）' % (len(result['segments']), result['format']))
        if args.json:
            _write_json(args.json, result)
        return 0
    if args.mode == 'store-proof':
        out_dir = Path(args.out).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            result = run_store_proof(args, out_dir)
        except (BuildError, SyntheticError, OSError, ValueError) as error:
            print('失败：%s' % error, file=sys.stderr)
            return 1
        print('store 兼容性证明通过：%s（%.2f s，%.1f MiB，事件 %s 条）'
              % (out_dir, result['elapsedSeconds'], result['storeBytes'] / 2 ** 20,
                 (result.get('counts') or {}).get('events')))
        if args.json:
            _write_json(args.json, result)
        return 0
    if args.record is None:
        parser.error('--mode %s 需要 --record' % args.mode)
    if args.segments is None:
        parser.error('--mode %s 需要 --segments' % args.mode)
    try:
        if args.segments < 1:
            raise BuildError('--segments 必须 ≥1')
        if args.event_shard_events < 1:
            raise BuildError('--event-shard-events 必须 ≥1')
        if args.sample_every < 1:
            raise BuildError('--sample-every 必须 ≥1')
    except BuildError as error:
        parser.error(str(error))
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    known = [out_dir / 'physical', out_dir / 'unit', out_dir / 'events-record',
             out_dir / 'events-shards', out_dir / 'verify', out_dir / 'synthetic.json']
    if not args.force and any(path.exists() for path in known):
        print('失败：输出目录已有本工具产物（%s）；加 --force 覆盖或换一个 --out'
              % out_dir, file=sys.stderr)
        return 1
    started = time.time()
    result = dict(schema=SCHEMA, mode=args.mode, out=str(out_dir),
                  command=' '.join(sys.argv), cwd=os.getcwd(),
                  startedAt=time.strftime('%Y-%m-%dT%H:%M:%S%z'), declaration=DECLARATION,
                  python=sys.version.split()[0], peakRssBytesBefore=peak_rss_bytes())
    try:
        if args.mode in ('physical', 'both'):
            run_physical(args, out_dir, result)
        if args.mode in ('events', 'both'):
            run_events(args, out_dir, result)
    except (BuildError, SyntheticError, OSError, ValueError) as error:
        result['ok'] = False
        result['error'] = '%s: %s' % (type(error).__name__, error)
        result['elapsedSeconds'] = round(time.time() - started, 3)
        result['peakRssBytes'] = peak_rss_bytes()
        if args.json:
            _write_json(args.json, result)
        print('失败：%s' % error, file=sys.stderr)
        return 1
    result['ok'] = True
    result['elapsedSeconds'] = round(time.time() - started, 3)
    result['peakRssBytes'] = peak_rss_bytes()
    _write_json(out_dir / 'synthetic-result.json', result)
    if args.json:
        _write_json(args.json, result)
    print('完成：%s（%.2f s，峰值 RSS %.0f MiB）'
          % (out_dir, result['elapsedSeconds'], result['peakRssBytes'] / 2 ** 20))
    return 0


if __name__ == '__main__':
    sys.exit(main())
