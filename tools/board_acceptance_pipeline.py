"""Native H743 SRAM pipeline over the SuperLinkPRO CMSIS-DAP USB interface."""

from contextlib import contextmanager
import struct
import time

from tools import board_acceptance_target as facts
from tools.board_acceptance_native import NativeFailure
from tools.board_acceptance_target import Pending, Unsafe, checked, measurement, median, pattern, sha


DHCSR = 0xE000EDF0
CCR = 0xE000ED14
CPUID = 0xE000ED00
DBGMCU_IDCODE = 0x5C001000
DPIDR_H743 = 0x6BA02477
GUARD = 64


class BulkDap:
    def __init__(self, out, inp):
        self.out = out
        self.inp = inp
        self.pending = 0
        self.packet_size = 64

    def send(self, packet):
        checked(len(packet) <= self.packet_size, 'CMSIS-DAP 请求超过探针包长')
        # An OUT timeout can leave a submitted command running on the probe.
        self.pending += 1
        checked(self.out.write(packet, timeout=3000) == len(packet), 'CMSIS-DAP 请求发送不完整')
        if len(packet) < self.packet_size and len(packet) % self.out.wMaxPacketSize == 0:
            self.out.write(b'', timeout=3000)

    def receive(self):
        checked(self.pending > 0, 'CMSIS-DAP 没有待完成的请求')
        reply = bytes(self.inp.read(self.packet_size, timeout=3000))
        self.pending -= 1
        return reply

    def command(self, packet, header, length):
        self.send(packet)
        reply = self.receive()
        checked(len(reply) == length and reply.startswith(header),
                f'CMSIS-DAP 0x{packet[0]:02x} 响应异常：{reply[:16].hex()}')
        return reply

    def capabilities(self):
        count = self.command(b'\x00\xfe', b'\x00\x01', 3)[2]
        size = int.from_bytes(self.command(b'\x00\xff', b'\x00\x02', 4)[2:], 'little')
        if not 2 <= count <= 255 or not 64 <= size <= 32768:
            raise Pending(f'CMSIS-DAP 包能力异常：长度 {size}，队列 {count}')
        self.packet_size = size
        return {'packetSize': size, 'packetCount': count,
                'outMaxPacketSize': self.out.wMaxPacketSize, 'inMaxPacketSize': self.inp.wMaxPacketSize}

    def transfer(self, request, value=None):
        packet = bytes((5, 0, 1, request))
        if value is not None:
            packet += struct.pack('<I', value)
        reply = self.command(packet, b'\x05\x01\x01', 7 if value is None else 3)
        return int.from_bytes(reply[3:], 'little') if value is None else None

    def clock(self, hz):
        self.command(b'\x11' + struct.pack('<I', hz), b'\x11\x00', 2)

    def swd(self, hz):
        self.command(b'\x02\x01', b'\x02\x01', 2)
        self.clock(1_000_000)
        self.command(b'\x13\x00', b'\x13\x00', 2)
        sequence = bytes((0x12, 152)) + b'\xff' * 8 + b'\x9e\xe7' + b'\xff' * 8 + b'\0'
        self.command(sequence, b'\x12\x00', 2)
        self.command(b'\x04\x00\x40\x00\x00\x00', b'\x04\x00', 2)
        checked(self.transfer(2) == DPIDR_H743, 'H743 SWD DPIDR 不符')
        self.transfer(0, 0x1e)
        self.transfer(8, 0)
        self.transfer(4, 0x50000000)
        facts.wait_for(lambda: self.transfer(6), lambda value: value & 0xa0000000 == 0xa0000000,
                       1, 'DAP 未确认系统与调试上电')
        self.transfer(1, (self.transfer(3) & ~0x37) | 0x12)
        self.clock(hz)
        checked(self.transfer(2) == DPIDR_H743, '主测 SWD 请求档上的 DPIDR 回读不符')

    def word(self, address):
        packet, header = block_packet(address, 1)
        return int.from_bytes(self.command(packet, header, len(header) + 4)[len(header):], 'little')

    def drain(self):
        while self.pending:
            self.receive()


