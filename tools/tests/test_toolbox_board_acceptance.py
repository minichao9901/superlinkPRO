"""离线回归：板卡验收 runner 的计划、判定与门控（不接触任何硬件）。

覆盖：
1. 计划展开与可复算（三板实例数、arm-jtag 适用范围、ETM 不适用）；
2. 链路别名与拒绝；
3. 夹具解析（唯一清单 + ELF 符号契约）；
4. 时钟判定（F103ZE 用 SWS、H743 用 RCC_CFGR.SWS，错误配置必须报 Pending）；
5. 性能判定辅助（组率、跳拍、窗内丢样、baseline 缺失只能待定标）；
6. execute 的 5 项编排逻辑（用假浏览器队列，验证判定与恢复锁定）；
7. 门控（plan 不碰硬件、flash 需 --allow-flash、Unsafe 锁存）；
8. review 的四类拒绝与退出码。
"""

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 必须与 board_acceptance.py 内部 ``from tools import ...`` 用同一个模块对象，
# 否则测试里打的补丁不会作用到被测代码。
from tools import board_acceptance as checks            # noqa: E402
from tools import board_acceptance_rtt as rtt_evidence  # noqa: E402
from tools.board_acceptance_stress import BoardStress  # noqa: E402
from tools import board_acceptance_scope as scope_mod   # noqa: E402
from tools import board_acceptance_target as facts      # noqa: E402
from tools import run_board_acceptance as runner        # noqa: E402


def scope_samples(channels=1, count=4):
    """构造 J-Scope 样本：值随行号变化，便于逐值对账。"""
    rows = []
    for index in range(count):
        rows.append([index * 10 + channel for channel in range(channels)])
    return rows


def build_packet(kind, payload=b'', *, version=scope_mod.VERSION_TICKS, count=0,
                 tick=0, seq=0):
    packet = bytearray(scope_mod.PACKET)
    struct.pack_into('<H', packet, 0, scope_mod.MAGIC)
    packet[2] = version
    packet[3] = kind
    struct.pack_into('<II', packet, 4, seq, tick)
    struct.pack_into('<HH', packet, 12, count, len(payload))
    packet[scope_mod.HEADER:scope_mod.HEADER + len(payload)] = payload
    return bytes(packet)


def build_jsp(rows, addresses=None, ticks=24):
    """按 web/app/scope/protocol.js 的格式拼一个最小 .jsp。"""
    channels = len(rows[0]) if rows else 1
    addresses = addresses or [0x20000000 + 4 * index for index in range(channels)]
    def_payload = struct.pack('<IIHBB', 1_000_000, ticks, 0, channels, 1)
    for address in addresses:
        def_payload += struct.pack('<IBBxx', address, 4, 4)
    data_payload = b''.join(struct.pack('<I', value) for row in rows for value in row)
    return (build_packet(scope_mod.KIND_DEF, def_payload)
            + build_packet(scope_mod.KIND_DATA, data_payload, count=len(rows), tick=24, seq=1))


def build_csv(rows, names=None, period=1):
    channels = len(rows[0]) if rows else 1
    names = names or ['ch%d' % index for index in range(channels)]
    lines = ['t_us,' + ','.join(names)]
    for index, row in enumerate(rows):
        lines.append(','.join([str(round(1 + index * period))] + [str(value) for value in row]))
    return ('\n'.join(lines) + '\n').encode('utf-8')


