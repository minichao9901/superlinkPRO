"""J-Scope 取证：JSP 解析、HPM 契约核对、原生 HSS 定速窗。

复用本仓已有实现，不另写协议：

* 包格式与网页一致（`web/app/scope/protocol.js`）：512 B 自描述包、magic `0x4a53`、
  16 B 头、`version==2` 时时间基为 24 MHz tick；
* HPM6800EVK 的字段契约与 `kSin20` 表取自夹具源码
  `web/tools/target-firmware/hpm6800evk_scope/src/main.c`，核对算法直接调用
  `script_test/scope_hss_test.py` 的 `rv_expect` / `lfsr_next` / `rv_same`
  （同一套契约只维护一处）；
* 原生定速窗使用 `tools/validate_f103_streams.py` 的 EP83 读队列、STOP ZLP 与读退休。

本模块在本机文件上做独立校验。
"""

from __future__ import annotations

import importlib.util
import mmap
import struct
import sys
from pathlib import Path

from tools import board_acceptance_target as facts
from tools.board_acceptance_target import Pending, checked

ROOT = facts.ROOT
NATIVE_PATH = ROOT / 'script_test/scope_hss_test.py'

# 包格式（与 web/app/scope/protocol.js 逐字段一致）
MAGIC = 0x4A53
PACKET = 512
HEADER = 16
PAYLOAD = PACKET - HEADER
KIND_DEF, KIND_DATA, KIND_STAT, KIND_EVT = 1, 2, 3, 4
VERSION_TICKS = 2
TICK_HZ = 24_000_000
SCALAR_SIZE = {'u8': 1, 'i8': 1, 'u16': 2, 'i16': 2, 'u32': 4, 'i32': 4,
               'f32': 4, 'f64': 8}
SCALAR_FMT = {'u8': '<B', 'i8': '<b', 'u16': '<H', 'i16': '<h',
              'u32': '<I', 'i32': '<i', 'f32': '<f', 'f64': '<d'}
TYPE_TO_SCALAR = {0: 'u8', 1: 'i8', 2: 'u16', 3: 'i16', 4: 'u32', 5: 'i32',
                  6: 'f32', 7: 'f64'}

# Faster neighbor, historical seed and slower neighbor, in 24 MHz ticks.
PERIODS = {
    ('f103ze', 'swd', 1): (480, 600, 720),
    ('f103ze', 'swd', 8): (1920, 2400, 2880),
    ('h743', 'swd', 1): (216, 240, 288),
    ('h743', 'swd', 8): (768, 840, 960),
    ('hpm6800evk', 'jtag', 1): (98, 99, 101),
    ('hpm6800evk', 'jtag', 8): (762, 764, 768),
}
STOP_FIELDS = ('produced', 'skipped', 'usb', 'errors', 'yields', 'invalidSamples', 'bytes')


def sweep_addresses(board, symbols, channels):
    """Use the same aligned fixture words as the historical stable windows."""
    if board == 'hpm6800evk':
        base = symbols['block']
    else:
        base = symbols['pack']
        if channels == 1:
            base += 20
    return [base + 4 * index for index in range(channels)]


def sweep_scalars(board, channels):
    if board == 'hpm6800evk' and channels == 8:
        return ['u32', 'u32', 'f32', 'f32', 'i32', 'i32', 'u32', 'u32']
    return ['u32'] * channels


def us_for_ticks(ticks, tick_hz=TICK_HZ):
    return ticks / tick_hz * 1_000_000


def ticks_for_us(microseconds, tick_hz=TICK_HZ):
    return int(round(microseconds * tick_hz / 1_000_000))


# --------------------------------------------------------------------------
# .jsp 解析
# --------------------------------------------------------------------------