@contextmanager
def open_dap(serial):
    try:
        import libusb_package
        import usb.core
        import usb.util
    except ImportError as error:
        raise Pending('H743 SRAM 流水需要 pyusb 和 libusb-package') from error

    candidates = usb.core.find(find_all=True, idVendor=0x0d28, idProduct=0x0204,
                               backend=libusb_package.get_libusb1_backend())
    matches = [device for device in candidates if
               (device.serial_number or '').casefold() == serial.casefold() and
               (device.product or '').casefold().startswith(('akalinkpro', 'superlinkpro'))]
    if len(matches) != 1:
        raise Pending('无法唯一匹配流水测速所用的 SuperLinkPRO USB 序列号')
    device = matches[0]
    try:
        usb.util.claim_interface(device, 0)
        interface = device.get_active_configuration()[(0, 0)]
        out = usb.util.find_descriptor(interface, bEndpointAddress=0x02)
        inp = usb.util.find_descriptor(interface, bEndpointAddress=0x81)
        checked(out is not None and inp is not None, 'CMSIS-DAP Bulk 端点缺失')
        dap = BulkDap(out, inp)
        try:
            yield dap
        finally:
            try:
                dap.drain()
                dap.command(b'\x03', b'\x03\x00', 2)
            except Exception as error:
                raise Unsafe('CMSIS-DAP 流水请求未确认收尾，保留现场与备份') from error
    finally:
        try:
            usb.util.dispose_resources(device)
        except Exception as error:
            raise Unsafe('未确认 CMSIS-DAP Bulk 接口已释放') from error


def block_packet(address, words, payload=None):
    # ExecuteCommands pairs a fresh TAR with each block so packets can be
    # queued without depending on the preceding block's TAR auto-increment.
    packet = b'\x7f\x02\x05\x00\x01\x05' + struct.pack('<I', address)
    packet += struct.pack('<BBHB', 6, 0, words, 0x0f if payload is None else 0x0d)
    header = b'\x7f\x02\x05\x01\x01\x06' + struct.pack('<HB', words, 1)
    return packet + (payload or b''), header