class FakeWeb:
    """假的浏览器队列：真实内存 + 可编排的页面状态，用来跑 execute 分支。"""

    def __init__(self, board='f103ze', memory=None, size=0x4000, base=0x20000000):
        self.runtime = None
        self.board = board
        self.base = base
        self.memory = bytearray(memory if memory is not None else bytes(size))
        self.pages = {}
        self.calls = []
        self.fail_restore = False
        self.heartbeat = 0x1000
        self.heartbeat_step = 1
        self.counter_address = None
        # J-Scope 判定旋钮
        self.scope_actual_ticks = None   # None = 与请求一致
        self.scope_tail = 0              # STOP 尾差
        self.scope_metrics_missing = False
        self.scope_empty = False
        self.scope_unstable = False

    # -- 布局 ------------------------------------------------------------
    def layout(self, ranges):
        for address, length in ranges:
            offset = address - self.base
            if offset < 0:
                self.base = address
                self.memory = bytearray(length) + self.memory
            elif offset + length > len(self.memory):
                self.memory.extend(bytes(offset + length - len(self.memory)))

    def _slice(self, address, length):
        self.layout([(address, length)])
        offset = address - self.base
        return slice(offset, offset + length)

    # -- 队列 ------------------------------------------------------------
    def ping(self):
        return {'ok': True}

    def page(self, page, op, **fields):
        self.calls.append((page, op, fields))
        return self.invoke(page, fields.get('method'), fields.get('args'), target='session')

    def state(self, page):
        if page == 'scope':
            return {'page': page, 'summary': self.scope_summary()}
        return {'page': page, 'summary': self.pages.get(page, {})}

    def set_control(self, page, selector, value):
        self.calls.append((page, 'set', {'selector': selector, 'value': value}))
        if selector == '#d-mem-addr':
            self.pages['memAddr'] = int(str(value), 0)
        elif selector == '#d-mem-len':
            self.pages['memLenInput'] = int(str(value), 0)
        elif page == 'scope':
            self.pages.setdefault('scope', {})
            self.pages['scope'].setdefault('controls', {})[selector] = value

    def click(self, page, selector):
        self.calls.append((page, 'click', {'selector': selector}))
        if selector == '#d-mem-read':
            # 页面的「读内存」按钮按输入长度刷新 memLen
            length = self.pages.get('memLenInput', 0)
            address = self.pages.get('memAddr', 0)
            self.read_memory(address, length)
            self.pages.setdefault('dbg', {})['memLen'] = length
            self.pages.setdefault('dbg', {})['memAddr'] = address
            state = self.pages['dbg']
            state['manualMemReadId'] = state.get('manualMemReadId', 0) + 1
        elif page == 'scope' and selector == '#sc-start':
            self.scope_begin()
        elif page == 'scope' and selector == '#sc-stop':
            self.scope_end()

    def invoke(self, page, method, args=None, target='view'):
        args = args or []
        self.calls.append((page, method, args))
        if method == 'memRead':
            address, length = args
            return {'$bytes': list(self.memory[self._slice(address, length)])}
        if method == 'memWrite':
            address, payload = args
            data = bytes(payload['$bytes'] if '$bytes' in payload else [])
            if self.fail_restore and len(data) > 0x400:
                data = bytes(bytearray(len(data)))          # 恢复写入故意写坏
            self.memory[self._slice(address, len(data))] = data
            return {'written': len(data)}
        if method == 'runLine':
            return {'lines': [{'t': 'ok', 'c': 'ok'}]}
        if method == 'bpList':
            return self.pages.setdefault('bpList', [])
        if method == 'bpAdd':
            self.pages.setdefault('bpList', []).append({'addr': args[0]})
            return {'added': True}
        if method == 'bpDel':
            self.pages['bpList'] = [bp for bp in self.pages.get('bpList', [])
                                    if bp.get('addr') != args[0]]
            return {'removed': True}
        if method == 'readIdcode':
            return {'idcode': '0x2ba01477'}
        # -- J-Scope 公开方法 --
        if page == 'scope':
            state = self.scope_state()
            if method == 'loadElfFile':
                state['elf'] = {'name': args[0].get('name') if isinstance(args[0], dict)
                                else 'fw.elf'}
                state.setdefault('mode', 'real')
                return True
            if method == 'toggleVar':
                variable, enable = args
                selected = state.setdefault('selected', [])
                key = (variable.get('name'), variable.get('addr'))
                if enable and key not in [(name, addr) for name, addr in selected]:
                    selected.append(key)
                elif not enable:
                    state['selected'] = [item for item in selected if item != key]
                state['vars'] = ['%s:u32@0x%x' % (name, addr)
                                 for name, addr in state.get('selected', [])]
                state['definitionVariables'] = [{'addr': addr, 'scalar': 'u32', 'size': 4}
                                                for _, addr in state.get('selected', [])]
                return True
            if method == 'start':
                self.scope_begin()
                return None
            if method == 'stop':
                self.scope_end()
                return None
            if method == 'readScopeMetrics':
                return self.scope_metrics()
            if method == 'saveRaw':
                self.scope_download('.jsp', self.scope_jsp())
                return None
            if method == 'exportCsv':
                self.scope_download('.csv', build_csv(self.scope_rows(),
                                    period=scope_mod.us_for_ticks(self.scope_period_ticks())))
                return None
        return None

    def word(self, address):
        return int.from_bytes(self.read_memory(address, 4), 'little')

    def read_memory(self, address, length):
        if address == 0x40021004:
            value = self.pages.get('RCC_CFGR', 0)
            return value.to_bytes(4, 'little')
        if self.counter_address and address == self.counter_address:
            value = self.heartbeat
            self.heartbeat += self.heartbeat_step
            return value.to_bytes(4, 'little')
        return bytes(self.memory[self._slice(address, length)])

    def write_memory(self, address, data):
        data = bytes(data)
        if self.fail_restore and len(data) > 0x400:
            data = bytes(bytearray(len(data)))      # 恢复写入故意写坏
        self.memory[self._slice(address, len(data))] = data

    def ack(self, job):
        return True

    # -- J-Scope 页面模型 -------------------------------------------------
    def scope_state(self):
        return self.pages.setdefault('scope', {})

    def scope_sample_count(self):
        return len(self.scope_rows())

    def scope_rows(self):
        channels = len(self.scope_state().get('selected', ())) or 1
        saved = self.pages.get('scope_samples')
        return saved if saved and len(saved[0]) == channels else scope_samples(channels)

    def scope_jsp(self):
        addresses = sorted(value for _, value in self.scope_state().get('selected', ()))
        return build_jsp(self.scope_rows(), addresses, self.scope_period_ticks())

    def scope_period_ticks(self):
        state = self.scope_state()
        period_us = float(state.get('controls', {}).get('#sc-period', 100) or 100)
        requested = int(round(period_us * scope_mod.TICK_HZ / 1_000_000))
        return self.scope_actual_ticks if self.scope_actual_ticks is not None else requested

    def scope_theory_rate(self):
        return round(scope_mod.TICK_HZ / max(self.scope_period_ticks(), 1))

    def scope_begin(self):
        state = self.scope_state()
        if state.get('mode') == 'mock':
            raise RuntimeError('mock 模式不能采样')
        count = 0 if self.scope_empty else self.scope_sample_count()
        state.update({'running': True, 'definitionReceived': True, 'metricsReads': 0,
                      'windowProduced': count, 'received': count, 'produced': count})

    def scope_metrics(self):
        """真实 METRICS 快照：窗口内读一次、STOP 后读两次。"""
        state = self.scope_state()
        if self.scope_metrics_missing:
            return None
        state['metricsReads'] = state.get('metricsReads', 0) + 1
        if state.get('running'):
            produced = 0 if state['metricsReads'] == 1 else state['windowProduced']
        else:
            produced = state['windowProduced'] + self.scope_tail
            if self.scope_unstable:
                # 模拟 STOP 后计数仍在涨（例如排空没完成）
                produced += max(state['metricsReads'] - 1, 0)
        actual = self.scope_period_ticks()
        metrics = {'produced': produced, 'skipped': 0, 'usb': 0, 'errors': 0, 'yields': 0,
                   'periodTicks': actual, 'invalidSamples': 0,
                   'bytes': len(self.scope_jsp()), 'flags': 256 if state.get('running') else 0,
                   'tick': produced * actual, 'hz': scope_mod.TICK_HZ}
        state['probeMetrics'] = metrics
        state['produced'] = produced
        return dict(metrics)

    def scope_summary(self):
        state = self.scope_state()
        metrics = state.get('probeMetrics')
        received = state.get('received', 0)
        produced = (metrics or {}).get('produced', state.get('produced', 0))
        invalid = (metrics or {}).get('invalidSamples') or 0
        theory = self.scope_theory_rate() if state.get('controls') else 0
        return {
            'state': 'running' if state.get('running') else 'idle',
            'running': bool(state.get('running')),
            'mode': state.get('mode', 'real'),
            'elf': state.get('elf'),
            'vars': state.get('vars'),
            'definitionReceived': bool(state.get('definitionReceived')),
            'definitionVariables': state.get('definitionVariables'),
            'samples': received, 'receivedSamples': received,
            'packets': 20 if received else 0,
            'rateHz': theory if received else 0,
            'lostProbe': 0, 'lostUsb': 0, 'readErrors': 0, 'dapYields': 0,
            'lostGap': 0, 'resyncs': 0,
            'tailCountDelta': received + invalid - produced if received else None,
            'probeMetrics': metrics,
            'plan': {'estHz': theory},
        }

    def scope_end(self):
        state = self.scope_state()
        state['running'] = False

    def scope_download(self, suffix, data):
        self.scope_state().setdefault('downloads', []).append(
            {'id': str(len(self.scope_state().get('downloads', [])) + 1),
             'name': 'scope-test' + suffix, 'bytes': len(data), '_data': data})

    def downloads(self, page):
        if page != 'scope':
            return []
        return [{key: value for key, value in entry.items() if key != '_data'}
                for entry in self.scope_state().get('downloads', [])]

    def fetch_download(self, page, entry):
        for item in self.scope_state().get('downloads', []):
            if item['id'] == entry['id']:
                return item['_data']
        raise RuntimeError('下载 ID 不存在')


def workspace_tmp():
    """工作区内的临时目录：沙箱不允许在系统 TEMP 下建目录。"""
    folder = ROOT / 'tmp' / ('acceptance-test-' + next(tempfile._get_candidate_names()))
    folder.mkdir(parents=True, exist_ok=False)
    return folder


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = workspace_tmp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


def make_context(board='f103ze', link='swd', mode='execute', web=None, out=None,
                 allow_flash=False):
    return facts.RunContext(board, link, mode, out, web=web, allow_flash=allow_flash)


# --------------------------------------------------------------------------
# 1. 计划
# --------------------------------------------------------------------------