def parse_packet(packet):
    checked(len(packet) >= PACKET, '包长度不足 512 B')
    if struct.unpack_from('<H', packet, 0)[0] != MAGIC:
        return None
    version, kind = packet[2], packet[3]
    if kind not in (KIND_DEF, KIND_DATA, KIND_STAT, KIND_EVT):
        return None
    seq, stamp = struct.unpack_from('<II', packet, 4)
    count, aux = struct.unpack_from('<HH', packet, 12)
    if kind == KIND_DATA and HEADER + aux > PACKET:
        return None
    return {'version': version, 'kind': kind, 'seq': seq, 'count': count, 'aux': aux,
            'stamp': stamp,
            'timeUs': us_for_ticks(stamp) if version == VERSION_TICKS else stamp,
            'payload': packet[HEADER:]}


def parse_def(payload, version):
    swd_hz, period = struct.unpack_from('<II', payload, 0)
    flags = struct.unpack_from('<H', payload, 8)[0]
    count, spans = payload[10], payload[11]
    offset, variables = 12, []
    for _ in range(count):
        address = struct.unpack_from('<I', payload, offset)[0]
        size, type_id = payload[offset + 4], payload[offset + 5]
        scalar = TYPE_TO_SCALAR.get(type_id)
        if scalar is None or SCALAR_SIZE[scalar] != size:
            raise Pending(f'DEF 通道定义无效：size={size} type={type_id}')
        variables.append({'address': address, 'size': size, 'scalar': scalar})
        offset += 8
    return {'swdHz': swd_hz,
            'periodUs': us_for_ticks(period) if version == VERSION_TICKS else period,
            'periodTicks': period if version == VERSION_TICKS else ticks_for_us(period),
            'flags': flags, 'spans': spans, 'vars': variables}