def packet_geometry(profile, capabilities):
    # 15 B is the compound request overhead. A power-of-two block divides
    # the 1 KiB TAR window, so no request can cross that hardware boundary.
    maximum = min(profile['maxWords'], (capabilities['packetSize'] - 15) // 4)
    if maximum < 1:
        raise Pending('CMSIS-DAP 包长不足以装载 TAR 与数据')
    words = 1 << (maximum.bit_length() - 1)
    depth = min(profile['maxDepth'], capabilities['packetCount'])
    return words, depth


def burst(dap, address, data, words, depth, count, *, write):
    block_bytes = words * 4
    blocks = [data[offset:offset + block_bytes] for offset in range(0, len(data), block_bytes)]
    commands = [block_packet(address + index * block_bytes, words, block if write else None)
                for index, block in enumerate(blocks)]
    returned = []
    started = time.perf_counter()
    for begin in range(0, count, depth):
        indices = [index % len(blocks) for index in range(begin, min(begin + depth, count))]
        for index in indices:
            dap.send(commands[index][0])
        for index in indices:
            returned.append((index, dap.receive()))
    elapsed = time.perf_counter() - started
    for index, reply in returned:
        expected = commands[index][1] + (b'' if write else blocks[index])
        checked(reply == expected,
                f'CMSIS-DAP 流水{"写" if write else "读"}回包不符，块 {index}，'
                f'回包头 {reply[:16].hex()}')
    return measurement(count * block_bytes, elapsed)


def measure(dap, address, original, profile, capabilities):
    words, depth = packet_geometry(profile, capabilities)
    length = words * 4
    checked(not address % 1024 and len(original) == profile['windowBytes'] and not len(original) % 1024,
            'H743 流水窗口须对齐 1 KiB TAR')
    burst(dap, address, original, words, depth, len(original) // length, write=False)
    loops = max(profile['loops'], len(original) // length)
    samples = {'read': [], 'write': []}
    # Change the bytes on every write to expose missing or stale write commands.
    cycles = 0

    def cycle():
        nonlocal cycles
        cycles += 1
        data = pattern(len(original), 0x743000 + cycles)
        write = burst(dap, address, data, words, depth, loops, write=True)
        read = burst(dap, address, data, words, depth, loops, write=False)
        return data, {'read': read, 'write': write}

    cycle()  # Warm up in both directions.
    for _ in range(profile['repeats']):
        _, result = cycle()
        for name in samples:
            samples[name].append(result[name])
    start = time.monotonic()
    soak = []
    while time.monotonic() - start < profile['soakSeconds'] or not soak:
        final, result = cycle()
        soak.append(result)
    return final, {
        'words': words, 'depth': depth, 'loops': loops, 'requestedLoops': profile['loops'],
        'windowBytes': len(original),
        'transport': 'CMSIS-DAP USB Bulk', 'capabilities': capabilities,
        'samples': samples, 'medianMiBps': {key: median([row['mebibytesPerSecond'] for row in values])
                                          for key, values in samples.items()},
        'soak': {'seconds': time.monotonic() - start, 'iterations': len(soak), 'samples': soak},
    }


def run(native, address, original, profile):
    """Measure a verified, halted H743 window, then independently verify restoration."""
    checked(native.board == 'h743' and native.link == 'swd', 'SRAM 流水验收要求 H743/SWD')
    length = profile['windowBytes']
    start = address - GUARD
    low, high = facts.SRAM_REGION['h743']
    checked(low <= start and start + len(original) <= high, '流水窗口超出 H743 AXI SRAM')
    checked(len(original) == length + 2 * GUARD, '流水 SRAM 原始快照不含完整窗口和哨兵')
    evidence = {'address': address, 'identity': native.preflight(), 'requestedHz': native.clock_hz}
    runner = native.runner
    dirty = False
    try:
        runner.halt()
        saved = runner.read(start, len(original))['data']
        if saved != original:
            raise Pending('原生流水前独立读取的 SRAM 与 Web 恢复快照不一致')
        (native.folder / 'pipeline-before.bin').write_bytes(saved)
        with open_dap(native.probe_serial) as dap:
            capabilities = dap.capabilities()
            dap.swd(native.clock_hz)
            cpuid = dap.word(CPUID)
            device_id = dap.word(DBGMCU_IDCODE) & 0xfff
            checked((cpuid >> 4) & 0xfff == 0xc27 and device_id == 0x450, '流水窗口 H743 核或 DEV_ID 不符')
            if not dap.word(DHCSR) & (1 << 17) or dap.word(CCR) & (1 << 16):
                raise Pending('H743 SRAM 流水需要停机并关闭 D-cache')
            profile_clock = facts.BOARDS['h743']['clock']
            rcc = profile_clock['rcc']
            registers = {name: dap.word(rcc + offset) for name, offset in
                         (('CR', 0), ('CFGR', 0x10), ('D1CFGR', 0x18), ('PLLCKSELR', 0x28),
                          ('PLLCFGR', 0x2c), ('PLL1DIVR', 0x30))}
            evidence['targetClock'] = facts.decode_stm32h7_clock(registers, profile_clock['hseHz'])
            if (evidence['targetClock']['sysclkHz'], evidence['targetClock']['hclkHz']) != profile_clock['expect']['scope']:
                raise Pending('H743 SRAM 流水夹具需要 CPU480／HCLK240')
            evidence.update(cpuid=cpuid, deviceId=device_id)
            dirty = True
            expected, result = measure(dap, address, saved[GUARD:-GUARD], profile, capabilities)
        evidence.update(result)
        actual = runner.read(start, len(saved))['data']
        (native.folder / 'pipeline-writeback.bin').write_bytes(actual)
        checked(actual == saved[:GUARD] + expected + saved[-GUARD:], '独立回读流水载荷或哨兵不匹配')
        evidence.update(verified=True, writebackSha256=sha(actual))
    except Unsafe as error:
        evidence['unsafe'] = str(error)
        raise
    except Pending as error:
        evidence['pending'] = str(error)
        raise
    except Exception as error:
        evidence['failure'] = str(error)
        raise NativeFailure(f'H743 SRAM 流水复核失败：{error}', evidence) from error
    finally:
        try:
            if 'unsafe' not in evidence:
                if dirty:
                    runner.write(start, saved)
                    readback = runner.read(start, len(saved))['data']
                    (native.folder / 'pipeline-restored.bin').write_bytes(readback)
                    checked(readback == saved, '恢复后的流水 SRAM 或哨兵与快照不符')
                    evidence['restoredSha256'] = sha(readback)
                    evidence['restored'] = True
                evidence['stateAfter'] = native._force_running()
                checked(evidence['stateAfter'].get('state') == 'running', '流水测后目标运行态未确认')
        except Exception as error:
            evidence['unsafe'] = str(error)
            raise Unsafe(f'H743 SRAM 流水恢复未确认，保留 {native.folder}') from error
        finally:
            native._evidence_file('native-pipeline.json', evidence)
    return evidence