class PlanTests(unittest.TestCase):
    def test_stress_schedule_is_stable_for_split_link_runs(self):
        groups = ('D', 'V', 'R', 'J')
        full = runner.plan_stress(('arm-jtag', 'swd'), groups, 17)
        single = runner.plan_stress(('swd',), groups, 17)
        self.assertEqual(full, single)
        self.assertEqual(len({tuple(row['order']) for row in single}), 3)
        with self.assertRaisesRegex(ValueError, '实际步骤缺项'):
            runner.verify_stress_steps(ROOT, {**single[0], 'status': 'pass'})

    def test_three_boards_expand_to_seven_groups_of_five(self):
        for board, expected_cases, expected_instances in (
                ('f103ze', 14, 70), ('h743', 14, 70), ('hpm6800evk', 7, 35)):
            cases, instances = checks.plan_cases(board)
            self.assertEqual(len(cases), expected_cases, board)
            self.assertEqual(len(instances), expected_instances, board)
            for case in cases:
                self.assertEqual(len(case['instances']), 5, case['id'])
            self.assertEqual({row['instance'] for row in instances},
                             set(facts.INSTANCE_IDS))

    def test_arm_jtag_keeps_native_case_ids_outside_workbench_plan(self):
        cases, instances = checks.plan_cases('f103ze', ['arm-jtag'])
        by_group = {case['group']: case for case in cases}
        self.assertNotIn('notApplicable', by_group['S'])
        for group in ('F', 'D', 'V', 'R', 'J'):
            self.assertNotIn('notApplicable', by_group[group], group)
        # ETM 仍是 1 MHz SWD 专属
        self.assertEqual(by_group['E']['notApplicable'],
                         '本仓 ETM 固定用 Kingst 采集与 1 MHz SWD 控制；'
                         'arm-jtag 链路下不采 ETM')
        rows = {row['instance']: row for row in instances}
        for instance in ('V01', 'R01', 'J03'):
            self.assertNotIn('notApplicable', rows[instance], instance)
        self.assertEqual(runner.workbench_links('f103ze'), ['swd'])
        self.assertEqual(runner.workbench_links('h743'), ['swd'])
        self.assertEqual(runner.workbench_links('hpm6800evk', ['jtag']), ['jtag'])
        with self.assertRaisesRegex(ValueError, '原生'):
            runner.workbench_links('h743', ['jtag'])

    def test_hpm_etm_is_not_applicable_with_reason(self):
        cases, _ = checks.plan_cases('hpm6800evk', ['jtag'])
        etm = [case for case in cases if case['group'] == 'E'][0]
        self.assertEqual(etm['notApplicable'], 'HPM6800EVK（RISC-V）没有 ARM ETM')

    def test_plan_is_reproducible_by_seed(self):
        first = checks.plan_cases('h743', None, None, 7)
        second = checks.plan_cases('h743', None, None, 7)
        self.assertEqual(first, second)

    def test_group_and_link_selection(self):
        cases, instances = checks.plan_cases('f103ze', ['swd'], ['S'])
        self.assertEqual([case['id'] for case in cases], ['swd-S'])
        self.assertEqual({row['instance'] for row in instances},
                         {'S01', 'S02', 'S03', 'S04', 'S05'})

    def test_link_alias_normalizes_but_hpm_has_no_swd(self):
        self.assertEqual(facts.normalize_link('f103ze', 'jtag'), 'arm-jtag')
        self.assertEqual(facts.normalize_link('hpm6800evk', 'jtag'), 'jtag')
        with self.assertRaises(facts.Pending):
            facts.normalize_link('hpm6800evk', 'swd')


# --------------------------------------------------------------------------
# 2. 夹具与离线前置
# --------------------------------------------------------------------------


class FixtureTests(unittest.TestCase):
    def test_fixtures_resolve_from_single_matrix_with_symbols(self):
        for board, kind, role, address in (
                ('f103ze', 'rtt', 'rtt', 0x2000000C),
                ('f103ze', 'scope', 'tick', 0x20001044),
                ('f103ze', 'debug', 'tick', 0x200000A0),
                ('h743', 'scope', 'sysclkHz', 0x24001058),
                ('h743', 'debug', 'tick', 0x240000A0),
                ('hpm6800evk', 'scope', 'mchtmrHz', 0x1240028),
                ('hpm6800evk', 'debug', 'tick', 0x1240000)):
            evidence = facts.fixture(board, kind)
            self.assertEqual(evidence['symbols'][role], address, f'{board}/{kind}/{role}')
            self.assertTrue(evidence['path'].startswith('web/tools/target-firmware/'))
            self.assertEqual(len(evidence['sha256']), 64)
            self.assertIn(evidence['source'], ('buildArtifact', 'publishedArtifact'))

    def test_offline_preconditions_report_missing_fixture(self):
        saved = facts.MATRIX
        try:
            facts.MATRIX = ROOT / 'tmp' / 'no-such-matrix.json'
            result = checks.offline_preconditions('f103ze', 'swd', 'S01')
            self.assertTrue(result['missing'], result)
            self.assertIn('夹具清单', result['missing'][0]['reason'])
        finally:
            facts.MATRIX = saved

    def test_flash_instances_require_allow_flash(self):
        result = checks.offline_preconditions('f103ze', 'swd', 'F01')
        self.assertTrue(any('--allow-flash' in item['reason'] for item in result['pending']))


# --------------------------------------------------------------------------
# 3. 时钟判定
# --------------------------------------------------------------------------


class ClockTests(unittest.TestCase):
    def test_stm32f1_uses_actual_sws_not_request(self):
        # SW=PLL(2)/SWS=PLL(2)、PLLSRC=HSE、PLLMUL=×9、HPRE=/1 → 72 MHz
        cfgr = (2 << 2) | (2 << 0) | (1 << 16) | (7 << 18)
        clock = facts.decode_stm32f1_clock({'CFGR': cfgr}, 8_000_000)
        self.assertEqual(clock['sysclkHz'], 72_000_000)
        self.assertEqual(clock['hclkHz'], 72_000_000)
        self.assertIn('SWS', clock['basis'])

    def test_stm32f1_hsi_default_is_8mhz(self):
        clock = facts.decode_stm32f1_clock({'CFGR': 0}, 8_000_000)
        self.assertEqual(clock['sysclkHz'], 8_000_000)
        self.assertEqual(clock['hclkHz'], 8_000_000)

    def test_stm32f1_reserved_sws_is_pending(self):
        with self.assertRaises(facts.Pending):
            facts.decode_stm32f1_clock({'CFGR': 3 << 2}, 8_000_000)

    def _h743_registers(self, **overrides):
        # 25 MHz HSE / DIVM1=5 / N=192 / P=2 → VCO 输入 5 MHz、PLL1P 480 MHz；
        # D1CPRE=÷1、HPRE=÷2 → CPU 480 MHz / HCLK 240 MHz。
        registers = {'CR': (1 << 17) | (1 << 25), 'CFGR': 3 << 3,
                     'D1CFGR': (0 << 8) | (8 << 0),
                     'PLLCKSELR': (5 << 4) | 2, 'PLLCFGR': 1 << 16,
                     'PLL1DIVR': (191 << 0) | (1 << 9)}
        registers.update(overrides)
        return registers

    def test_h743_clock_identity(self):
        clock = facts.decode_stm32h7_clock(self._h743_registers(), 25_000_000)
        self.assertEqual(clock['sysclkHz'], 480_000_000)
        self.assertEqual(clock['hclkHz'], 240_000_000)

    def test_h743_wrong_sws_is_pending(self):
        with self.assertRaises(facts.Pending):
            facts.decode_stm32h7_clock(self._h743_registers(CFGR=0), 25_000_000)

    def test_h743_fractional_pll_is_pending(self):
        with self.assertRaises(facts.Pending):
            facts.decode_stm32h7_clock(self._h743_registers(PLLCFGR=1), 25_000_000)

    def test_verify_clock_needs_actual_value(self):
        self.assertEqual(facts.verify_clock(60_000_000, 60_000_000)['actualHz'], 60_000_000)
        with self.assertRaises(facts.Pending):
            facts.verify_clock(None, 60_000_000)
        with self.assertRaises(RuntimeError):
            facts.verify_clock(45_000_000, 60_000_000)

    def test_verify_target_clock_without_expected_is_pending(self):
        verdict = facts.verify_target_clock({'sysclkHz': 24_000_000}, None, None)
        self.assertEqual(verdict['status'], 'pending')


# --------------------------------------------------------------------------
# 4. 性能判定
# --------------------------------------------------------------------------