def parse_packet_stream(data, tick_hz=TICK_HZ):
    """解析 `.jsp` 字节流：DEF、逐组样本、STAT 与重同步计数。"""
    buffer = bytearray(bytes(data))
    definition, samples, stats = None, [], []
    packets = resync = skipped = 0
    previous_stamp, previous_time, previous_count, previous_seq = None, None, 0, None
    unwrapped = 0
    while len(buffer) >= PACKET:
        packet = parse_packet(bytes(buffer[:PACKET]))
        if packet is None:
            index = buffer.find(struct.pack('<H', MAGIC), 1)
            if index < 0:
                buffer = buffer[-1:]
                resync += 1
                break
            del buffer[:index]
            resync += 1
            continue
        del buffer[:PACKET]
        seq = packet['seq']
        contiguous = previous_seq is None or ((seq - previous_seq) & 0xffffffff) == 1
        previous_seq = seq
        packets += 1
        if packet['kind'] == KIND_DEF:
            definition = parse_def(packet['payload'], packet['version'])
        elif packet['kind'] == KIND_STAT:
            produced, dropped, pkts = struct.unpack_from('<III', packet['payload'], 0)
            usb_errors, swd_errors = struct.unpack_from('<HH', packet['payload'], 12)
            stats.append({'produced': produced, 'dropped': dropped, 'packets': pkts,
                          'usbErrors': usb_errors, 'swdErrors': swd_errors,
                          'timeUs': packet['timeUs']})
        elif packet['kind'] == KIND_DATA:
            if definition is None:
                skipped += 1
                continue
            frame = sum(item['size'] for item in definition['vars'])
            if not frame:
                skipped += 1
                continue
            usable = min(packet['count'], packet['aux'] // frame)
            stamp = packet['stamp']
            unwrapped = stamp if previous_stamp is None else unwrapped + ((stamp - previous_stamp) & 0xffffffff)
            start = us_for_ticks(unwrapped, tick_hz) if packet['version'] == VERSION_TICKS else unwrapped
            period = definition['periodUs']
            if previous_count and contiguous:
                measured = (start - previous_time) / previous_count
                if period * .5 < measured < period * 2:
                    period = measured
            previous_stamp, previous_time, previous_count = stamp, start, usable
            for index in range(usable):
                offset, values = index * frame, []
                for item in definition['vars']:
                    values.append(struct.unpack_from(SCALAR_FMT[item['scalar']],
                                                     packet['payload'], offset)[0])
                    offset += item['size']
                samples.append({'timeUs': start + index * period, 'values': values})
    return {'definition': definition, 'samples': samples, 'stats': stats,
            'packets': packets, 'resync': resync, 'skippedFrames': skipped,
            'tickHz': tick_hz}


def count_raw_packets(data):
    """Count received groups and check packet order without storing decoded samples."""
    checked(len(data) % PACKET == 0, '原始文件在半包处结束')
    definition = None
    received = 0
    previous = None
    for offset in range(0, len(data), PACKET):
        packet = parse_packet(data[offset:offset + PACKET])
        checked(packet is not None, f'无效采样包，偏移 {offset}')
        seq = packet['seq']
        checked(previous is None or ((seq - previous) & 0xffffffff) == 1, f'采样包序不连续，偏移 {offset}')
        previous = seq
        if packet['kind'] == KIND_DEF:
            current = parse_def(packet['payload'], packet['version'])
            checked(definition in (None, current), '窗口内 DEF 变化')
            definition = current
        elif packet['kind'] == KIND_DATA:
            checked(definition is not None, 'DATA 先于 DEF')
            frame = sum(var['size'] for var in definition['vars'])
            checked(frame and packet['count'] * frame == packet['aux'], f'DATA 长度不一致，偏移 {offset}')
            received += packet['count']
    checked(received > 0, '原始文件没有样本')
    return {'definition': definition, 'received': received, 'packets': len(data) // PACKET}


def read_raw(path):
    with path.open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        return count_raw_packets(data)


def check_raw(raw, length, final, addresses, ticks, transport, scalars=None):
    definition = raw['definition']
    backend = 'arm-jtag' if definition['flags'] & 0x100 else 'jtag' if definition['flags'] & 0x40 else 'swd'
    if (definition['periodTicks'] != ticks or backend != transport or
            [item['address'] for item in definition['vars']] != addresses or
            [item['scalar'] for item in definition['vars']] != (scalars or ['u32'] * len(addresses))):
        raise ValueError('JSP 的 DEF 周期、链路或通道与请求不一致')
    if raw['received'] != final.get('produced') or length != final.get('bytes'):
        raise ValueError('JSP 整组数或格式化字节数与 STOP 终态不符')


def counter_quality(before, after, final, repeated, ticks, duration):
    """Verify the 24 MHz counter window and the two independent STOP reads."""
    problems, gaps = [], []
    result = {'problems': problems, 'gaps': gaps}
    snapshots = (('开始', before), ('结束', after), ('STOP', final), ('再次 STOP', repeated))
    for stage, metric in snapshots:
        if any(type((metric or {}).get(key)) is not int or not 0 <= metric[key] <= 0xffffffff
               for key in (*STOP_FIELDS, 'tick', 'hz', 'periodTicks', 'flags')):
            gaps.append(f'{stage}缺少完整的硬件计数')
    if gaps:
        return result
    if any(metric['periodTicks'] != ticks for _, metric in snapshots):
        problems.append(f'实际周期与请求 {ticks} tick 不符')
    if any(metric['hz'] != TICK_HZ for _, metric in snapshots):
        problems.append('硬件时基与 24 MHz 不符')
    if not (before['flags'] & 256 and after['flags'] & 256) or (final['flags'] | repeated['flags']) & 256:
        problems.append('计数窗或 STOP 的探针运行状态不符')
    for field in (*STOP_FIELDS, 'periodTicks', 'hz'):
        if final[field] != repeated[field]:
            problems.append(f'STOP 后 {field} 计数缺失或未稳定')
    if not 0 <= before['produced'] < after['produced'] <= final['produced']:
        problems.append('硬件窗口内的样本计数未推进或超出 STOP 终态')
    for field in ('usb', 'errors', 'yields', 'invalidSamples'):
        if any(metric[field] != 0 for _, metric in snapshots):
            problems.append(f'硬件计数 {field} 非零')
    if not before['skipped'] <= after['skipped'] <= final['skipped']:
        problems.append('探针跳拍计数倒退')
    produced = after['produced'] - before['produced']
    skipped = after['skipped'] - before['skipped']
    seconds = ((after['tick'] - before['tick']) & 0xffffffff) / TICK_HZ
    rate = produced / seconds if seconds else 0
    if seconds <= 0 or not duration - 0.05 <= seconds <= duration + 5:
        problems.append(f'硬件计数窗口长度异常（{seconds:.3f} s，请求 {duration} s）')
    quality = facts.scope_window(rate, max(0, produced), max(0, skipped), final['usb'], ticks)
    if not quality['rateOk']:
        problems.append('硬件实得组率低于理论档的 99%')
    total = final['produced'] + final['skipped']
    if not total or not quality['skipOk'] or final['skipped'] / total >= facts.SCOPE_SKIP_CEILING:
        problems.append('探针跳拍率达到 0.1%')
    return {**result, 'seconds': seconds, 'rateHz': rate, 'skipFraction': quality['skipFraction'],
            'windowProduced': produced, 'windowSkipped': skipped, 'requestedTicks': ticks}


def native_verdict(value, path, addresses, period_us, transport, scalars=None):
    ticks, final = ticks_for_us(period_us), value.get('finalMetrics') or {}
    proof = counter_quality(value.get('metricsBefore'), value.get('metricsAfter'), final,
                            value.get('confirmedFinalMetrics'), ticks, facts.SCOPE_WINDOW_SECONDS)
    problems = proof['problems'] + proof['gaps']
    try:
        raw = read_raw(path)
        check_raw(raw, path.stat().st_size, final, addresses, ticks, transport, scalars)
    except (OSError, ValueError, RuntimeError, Pending) as error:
        return {'problems': problems + [str(error)]}
    if not value.get('tailDrain') or value.get('receiveErrors'):
        problems.append('原生 ZLP/排空或 USB 读未通过')
    nominal = facts.scope_theory_hz(ticks)
    if abs(proof.get('rateHz', 0) / nominal - 1) > 0.0011:
        problems.append('原生实得组率与固定档偏差超过 0.11%')
    return {**proof, 'problems': problems, 'tailCountDelta': 0, **raw, 'finalMetrics': final}


def csv_rows(text):
    """解析页面 exportCsv 的输出（首行 `t_us,<通道名…>`）。"""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise Pending('CSV 为空')
    header = lines[0].split(',')
    rows = []
    for line in lines[1:]:
        cells = line.split(',')
        if len(cells) != len(header):
            raise Pending(f'CSV 列数不一致：{len(cells)} != {len(header)}')
        rows.append([cells[0]] + cells[1:])
    return {'columns': header, 'rows': rows}


def compare_jsp_csv(packed, csv, channels, tolerance=1e-6):
    """同一窗口的 JSP 与 CSV 逐值对账：数量、通道数、时间单调、数值。"""
    if not packed.get('definition'):
        return {'ok': False, 'reason': 'JSP 缺少 DEF 定义'}
    if len(packed['definition']['vars']) != channels:
        return {'ok': False, 'reason': f'JSP 通道数 {len(packed["definition"]["vars"])}'
                                       f' 与请求 {channels} 不符'}
    if len(csv['columns']) != channels + 1:
        return {'ok': False, 'reason': f'CSV 列数 {len(csv["columns"])} 与通道 {channels} 不符'}
    if len(packed['samples']) != len(csv['rows']):
        return {'ok': False, 'reason': f'样本数不一致：JSP {len(packed["samples"])}'
                                       f' 对 CSV {len(csv["rows"])}'}
    if csv['columns'][0] != 't_us':
        return {'ok': False, 'reason': 'CSV 缺少 t_us 列'}
    mismatch = None
    times = [sample['timeUs'] for sample in packed['samples']]
    for index, (sample, row) in enumerate(zip(packed['samples'], csv['rows'])):
        # The page packs one timestamp anchor per 64 samples and interpolates in between.
        anchor = index - index % 64
        following = min(anchor + 64, len(times) - 1)
        expected_time = times[anchor]
        if following > anchor:
            expected_time += (index - anchor) * (times[following] - times[anchor]) / (following - anchor)
        try:
            csv_time = float(row[0])
        except (TypeError, ValueError):
            csv_time = float('inf')
        if not abs(csv_time - expected_time) <= 0.51:  # CSV uses toFixed(0).
            mismatch = {'row': index, 'field': 't_us', 'jsp': expected_time, 'csv': row[0]}
            break
        for channel, value in enumerate(sample['values']):
            try:
                expected = float(row[channel + 1])
            except (TypeError, ValueError):
                mismatch = {'row': index, 'channel': channel, 'jsp': value,
                            'csv': row[channel + 1]}
                break
            if not abs(expected - float(value)) <= tolerance:
                mismatch = {'row': index, 'channel': channel, 'jsp': value,
                            'csv': row[channel + 1]}
                break
        if mismatch:
            break
    monotonic = all(later >= earlier for earlier, later in zip(times, times[1:]))
    return {'ok': mismatch is None and monotonic, 'firstMismatch': mismatch,
            'rows': len(csv['rows']), 'monotonic': monotonic,
            'jspPackets': packed['packets'], 'resync': packed['resync']}


# --------------------------------------------------------------------------
# HPM6800EVK 契约核对（复用 scope_hss_test 的算法与 kSin20 表）
# --------------------------------------------------------------------------


def load_native():
    """按路径装载 script_test/scope_hss_test.py（它是脚本而不是包）。"""
    if not NATIVE_PATH.is_file():
        raise Pending(f'缺少原生采样器：{NATIVE_PATH.relative_to(ROOT).as_posix()}')
    folder = str(NATIVE_PATH.parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    spec = importlib.util.spec_from_file_location('scope_hss_test', NATIVE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_hpm_contract(samples, native=None):
    """按 g_tick 反算逐字段契约；允许读期间推进一拍，但取值必须随偏移单调。"""
    native = native or load_native()
    expect, same, order = native.rv_expect, native.rv_same, native.RV_ORDER
    if not samples:
        return {'ok': False, 'reason': '没有样本', 'samples': 0}
    ticks = [int(sample['g_tick']) for sample in samples]
    # 冻结判定只在 ≥2 帧时成立：单帧的 tick 本来就是常数。
    if len(ticks) >= 2 and (max(ticks) == 0 or len(set(ticks)) == 1):
        return {'ok': False, 'reason': f'tick 恒为 {ticks[0]}（靶子没在动或读路径错）',
                'samples': len(samples)}
    matched = dict.fromkeys(order, 0)
    exact = torn = 0
    bad = []
    for index, sample in enumerate(samples):
        tick = int(sample['g_tick'])
        low, high = expect(tick), expect(tick + 1)
        allowed, previous, failure = {}, 0, None
        for field in order:
            options = {generation for generation, wanted in ((0, low[field]), (1, high[field]))
                       if same(field, sample[field], wanted)}
            if options:
                matched[field] += 1
            allowed[field] = options
        generations = []
        for field in order:
            pick = next((value for value in sorted(allowed[field]) if value >= previous), None)
            if pick is None:
                failure = {'sample': index, 'field': field,
                           'got': sample[field], 'tick': tick}
                break
            previous = pick
            generations.append(pick)
        if failure:
            bad.append(failure)
            continue
        if set(generations) == {0} or not generations:
            exact += 1
        else:
            torn += 1
    return {'ok': not bad, 'samples': len(samples), 'exact': exact, 'torn': torn,
            'tornFraction': torn / len(samples), 'matched': matched,
            'bad': bad[:10], 'badCount': len(bad)}


# --------------------------------------------------------------------------
# 原生 HSS 定速窗
# --------------------------------------------------------------------------
