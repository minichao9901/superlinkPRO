"""RTT ring and wire evidence shared by the board-acceptance paths."""

import math
import re
from random import Random
import struct


SEQUENCE = re.compile(rb'seq (\d{7})\r\n')
SEQUENCE_MODULUS = 10_000_000
FLOOD = b'hello world!\n'
SEQUENCE_BANNER = '\r\n=== RTT 完整性测试：BLOCK_IF_FIFO_FULL，死循环发 seq NNNNNNN ===\r\n'.encode()
RTT_FLOOD_SECONDS = 8
RTT_SEQUENCE_SECONDS = {'f103ze': 8, 'h743': 60, 'hpm6800evk': 60}
WINDOW_CLOCK_TOLERANCE = 0.05
VIEWER_UP_SIZE = 512
VIEWER_DOWN_SIZE = 64
VIEWER_UP_START = 500
VIEWER_DOWN_START = 60
VIEWER_DOWN = bytes.fromhex('00 ff 80 31 c3 7e a5 0d')


def probe_port(ports, probe_serial):
    """Bind WebSerial's VID/PID-only view to the one Windows CDC device."""
    candidates = [item for item in ports if (item.vid, item.pid) == (0x0d28, 0x0204)]
    if len(candidates) != 1 or (candidates[0].serial_number or '').casefold() != probe_serial.casefold():
        raise ValueError('系统须能唯一匹配验收探针的 CDC 串口（VID/PID 与序列号）')
    return candidates[0].device


def ring_state(raw, address, channel=0):
    if len(raw) != 48 + 24 * channel or not raw.startswith(b'SEGGER RTT\0'):
        raise ValueError('RTT 控制块标识或长度错误')
    up_count, down_count = struct.unpack_from('<2I', raw, 16)
    _, ptr, size, wr, rd, flags = struct.unpack_from('<6I', raw, 24 * (channel + 1))
    if not channel < up_count <= 16 or down_count > 16 or not ptr or not 1 < size <= 0x100000 or max(wr, rd) >= size:
        raise ValueError('RTT 上行通道布局或游标越界')
    return dict(address=address, pointer=ptr, size=size, wr=wr, rd=rd,
                pending=(wr - rd) % size, flags=flags)


def boundary_fixture(address, packet_bytes):
    """Three independent RTT up rings with distinguishable finite payloads."""
    ring_size = 2 * packet_bytes
    header_size = 256
    image = bytearray(header_size + 3 * ring_size)
    image[:10] = b'SEGGER RTT'
    struct.pack_into('<2I', image, 16, 3, 0)
    payloads = []
    for channel, length in enumerate((packet_bytes - 1, packet_bytes, packet_bytes + 1)):
        source = Random(length).randbytes(length)
        offset = header_size + channel * ring_size
        struct.pack_into('<6I', image, 24 * (channel + 1), 0, address + offset, ring_size, length, 0, 0)
        image[offset:offset + length] = source
        payloads.append(source)
    return bytes(image), payloads


def viewer_fixture(address):
    """Preload a finite sequence across the up ring tail and leave down empty."""
    payload = b''.join(f'seq {number % SEQUENCE_MODULUS:07d}\r\n'.encode()
                       for number in range(SEQUENCE_MODULUS - 8, SEQUENCE_MODULUS + 24))
    up_at = 128
    down_at = up_at + VIEWER_UP_SIZE
    image = bytearray(b'\x6e' * (down_at + VIEWER_DOWN_SIZE))
    struct.pack_into('<16s2I', image, 0, b'SEGGER RTT', 1, 1)
    up_wr = (VIEWER_UP_START + len(payload)) % VIEWER_UP_SIZE
    struct.pack_into('<6I', image, 24, 0, address + up_at, VIEWER_UP_SIZE, up_wr, VIEWER_UP_START, 0)
    struct.pack_into('<6I', image, 48, 0, address + down_at, VIEWER_DOWN_SIZE,
                     VIEWER_DOWN_START, VIEWER_DOWN_START, 0)
    first = VIEWER_UP_SIZE - VIEWER_UP_START
    image[up_at + VIEWER_UP_START:down_at] = payload[:first]
    image[up_at:up_at + up_wr] = payload[first:]

    expected = image.copy()
    struct.pack_into('<I', expected, 24 + 16, up_wr)
    down_wr = (VIEWER_DOWN_START + len(VIEWER_DOWN)) % VIEWER_DOWN_SIZE
    struct.pack_into('<I', expected, 48 + 12, down_wr)
    first = VIEWER_DOWN_SIZE - VIEWER_DOWN_START
    expected[down_at + VIEWER_DOWN_START:] = VIEWER_DOWN[:first]
    expected[down_at:down_at + down_wr] = VIEWER_DOWN[first:]
    return bytes(image), bytes(expected), payload


