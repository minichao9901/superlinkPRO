"""Resolve source and portable resources without a system Python/Node install."""
from pathlib import Path
import shutil
import sys


def frozen():
    return bool(getattr(sys, 'frozen', False))


def asset_root():
    return Path(sys.executable).resolve().parent / 'cli' if frozen() else Path(__file__).resolve().parents[2]


def backend_root():
    return Path(sys._MEIPASS) / 'backend' if frozen() else asset_root() / 'etm'


def node_executable():
    bundled = asset_root() / 'node/node.exe'
    if frozen():
        if not bundled.is_file():
            raise RuntimeError('完整包缺少 cli/node/node.exe，请重新解压完整包')
        return str(bundled)
    node = shutil.which('node')
    if not node:
        raise RuntimeError('缺少 Node.js')
    return node


def host_command(folder):
    if frozen():
        return [sys.executable, '--cli-host', str(folder)]
    return [sys.executable, '-X', 'utf8', str(Path(__file__).with_name('trace_cli_runtime.py')), 'host', str(folder)]


def hardware_lock():
    return Path(sys.executable).resolve().parent / 'data/trace-cli-hardware.lock'
