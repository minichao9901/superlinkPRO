"""Native OpenOCD entry point for STM32 ARM/JTAG."""

import re
import struct
import sys
import time
from contextlib import contextmanager

from tools.board_acceptance_native import NativeOpenOcd, cfg_for
from tools.probe_flash import app_devices


VID, PID = 0x0D28, 0x0204


def hid_command(handle, command, payload=b''):
    request = bytes((1, len(payload) + 1, command)) + payload
    if handle.write(request.ljust(64, b'\0')) != 64:
        raise RuntimeError(f'HID 0x{command:02x} 发送不完整')
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        report = bytes(handle.read(64, timeout_ms=200))
        if len(report) >= 3 and report[0] == 2 and report[2] == command:
            if not 1 <= report[1] <= len(report) - 2:
                raise RuntimeError(f'HID 0x{command:02x} 回包长度错误')
            # GET_CONFIG puts seven data bytes on the wire but reports 7
            # instead of 8 in the command-plus-payload length field.
            if command == 1 and report[1] == 7:
                return report[3:10]
            return report[3:2 + report[1]]
    raise TimeoutError(f'HID 0x{command:02x} 未回包')


@contextmanager
def jtag_pins(serial):
    devices = app_devices(serial)
    if len(devices) != 1:
        raise RuntimeError('HID 序列号须唯一匹配一只 SuperLinkPRO 探针')
    import hid

    handle = hid.device()
    handle.open_path(devices[0]['path'])
    original = None
    try:
        if hid_command(handle, 0x10).rstrip(b'\0') != b'akaLinkPro':
            raise RuntimeError('探针 HID 型号与 SuperLinkPRO 不匹配')
        for command, busy_mask in ((0x31, 1), (0x32, 0xc1)):
            status = hid_command(handle, command, b'\x02')
            if len(status) < 5 or status[0] != 0:
                raise RuntimeError('探针高速流状态未确认')
            if struct.unpack_from('<I', status, 1)[0] & busy_mask:
                raise RuntimeError('探针仍有活动或未完成的高速流')
        config = hid_command(handle, 1)
        if len(config) != 7:
            raise RuntimeError('探针引脚配置不完整')
        if config[0] != 1:
            original = config
            updated = b'\x01' + config[1:]
            hid_command(handle, 2, updated)
            if hid_command(handle, 1) != updated:
                raise RuntimeError('未确认探针已切换到 SWD + JTAG')
        yield
    finally:
        try:
            if original is not None:
                hid_command(handle, 2, original)
                if hid_command(handle, 1) != original:
                    raise RuntimeError('未确认探针原引脚配置已恢复')
        finally:
            handle.close()


def switch_swj(serial):
    import libusb_package
    import usb.core
    import usb.util

    devices = usb.core.find(find_all=True, idVendor=VID, idProduct=PID,
                            backend=libusb_package.get_libusb1_backend())
    matches = [device for device in devices if device.serial_number == serial]
    if len(matches) != 1:
        raise RuntimeError('USB 序列号须唯一匹配同一只探针')
    device = matches[0]

    def exchange(packet, expected):
        if device.write(0x02, packet, timeout=2000) != len(packet):
            raise RuntimeError('CMSIS-DAP SWJ 命令发送不完整')
        if bytes(device.read(0x81, 512, timeout=2000))[:2] != bytes((packet[0], expected)):
            raise RuntimeError(f'探针未确认 CMSIS-DAP 0x{packet[0]:02x}')

    try:
        usb.util.claim_interface(device, 0)
        exchange(b'\x02\x02', 2)
        try:
            exchange(b'\x11' + struct.pack('<I', 1_000_000), 0)
            exchange(bytes((0x12, 88)) + b'\xff' * 8 + b'\x3c\xe7\x1f', 0)
        finally:
            exchange(b'\x03', 0)
    finally:
        usb.util.dispose_resources(device)


def run(args):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', args.probe_serial):
        raise ValueError('探针序列号格式错误')
    if not 1 <= args.clock_khz <= 60_000:
        raise ValueError('请求 TCK 必须在 1–60000 kHz')
    commands = args.tcl_command or []
    if args.stay_open and not commands:
        raise ValueError('--stay-open 需要至少一条 --command')
    _, _, config = cfg_for(args.board, 'arm-jtag')
    plan = {'board': args.board, 'link': 'arm-jtag', 'requestedClockKhz': args.clock_khz,
            'probeSerial': args.probe_serial, 'commands': commands, 'cfg': config,
            'execute': args.execute, 'stayOpen': args.stay_open}
    if not args.execute:
        return plan
    if args.evidence_dir is None:
        raise ValueError('--execute 需要一个新的 --evidence-dir')
    folder = args.evidence_dir.resolve()
    native = NativeOpenOcd(args.board, 'arm-jtag', args.clock_khz * 1000,
                           args.probe_serial, folder, timeout=args.timeout,
                           openocd=args.openocd, scripts=args.openocd_scripts)
    folder.mkdir(parents=True, exist_ok=False)
    with jtag_pins(args.probe_serial):
        switch_swj(args.probe_serial)
        plan['identity'] = native.preflight(require_running=False)
        if commands:
            if args.stay_open:
                print(f'OpenOCD 日志 {folder / "native-commands.log"}，按 Ctrl+C 退出', file=sys.stderr)
            plan['result'] = native.runner.run_commands(commands, stay_open=args.stay_open)
    plan['evidenceDir'] = str(folder)
    return plan