class RateTests(unittest.TestCase):
    def test_scope_theory_and_floor(self):
        self.assertAlmostEqual(facts.scope_theory_hz(24), 1_000_000)
        self.assertAlmostEqual(facts.scope_rate_floor(24), 990_000)
        with self.assertRaises(RuntimeError):
            facts.scope_theory_hz(0)

    def test_scope_window_thresholds(self):
        good = facts.scope_window(1_000_000, 60_000_000, 1, 0, 24)
        self.assertTrue(good['skipOk'] and good['usbOk'] and good['rateOk'])
        # 5000/1e6 = 0.5% 跳拍，超过 0.1% 门槛
        bad_skip = facts.scope_window(1_000_000, 1_000_000, 5_000, 0, 24)
        self.assertFalse(bad_skip['skipOk'])
        # 5 ppm 属于允许范围
        fine_skip = facts.scope_window(1_000_000, 1_000_000, 5, 0, 24)
        self.assertTrue(fine_skip['skipOk'])
        bad_usb = facts.scope_window(1_000_000, 1_000_000, 0, 1, 24)
        self.assertFalse(bad_usb['usbOk'])
        slow = facts.scope_window(900_000, 1_000_000, 0, 0, 24)
        self.assertFalse(slow['rateOk'])

    def test_native_scope_rate_margin(self):
        row = {'nominalGroupsPerSecond': 1_000_000, 'groupsPerSecond': 1_000_500}
        self.assertTrue(facts.native_scope_rate_matches(row))
        row['groupsPerSecond'] = 1_100_000
        self.assertFalse(facts.native_scope_rate_matches(row))
        self.assertFalse(facts.native_scope_rate_matches({}))

    def test_rate_verdict_never_borrows_another_band(self):
        baselines = {45_000_000: 2.0, 60_000_000: 2.5}
        self.assertEqual(facts.rate_verdict(baselines, 60_000_000, 2.6)['status'], 'pass')
        self.assertEqual(facts.rate_verdict(baselines, 60_000_000, 2.4)['status'], 'fail')
        missing = facts.rate_verdict(baselines, 20_000_000, 3.0)
        self.assertEqual(missing['status'], 'pending')
        self.assertIn('不能借用', missing['reason'])
        self.assertEqual(facts.rate_verdict(None, 60_000_000, 3.0)['status'], 'pending')

    def test_measurement_median_and_pattern(self):
        row = facts.measurement(1 << 20, 1.0)
        self.assertAlmostEqual(row['mebibytesPerSecond'], 1.0)
        self.assertEqual(facts.median([3, 1, 2]), 2)
        self.assertEqual(facts.median([4, 1, 3, 2]), 2.5)
        data = facts.pattern(64)
        self.assertEqual(len(data), 64)
        self.assertNotEqual(data[:4], data[4:8])

    def test_differ_reports_first_mismatch(self):
        self.assertEqual(facts.differ(b'ab', b'ab'), {})
        self.assertEqual(facts.differ(b'ab', b'ax')['firstMismatch'], 1)
        self.assertIn('lengthMismatch', facts.differ(b'ab', b'abc'))

    def test_flash_ranges_align_to_sectors(self):
        ranges = facts.flash_ranges(0x08000000, 0x80000, 0x800, 0x08001000, 0x100)
        self.assertEqual(ranges, [(0x08001000, 0x08001800)])
        spanning = facts.flash_ranges(0x08000000, 0x80000, 0x800, 0x08000F00, 0x400)
        self.assertEqual(spanning, [(0x08000800, 0x08001800)])
        with self.assertRaises(RuntimeError):
            facts.flash_ranges(0x08000000, 0x80000, 0x800, 0x0807FF00, 0x400)

    def test_finish_priority(self):
        self.assertEqual(facts.finish(['pass', 'pending']), 'pending')
        self.assertEqual(facts.finish(['pass', 'fail']), 'fail')
        self.assertEqual(facts.finish(['blocked', 'pass']), 'blocked')
        self.assertEqual(facts.finish(['pass']), 'pass')


# --------------------------------------------------------------------------
# 5. 门控
# --------------------------------------------------------------------------


class GateTests(unittest.TestCase):
    def test_plan_mode_refuses_hardware(self):
        ctx = make_context(mode='plan')
        with self.assertRaises(facts.Pending):
            ctx.hardware('读内存')
        with self.assertRaises(facts.Pending):
            ctx.flash('烧录')

    def test_execute_without_runtime_is_pending(self):
        ctx = make_context(mode='execute', web=None)
        with self.assertRaises(facts.Pending):
            ctx.hardware('读内存')

    def test_flash_requires_explicit_allow(self):
        ctx = make_context(web=FakeWeb(), allow_flash=False)
        with self.assertRaises(facts.Pending):
            ctx.flash('烧录')
        ctx.allow_flash = True
        ctx.flash('烧录')      # 不抛即通过

    def test_manual_evidence_requires_authorization(self):
        ctx = make_context(web=FakeWeb())
        with self.assertRaises(facts.Pending):
            ctx.manual('物理拔插 USB')
        ctx.allow_manual = True
        self.assertEqual(ctx.manual('物理拔插 USB')['event'], 'operator')

    def test_uncertain_latches_and_blocks_further_hardware(self):
        ctx = make_context(web=FakeWeb(), allow_flash=True)
        ctx.hardware('第一步')
        ctx.uncertain = '恢复未确认'
        for action in (ctx.hardware, ctx.flash):
            with self.assertRaises(facts.Unsafe):
                action('后续动作')


# --------------------------------------------------------------------------
# 6. execute 编排（假队列）
# --------------------------------------------------------------------------


class ExecuteTests(TempCase):
    def _sram_context(self, fail_restore=False):
        # 预置非零内容，否则"恢复写入被写坏"和备份都全零，没法区分
        content = bytes((index * 7 + 3) & 0xFF for index in range(0x9000))
        web = FakeWeb(memory=content, base=0x20000000)
        web.fail_restore = fail_restore
        web.counter_address = 0x20001044
        ctx = make_context(web=web, out=self.tmp)
        return ctx, web, facts.BOARDS['f103ze']['sram']

    def test_sram_group_emits_five_rows(self):
        ctx, web, profile = self._sram_context()
        driver = checks.Driver(ctx)
        case = {'id': 'swd-S', 'group': 'S', 'link': 'swd', 'requestedHz': 60_000_000}
        results = driver.sram(case)
        self.assertEqual(set(results), {'S01', 'S02', 'S03', 'S04', 'S05'})
        self.assertEqual(results['S01']['status'], 'pass')
        self.assertEqual(results['S03']['status'], 'pass')
        self.assertEqual(results['S04']['status'], 'pass')
        self.assertEqual(results['S05']['status'], 'pass')
        # 没有批准下限 → S02 只报基线
        self.assertEqual(results['S02']['status'], 'pending')
        self.assertIn('baseline', results['S02']['evidence'])
        self.assertEqual(len(ctx.records), 5)

    def test_sram_restore_mismatch_latches_unsafe_and_fails(self):
        ctx, web, profile = self._sram_context()
        web.fail_restore = True
        driver = checks.Driver(ctx)
        case = {'id': 'swd-S', 'group': 'S', 'link': 'swd', 'requestedHz': 60_000_000}
        results = driver.sram(case)
        self.assertEqual(results['S04']['status'], 'fail')
        self.assertIsNotNone(ctx.uncertain)
        with self.assertRaises(facts.Unsafe):
            ctx.hardware('后续')

    def test_flash_group_stops_without_allow_flash(self):
        ctx = make_context(web=FakeWeb())
        driver = checks.Driver(ctx)
        with self.assertRaises(facts.Pending):
            driver.flash({'id': 'swd-F', 'group': 'F', 'link': 'swd', 'requestedHz': 60_000_000})
        self.assertEqual(ctx.records, [])

    def test_debug_identity_reports_clock_from_rcc(self):
        web = FakeWeb()
        # SWS=PLL、PLLSRC=HSE、PLLMUL=×9 → 72 MHz
        web.pages['RCC_CFGR'] = (2 << 2) | (2 << 0) | (1 << 16) | (7 << 18)
        ctx = make_context(web=web)
        web.pages['dbg'] = {'connected': True, 'backend': 'openocd', 'clockKhz': 60000,
                            'regs': 17, 'pc': 0x8000051, 'bpCap': 6, 'elf': {'name': 'fixture.elf'},
                            'idcode': 0x1ba01477}
        driver = checks.Driver(ctx)
        row = driver.debug_identity({'path': 'x', 'sha256': 'y'},
                                    web.pages['dbg'], 0.5)
        self.assertEqual(row['status'], 'pass')
        self.assertEqual(row['evidence']['targetClock']['measured']['sysclkHz'],
                         72_000_000)

    def test_debug_identity_pending_when_clock_differs(self):
        web = FakeWeb()
        web.pages['RCC_CFGR'] = 0          # HSI 8 MHz，与登记的 72 MHz 不符
        ctx = make_context(web=web)
        driver = checks.Driver(ctx)
        row = driver.debug_identity({'path': 'x', 'sha256': 'y'},
                                    {'connected': True, 'backend': 'openocd'}, 0.5)
        self.assertEqual(row['status'], 'pending')

    def test_jscope_reports_gap_when_page_never_reports_metrics(self):
        """缺整组计数 → 「不支持/缺口」，绝不能算通过。"""
        ctx = make_context(web=FakeWeb(), out=self.tmp)
        driver = checks.Driver(ctx)
        row = driver._scope_stop_row({'gaps': ['缺少整组计数（探针 METRICS 未上报或页面未取到）'],
                                      'problems': []})
        self.assertEqual(row['status'], 'unsupported')
        self.assertIn('缺', row['reason'])

    def test_jscope_fails_on_nonzero_stop_tail(self):
        ctx = make_context(web=FakeWeb(), out=self.tmp)
        driver = checks.Driver(ctx)
        row = driver._scope_stop_row({'gaps': [], 'problems': ['STOP 尾差 3'],
                                      'produced': 100, 'tailCountDelta': 3})
        self.assertEqual(row['status'], 'fail')

    def test_jscope_passes_only_with_clean_counters(self):
        ctx = make_context(web=FakeWeb(), out=self.tmp)
        driver = checks.Driver(ctx)
        row = driver._scope_stop_row({'gaps': [], 'problems': [], 'produced': 1000,
                                      'skipped': 0, 'usbDrops': 0, 'readErrors': 0,
                                      'dapYields': 0, 'invalidSamples': 0,
                                      'tailCountDelta': 0, 'lostGap': 0})
        self.assertEqual(row['status'], 'pass')

    def test_folder_requires_out_directory(self):
        """证据必须落在 --out；不允许退回当前工作目录。"""
        ctx = make_context(web=FakeWeb())
        driver = checks.Driver(ctx)
        with self.assertRaises(facts.Pending):
            driver.folder('sram')
        self.assertFalse((ROOT / 'sram').exists())

    def test_run_checks_idle_after_case(self):
        ctx = make_context(web=FakeWeb())
        driver = checks.Driver(ctx)
        case = {'id': 'swd-E', 'group': 'E', 'link': 'swd', 'requestedHz': 1_000_000}
        driver.etm = lambda case: {'E01': driver.record('E01', 'pending', reason='x')}
        driver.run(case)
        self.assertTrue(any(event['event'] == 'idle' for event in ctx.events))


