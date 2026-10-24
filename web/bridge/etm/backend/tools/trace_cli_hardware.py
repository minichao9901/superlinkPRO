"""CLI transport into the existing WebUSB workbench; no pyOCD dependency."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import zlib
from trace_cli_paths import asset_root, frozen, hardware_lock, node_executable


def lock_path():
    if frozen():
        path = hardware_lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(['git', 'rev-parse', '--git-common-dir'], cwd=repo,
                            capture_output=True, text=True, timeout=10)
    if result.returncode:
        return repo / '.trace-cli-hardware.lock'
    common = Path(result.stdout.strip())
    return (common if common.is_absolute() else repo / common).resolve() / 'trace-cli-hardware.lock'


class HardwareLease:
    """Shared across this repository's worktrees; never steals or deletes an old lease."""
    def __init__(self, command):
        self.path = lock_path()
        self.command = command

    def __enter__(self):
        try:
            self.path.mkdir()
        except FileExistsError:
            raise RuntimeError('硬件被CLI租约占用；确认原进程已退出后再人工处理：' + str(self.path)) from None
        self.owner = dict(pid=os.getpid(), command=self.command, created=time.time())
        try:
            (self.path / 'owner.json').write_text(json.dumps(self.owner), encoding='utf8')
        except BaseException:
            self.path.rmdir()
            raise
        return self

    def __exit__(self, *_):
        owner = self.path / 'owner.json'
        if json.loads(owner.read_text(encoding='utf8')) != self.owner:
            raise RuntimeError('硬件租约归属变化，未删除租约：' + str(self.path))
        owner.unlink()
        self.path.rmdir()


