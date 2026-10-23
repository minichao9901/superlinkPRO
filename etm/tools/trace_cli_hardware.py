"""CLI-only hardware transport. Register policy lives in production target.js."""
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


def lock_path():
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


def probes():
    from pyocd.core.helpers import ConnectHelper
    return ConnectHelper.get_all_connected_probes(blocking=False)


class TraceTransportDelegate:
    """Prevent pyOCD discovery from enabling every Funnel port before target.js runs.

    The local discovery-instance hook is tested with pyOCD 0.45.1. Nothing is
    monkey-patched globally; CPU/DWT/FPB discovery still uses pyOCD's normal path.
    """
    def will_init_target(self, target, init_sequence):
        from pyocd.coresight.funnel import TraceFunnel
        def after_discoverer(_):
            discovery = target._discoverer
            original = discovery._create_component
            def create(component):
                if component.factory == TraceFunnel.factory:
                    return
                return original(component)
            discovery._create_component = create
        init_sequence.wrap_task('create_discoverer', after_discoverer)


class TargetBridge:
    """Node sends hardware RPC on stdout; Python services it with the selected pyOCD probe.

    No listening port, browser, default probe choice, flash erase or reset is involved.
    """
    def __init__(self, probe_id, profile, hse_hz, swd_hz=1000000, timeout=60):
        self.probe_id, self.profile, self.hse_hz = probe_id, profile, hse_hz
        self.swd_hz, self.timeout = swd_hz, timeout
        self.process = self.session = None
        self.recovery_state = None
        self.restore_on_close = True
        self.initialized = False
        self.cleanup_result = {}

    def __enter__(self):
        from pyocd.core.helpers import ConnectHelper
        node = shutil.which('node')
        if not node:
            raise RuntimeError('缺少Node.js；硬件CLI复用生产target.js配置逻辑')
        found = [p for p in probes() if self.probe_id is None or p.unique_id == self.probe_id]
        if len(found) != 1:
            raise RuntimeError('必须唯一选择探针，请传 --probe；已匹配：' + str([p.unique_id for p in found]))
        self.probe_id = found[0].unique_id
        self.session = ConnectHelper.session_with_chosen_probe(unique_id=self.probe_id,
            target_override=self.profile.replace('stm32h743', 'stm32h743xx').replace('stm32h7b0', 'stm32h7b0xx'),
            frequency=self.swd_hz, options={'connect_mode': 'attach', 'auto_unlock': False,
                                          'resume_on_disconnect': False})
        try:
            self.session.delegate = TraceTransportDelegate()
            self.session.open()
            self.process = subprocess.Popen([node, str(Path(__file__).with_name('trace_target_cli.mjs'))],
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
                    self.messages.put(EOFError('目标配置子进程已退出'))
            self.reader = threading.Thread(target=reader, daemon=True)
            self.reader.start()
            self.info = self.call('init', profile=self.profile, hseHz=self.hse_hz)
            self.initialized = True
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def _send(self, value):
        self.process.stdin.write(json.dumps(value) + '\n')
        self.process.stdin.flush()

    def call(self, action, **args):
        self._send(dict(type='command', id=1, action=action, **args))
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                value = self.messages.get(timeout=max(.01, deadline-time.monotonic()))
            except queue.Empty:
                raise TimeoutError('目标CLI超时：' + action) from None
            if isinstance(value, Exception):
                raise value
            if value.get('type') == 'hardware':
                reply = dict(type='reply', id=value['id'])
                try:
                    target = self.session.target
                    op = value['op']
                    if op in ('read', 'write'):
                        address = value['address']
                        if not isinstance(address, int) or not 0 <= address <= 0xffffffff:
                            raise ValueError('无效内存地址')
                    if op == 'read':
                        length = value['length']
                        if not isinstance(length, int) or not 0 < length <= 4096:
                            raise ValueError('内存读取长度越界')
                        reply['value'] = target.read_memory_block8(address, length)
                    elif op == 'write':
                        data = bytes(value['bytes'])
                        if not 0 < len(data) <= 512:
                            raise ValueError('内存写入长度越界')
                        target.write_memory_block8(address, data)
                        target.flush()
                    elif op == 'halt':
                        target.halt()
                    elif op == 'run':
                        target.resume()
                    else:
                        raise ValueError('未知硬件操作')
                except Exception as error:
                    reply['error'] = str(error)
                self._send(reply)
            elif value.get('type') == 'result':
                if not value['ok']:
                    self.recovery_state = value.get('state')
                    raise RuntimeError(value['error'])
                return value.get('value')

    def __exit__(self, kind, error, _tb):
        cleanup = None
        if self.process is not None:
            try:
                if self.process.poll() is None:
                    self.cleanup_result = self.call('close', restore=self.restore_on_close)
            except BaseException as failure:
                cleanup = str(failure)
            finally:
                if self.process.poll() is None:
                    self.process.terminate()
                self.process.wait(timeout=10)
                for stream in (self.process.stdin, self.process.stdout):
                    if stream:
                        stream.close()
        if self.session is not None:
            self.session.close()
        if cleanup:
            if error is not None:
                error.add_note('目标恢复未确认：' + cleanup)
            else:
                raise RuntimeError('目标恢复未确认：' + cleanup)


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
