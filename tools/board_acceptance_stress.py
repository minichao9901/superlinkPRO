"""Run the seeded cross-page plan through the production workbench APIs."""

import sys
import time

from tools import board_acceptance_rtt as rtt
from tools import board_acceptance_scope as scope
from tools.board_acceptance_target import Pending, Unsafe, checked, digest, elf_symbols, wait_for, ROOT

IDLE_EXPECTED = {'owners': [], 'pending': [], 'failures': [], 'busy': False,
                 'rttConnected': False, 'hidConnected': False, 'serialOpen': False,
                 'scopeTransport': None, 'scopeRunning': False, 'scopeStopping': False,
                 'debugConnected': False}


class BoardStress:
    def __init__(self, driver, case):
        self.driver = driver
        self.ctx = driver.ctx
        self.web = driver.web
        self.case = case
        self.serial = self.ctx.probe_serial
        self.mode = {'swd': 'swd', 'jtag': 'riscv'}[driver.link]
        self.app_running = False
        self.started = False

    def prepare(self):
        if not self.serial:
            raise Pending('跨页压力需要 --probe-serial 绑定已授权探针与 COM')
        self.ctx.hardware('跨页与断线压力')
        state = self.probe('rtt', 'state')
        if state.get('busy') or state.get('pending') or state.get('failures') or any(
                owner not in ('rtt', 'hid', 'scope', 'dbg') for owner in state.get('owners', [])):
            raise Pending('顶栏存在本轮以外的业务或待处理任务')
        active = [self.summary(name) for name in ('rtt', 'scope', 'dbg', 'rttcdc')]
        if any(row.get('running') or row.get('connected') or row.get('open') or row.get('stopping') for row in active):
            raise Pending('独立工作台仍有活动会话，跨页压力未接管')
        self.started = True
        self.close_workbench()
        self.kind, self.fixture, self.pattern = self.driver.match_rtt_fixture(self.case)
        self.cb = self.fixture['symbols']['rtt']
        self.symbols = elf_symbols(ROOT / self.fixture['path'])
        self.tick = self.symbols.get('g_ms') or self.symbols.get('g_ticks')
        if not self.tick:
            raise Pending('当前 RTT 夹具缺少可由 J-Scope 和调试器核对的心跳')
        # Validate the running application before we ever halt it in this plan.
        self.driver.rtt_ring(self.case, self.serial, self.cb)

    def probe(self, page, action, *args):
        return self.web.invoke(page, action, list(args), target='probe')

    def summary(self, page):
        return self.web.state(page)['summary']

    def show(self, page):
        self.web.page(page, 'show')
        wait_for(lambda: self.probe(page, 'state'),
                 lambda state: state['tab'] == page and not state['busy'] and not state['scanning'],
                 8, f'{page} 顶栏未就绪')

    def configure(self, page):
        clock = self.case['requestedHz']
        controls = {
            'rtt': {'#r-target': self.mode, '#r-chip': self.driver.chip_option},
            'scope': {'#sc-target': self.mode, '#sc-period': '1000', '#sc-seconds': '90', '#sc-raw': True},
            'dbg': {'#d-backend': 'riscv' if self.driver.riscv else 'webusb',
                    '#d-hpm-target' if self.driver.riscv else '#d-arm-target': self.driver.chip_option},
            'rttcdc': {},
        }[page]
        if clock and page in ('rtt', 'dbg', 'scope') and not (page == 'scope' and self.driver.riscv):
            controls[{'rtt': '#r-usb-clock', 'dbg': '#d-clock', 'scope': '#sc-clock'}[page]] = str(clock // 1000)
        for selector, value in controls.items():
            self.web.set_control(page, selector, value)
        if page in ('rtt', 'scope'):
            checked(self.web.invoke(page, 'loadElfFile', [self.driver._elf_file(self.fixture)]) is True,
                    f'{page} 未接受配套 ELF')
        if page == 'rtt':
            self.web.click(page, '#r-clear')
        if page == 'scope':
            self.web.invoke(page, 'clearTrigger')
            self.driver._scope_select(('tick',), {'tick': self.tick})

    def arm(self, page, folder, after_disconnect=False):
        self.show(page)
        self.configure(page)
        connected = self.probe(page, 'connectAuthorized', self.serial, page)
        checked(connected and connected.get('connected') is True and
                connected.get('serial', '').casefold() == self.serial.casefold(), '顶栏未连接指定探针')
        if page == 'dbg':
            return self.debug_progress(after_disconnect)
        if page == 'rtt':
            state = wait_for(lambda: self.summary(page),
                             lambda s: s['running'] and s['controlBlock'] == self.cb and s['stats']['bytes'] >= 256,
                             8, 'RTT Viewer 未在匹配控制块收到数据')
            raw = self.raw(page, 'saveRaw', folder / 'viewer.bin')
            pattern = rtt.check_payload(raw, self.pattern)
            checked(pattern['exact'], 'RTT Viewer 原始数据不连续')
            return {'active': True, 'cbAddr': self.cb, 'bytes': state['stats']['bytes'], 'payload': pattern,
                    'raw': self.file_reference(folder / 'viewer.bin')}
        if page == 'rttcdc':
            try:
                from serial.tools.list_ports import comports
                port = rtt.probe_port(comports(), self.serial)
            except (ImportError, ValueError) as error:
                raise Pending(str(error)) from error
            state = self.web.invoke(page, 'refresh', [{'strict': True}], target='hid')
            checked(state['probeSerial'].casefold() == self.serial.casefold() and not state['running'],
                    'RTT 转发前 HID 序列号或空闲状态不符')
            self.driver.open_rtt_serial(self.case, self.cb)
            self.web.click(page, '#c-statclear')
            checked(self.web.invoke(page, 'start', target='hid') is not False, 'RTT 转发未启动')
            state = wait_for(lambda: self.summary(page), lambda s: s['open'] and s['bytes'] >= 256,
                             8, 'RTT 转发未收到 CDC 数据')
            raw = self.raw(page, 'captureWindow', folder / 'forward.bin', [{'seconds': .25, 'maxMiB': 8}])
            pattern = rtt.check_payload(raw, self.pattern)
            checked(pattern['exact'], 'RTT 转发原始数据不连续')
            return {'active': True, 'cbAddr': self.cb, 'com': port, 'bytes': state['bytes'], 'payload': pattern,
                    'raw': self.file_reference(folder / 'forward.bin')}
        self.web.invoke(page, 'start')
        state = wait_for(lambda: self.summary(page),
                         lambda s: s['running'] and s.get('receivedSamples', 0) >= 8,
                         8, 'J-Scope 未收到本轮样本')
        fields = state.get('definitionVariables') or []
        checked(len(fields) == 1 and fields[0]['addr'] == self.tick and fields[0]['size'] == 4,
                'J-Scope DEF 与当前 ELF 的心跳不一致')
        raw_path = folder / 'scope-prefix.jsp'
        raw = self.raw(page, 'saveRaw', raw_path)
        parsed = scope.count_raw_packets(raw)
        checked(parsed['received'] >= 8 and parsed['definition']['vars'][0]['address'] == self.tick,
                'J-Scope 首帧原始数据与当前心跳不一致')
        return {'active': True, 'samples': state['receivedSamples'], 'address': self.tick,
                'stalePackets': state.get('stalePackets', 0), 'raw': self.file_reference(raw_path)}

    def debug_progress(self, after_disconnect):
        state = self.summary('dbg')
        checked(state['connected'] and (state.get('probeSerial') or '').casefold() == self.serial.casefold(),
                '调试页未连接指定探针')
        self.app_running = state['targetState'] == 'running' or after_disconnect
        checked(self.app_running, '跨页调试要求开始时目标在运行')
        if state['targetState'] == 'running':
            self.web.invoke('dbg', 'halt', target='session')
        checked(self.summary('dbg')['halted'], '调试停机未确认')
        pc = self.web.invoke('dbg', 'readReg', ['pc'], target='session')
        self.web.invoke('dbg', 'step', target='session')
        later = self.web.invoke('dbg', 'readReg', ['pc'], target='session')
        checked(type(pc) is int and type(later) is int and pc != later and self.summary('dbg')['halted'],
                '调试单步或 PC 变化未确认')
        return {'active': True, 'halted': True, 'pc': pc, 'steppedPc': later}

    def raw(self, page, method, destination, args=None):
        existing = {entry['id'] for entry in self.web.downloads(page)}
        value = self.web.invoke(page, method, args or [])
        entries = [entry for entry in self.web.downloads(page) if entry['id'] not in existing]
        checked(len(entries) == 1, f'{page} 本轮原始下载未确认')
        data = self.web.fetch_download(page, entries[0])
        destination.write_bytes(data)
        self.web.page(page, 'discard', id=entries[0]['id'])
        checked(not isinstance(value, dict) or value.get('complete') is not False, f'{page} 原始窗口不完整')
        return data

    def stop(self, page, folder):
        if page == 'rtt':
            state = self.summary(page)
            checked(state['stats']['lost'] == state['stats']['corrupt'] == 0,
                    'Viewer 状态出现丢失或损坏')
            self.web.invoke(page, 'disconnect')
            return {'lost': 0, 'corrupt': 0}
        if page == 'rttcdc':
            self.web.invoke(page, 'stop', target='hid')
            return self.forward_tail()
        if page == 'scope':
            self.web.invoke(page, 'stop')
            return self.scope_tail(folder)
        if self.summary('dbg').get('halted') and self.app_running:
            self.web.invoke('dbg', 'run', target='session')
        checked(self.summary('dbg')['targetState'] == 'running', '原目标尚未恢复运行')
        self.web.invoke('dbg', 'disconnect')
        return {'targetState': 'running'}

    def forward_tail(self):
        engine = self.web.invoke('rttcdc', 'summary', target='hid')
        checked(engine['running'] is False and engine['rdErr'] == engine['wrErr'] == 0 and engine['moved'] > 0,
                'RTT 转发探针终态缺失或有读写错误')
        count = wait_for(lambda: self.summary('rttcdc')['bytes'], lambda n: n >= engine['moved'],
                         4, 'RTT STOP 后串口仍缺字节')
        checked(count == engine['moved'], 'RTT STOP 终态与串口实收不一致')
        return {'moved': engine['moved'], 'received': count}

    def scope_tail(self, folder):
        state = self.summary('scope')
        checked(state['running'] is False and not state.get('stopping') and state.get('tailCountDelta') == 0 and
                not state.get('metricsError') and (state.get('probeMetrics') or {}).get('invalidSamples') == 0 and
                all(state.get(key) == 0 for key in ('lostUsb', 'lostGap', 'readErrors')),
                'J-Scope STOP 的计数或状态未确认')
        path = folder / 'scope.jsp'
        parsed = scope.count_raw_packets(self.raw('scope', 'saveRaw', path))
        checked(parsed['received'] == state['receivedSamples'], 'J-Scope 原始 JSP 与实收不一致')
        return {'received': parsed['received'], 'tailCountDelta': 0, 'raw': self.file_reference(path)}

    def close_workbench(self, page='rtt'):
        state = self.probe(page, 'state')
        if state.get('disconnectable'):
            checked(self.probe(page, 'disconnect') is True, '顶栏断开未确认')
        if self.summary('rttcdc').get('open'):
            self.web.invoke('rttcdc', 'close', target='session')
        summaries = {name: self.summary(name) for name in ('rtt', 'rttcdc', 'scope', 'dbg')}
        top = self.probe(page, 'state')
        hid = self.web.invoke('rttcdc', 'summary', target='hid')
        idle = {key: top.get(key) for key in ('owners', 'pending', 'failures', 'busy')}
        idle.update(rttConnected=summaries['rtt']['connected'], hidConnected=hid['connected'],
                    serialOpen=summaries['rttcdc']['open'],
                    scopeTransport=summaries['scope']['transport'],
                    scopeRunning=summaries['scope']['running'], scopeStopping=summaries['scope']['stopping'],
                    debugConnected=summaries['dbg']['connected'])
        checked(idle == IDLE_EXPECTED, '顶栏断开后残留探针、串口或采样会话')
        return idle

    def usb_cycle(self):
        # This is the only stress operation which asks the on-site operator to act.
        import hid

        def present():
            return any((item.get('serial_number') or '').casefold() == self.serial.casefold()
                       for item in hid.enumerate(0x0d28, 0x0204) if item.get('usage_page') == 0xff00)

        checked(present(), '拔插前未枚举到指定探针')
        self.ctx.manual('拔插指定探针的 USB 数据线')
        print(f'请仅断开探针 {self.serial} 的 USB 数据线，保持目标供电。', file=sys.stderr, flush=True)
        wait_for(present, lambda value: not value, 60, '未观察到指定探针消失')
        print(f'请接回探针 {self.serial} 的 USB 数据线。', file=sys.stderr, flush=True)
        try:
            wait_for(present, bool, 60, '未观察到同序列号探针重枚举')
        except (Exception, KeyboardInterrupt) as error:
            self.ctx.fail_unsafe('物理拔插后的目标与探针', error)
        return {'serial': self.serial, 'removed': True, 'reappeared': True}

    def artifact(self, folder, name, data):
        path = self.driver.write_json(folder, name, data)
        return self.file_reference(path)

    def file_reference(self, path):
        return {'path': path.relative_to(self.ctx.out).as_posix(), 'sha256': digest(path)['sha256']}

    def run_step(self, page, ending, next_page, folder):
        folder.mkdir(parents=True)
        if ending == 'USB 数据线拔插' and not self.ctx.allow_manual:
            return {'page': page, 'ending': ending, 'status': 'pending', 'reason': '物理断线需要 --allow-manual'}
        initial = {**self.arm(page, folder), 'serial': self.serial}
        row = {'page': page, 'ending': ending, 'before': self.artifact(folder, 'before.json', initial)}
        if ending == '切页':
            self.show(next_page)
            switched = self.probe(next_page, 'state')['tab']
            stopped = self.stop(page, folder)
            idle = self.close_workbench(next_page)
            after = {'nextPage': switched, 'stopped': stopped, 'idle': idle}
        elif ending == '空闲断开':
            stopped = self.stop(page, folder)
        elif ending == '工作中断开':
            checked(self.probe(page, 'disconnect') is True, '工作中顶栏断开未确认')
            stopped = {'activeDisconnect': True}
        else:
            stopped = self.usb_cycle()
        if ending != '切页':
            idle_before_reconnect = self.close_workbench(page)
            again = folder / 'reconnected'
            again.mkdir()
            started = time.monotonic()
            live = {**self.arm(page, again, after_disconnect=True), 'serial': self.serial}
            reconnect_seconds = time.monotonic() - started
            final = self.stop(page, again)
            idle = self.close_workbench(page)
            after = {'stopped': stopped, 'reconnected': live, 'reconnectSeconds': reconnect_seconds,
                     'final': final, 'idleBeforeReconnect': idle_before_reconnect, 'idle': idle}
        row.update(status='pass', after=self.artifact(folder, 'after.json', after))
        return row

    def run(self, plan):
        folder = self.driver.folder('steps')
        rows = []
        try:
            self.prepare()
            for index, page in enumerate(plan['order']):
                ending = plan['endings'][page]
                step_dir = folder / f'{index + 1:02d}-{page}'
                next_page = plan['order'][(index + 1) % len(plan['order'])]
                rows.append(self.run_step(page, ending, next_page, step_dir))
                self.driver.write_json(folder, 'progress.json', rows)
        except Pending as error:
            self.cleanup()
            return {**plan, 'steps': rows, 'status': 'pending', 'reason': str(error)}
        except Unsafe:
            raise
        except (Exception, KeyboardInterrupt) as error:
            self.cleanup()
            return {**plan, 'steps': rows, 'status': 'fail', 'reason': str(error)}
        status = 'pending' if any(step['status'] != 'pass' for step in rows) else 'pass'
        return {**plan, 'status': status, 'steps': rows}

    def cleanup(self):
        if not self.started:
            return
        try:
            state = self.summary('dbg')
            if state.get('halted') and self.app_running:
                self.web.invoke('dbg', 'run', target='session')
            self.close_workbench()
        except Exception as error:
            self.ctx.fail_unsafe('跨页压力恢复', error)