class WebPipe:
    """Local JSON pipe into the actual Web workbench. Never opens USB itself."""
    def __init__(self, timeout=210):
        self.timeout = timeout
        self.process = None
        self.sequence = 0

    def __enter__(self):
        node = node_executable()
        self.process = subprocess.Popen([node, str(asset_root()/'etm/tools/trace_target_cli.mjs')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
            text=True, encoding='utf8', bufsize=1)
        self.messages = queue.Queue()
        def reader():
            try:
                for line in self.process.stdout:
                    self.messages.put(json.loads(line))
            except Exception as error:
                self.messages.put(error)
            finally:
                self.messages.put(EOFError('网页命令子进程已退出'))
        threading.Thread(target=reader, daemon=True).start()
        return self

    def call(self, action, **args):
        self.sequence += 1
        expected = self.sequence
        self.process.stdin.write(json.dumps(dict(type='command', id=expected, action=action, **args),allow_nan=False)+'\n')
        self.process.stdin.flush()
        deadline=time.monotonic()+self.timeout
        while True:
            try:
                value = self.messages.get(timeout=max(.01,deadline-time.monotonic()))
            except queue.Empty:
                raise TimeoutError('网页命令超时；操作可能仍在进行，请先web cancel并检查目标') from None
            if isinstance(value, Exception) or value.get('id') == expected:
                break
        if isinstance(value, Exception):
            raise value
        if not value['ok']:
            error = RuntimeError(value['error'])
            error.details = value.get('results')
            raise error
        return value.get('value')

    def __exit__(self, *_):
        if self.process:
            if self.process.poll() is None:
                self.process.stdin.close()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    self.process.wait(timeout=5)
            self.process.stdout.close()


def web_request(request):
    with WebPipe(timeout=960 if request.get('action')=='flash' else 210) as pipe:
        return pipe.call('web', request=request)


class TargetBridge(WebPipe):
    """Reuse the real page's EtmTarget and ProbeManager, not another debugger."""
    def __init__(self, probe_id, profile, hse_hz, swd_hz=1000000, timeout=210, purpose=None):
        super().__init__(timeout)
        self.probe_id, self.profile, self.hse_hz = probe_id, profile, hse_hz
        self.swd_hz = swd_hz
        self.purpose = purpose
        self.restore_on_close = True
        self.initialized = False
        self.cleanup_result = {}

    def __enter__(self):
        super().__enter__()
        try:
            self.info = self.call('init', profile=self.profile, hseHz=self.hse_hz,
                                  probe=self.probe_id, swdHz=self.swd_hz, purpose=self.purpose)
            self.probe_id = self.info.pop('cliProbe', None) or self.probe_id
            self.initialized = True
            return self
        except BaseException:
            super().__exit__()
            raise

    def __exit__(self, kind, error, tb):
        cleanup = None
        try:
            if self.initialized:
                if kind and issubclass(kind,(KeyboardInterrupt,TimeoutError)):
                    web_request(dict(action='cancel'))
                self.cleanup_result = self.call('close', restore=self.restore_on_close)
        except BaseException as failure:
            cleanup = str(failure)
        finally:
            super().__exit__(kind, error, tb)
        if cleanup:
            if error is not None:
                error.add_note('网页目标恢复未确认：'+cleanup)
            else:
                raise RuntimeError('网页目标恢复未确认：'+cleanup)


class EchoLoad:
    """Optional CDC traffic, exact full-line CRC echo accounting; never selects COM automatically."""
    def __init__(self, port, folder, interval=.004):
        self.port_name, self.folder, self.interval = port, Path(folder), interval
        self.stop_event = threading.Event()
        self.sent = self.received = self.bad = self.duplicates = self.banners = 0
        self.seen = set()
        self.failure = None

    def start(self):
        import serial
        from serial.tools import list_ports
        info = next((p for p in list_ports.comports() if p.device == self.port_name), None)
        if info and info.vid == 0x0d28:
            raise ValueError('所选串口是探针，不能当作目标CDC端口')
        self.serial = serial.Serial(self.port_name, 115200, timeout=0, write_timeout=.5)
        self.serial.dtr = True
        self.serial.reset_input_buffer()
        self.raw = (self.folder / 'echo-rx.bin').open('xb')
        self.events = (self.folder / 'echo-events.jsonl').open('x', encoding='utf8')
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def _run(self):
        buffer = bytearray()
        next_send = time.monotonic()
        drain_until = None
        try:
            while True:
                now = time.monotonic()
                if self.stop_event.is_set():
                    drain_until = drain_until or now+.3
                    if now >= drain_until:
                        break
                if drain_until is None and now >= next_send:
                    seq = self.sent
                    text = ('%08d' % seq).encode()
                    line = b'TRACE-CLI:'+text+b':'+('%08x' % (zlib.crc32(text) & 0xffffffff)).encode()
                    payload = line+b'\r\n'
                    if self.serial.write(payload) != len(payload):
                        raise RuntimeError('CDC写入不完整')
                    self.sent += 1
                    self.events.write(json.dumps(dict(kind='tx', seq=seq, time=time.time()))+'\n')
                    next_send = now+self.interval
                chunk = self.serial.read(8192)
                if chunk:
                    self.raw.write(chunk)
                    buffer.extend(chunk)
                    while b'\n' in buffer:
                        line, _, rest = buffer.partition(b'\n');buffer[:] = rest
                        if b'=== STM32H743' in line:
                            self.banners += 1
                        match = re.fullmatch(rb'TRACE-CLI:(\d{8}):([0-9a-f]{8})\r?', line)
                        if match:
                            seq, crc = int(match[1]), int(match[2], 16)
                            if crc != zlib.crc32(match[1]) & 0xffffffff or seq >= self.sent:
                                self.bad += 1
                            elif seq in self.seen:
                                self.duplicates += 1
                            else:
                                self.seen.add(seq);self.received += 1
                            self.events.write(json.dumps(dict(kind='rx', seq=seq, time=time.time()))+'\n')
                time.sleep(.0005)
        except BaseException as error:
            self.failure = str(error)
        finally:
            self.events.flush();self.raw.flush()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            self.serial.cancel_read();self.serial.cancel_write()
            self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise RuntimeError('CDC负载线程未退出，端口释放未确认')
        self.serial.close();self.raw.close();self.events.close()
        result = dict(port=self.port_name, sent=self.sent, received=self.received,
            missing=sorted(set(range(self.sent))-self.seen), badCrc=self.bad,
            duplicates=self.duplicates, banners=self.banners, error=self.failure)
        result['passed'] = self.sent > 0 and self.sent == self.received and not (
            self.failure or self.bad or self.duplicates)
        (self.folder / 'echo-result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
        return result