# --------------------------------------------------------------------------
# 7. review
# --------------------------------------------------------------------------


class StressLifecycleTests(TempCase):
    def make_stress(self, allow_manual=False):
        ctx = make_context(web=FakeWeb(), out=self.tmp)
        ctx.allow_manual = allow_manual
        ctx.case = 'swd-stress-1'
        ctx.probe_serial = 'PROBE-123'

        class SimulatedStress(BoardStress):
            def __init__(self, driver):
                super().__init__(driver, {'requestedHz': 60_000_000})
                self.events = []

            def prepare(self):
                self.events.append('prepare')

            def arm(self, page, folder, after_disconnect=False):
                self.events.append((page, 'arm', after_disconnect))
                return {'active': True, 'halted': True, 'pc': 0x8000000, 'steppedPc': 0x8000002}

            def probe(self, page, action, *args):
                self.events.append((page, action))
                if action == 'state':
                    return {'tab': page}
                return True

            def stop(self, page, folder):
                self.events.append((page, 'stop'))
                return {'targetState': 'running'}

            def close_workbench(self, page='rtt'):
                self.events.append((page, 'idle'))
                from tools.board_acceptance_stress import IDLE_EXPECTED
                return IDLE_EXPECTED.copy()

            def show(self, page):
                self.events.append((page, 'show'))

            def usb_cycle(self):
                self.events.append('usb-cycle')
                return {'serial': self.serial, 'removed': True, 'reappeared': True}

        return SimulatedStress(checks.Driver(ctx))

    def test_stress_executes_three_automatic_modes_without_touching_manual_mode(self):
        stress = self.make_stress()
        plan = {'order': ['rtt', 'rttcdc', 'scope', 'dbg'], 'endings': {
            'rtt': '切页', 'rttcdc': '空闲断开', 'scope': '工作中断开', 'dbg': 'USB 数据线拔插'}}
        result = stress.run(plan)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual([step['status'] for step in result['steps']], ['pass', 'pass', 'pass', 'pending'])
        self.assertIn(('rttcdc', 'show'), stress.events)
        self.assertLess(stress.events.index(('rttcdc', 'show')), stress.events.index(('rtt', 'stop')))
        self.assertNotIn('usb-cycle', stress.events)
        self.assertNotIn(('dbg', 'arm', False), stress.events)
        index = stress.events.index(('scope', 'arm', False))
        self.assertEqual(stress.events[index + 1:index + 5], [
            ('scope', 'disconnect'), ('scope', 'idle'), ('scope', 'arm', True), ('scope', 'stop')])

    def test_review_verifies_the_reconnected_business_not_just_artifact_names(self):
        stress = self.make_stress(allow_manual=True)
        result = stress.run({'order': ['dbg'], 'endings': {'dbg': 'USB 数据线拔插'}})
        step = result['steps'][0]
        after_file = self.tmp / step['after']['path']
        after = json.loads(after_file.read_text(encoding='utf-8'))
        runner.verify_stress_steps(self.tmp, result)
        after['reconnected']['steppedPc'] = after['reconnected']['pc']
        after_file.write_text(json.dumps(after), encoding='utf-8')
        step['after']['sha256'] = facts.digest(after_file)['sha256']
        with self.assertRaisesRegex(ValueError, '单步进展'):
            runner.verify_stress_steps(self.tmp, result)

    def test_review_rejects_unspecified_idle_snapshot(self):
        stress = self.make_stress(allow_manual=True)
        result = stress.run({'order': ['dbg'], 'endings': {'dbg': 'USB 数据线拔插'}})
        step = result['steps'][0]
        after_file = self.tmp / step['after']['path']
        after = json.loads(after_file.read_text(encoding='utf-8'))
        after['idle'] = True
        after_file.write_text(json.dumps(after), encoding='utf-8')
        step['after']['sha256'] = facts.digest(after_file)['sha256']
        with self.assertRaisesRegex(ValueError, '没有释放会话'):
            runner.verify_stress_steps(self.tmp, result)