def check_viewer_fixture(address, saved, restored, readback, raw):
    initial, expected, payload = viewer_fixture(address)
    if len(saved) != len(initial) + 128 or restored != saved:
        raise ValueError('Viewer 夹具 SRAM 恢复未独立对齐')
    if saved[:64] + expected + saved[-64:] != readback:
        raise ValueError('Viewer 跨尾回读、游标或哨兵不符')
    continuity = check_payload(raw, 'seq')
    if raw != payload or not continuity['exact']:
        raise ValueError('Viewer 原始上行序号、字节或数量不符')
    return {
        'controlBlock': address, 'bytes': len(raw), 'sequence': continuity,
        'sent': len(VIEWER_DOWN), 'readbackBytes': len(VIEWER_DOWN),
        'wrOffBefore': VIEWER_DOWN_START,
        'wrOffAfter': (VIEWER_DOWN_START + len(VIEWER_DOWN)) % VIEWER_DOWN_SIZE,
        'upWrap': True, 'downWrap': True,
    }


def boundary_address(board, image_path, image_bytes):
    """Verify the board's scratch range against the exact linked fixture."""
    from elftools.elf.elffile import ELFFile

    address, limit = {'f103ze': (0x2000a000, 0x2000f000),
                      'h743': (0x24020000, 0x2407f000),
                      'hpm6800evk': (0x01250000, 0x01280000)}[board]
    start, end = address - 64, address + image_bytes + 64
    if end > limit:
        raise ValueError('边界夹具超出可核对的 SRAM 范围')
    with image_path.open('rb') as stream:
        elf = ELFFile(stream)
        for section in elf.iter_sections():
            if section['sh_flags'] & 2 and start < section['sh_addr'] + section['sh_size'] and end > section['sh_addr']:
                raise ValueError(f'边界夹具与 ELF 分配区重叠（{section.name}）')
        for segment in elf.iter_segments():
            if segment['p_type'] == 'PT_LOAD' and start < segment['p_vaddr'] + segment['p_memsz'] and end > segment['p_vaddr']:
                raise ValueError('边界夹具与 ELF 装载区重叠')
    return address


def check_window(before, after, raw_size, stop):
    measured = stop.get('moved')
    if type(measured) is not int or measured <= 0 or stop.get('running') is not False:
        raise ValueError('缺少真实 STOP 搬运计数或引擎仍处于活动状态')
    if any(stop.get(key) != 0 for key in ('rdErr', 'wrErr')):
        raise ValueError('探针侧 RTT 读写错误未对齐')
    if raw_size != measured:
        raise ValueError(f'主机实收与探针搬运不一致（{raw_size}／{measured} B）')
    identity = ('address', 'pointer', 'size', 'flags')
    if any(before[key] != after[key] for key in identity):
        raise ValueError('窗口前后 RTT 通道布局变化')
    expected = (before['rd'] + measured) % before['size']
    if after['rd'] != expected:
        raise ValueError(f'独立回读的目标 RdOff 不一致（{after["rd"]}／{expected}）')
    return dict(moved=measured, rdBefore=before['rd'], rdAfter=after['rd'],
                wrAfter=after['wr'], ringBytes=after['size'], ringLeft=after['pending'], tailBytes=0)


def check_payload(raw, pattern):
    if pattern == 'hello':
        phase = next((offset for offset in range(len(FLOOD))
                      if raw.startswith((FLOOD + FLOOD)[offset:offset + len(FLOOD)])), None)
        if phase is None:
            return {'exact': False, 'format': pattern}
        repeated = (FLOOD + FLOOD)[phase:phase + len(FLOOD)]
        return dict(exact=raw == (repeated * ((len(raw) + len(FLOOD) - 1) // len(FLOOD)))[:len(raw)],
                    format=pattern, phase=phase, bytes=len(raw))
    if raw.startswith(SEQUENCE_BANNER):
        raw = raw[len(SEQUENCE_BANNER):]
    first = SEQUENCE.search(raw)
    if first is None:
        return {'exact': False, 'format': pattern, 'records': 0}
    start = int(first[1])
    prefix = raw[:first.start()]
    before = f'seq {(start - 1) % SEQUENCE_MODULUS:07d}\r\n'.encode()
    if len(prefix) >= len(before) or not before.endswith(prefix):
        return {'exact': False, 'format': pattern, 'records': 0}
    count, end = 0, first.start()
    for record in SEQUENCE.finditer(raw, end):
        if record.start() != end or int(record[1]) != (start + count) % SEQUENCE_MODULUS:
            return {'exact': False, 'format': pattern, 'records': count}
        end, count = record.end(), count + 1
    after = f'seq {(start + count) % SEQUENCE_MODULUS:07d}\r\n'.encode()
    fragment = raw[end:]
    return dict(exact=count >= 2 and len(fragment) < len(after) and after.startswith(fragment),
                format=pattern, records=count, first=start, last=(start + count - 1) % SEQUENCE_MODULUS,
                prefix=len(prefix), suffix=len(fragment))


def sequence_run(board, web_seconds, cli_seconds, web_bytes, cli_bytes):
    required = RTT_SEQUENCE_SECONDS[board]
    if any(type(value) not in (int, float) or not math.isfinite(value) or
           value < required - WINDOW_CLOCK_TOLERANCE for value in (web_seconds, cli_seconds)):
        raise ValueError(f'Web 和原生 RTT 序号主窗须各跑满 {required} 秒')
    proof = check_payload(web_bytes + cli_bytes, 'seq')
    if not proof['exact']:
        raise ValueError('Web 与原生 RTT 窗口之间的原始序号不连续')
    return proof
