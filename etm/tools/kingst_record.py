"""Validated KingstVIS 3.6 KVDAT edge records (32-bit sample positions).

Only the observed layout is accepted. Unknown versions, flags and trailing data
are rejected rather than guessed; the original file is never modified.
"""
import struct
from pathlib import Path
from xml.etree import ElementTree

HEADER_LIMIT = 256 * 1024
FILE_HEADER = struct.Struct('<4Q')
CHANNEL_HEADER = struct.Struct('<IBBHQ')


def layout(path):
    """Validate lengths/sentinels without reading millions of edge records.

    This also distinguishes a complete asynchronous save from a temporarily
    stable, empty or partially written file.
    """
    path = Path(path)
    size = path.stat().st_size
    with path.open('rb') as stream:
        header = stream.read(HEADER_LIMIT)
        end = header.find(b'</settings>')
        if end < 0:
            raise ValueError('KVDAT 设置头不完整或超过上限')
        try:
            settings = ElementTree.fromstring(header[:end + len(b'</settings>')])
        except ElementTree.ParseError as error:
            raise ValueError('KVDAT 设置头无效') from error
        version = settings.findtext('./global/version', '')
        if not version.startswith('3.6.'):
            raise ValueError('尚不支持此 KVDAT 版本：' + version)
        marker = header.find(b'kvdat\0\0\0', end)
        if marker < 0 or marker + 40 > len(header):
            raise ValueError('KVDAT 采样头不完整')
        depth, rate, trigger, count = FILE_HEADER.unpack_from(header, marker + 8)
        if not (0 < depth < 2**32 and 0 < rate <= 500000000 and trigger <= depth and 0 < count <= 16):
            raise ValueError('不支持的 KVDAT 采样头')
        position = marker + 40
        channels = {}
        for _ in range(count):
            if position + 16 > size:
                raise ValueError('KVDAT 通道头尚未写完')
            stream.seek(position)
            magic, channel, initial, reserved, edges = CHANNEL_HEADER.unpack(stream.read(16))
            position += 16
            if magic != 0x442323 or reserved or initial not in (0, 1) or channel > 15 or channel in channels or not 1 <= edges <= depth + 1:
                raise ValueError('不支持的 KVDAT 通道块')
            stop = position + edges * 5
            if stop > size:
                raise ValueError('KVDAT 边沿数据尚未写完')
            stream.seek(stop - 5)
            final, flag = struct.unpack('<IB', stream.read(5))
            if final != depth or flag:
                raise ValueError('KVDAT 结束标记无效')
            channels[channel] = dict(initial=initial, offset=position, count=edges)
            position = stop
        if position != size:
            raise ValueError('KVDAT 有未解析尾部')
    return dict(depth=depth, rate=rate, trigger=trigger, channels=channels, version=version)


def read_tracks(path):
    import numpy as np
    info = layout(path)
    initial, times, levels = {}, {}, {}
    dtype = np.dtype([('position', '<u4'), ('flag', 'u1')])
    with Path(path).open('rb') as stream:
        for channel, block in info['channels'].items():
            stream.seek(block['offset'])
            rows = np.fromfile(stream, dtype=dtype, count=block['count'])
            if len(rows) != block['count']:
                raise ValueError('KVDAT 读取时发生截断')
            positions = rows['position']
            if np.any(rows['flag']) or np.any(positions[1:] <= positions[:-1]) or np.any(positions > info['depth']):
                raise ValueError('KVDAT 边沿位置或标记无效')
            initial[channel] = block['initial']
            positions = positions[:-1]  # Last item is the extent sentinel, not an edge.
            times[channel] = (positions.astype(np.float64) - info['trigger']) / info['rate']
            levels[channel] = (block['initial'] ^ ((np.arange(len(positions), dtype=np.uint32) + 1) & 1)).astype(np.uint8)
    return info, initial, times, levels