class RttTransferTests(TempCase):
    def test_serial_binding_refuses_second_matching_cdc(self):
        from types import SimpleNamespace

        probe = SimpleNamespace(vid=0x0d28, pid=0x0204, serial_number='ABCD', device='COM52')
        other = SimpleNamespace(vid=0x0d28, pid=0x0204, serial_number='EFGH', device='COM53')
        self.assertEqual(rtt_evidence.probe_port([probe], 'abcd'), 'COM52')
        for ports, serial in [([probe, other], 'ABCD'), ([probe], 'wrong')]:
            with self.assertRaisesRegex(ValueError, '唯一匹配'):
                rtt_evidence.probe_port(ports, serial)

    def test_rtt_sequence_rejects_garbage_inside_or_between_windows(self):
        good = b'999\r\nseq 0000000\r\nseq 0000001\r\nseq 000'
        self.assertTrue(rtt_evidence.check_payload(good, 'seq')['exact'])
        for raw in (good.replace(b'0000001', b'0000002'), good.replace(b'\r\nseq 0000001', b'\r\nBADseq 0000001')):
            self.assertFalse(rtt_evidence.check_payload(raw, 'seq')['exact'])

    def test_three_packet_sizes_and_different_channels(self):
        rb = rtt_evidence
        address = rb.boundary_address('f103ze', facts.ROOT / facts.fixture('f103ze', 'rtt-seq')['path'], 3328)
        image, payloads = rb.boundary_fixture(address, 512)
        self.assertEqual([len(blob) for blob in payloads], [511, 512, 513])
        descriptor_offsets = [24 * (channel + 1) for channel in range(3)]
        for channel, offset in enumerate(descriptor_offsets):
            populated = rb.ring_state(image[:offset + 24], address, channel)
            self.assertEqual((populated['pending'], populated['rd']), (len(payloads[channel]), 0))
            ptr = populated['pointer'] - address
            self.assertEqual(image[ptr:ptr + populated['pending']], payloads[channel])

    def test_viewer_replays_both_ring_tails_including_sequence_rollover(self):
        address = 0x2000a000
        image, expected, raw = rtt_evidence.viewer_fixture(address)
        ring = rtt_evidence.ring_state(image[:48], address)
        self.assertEqual((ring['rd'], ring['pending']), (500, len(raw)))
        guard = b'G' * 64
        backup = b'S' * len(image)
        originals = guard + backup + guard
        readback = guard + expected + guard
        evidence = rtt_evidence.check_viewer_fixture(address, originals, originals, readback, raw)
        self.assertEqual(evidence['sequence']['records'], 32)
        self.assertEqual(evidence['sequence']['last'], 23)
        self.assertEqual(evidence['wrOffAfter'], 4)
        files = {'before': originals, 'restored': originals, 'readback': readback, 'upstream': raw}
        artifacts = {}
        for key, data in files.items():
            path = self.tmp / f'{key}.bin'
            path.write_bytes(data)
            artifacts[key] = {'path': path.name, 'sha256': facts.sha(data)}
        row = {'evidence': {**evidence, 'probeSerial': 'ABCD', 'lost': 0, 'corrupt': 0, 'artifacts': artifacts}}
        runner.verify_viewer_result(self.tmp, row)
        # Update the fingerprint too, so the semantic check has to catch the bad cursor.
        moved = bytearray(readback)
        moved[64 + 48 + 12] ^= 1
        (self.tmp / 'readback.bin').write_bytes(moved)
        artifacts['readback']['sha256'] = facts.sha(moved)
        with self.assertRaisesRegex(ValueError, '游标'):
            runner.verify_viewer_result(self.tmp, row)

    def test_boundary_scratch_restores_original_bytes_after_failed_window(self):
        class ScratchWeb(FakeWeb):
            def invoke(self, page, method, args=None, target='view'):
                if page == 'dbg' and method in ('halt', 'run', 'disconnect', 'connect'):
                    halted = method == 'halt'
                    self.pages['dbg'] = {'probeSerial': 'ABCD', 'halted': halted,
                                         'targetState': ('disconnected' if method == 'disconnect'
                                                         else 'halted' if halted else 'running')}
                    return True
                if page == 'dbg' and method == 'readReg':
                    return 0x2000fff0
                return super().invoke(page, method, args, target)

        address = 0x2000a000
        image, _ = rtt_evidence.boundary_fixture(address, 512)
        web = ScratchWeb(memory=b'Z' * 65536)
        ctx = make_context(web=web, out=self.tmp)
        driver = checks.Driver(ctx)
        with self.assertRaisesRegex(ValueError, 'injected'):
            with driver.rtt_scratch({'requestedHz': 60_000_000}, 'ABCD', address, image, self.tmp):
                self.assertEqual(web.read_memory(address, len(image)), image)
                raise ValueError('injected')
        self.assertEqual(web.read_memory(address - 64, len(image) + 128), b'Z' * (len(image) + 128))
        self.assertEqual(driver.target_state()['running'], 'disconnected')
        self.assertIsNone(ctx.uncertain)

    def test_stop_requires_host_count_and_modular_target_cursor(self):
        image, _ = rtt_evidence.boundary_fixture(0x24020000, 512)
        before = {**rtt_evidence.ring_state(image[:48], 0x24020000), 'rd': 1000}
        after = {**before, 'rd': 16, 'pending': 51}
        stop = {'running': False, 'rdErr': 0, 'wrErr': 0, 'moved': 40}
        self.assertEqual(rtt_evidence.check_window(before, after, 40, stop)['ringLeft'], 51)
        with self.assertRaisesRegex(ValueError, 'RdOff'):
            rtt_evidence.check_window(before, {**after, 'rd': 17}, 40, stop)
        with self.assertRaisesRegex(ValueError, '主机实收'):
            rtt_evidence.check_window(before, after, 39, stop)

    def test_review_recomputes_web_and_native_counts_from_raw(self):
        root = self.tmp / 'rttevidence'
        (root / 'native').mkdir(parents=True)
        unit = b'seq 0000000\r\nseq 0000001\r\n'
        following = b'seq 0000002\r\nseq 0000003\r\n'
        native = b'seq 0000004\r\nseq 0000005\r\n'
        stop = {'running': False, 'rdErr': 0, 'wrErr': 0, 'moved': len(unit), 'stableMoved': True}
        window = {'complete': True, 'canRestart': True, 'stop': stop, 'seconds': 60.01}
        before = {'address': 0x20000014, 'pointer': 0x20000100, 'size': 128, 'rd': 0, 'flags': 2}
        peer = {'targetElf': 'rtt-seq', 'probeSerial': 'ABCD'}
        evidence = {**peer, 'format': 'seq', 'ring': before,
                    'ringAfterWeb': {**before, 'rd': 52, 'wr': 52, 'pending': 0},
                    'ringAfterNative': {**before, 'rd': 78, 'wr': 78, 'pending': 0},
                    'web': [{**window, 'sha256': facts.sha(raw)} for raw in (unit, following)],
                    'cli': {'stop': stop, 'sha256': facts.sha(native), 'seconds': 60.01}}
        for name, raw in [('web-1.bin', unit), ('web-2.bin', following), ('native/capture.bin', native)]:
            (root / name).write_bytes(raw)
        manifest = root / 'comparison.json'
        manifest.write_text(json.dumps(evidence), encoding='utf-8')
        row = {'instance': 'R04', 'evidence': {**peer, 'captureEvidence': {'path': manifest.name,
                                                                         'sha256': facts.digest(manifest)['sha256']}}}
        runner.verify_rtt_result(root, row)
        r01 = {'instance': 'R01', 'board': 'h743',
               'evidence': {**row['evidence'], 'webSeconds': 60.01, 'cliSeconds': 60.01,
                            'sequence': rtt_evidence.check_payload(unit + following + native, 'seq')}}
        runner.verify_rtt_result(root, r01)
        evidence['cli']['seconds'] = 59.2
        manifest.write_text(json.dumps(evidence), encoding='utf-8')
        row['evidence']['captureEvidence']['sha256'] = facts.digest(manifest)['sha256']
        with self.assertRaisesRegex(ValueError, '各跑满 60 秒'):
            runner.verify_rtt_result(root, r01)
        (root / 'web-2.bin').write_bytes(unit)
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            runner.verify_rtt_result(root, row)

    def test_review_does_not_replace_measured_results_with_other_fixture(self):
        for instance, other in (('R01', 'rtt'), ('R02', 'rtt-seq')):
            row = {'instance': instance, 'status': 'pending', 'evidence': {'targetElf': other}}
            self.assertTrue(runner.pending_rtt_fixture(row))
            self.assertFalse(runner.pending_rtt_fixture({**row, 'status': 'fail'}))


