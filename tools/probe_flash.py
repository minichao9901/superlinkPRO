"""Program the SuperLinkPRO HPM5301 APP over its USB DFU bootloader."""
import hashlib
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib


APP_BASE = 0x80020000
APP_SIZE = 55 * 16 * 1024
HEADER_SIZE = 256
DFU_ID = ',0d28:0207'
APP_ID = '0d28:0204,-'
LAYOUT = re.compile(r'@Internal Flash\s*/0x80020000/55\*16Kg', re.I)
LIST_ROW = re.compile(r'^Found (DFU|Runtime): \[0d28:(0207|0204)\].*?path="([\d.\-]+)".*?name="([^"]*)".*?serial="([A-Za-z0-9_-]+)"', re.I | re.M)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def image_size(data):
    if not HEADER_SIZE < len(data) <= APP_SIZE:
        raise ValueError('APP 包长度超出允许范围')
    signature, length, crc, version = struct.unpack_from('<4I', data)
    if signature != 0x48504D21 or version != 1 or not 0 < length <= len(data) - HEADER_SIZE:
        raise ValueError('无效的 SuperLinkPRO APP 包头')
    size = HEADER_SIZE + length
    if zlib.crc32(data[HEADER_SIZE:size], 0x0D000721) != crc:
        raise ValueError('探针 APP 固件的 CRC32 不匹配')
    return size


def run(tool, args, *, log=None, allow_failure=False):
    result = subprocess.run([tool, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            encoding='utf8', errors='replace', timeout=120)
    if log is not None:
        log.write_text(result.stdout, encoding='utf8')
    if result.returncode and not allow_failure:
        detail = f'。日志 {log}' if log else f'。{result.stdout.strip()}'
        raise RuntimeError(f'dfu-util 退出码 {result.returncode}{detail}')
    return result.stdout


def device_rows(text):
    return [(kind.lower(), product, path, name, serial)
            for kind, product, path, name, serial in LIST_ROW.findall(text)]


def select_device(tool, serial):
    rows = device_rows(run(tool, ['-l', '-d', DFU_ID, '-i', '0', '-a', '0']))
    devices = [(path, uid) for mode, product, path, name, uid in rows
               if mode == 'dfu' and product == '0207' and LAYOUT.fullmatch(name.strip())
               and (serial is None or uid == serial)]
    if len(devices) != 1:
        raise RuntimeError('需要唯一一只已进入 DFU 的 SuperLinkPRO（0D28:0207，接口 0）。多只设备时用 --serial 指定。')
    return devices[0]


def flash(args):
    image = args.file.resolve()
    with image.open('rb') as source:
        data = source.read(APP_SIZE + 1)
    if image_size(data) != len(data):
        raise ValueError('请选择完整的打包 APP 文件，包头长度须与文件一致')
    value = {'file': str(image), 'bytes': len(data), 'sha256': digest(data),
             'address': f'{APP_BASE:#x}', 'dryRun': args.dry_run}
    if args.dry_run:
        return value

    tool = shutil.which('dfu-util') if args.dfu_util is None else str(args.dfu_util.resolve())
    if not tool or not Path(tool).is_file():
        raise FileNotFoundError('找不到 dfu-util，请加入 PATH 或用 --dfu-util 指定其绝对路径')
    path, serial = select_device(tool, args.serial)
    if args.backup_dir:
        folder = args.backup_dir.resolve()
        folder.mkdir(parents=True, exist_ok=False)
    else:
        folder = Path(tempfile.mkdtemp(prefix='superlink-probe-flash-'))
    print(f'已定位探针 {serial}。备份和日志目录 {folder}', file=sys.stderr, flush=True)

    selected = ['-d', DFU_ID, '-i', '0', '-a', '0', '-p', path, '-S', serial]
    backup = folder / 'before-app-region.bin'
    payload = folder / 'requested-app.bin'
    payload.write_bytes(data)
    run(tool, [*selected, '-s', f'{APP_BASE:#x}:{APP_SIZE}', '-U', str(backup)], log=folder/'backup.log')
    old = backup.read_bytes()
    if len(old) != APP_SIZE:
        raise RuntimeError(f'应用区备份长度不符，目录 {folder}')
    try:
        old_size = image_size(old)
    except ValueError:
        pass  # A probe in DFU recovery may have an invalid or erased APP.
    else:
        (folder/'before-app-pack.bin').write_bytes(old[:old_size])

    print('备份完成，开始写入探针 APP。', file=sys.stderr, flush=True)
    run(tool, [*selected, '-s', f'{APP_BASE:#x}', '-D', str(payload)], log=folder/'write.log')
    readback = folder/'after-app-pack.bin'
    run(tool, [*selected, '-s', f'{APP_BASE:#x}:{len(data)}', '-U', str(readback)], log=folder/'readback.log')
    if readback.read_bytes() != data:
        raise RuntimeError(f'探针 APP 逐字节校验失败，目录 {folder}')

    print('读回一致，等待同一探针重新进入运行态。', file=sys.stderr, flush=True)
    run(tool, [*selected, '-s', ':leave'], log=folder/'leave.log', allow_failure=True)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        rows = device_rows(run(tool, ['-l', '-d', APP_ID, '-p', path, '-S', serial]))
        if any(mode == 'runtime' and product == '0204' and bus == path and uid == serial
               for mode, product, bus, _, uid in rows):
            return {**value, 'serial': serial, 'backupDir': str(folder), 'backupSha256': digest(old),
                    'verified': True, 'runtimeDetected': True}
        time.sleep(.5)
    raise RuntimeError(f'APP 已读回一致，未确认探针重新枚举。请检查 USB 和 {folder / "leave.log"}')