class ReviewTests(TempCase):
    def _report(self, folder, **overrides):
        board = 'f103ze'
        cases, instances = checks.plan_cases(board, ['swd'], ['S'], 20261122)
        fixture = facts.fixture(board, 'scope')
        files = {'scope': {'path': fixture['path'], 'sha256': fixture['sha256']}}
        results = []
        for row in instances:
            identity = {key: row[key] for key in ('instance', 'case', 'link', 'board')}
            if row['instance'] == 'S02':
                results.append({**identity, 'status': 'pending', 'reason': 'baseline'})
            else:
                results.append({**identity, 'status': 'pass',
                                'evidence': {'targetElf': 'scope',
                                             'elf': {'sha256': fixture['sha256']}}})
        report = {'board': board, 'label': 'STM32F103ZE', 'mode': 'execute',
                  'seed': 20261122, 'plan': cases, 'instances': instances,
                  'files': files, 'results': results, 'status': 'incomplete',
                  'accepted': False, 'finishedAt': '2026-11-22T10:00:00Z',
                  'revision': 'a' * 40, 'sourceDirty': False}
        report.update(overrides)
        folder.mkdir(parents=True, exist_ok=True)
        report['digest'] = facts.sha(json.dumps(report['results'], ensure_ascii=False, sort_keys=True).encode())
        (folder / 'results.jsonl').write_text(
            ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in report['results']), encoding='utf-8')
        (folder / 'summary.json').write_text(
            json.dumps(report, ensure_ascii=False), encoding='utf-8')
        return report

    def test_review_accepts_complete_evidence_and_reports_incomplete(self):
        folder = self.tmp / 'run'
        self._report(folder)
        code = runner.review([folder])
        self.assertEqual(code, 2)          # 仍有 pending → incomplete

    def test_review_rejects_unfinished_session(self):
        folder = self.tmp / 'run'
        self._report(folder, status='running', finishedAt=None)
        with self.assertRaises(ValueError):
            runner.review([folder])

    def test_review_rejects_dirty_source(self):
        folder = self.tmp / 'run'
        self._report(folder, sourceDirty=True)
        with self.assertRaises(ValueError):
            runner.review([folder])

    def test_review_rejects_tampered_plan(self):
        folder = self.tmp / 'run'
        report = self._report(folder)
        report['plan'][0]['requestedHz'] = 1
        (folder / 'summary.json').write_text(json.dumps(report), encoding='utf-8')
        with self.assertRaises(ValueError):
            runner.review([folder])

    def test_review_rejects_divergent_results_journal(self):
        folder = self.tmp / 'run'
        self._report(folder)
        (folder / 'results.jsonl').write_text('{}\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'JSONL'):
            runner.review([folder])

    def test_review_rejects_pass_without_matching_fixture_evidence(self):
        folder = self.tmp / 'run'
        report = self._report(folder)
        report['results'][0]['evidence']['elf']['sha256'] = 'b' * 64
        (folder / 'summary.json').write_text(json.dumps(report), encoding='utf-8')
        with self.assertRaises(ValueError):
            runner.review([folder])

    def test_review_prefers_latest_and_detects_conflicting_identity(self):
        first = self.tmp / 'a'
        second = self.tmp / 'b'
        self._report(first, finishedAt='2026-11-22T10:00:00Z')
        self._report(second, finishedAt='2026-11-22T11:00:00Z')
        self.assertIn(runner.review([first, second]), (1, 2))
        self._report(second, revision='c' * 40)
        with self.assertRaises(ValueError):
            runner.review([first, second])

    def test_overall_status_and_exit_codes(self):
        instances = [('swd-S', 'S01'), ('swd-S', 'S02')]
        rows = [{'case': case, 'instance': instance, 'status': 'pass'} for case, instance in instances]
        self.assertEqual(checks.overall_status(rows, instances), 'pass')
        self.assertEqual(checks.overall_status(rows, [*instances, ('arm-jtag-S', 'S01')]), 'incomplete')
        self.assertEqual(checks.exit_code('pass'), 0)
        self.assertEqual(checks.exit_code('fail'), 1)
        self.assertEqual(checks.exit_code('incomplete'), 2)
        rows[1]['status'] = 'pending'
        self.assertEqual(checks.overall_status(rows, instances), 'incomplete')
        rows[1]['status'] = 'blocked'
        self.assertEqual(checks.overall_status(rows, instances), 'fail')
        self.assertEqual(checks.overall_status(rows[:1], instances), 'incomplete')


class DocSyncTests(unittest.TestCase):
    """文档与代码共用一份事实：主清单的实例表必须与 ITEM_META 一致。"""

    def test_checklist_table_matches_runner_metadata(self):
        text = (ROOT / 'docs/板卡验收清单.md').read_text(encoding='utf-8')
        self.assertIn(checks.render_checklist_table(), text)
        for instance in facts.INSTANCE_IDS:
            self.assertIn(f'`{instance}`', text)

    def test_reference_doc_maps_every_instance(self):
        text = (ROOT / 'docs/板卡验收用例参考.md').read_text(encoding='utf-8')
        for instance in facts.INSTANCE_IDS:
            self.assertIn(f'`{instance}`', text, instance)

    def test_cli_docs_list_the_acceptance_entry(self):
        text = (ROOT / 'docs/toolbox-cli-options.md').read_text(encoding='utf-8')
        self.assertIn('toolbox.py board-acceptance', text)
        self.assertIn('--allow-flash', text)
        self.assertIn('--review', text)


class JScopePageTests(TempCase):
    """J 组：1 路 vs 8 路、Web 与 CLI 两条路径、STOP 计数与导出对账。"""

    def _run(self, web=None, channels=1):
        import unittest.mock as mock
        import time as _time
        web = web or FakeWeb()
        web.pages['scope'] = {'mode': 'real'}
        web.pages['scope_samples'] = scope_samples(channels or 1)
        ctx = make_context(web=web)
        ctx.out = self.tmp
        driver = checks.Driver(ctx)
        case = {'id': 'swd-J', 'group': 'J', 'link': 'swd', 'requestedHz': 60_000_000}
        with mock.patch.object(checks, 'SCOPE_SHORT_WINDOW_SECONDS', 0), \
             mock.patch.object(checks, 'SCOPE_WINDOW_SECONDS', 0), \
             mock.patch.object(facts, 'SCOPE_WINDOW_SECONDS', 0), \
             mock.patch.object(_time, 'sleep', lambda *_: None):
            return driver.jscope(case), web

    def test_single_and_eight_channels_use_distinct_symbols(self):
        results, web = self._run(channels=1)
        self.assertEqual(results['J01']['status'], 'pass')
        single = results['J03']['evidence']['selection']
        self.assertEqual(single['count'], 1, '单路用例应只勾 1 个符号')
        eight = results['J04']['evidence']['selection']
        self.assertEqual(eight['count'], 8, '八路用例应勾 8 个符号')
        roles = checks.Driver.J_EIGHT
        self.assertEqual(len(set(roles)), 8)
        symbols = facts.fixture('f103ze', 'scope')['symbols']
        addresses = set(scope_mod.sweep_addresses('f103ze', symbols, 8))
        self.assertEqual(len(addresses), 8, '八路必须选 8 个不同符号地址')

    def test_clean_window_passes_and_reports_both_paths(self):
        results, web = self._run(channels=1)
        self.assertEqual(results['J02']['status'], 'pass')
        for instance in ('J03', 'J04'):
            row = results[instance]
            self.assertEqual(row['status'], 'pending', row.get('reason'))
            self.assertIn('web', row['evidence'])
            self.assertIn('cli', row['evidence'])
            self.assertEqual(row['evidence']['web']['path'], 'web')
            self.assertEqual(row['evidence']['cli']['path'], 'native')
            self.assertIsNotNone(row['evidence']['chosenPeriodUs'])

    def test_tail_delta_and_missing_metrics_are_not_passed(self):
        web = FakeWeb()
        web.scope_tail = 7
        results, _ = self._run(web=web)
        self.assertEqual(results['J02']['status'], 'fail')
        self.assertEqual(results['J03']['status'], 'fail')
        web = FakeWeb()
        web.scope_metrics_missing = True
        results, _ = self._run(web=web)
        self.assertEqual(results['J02']['status'], 'unsupported')

    def test_wrong_actual_period_is_rejected(self):
        web = FakeWeb()
        web.scope_actual_ticks = 999
        results, _ = self._run(web=web)
        self.assertEqual(results['J02']['status'], 'fail')
        self.assertIn('实际周期', results['J02']['reason'])

    def test_export_cross_check_compares_jsp_and_csv(self):
        results, _ = self._run(channels=1)
        self.assertEqual(results['J05']['status'], 'pass', results['J05'].get('reason'))
        verdict = results['J05']['evidence']['verdict']
        self.assertTrue(verdict['ok'])
        same = results['J05']['evidence']['sameWindow']
        self.assertTrue(same['consistent'], same)

    def test_j02_uses_raw_stop_counters_from_page(self):
        """J02 必须带 STOP 前后的原始 METRICS 快照与推出的尾差。"""
        results, _ = self._run(channels=1)
        evidence = results['J02']['evidence']
        self.assertIsNotNone(evidence['rawStopMetrics'])
        self.assertIsNotNone(evidence['rawWindowEndMetrics'])
        self.assertIsNotNone(evidence['rawRepeatedMetrics'])
        self.assertEqual(evidence['derivedTail'], 0)
        self.assertEqual(evidence['tailCountDelta'], 0)

    def test_j02_rejects_counters_that_keep_moving_after_stop(self):
        web = FakeWeb()
        web.scope_unstable = True
        results, _ = self._run(web=web)
        self.assertIn(results['J02']['status'], ('fail',))
        self.assertIn('计数', '；'.join(results['J02']['reason'] for _ in [0]))

    def test_j02_rejects_missing_error_counter_even_if_page_rate_is_valid(self):
        web = FakeWeb()
        original = web.scope_metrics

        def missing():
            value = original()
            value.pop('yields')
            return value

        web.scope_metrics = missing
        results, _ = self._run(web=web)
        self.assertEqual(results['J02']['status'], 'unsupported')

    def test_j05_rejects_exports_from_another_window(self):
        """导出的样本数与 J02 那一窗不一致 → 不能当同窗口证据。"""
        import unittest.mock as mock
        import time as _time
        web = FakeWeb()
        web.pages['scope'] = {'mode': 'real'}
        web.pages['scope_samples'] = scope_samples(1, 4)
        # 摘要报 4 个样本，但导出文件只有 2 行：模拟"取错窗口"
        original = web.scope_sample_count
        web.scope_sample_count = lambda: 6
        ctx = make_context(web=web)
        ctx.out = self.tmp
        driver = checks.Driver(ctx)
        case = {'id': 'swd-J', 'group': 'J', 'link': 'swd', 'requestedHz': 60_000_000}
        with mock.patch.object(checks, 'SCOPE_SHORT_WINDOW_SECONDS', 0), \
             mock.patch.object(checks, 'SCOPE_WINDOW_SECONDS', 0), \
             mock.patch.object(facts, 'SCOPE_WINDOW_SECONDS', 0), \
             mock.patch.object(_time, 'sleep', lambda *_: None):
            results, _ = driver.jscope(case), web
        self.assertEqual(results['J05']['status'], 'fail')
        self.assertFalse(results['J05']['evidence']['sameWindow']['consistent'])


class PacketTests(unittest.TestCase):
    def test_parse_def_and_samples(self):
        rows = scope_samples(channels=2, count=3)
        packed = scope_mod.parse_packet_stream(build_jsp(rows))
        self.assertEqual(packed['packets'], 2)
        self.assertEqual(len(packed['definition']['vars']), 2)
        self.assertEqual(len(packed['samples']), 3)
        self.assertEqual(packed['samples'][1]['values'], rows[1])

    def test_resync_counts_garbage_prefix(self):
        packed = scope_mod.parse_packet_stream(b'\x00\xff' * 8 + build_jsp(scope_samples()))
        self.assertGreaterEqual(packed['resync'], 1)
        self.assertEqual(len(packed['samples']), 4)

    def test_compare_jsp_csv_against_existing_export_format(self):
        rows = scope_samples(channels=2, count=4)
        packed = scope_mod.parse_packet_stream(build_jsp(rows))
        table = scope_mod.csv_rows(build_csv(rows, ['a', 'b']).decode('utf-8'))
        self.assertTrue(scope_mod.compare_jsp_csv(packed, table, 2)['ok'])
        broken = scope_mod.csv_rows(build_csv(rows, ['a', 'b']).decode('utf-8')
                                    .replace('11', '99', 1))
        verdict = scope_mod.compare_jsp_csv(packed, broken, 2)
        self.assertFalse(verdict['ok'])
        self.assertIsNotNone(verdict['firstMismatch'] or verdict.get('reason'))
        self.assertFalse(scope_mod.compare_jsp_csv(packed, table, 8)['ok'])


class NativeWindowTests(TempCase):
    def test_native_requires_contiguous_packets_and_final_count(self):
        ticks = 480_000_000
        period = scope_mod.us_for_ticks(ticks)
        data = build_jsp(scope_samples(), ticks=ticks)
        path = self.tmp / 'scope.jsp'
        path.write_bytes(data)
        final = {'produced': 4, 'skipped': 0, 'usb': 0, 'errors': 0, 'yields': 0,
                 'invalidSamples': 0, 'bytes': len(data), 'flags': 0, 'tick': 3 * ticks,
                 'periodTicks': ticks, 'hz': scope_mod.TICK_HZ}
        report = {'metricsBefore': {**final, 'flags': 256, 'tick': 0, 'produced': 1},
                  'metricsAfter': {**final, 'flags': 256}, 'tailDrain': {'retiredReads': 2},
                  'finalMetrics': final, 'confirmedFinalMetrics': final.copy()}
        verdict = scope_mod.native_verdict(report, path, [0x20000000], period, 'swd')
        self.assertEqual(verdict['problems'], [])
        self.assertEqual(verdict['tailCountDelta'], 0)
        report['confirmedFinalMetrics']['produced'] = 5
        verdict = scope_mod.native_verdict(report, path, [0x20000000], period, 'swd')
        self.assertIn('STOP 后 produced 计数缺失或未稳定', verdict['problems'])
        repeated = data + data[scope_mod.PACKET:]
        with self.assertRaisesRegex(RuntimeError, '包序不连续'):
            scope_mod.count_raw_packets(repeated)


class HpmContractTests(unittest.TestCase):
    def _frames(self, native, ticks):
        return [{'g_tick': tick, **native.rv_expect(tick)} for tick in ticks]

    def test_contract_passes_for_consistent_frames(self):
        native = scope_mod.load_native()
        frames = self._frames(native, range(100, 140))
        verdict = scope_mod.verify_hpm_contract(frames, native=native)
        self.assertTrue(verdict['ok'], verdict['bad'][:3])
        self.assertEqual(verdict['torn'], 0)

    def test_torn_frame_with_monotonic_fields_is_accepted(self):
        native = scope_mod.load_native()
        frame = {'g_tick': 100}
        low, high = native.rv_expect(100), native.rv_expect(101)
        order = native.RV_ORDER
        for index, field in enumerate(order):
            frame[field] = high[field] if index >= 4 else low[field]
        verdict = scope_mod.verify_hpm_contract([frame], native=native)
        self.assertTrue(verdict['ok'], verdict['bad'][:3])
        self.assertEqual(verdict['torn'], 1)

    def test_wrong_field_is_rejected(self):
        native = scope_mod.load_native()
        frames = self._frames(native, range(10, 20))
        frames[3]['u_ramp'] = 12345
        verdict = scope_mod.verify_hpm_contract(frames, native=native)
        self.assertFalse(verdict['ok'])
        self.assertGreaterEqual(verdict['badCount'], 1)

    def test_frozen_tick_is_rejected(self):
        native = scope_mod.load_native()
        frames = self._frames(native, [50] * 5)
        verdict = scope_mod.verify_hpm_contract(frames, native=native)
        self.assertFalse(verdict['ok'])
        self.assertIn('没在动', verdict['reason'])


if __name__ == '__main__':
    unittest.main()
