"""离线回归：``tools/board_acceptance_native.py`` 的原生 SRAM / Flash 对照（绝不接触硬件）。

全部用注入的 mock transport：不启动 OpenOCD、不打开任何设备、不枚举 USB。

覆盖两轮审查要求：

1. 前置只读：``probe()`` 不主动 halt；身份不符时保留原运行状态；
2. 按链路核对身份：SWD 用 DPIDR（F103ZE 还要 DBGMCU DEV_ID=0x414），ARM JTAG 用
   CPU TAP idcode（0x3ba00477 / 0x6ba00477）并核对 scan_chain，HPM 必须回读 0x1000563d；
3. ``_argv`` 带上 ``script_dirs``；HPM 原版脚本依赖缺失要 Pending；
4. SRAM：``expected=`` 快照先读后比、失败先恢复再报、成功/失败都回到运行态；
   阶段区分进程墙钟与 TCL 实测时间；``pipeline=`` 直接拒绝（不冒充流水数据）；
5. Flash：擦除计划**按段合并**（中间空扇区不擦）、整扇区内容含 padding、独立回读、
   验证后恢复（恢复前后都有记录）、默认 ``reset run`` 与 Web 等价、``reset=False`` 如实登记；
6. 快照门、现场与快照不一致、物理扇区表不符、回读不一致、恢复被阻断的分类不造假。
"""

import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import board_acceptance_native as native_mod  # noqa: E402
from tools.arm_jtag_cli import hid_command  # noqa: E402
from tools.board_acceptance_native import (NativeFailure, NativeOpenOcd,  # noqa: E402
                                           cfg_for, resolve_tools)
from tools.board_acceptance_target import Pending, Unsafe  # noqa: E402

SERIAL = 'SN-0001'
SRAM_BASE = 0x20003000          # f103ze 的验收 SRAM 窗口（facts.BOARDS 登记）
FLASH_BASE = 0x80000000         # HPM XIP 映射区
ERASE = 0x1000

# 证据目录摆在仓库 tmp/ 下（系统 TEMP 在受限环境下不可写，见 ISSUE-029）。
TEST_ROOT = ROOT / 'tmp'


class NativeHid(unittest.TestCase):
    def test_config_read_preserves_the_high_voltage_byte(self):
        data = bytes((1, 0, 1, 1, 1, 0xe4, 0x0c))
        handle = Mock()
        handle.write.return_value = 64
        handle.read.return_value = bytes((2, 7, 1)) + data + bytes(54)
        self.assertEqual(hid_command(handle, 1), data)


def tempdir():
    """本仓 tmp/ 下的独立目录（不用 tempfile：受限环境对 mkdtemp 子项不可写）。"""
    TEST_ROOT.mkdir(parents=True, exist_ok=True)
    path = TEST_ROOT / ('native-acceptance-' + uuid.uuid4().hex[:10])
    path.mkdir()
    return path


def cleanup(path):
    shutil.rmtree(path, ignore_errors=True)


def pattern(address, length, seed=0x5A):
    return bytes((seed + index * 7 + (address >> 8)) & 0xFF for index in range(length))


class MockTarget:
    """内存目标：SRAM 字典 + Flash 稀疏字典（Flash 缺省 0xFF = 擦除态）。"""

    def __init__(self, *, dpidr=None, idcode=None, scan_chain=None, debug_idcode=None,
                 state='running', base=FLASH_BASE, size=0x20000, erase=ERASE,
                 serial=SERIAL, probe_error=None, sector_size_override=None,
                 bank_size=None, bank_count=1,
                 ignore_writes_after=None, ignore_programs_after=None,
                 corrupt_after_program=None, corrupt_first_write=False,
                 program_error=None, fail_program_after_write=False,
                 reset_fail=False, resume_fail=False,
                 read_tcl_ms=3, write_tcl_ms=5):
        self.dpidr, self.idcode, self.scan_chain = dpidr, idcode, scan_chain
        self.debug_idcode = debug_idcode
        self.state = state
        self.base, self.size, self.erase = base, size, erase
        self.bank_size, self.bank_count = bank_size or size, bank_count
        self.serial = serial
        self.probe_error = probe_error
        self.sector_size_override = sector_size_override
        self.ignore_writes_after = ignore_writes_after
        self.ignore_programs_after = ignore_programs_after
        self.corrupt_after_program = corrupt_after_program
        self.corrupt_first_write = corrupt_first_write
        self.program_error = program_error
        self.fail_program_after_write = fail_program_after_write
        self.reset_fail = reset_fail
        self.resume_fail = resume_fail
        self.read_tcl_ms, self.write_tcl_ms = read_tcl_ms, write_tcl_ms
        self.sram, self.flash = {}, {}
        self.probe_calls = self.reads = self.writes = self.programs = 0
        self.halt_calls = self.resume_calls = self.resets = 0
        self.erased = set()
        self.programmed = []
        self._programmed = False

    # -- 存数 ------------------------------------------------------------
    def _in_flash(self, address):
        return self.base <= address < self.base + self.size

    def peek(self, address, length):
        return bytes(self.flash.get(address, 0xFF) if self._in_flash(address)
                     else self.sram.get(address, 0x00)
                     for address in range(address, address + length))

    def poke(self, address, data):
        for offset, value in enumerate(bytes(data)):
            target = self.flash if self._in_flash(address + offset) else self.sram
            target[address + offset] = value

    def _sector(self, address):
        return self.base + ((address - self.base) // self.erase) * self.erase

    # -- transport 协议 ---------------------------------------------------
    def probe(self):
        self.probe_calls += 1
        if self.probe_error:
            raise Pending(self.probe_error)
        return {'probeSerial': self.serial, 'dpidr': self.dpidr, 'idcode': self.idcode,
                'idcodes': [self.idcode] if self.idcode is not None else [],
                'scanChain': self.scan_chain, 'debugIdcode': self.debug_idcode,
                'state': self.state, 'haltedByOpenOcd': self.state == 'halted',
                'log': 'mock-probe', 'processSeconds': 0.05, 'tclMs': None}

    def geometry(self):
        """按真实 ``flash info <bank> sectors`` 的结构回读：每 bank 自带扇区列表。"""
        size = self.sector_size_override or self.erase
        banks = []
        for index in range(self.bank_count):
            bank_address = self.base + index * self.bank_size
            count = self.bank_size // self.erase
            banks.append({'index': index, 'address': bank_address, 'size': self.bank_size,
                          'sectors': [{'index': sector,
                                       'address': bank_address + sector * self.erase,
                                       'size': size} for sector in range(count)]})
        sectors = [sector for bank in banks for sector in bank['sectors']]
        return {'banks': banks, 'sectors': sectors, 'probing': list(range(self.bank_count)),
                'processSeconds': 0.05, 'log': 'mock-geometry'}

    def read(self, address, length):
        self.reads += 1
        data = bytearray(self.peek(address, length))
        if self.corrupt_after_program is not None and self._programmed:
            at, value = self.corrupt_after_program
            if address <= at < address + length:
                data[at - address] = value
                self.corrupt_after_program = None
        return {'data': bytes(data), 'processSeconds': 0.02, 'tclMs': self.read_tcl_ms,
                'log': 'mock-read'}

    def write(self, address, data):
        self.writes += 1
        if self.ignore_writes_after is not None and self.writes > self.ignore_writes_after:
            return {'processSeconds': 0.02, 'tclMs': self.write_tcl_ms, 'log': 'mock-write'}
        self.poke(address, data)
        if self.corrupt_first_write and self.writes == 1:
            self.poke(address, bytes([value ^ 0xFF for value in bytes(data)]))
        return {'processSeconds': 0.02, 'tclMs': self.write_tcl_ms, 'log': 'mock-write'}

    def program(self, ranges):
        self.programs += 1
        self._programmed = True
        if self.program_error is not None and self.programs == 1:
            if self.fail_program_after_write:
                # 模拟擦除/写入到一半就断：内容已变，但进程报错/超时
                for address, payload in ranges:
                    self.poke(address, payload)
            raise self.program_error
        if self.ignore_programs_after is not None and self.programs > self.ignore_programs_after:
            return {'processSeconds': 0.0, 'programMs': 0, 'verifyMs': 0, 'bytes': 0,
                    'ranges': [], 'log': 'mock-program'}
        for address, payload in ranges:
            last = self._sector(address + len(payload) - 1)
            for start in range(self._sector(address), last + self.erase, self.erase):
                self.erased.add(start)
                for at in range(start, start + self.erase):
                    self.flash.pop(at, None)              # 擦除 → 回到 0xFF
            self.poke(address, payload)
            self.programmed.append((address, bytes(payload)))
        return {'processSeconds': 0.25, 'programMs': 7, 'verifyMs': 3,
                'bytes': sum(len(payload) for _, payload in ranges),
                'ranges': [[address, len(payload)] for address, payload in ranges],
                'log': 'mock-program'}

    def halt(self):
        self.halt_calls += 1
        self.state = 'halted'

    def resume(self):
        self.resume_calls += 1
        if self.resume_fail:
            raise RuntimeError('resume 失败（模拟目标无响应）')
        self.state = 'running'

    def reset_run(self):
        self.resets += 1
        if self.reset_fail:
            self.state = 'halted'      # 模拟复位失败后目标停在暂停态
            raise native_mod.OpenOcdStageError('OpenOCD reset-run 失败（exit 1）')
        self.state = 'running'
        return {'processSeconds': 0.4, 'log': 'mock-reset'}


def mock_for(board, link='swd', **overrides):
    """按板卡/链路给一组与登记值一致的默认身份。"""
    kind, value = native_mod.EXPECTED_ID[(board, link)]
    defaults = {'dpidr': None, 'idcode': None, 'scan_chain': None, 'debug_idcode': None}
    if kind == 'dpidr':
        defaults['dpidr'] = value
    else:
        defaults['idcode'] = value
        defaults['scan_chain'] = [[value]]
        if link == 'arm-jtag':
            defaults['scan_chain'].append([native_mod.ARM_BOUNDARY_TAP[board]])
    if board == 'f103ze' and link == 'swd':
        defaults['debug_idcode'] = native_mod.STM32F1_DEVICE_ID
    defaults.update(overrides)
    return MockTarget(**defaults)


def flash_snapshot(backup, segments, *, image=None, image_sha=None, elf=False):
    return {'backupSha256': native_mod.sha(backup),
            'segmentsSha256': native_mod.segments_sha(segments),
            'image': str(image) if image is not None else None,
            'imageSha256': image_sha, 'elf': elf}


class NativeSetup:
    """构造 NativeOpenOcd 的公共夹具。"""

    def __init__(self, board='f103ze', link='swd', clock_hz=1_000_000, mock=None, **mock_kw):
        self.tmp = tempdir()
        self.folder = self.tmp / 'native'
        self.mock = mock if mock is not None else mock_for(board, link, **mock_kw)
        self.native = NativeOpenOcd(board, link, clock_hz, SERIAL, self.folder, runner=self.mock)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        cleanup(self.tmp)

    @staticmethod
    def flash_args(*, backup_address=FLASH_BASE + ERASE, sectors=3):
        length = sectors * ERASE
        backup = pattern(backup_address, length)
        segments = [(backup_address + 0x100, pattern(backup_address + 0x100, 256, 0x11)),
                    (backup_address + 0x1100, pattern(backup_address + 0x1100, 128, 0x33))]
        geometry = {'base': FLASH_BASE, 'bytes': 0x20000, 'erase': ERASE}
        return backup_address, backup, segments, geometry


class ToolsAndCfg(unittest.TestCase):
    def test_tool_resolution_prefers_explicit_then_env_then_sdk_default(self):
        tmp = tempdir()
        try:
            tools = resolve_tools(openocd=tmp / 'x.exe', scripts=tmp / 'tcl', hpm_env=tmp)
            self.assertEqual(tools['openocdSource'], 'explicit')
            self.assertEqual(tools['hpmSdkEnvSource'], 'explicit')
            self.assertFalse(tools['openocdPresent'])
            report = native_mod.hpm_dependency_report(tools)
            self.assertEqual(len(report['missing']), len(native_mod.HPM_CFG_DEPENDENCIES))
        finally:
            cleanup(tmp)

    def test_cfg_selection_uses_registered_tap_ids(self):
        text, dirs, source = cfg_for('f103ze', 'swd')
        self.assertEqual(source['kind'], 'official')
        self.assertIn('transport select swd', text)
        text, dirs, source = cfg_for('f103ze', 'arm-jtag')
        self.assertEqual(source['kind'], 'official')
        self.assertIn('transport select jtag', text)
        self.assertIn('set CPUTAPID 0x3ba00477', text)
        self.assertTrue(source['target'].endswith('stm32f1x.cfg'))
        self.assertTrue(dirs and dirs[0].endswith('openocd-cfgs'))
        text, dirs, source = cfg_for('h743', 'arm-jtag')
        self.assertIn('0x6ba00477', text)
        self.assertTrue(source['target'].endswith('stm32h7x_dual_bank.cfg'))
        self.assertIn('examine-start', text)
        text, dirs, source = cfg_for('hpm6800evk', 'jtag')
        self.assertEqual(source['kind'], 'repo')
        self.assertIn('cmsis-dap', text)
        tmp = tempdir()
        try:
            with self.assertRaises(Pending):
                cfg_for('f103ze', 'swd', cfg=tmp / 'missing.cfg')
        finally:
            cleanup(tmp)

    def test_argv_carries_script_dirs_and_probe_commands_do_not_halt(self):
        tmp = tempdir()
        try:
            try:
                native = NativeOpenOcd('f103ze', 'arm-jtag', 1_000_000, SERIAL, tmp)
            except Pending as error:
                self.skipTest(f'本机没有原生 OpenOCD：{error}')
            native.prepare()
            argv = native.runner._argv(['init', 'shutdown'])
            self.assertIn('-s', argv)
            self.assertIn(str(native_mod.OFFICIAL_DIR), argv)
            commands = native.runner.probe_script()
            self.assertFalse(any('halt' in line for line in commands),
                             f'前置只读，不能主动 halt：{commands}')
            self.assertTrue(any('[$zn_target curstate]' in line for line in commands),
                            '参数语法用 [$变量 ...]')
            self.assertTrue(any('puts [capture {scan_chain}]' in line for line in commands))
            self.assertEqual(native.runner._argv(None)[0], native.tools['openocd'])
            try:
                hpm = NativeOpenOcd('hpm6800evk', 'jtag', 8_000_000, SERIAL, tmp)
            except Pending as error:
                self.skipTest(f'本机没有 HPM 原生环境：{error}')
            hpm.prepare()
            hpm_commands = hpm.runner.probe_script()
            self.assertIn('puts [capture {scan_chain}]', hpm_commands,
                          'HPM 必须回读 scan_chain 里的 0x1000563d')
            self.assertIn('-s', hpm.runner._argv(None))
        finally:
            cleanup(tmp)


class Construction(unittest.TestCase):
    def test_link_alias_and_invalid_inputs(self):
        tmp = tempdir()
        try:
            native = NativeOpenOcd('f103ze', 'jtag', 1_000_000, SERIAL, tmp, runner=MockTarget())
            self.assertEqual(native.link, 'arm-jtag')
            self.assertEqual(native.expected_identity, ('idcode', 0x3ba00477))
            with self.assertRaises(RuntimeError):
                NativeOpenOcd('f103ze', 'swd', 0, SERIAL, tmp, runner=MockTarget())
            with self.assertRaises(RuntimeError):
                NativeOpenOcd('f103ze', 'swd', 1_000_000, '  ', tmp, runner=MockTarget())
            with self.assertRaises(Pending):
                NativeOpenOcd('hpm6800evk', 'swd', 1_000_000, SERIAL, tmp, runner=MockTarget())
        finally:
            cleanup(tmp)

    def test_config_binds_the_named_probe_and_clock(self):
        with NativeSetup() as setup:
            text = setup.native.prepare().read_text(encoding='utf-8')
            self.assertIn('adapter serial {SN-0001}', text)
            self.assertIn('adapter speed 1000', text)
            self.assertIn('gdb port disabled', text)
            self.assertIn('reset_config none', text)
            self.assertEqual(len(setup.native.cfg_digest), 64)


class Identity(unittest.TestCase):
    def test_swd_dpidr_mismatch_is_pending_without_touching_target(self):
        with NativeSetup(mock=mock_for('f103ze', 'swd', dpidr=0x12345678)) as setup:
            with self.assertRaises(Pending) as caught:
                setup.native.sram(SRAM_BASE, b'1234')
            self.assertIn('不符', str(caught.exception))
            self.assertEqual(setup.mock.writes, 0)
            self.assertEqual(setup.mock.reads, 0)
            self.assertEqual(setup.mock.halt_calls, 0, '前置只读，不能暂停')

    def test_wrong_target_keeps_its_original_state(self):
        mock = mock_for('f103ze', 'swd', dpidr=0x12345678, state='halted')
        with NativeSetup(mock=mock) as setup:
            with self.assertRaises(Pending):
                setup.native.sram(SRAM_BASE, b'1234')
            self.assertEqual(mock.resume_calls, 0)
            self.assertEqual(mock.state, 'halted')
            identity = json.loads((setup.folder / 'native-identity.json').read_text(encoding='utf-8'))
            self.assertIn('不符', identity['failure'])

    def test_f103ze_requires_dbgmcu_device_id(self):
        mock = mock_for('f103ze', 'swd', debug_idcode=0x450)      # 同 DPIDR 的其它 STM32
        with NativeSetup(mock=mock) as setup:
            with self.assertRaises(Pending) as caught:
                setup.native.sram(SRAM_BASE, b'1234')
            self.assertIn('DEV_ID', str(caught.exception))
            self.assertEqual(mock.writes, 0)

    def test_arm_jtag_checks_cpu_and_boundary_taps(self):
        with NativeSetup(board='f103ze', link='arm-jtag',
                         mock=mock_for('f103ze', 'arm-jtag', state='halted')) as setup:
            with self.assertRaises(Pending):
                setup.native.preflight()
            identity = setup.native.preflight(require_running=False)
            self.assertEqual(identity['idcode'], 0x3ba00477)
            self.assertEqual(identity['scanChain'], [[0x3ba00477], [0x06414041]])
            self.assertEqual(setup.mock.state, 'halted')
        with NativeSetup(board='h743', link='arm-jtag',
                         mock=mock_for('h743', 'arm-jtag')) as setup:
            identity = setup.native.preflight()
            self.assertEqual(identity['idcode'], 0x6ba00477)

    def test_hpm_requires_the_registered_tap_idcode(self):
        with NativeSetup(board='hpm6800evk', link='jtag', mock=mock_for('hpm6800evk', 'jtag')) as setup:
            self.assertEqual(setup.native.preflight()['idcode'], 0x1000563d)
        mock = mock_for('hpm6800evk', 'jtag', idcode=None, scan_chain=None)
        with NativeSetup(board='hpm6800evk', link='jtag', mock=mock) as setup:
            with self.assertRaises(Pending):
                setup.native.sram(0x01250000, b'1234')

    def test_jtag_chain_with_unexpected_tap_is_pending(self):
        two_taps = mock_for('f103ze', 'arm-jtag', scan_chain=[[0x3ba00477], [0x1ba01477]])
        with NativeSetup(board='f103ze', link='arm-jtag', mock=two_taps) as setup:
            with self.assertRaises(Pending) as caught:
                setup.native.sram(SRAM_BASE, b'1234')
            self.assertIn('链边界', str(caught.exception))
            self.assertEqual(two_taps.writes, 0)
        hpm_two = mock_for('hpm6800evk', 'jtag', scan_chain=[[0x1000563d], [0x6ba02477]])
        with NativeSetup(board='hpm6800evk', link='jtag', mock=hpm_two) as setup:
            with self.assertRaises(Pending):
                setup.native.sram(0x01250000, b'1234')

    def test_probe_pending_propagates(self):
        mock = MockTarget(probe_error='指定探针序列号未找到')
        with NativeSetup(mock=mock) as setup:
            with self.assertRaises(Pending):
                setup.native.sram(SRAM_BASE, b'1234')


class Sram(unittest.TestCase):
    def test_sram_roundtrip_keeps_backup_and_reports_dual_timings(self):
        with NativeSetup() as setup:
            mock = setup.mock
            original = pattern(SRAM_BASE, 64, 0x77)
            mock.poke(SRAM_BASE, original)
            payload = pattern(SRAM_BASE, 64, 0x21)
            evidence = setup.native.sram(SRAM_BASE, payload, expected=original)
            self.assertTrue(evidence['verified'])
            self.assertTrue(evidence['restored'])
            self.assertEqual(evidence['sha256'], native_mod.sha(payload))
            self.assertEqual(evidence['readbackSha256'], native_mod.sha(payload))
            self.assertEqual(evidence['backupSha256'], native_mod.sha(original))
            self.assertEqual(evidence['snapshot']['differences'], 0)
            self.assertEqual(mock.peek(SRAM_BASE, 64), original, 'SRAM 必须恢复原值')
            self.assertEqual(mock.writes, 2, '写图案 + 写回备份')
            # 双口径：进程墙钟与 TCL 实测分开
            for stage in ('backup', 'write', 'read', 'restore', 'restoreRead'):
                self.assertIn('processSeconds', evidence['stages'][stage])
                self.assertIn('tclMs', evidence['stages'][stage])
            self.assertEqual(evidence['stages']['write']['tclMs'], 5)
            self.assertEqual(evidence['stages']['read']['tclMs'], 3)
            # 速率用 TCL 实测时间（64 B / 5 ms）
            self.assertAlmostEqual(evidence['writeBytesPerSecond'], 64 / 0.005)
            self.assertEqual(evidence['pipeline'], None)
            self.assertEqual(mock.halt_calls, 0, '模块自己从不发 halt；写入由 transport 负责')
            self.assertTrue(evidence['stateAfter']['resumed'])
            self.assertEqual(mock.state, 'running')
            self.assertEqual(evidence['identity']['probeSerial'], SERIAL)
            self.assertTrue((setup.folder / 'native-sram.json').is_file())
            on_disk = json.loads((setup.folder / 'native-sram.json').read_text(encoding='utf-8'))
            self.assertTrue(on_disk['verified'])
            self.assertIn('preRestore', on_disk['restore'])
            self.assertIn('postRestore', on_disk['restore'])

    def test_sram_snapshot_mismatch_is_pending(self):
        with NativeSetup() as setup:
            mock = setup.mock
            mock.poke(SRAM_BASE, pattern(SRAM_BASE, 32, 0x10))
            with self.assertRaises(Pending) as caught:
                setup.native.sram(SRAM_BASE, pattern(SRAM_BASE, 32, 0x21),
                                  expected=pattern(SRAM_BASE, 32, 0xEE))
            self.assertIn('快照不一致', str(caught.exception))
            self.assertEqual(mock.writes, 0, '快照不符时一次写入都不许发')

    def test_sram_pipeline_is_rejected(self):
        with NativeSetup() as setup:
            with self.assertRaises(Pending) as caught:
                setup.native.sram(SRAM_BASE, b'1234', pipeline={'words': 512, 'depth': 32})
            self.assertIn('流水', str(caught.exception))
            self.assertEqual(setup.mock.writes, 0)

    def test_sram_readback_mismatch_restores_then_fails_and_resumes(self):
        mock = mock_for('f103ze', 'swd', corrupt_first_write=True)
        with NativeSetup(mock=mock) as setup:
            original = pattern(SRAM_BASE, 32, 0x10)
            mock.poke(SRAM_BASE, original)
            with self.assertRaises(NativeFailure) as caught:
                setup.native.sram(SRAM_BASE, pattern(SRAM_BASE, 32, 0x21))
            evidence = caught.exception.evidence
            self.assertGreater(evidence['writeMismatch']['differences'], 0)
            self.assertFalse(evidence['verified'])
            self.assertTrue(evidence['restore']['verified'])
            self.assertEqual(mock.peek(SRAM_BASE, 32), original, '目标必须恢复原值')
            self.assertTrue(evidence['stateAfter']['resumed'], '失败路径也要回到运行态')
            self.assertEqual(mock.state, 'running')

    def test_sram_restore_unconfirmed_is_unsafe(self):
        mock = mock_for('f103ze', 'swd', ignore_writes_after=1)
        with NativeSetup(mock=mock) as setup:
            original = pattern(SRAM_BASE, 32, 0x10)
            mock.poke(SRAM_BASE, original)
            with self.assertRaises(Unsafe):
                setup.native.sram(SRAM_BASE, pattern(SRAM_BASE, 32, 0x21))
            evidence = json.loads((setup.folder / 'native-sram.json').read_text(encoding='utf-8'))
            self.assertFalse(evidence['verified'])

    def test_sram_window_outside_region_is_rejected_before_any_access(self):
        with NativeSetup() as setup:
            with self.assertRaises(RuntimeError):
                setup.native.sram(0x1000, b'1234')
            self.assertEqual(setup.mock.probe_calls, 0)
            self.assertEqual(setup.mock.writes, 0)


class FlashParsing(unittest.TestCase):
    """按真实 native OpenOCD ``flash info <bank> sectors`` 行格式解析（H743 双 bank）。"""

    def test_stm32f1_and_hpm_formats(self):
        f1 = (
            '# 0 : stm32f1x at 0x08000000, size 0x00080000, buswidth 0, chipwidth 0\n'
            '\t#  0:\t0x00000000 (0x00000800   2kB) not protected\n'
            '\t#  1:\t0x00000800 (0x00000800   2kB) not protected\n'
        )
        parsed = native_mod.parse_flash_sectors(f1)
        self.assertEqual(len(parsed['banks']), 1)
        self.assertEqual(parsed['banks'][0]['address'], 0x08000000)
        self.assertEqual([(s['address'], s['size']) for s in parsed['sectors']],
                         [(0x08000000, 0x800), (0x08000800, 0x800)])
        hpm = ('# 0 : hpm_xpi at 0x80000000, size 0x02000000, buswidth 0, chipwidth 0\n'
               '\t#  0:\t0x00000000 (0x00001000   4kB) not protected\n')
        parsed = native_mod.parse_flash_sectors(hpm)
        self.assertEqual(parsed['banks'][0]['address'], 0x80000000)
        self.assertEqual(parsed['sectors'], [{'index': 0, 'address': 0x80000000,
                                              'size': 0x1000}])

    def test_h743_dual_bank_offsets_are_relative_to_each_bank(self):
        text = (
            '# 0 : stm32h7x at 0x08000000, size 0x00100000, buswidth 0, chipwidth 0\n'
            '\t#  0:\t0x00000000 (0x00020000 128kB) not protected\n'
            '\t#  1:\t0x00020000 (0x00020000 128kB) not protected\n'
            '# 1 : stm32h7x at 0x08100000, size 0x00100000, buswidth 0, chipwidth 0\n'
            '\t#  0:\t0x00000000 (0x00020000 128kB) not protected\n'
            '\t#  1:\t0x00020000 (0x00020000 128kB) not protected\n'
        )
        parsed = native_mod.parse_flash_sectors(text)
        self.assertEqual([bank['address'] for bank in parsed['banks']],
                         [0x08000000, 0x08100000])
        self.assertEqual([s['address'] for s in parsed['banks'][1]['sectors']],
                         [0x08100000, 0x08120000])
        self.assertEqual(len(parsed['sectors']), 4)

    def test_empty_or_garbage_table_parses_to_nothing(self):
        self.assertEqual(native_mod.parse_flash_sectors(''),
                         {'banks': [], 'sectors': []})
        self.assertEqual(native_mod.parse_flash_sectors('no flash here\n'),
                         {'banks': [], 'sectors': []})


class Flash(unittest.TestCase):
    def test_flash_sparse_ranges_skip_empty_middle_sectors(self):
        mock = mock_for('hpm6800evk', 'jtag')
        with NativeSetup(board='hpm6800evk', link='jtag', mock=mock) as setup:
            address = FLASH_BASE + ERASE
            backup = pattern(address, 4 * ERASE)
            mock.poke(address, backup)
            segments = [(address + 0x10, pattern(address + 0x10, 64, 0x11)),
                        (address + 3 * ERASE + 0x10, pattern(address + 3 * ERASE + 0x10, 64, 0x33))]
            geometry = {'base': FLASH_BASE, 'bytes': 0x20000, 'erase': ERASE}
            evidence = setup.native.flash(address, backup, segments, geometry,
                                          snapshot=flash_snapshot(backup, segments),
                                          web_seconds=1.234)
            self.assertEqual(evidence['ranges'],
                             [[address, address + ERASE],
                              [address + 3 * ERASE, address + 4 * ERASE]])
            self.assertEqual(evidence['sectors'], 2)
            self.assertEqual(mock.erased, {address, address + 3 * ERASE})
            self.assertGreater(evidence['segmentGapBytes'], 2 * ERASE,
                               '两段之间有大空档：证明没有被一把梭');
            self.assertTrue(evidence['verified'])
            self.assertTrue(evidence['restored'])
            self.assertEqual(mock.peek(address, len(backup)), backup)
            self.assertTrue(evidence['reset']['included'])
            self.assertEqual(mock.resets, 1)
            self.assertIsNotNone(evidence['stages']['reset']['processSeconds'],
                                 'Runner 用 stages.reset.processSeconds 记录复位墙钟')
            self.assertIsNotNone(evidence['stages']['program']['processSeconds'])
            self.assertEqual(evidence['mapping'], 'XIP 映射回读')

    def test_flash_programs_whole_sectors_with_padding_from_backup(self):
        mock = mock_for('hpm6800evk', 'jtag')
        with NativeSetup(board='hpm6800evk', link='jtag', mock=mock) as setup:
            address, backup, segments, geometry = setup.flash_args(sectors=2)
            mock.poke(address, backup)
            setup.native.flash(address, backup, segments, geometry,
                               snapshot=flash_snapshot(backup, segments))
            program_address, payload = mock.programmed[0]
            self.assertEqual(program_address, address)
            self.assertEqual(len(payload), 2 * ERASE, '擦除扇区必须整段重写（含 padding）')
            for offset, value in enumerate(payload):
                covered = any(at <= program_address + offset < at + len(data)
                              for at, data in segments)
                if not covered:
                    self.assertEqual(value, backup[offset],
                                     f'padding 0x{program_address + offset:#x} 必须来自备份')

    def test_flash_reports_dual_timings_and_restore_records(self):
        mock = mock_for('hpm6800evk', 'jtag')
        with NativeSetup(board='hpm6800evk', link='jtag', mock=mock) as setup:
            address, backup, segments, geometry = setup.flash_args()
            mock.poke(address, backup)
            evidence = setup.native.flash(address, backup, segments, geometry,
                                          snapshot=flash_snapshot(backup, segments),
                                          web_seconds=1.234)
            self.assertEqual(evidence['cliSeconds'], 0.01)            # TCL 7 ms + 3 ms
            self.assertEqual(evidence['cliProcessSeconds'], 0.25)     # 进程墙钟单列
            self.assertEqual(evidence['stages']['program']['tclMs'], None)
            self.assertEqual(evidence['stages']['programTclMs']['programMs'], 7)
            self.assertEqual(evidence['webSeconds'], 1.234)
            self.assertEqual(evidence['bytes'], 2 * ERASE)
            restore = evidence['restore']
            self.assertGreater(restore['preRestore']['differences'], 0, '编程后必须与备份不同')
            self.assertEqual(restore['postRestore']['differences'], 0)
            self.assertTrue(restore['rewritten'])
            self.assertEqual(mock.peek(address, len(backup)), backup)

    def test_flash_without_reset_declares_it_and_restores_running(self):
        mock = mock_for('hpm6800evk', 'jtag')
        with NativeSetup(board='hpm6800evk', link='jtag', mock=mock) as setup:
            address, backup, segments, geometry = setup.flash_args()
            mock.poke(address, backup)
            evidence = setup.native.flash(address, backup, segments, geometry,
                                          snapshot=flash_snapshot(backup, segments), reset=False)
            self.assertFalse(evidence['reset']['included'])
            self.assertEqual(mock.resets, 0)
            self.assertEqual(evidence['stateAfter']['state'], 'running')
            self.assertIsNone(evidence['stages']['reset']['processSeconds'])
            self.assertEqual(mock.state, 'running')

    def test_h743_dual_bank_geometry_and_bank1_backup(self):
        mock = mock_for('h743', 'arm-jtag', base=0x08000000, size=0x200000,
                        bank_size=0x100000, bank_count=2, erase=0x20000)
        with NativeSetup(board='h743', link='arm-jtag', mock=mock) as setup:
            address = 0x08120000                      # bank1 里
            backup = pattern(address, 2 * 0x20000)
            mock.poke(address, backup)
            segments = [(address + 0x100, pattern(address + 0x100, 256, 0x11))]
            geometry = {'base': 0x08000000, 'bytes': 0x200000, 'erase': 0x20000}
            evidence = setup.native.flash(address, backup, segments, geometry,
                                          snapshot=flash_snapshot(backup, segments))
            self.assertTrue(evidence['verified'])
            self.assertEqual(evidence['physical']['banks'],
                             [['0x8000000', 0x100000], ['0x8100000', 0x100000]])
            self.assertEqual(mock.peek(address, len(backup)), backup)

    def test_h743_bank1_sector_mismatch_is_pending(self):
        mock = mock_for('h743', 'arm-jtag', base=0x08000000, size=0x200000,
                        bank_size=0x100000, bank_count=2, erase=0x20000,
                        sector_size_override=0x40000)
        with NativeSetup(board='h743', link='arm-jtag', mock=mock) as setup:
            address = 0x08120000
            backup = pattern(address, 2 * 0x20000)
            mock.poke(address, backup)
            segments = [(address + 0x100, pattern(address + 0x100, 256, 0x11))]
            geometry = {'base': 0x08000000, 'bytes': 0x200000, 'erase': 0x20000}
            with self.assertRaises(Pending) as caught:
                setup.native.flash(address, backup, segments, geometry,
                                   snapshot=flash_snapshot(backup, segments))
            self.assertIn('物理扇区', str(caught.exception))
            self.assertEqual(mock.programs, 0)

    def test_program_failure_after_write_is_restored_not_pending(self):
        mock = mock_for('hpm6800evk', 'jtag', fail_program_after_write=True,
                        program_error=native_mod.OpenOcdStageError('OpenOCD program 超时',
                                                                   timed_out=True))
        with NativeSetup(board='hpm6800evk', link='jtag', mock=mock) as setup:
            address, backup, segments, geometry = setup.flash_args()
            mock.poke(address, backup)
            with self.assertRaises(NativeFailure) as caught:
                setup.native.flash(address, backup, segments, geometry,
                                   snapshot=flash_snapshot(backup, segments))
            evidence = caught.exception.evidence
            self.assertFalse(evidence['verified'])
            self.assertTrue(evidence['restored'], '写入后失败也必须复核并恢复')
            self.assertGreater(evidence['restore']['preRestore']['differences'], 0)
            self.assertEqual(mock.peek(address, len(backup)), backup)
            self.assertEqual(evidence['stateAfter']['state'], 'running')
            self.assertEqual(mock.resets, 0, '对照失败时不引入复位副作用')

    def test_program_pending_after_write_still_restores_before_propagating(self):
        mock = mock_for('hpm6800evk', 'jtag', fail_program_after_write=True,
                        program_error=Pending('OpenOCD 无法启动（program）'))
        with NativeSetup(board='hpm6800evk', link='jtag', mock=mock) as setup:
            address, backup, segments, geometry = setup.flash_args()
            mock.poke(address, backup)
            with self.assertRaises(Pending):
                setup.native.flash(address, backup, segments, geometry,
                                   snapshot=flash_snapshot(backup, segments))
            # 先复核并恢复成备份，再把「进程根本没起来」的 Pending 透传
            self.assertEqual(mock.peek(address, len(backup)), backup)
            on_disk = json.loads((setup.folder / 'native-flash.json').read_text(encoding='utf-8'))
            self.assertTrue(on_disk['restored'])

    def test_program_failure_with_blocked_restore_is_unsafe(self):
        mock = mock_for('hpm6800evk', 'jtag', fail_program_after_write=True,
                        ignore_programs_after=1,
                        program_error=native_mod.OpenOcdStageError('OpenOCD program 失败'))
        with NativeSetup(board='hpm6800evk', link='jtag', mock=mock) as setup:
            address, backup, segments, geometry = setup.flash_args()
            mock.poke(address, backup)
            with self.assertRaises(Unsafe):
                setup.native.flash(address, backup, segments, geometry,
                                   snapshot=flash_snapshot(backup, segments))
            on_disk = json.loads((setup.folder / 'native-flash.json').read_text(encoding='utf-8'))
            self.assertFalse(on_disk['verified'])

    def test_reset_failure_is_not_verified_and_target_stays_running(self):
        mock = mock_for('hpm6800evk', 'jtag', reset_fail=True)
        with NativeSetup(board='hpm6800evk', link='jtag', mock=mock) as setup:
            address, backup, segments, geometry = setup.flash_args()
            mock.poke(address, backup)
            with self.assertRaises(NativeFailure) as caught:
                setup.native.flash(address, backup, segments, geometry,
                                   snapshot=flash_snapshot(backup, segments))
            evidence = caught.exception.evidence
            self.assertFalse(evidence['verified'], '复位失败不能 verified=True')
            self.assertIn('error', evidence['reset'])
            self.assertIsNotNone(evidence['stages']['reset']['processSeconds'])
            self.assertEqual(evidence['stateAfter']['state'], 'running')
            self.assertEqual(mock.state, 'running')
            self.assertTrue(evidence['restored'], '复位失败前扇区已恢复')

    def test_reset_failure_with_resume_failure_is_unsafe(self):
        mock = mock_for('hpm6800evk', 'jtag', reset_fail=True, resume_fail=True)
        with NativeSetup(board='hpm6800evk', link='jtag', mock=mock) as setup:
            address, backup, segments, geometry = setup.flash_args()
            mock.poke(address, backup)
            with self.assertRaises(Unsafe):
                setup.native.flash(address, backup, segments, geometry,
                                   snapshot=flash_snapshot(backup, segments))
            on_disk = json.loads((setup.folder / 'native-flash.json').read_text(encoding='utf-8'))
            self.assertFalse(on_disk['verified'])

    def test_snapshot_required_for_native_flash(self):
        mock = mock_for('hpm6800evk', 'jtag')
        with NativeSetup(board='hpm6800evk', link='jtag', mock=mock) as setup:
            address, backup, segments, geometry = setup.flash_args()
            mock.poke(address, backup)
            with self.assertRaises(Pending) as caught:
                setup.native.flash(address, backup, segments, geometry)
            self.assertIn('快照', str(caught.exception))
            self.assertEqual(mock.programs, 0)
            self.assertEqual(mock.probe_calls, 0)

    def test_snapshot_fingerprints_must_match(self):
        mock = mock_for('hpm6800evk', 'jtag')
        with NativeSetup(board='hpm6800evk', link='jtag', mock=mock) as setup:
            address, backup, segments, geometry = setup.flash_args()
            mock.poke(address, backup)
            wrong_backup = flash_snapshot(backup, segments)
            wrong_backup['backupSha256'] = '0' * 64
            with self.assertRaises(Pending):
                setup.native.flash(address, backup, segments, geometry, snapshot=wrong_backup)
            wrong_segments = flash_snapshot(backup, segments)
            wrong_segments['segmentsSha256'] = '1' * 64
            with self.assertRaises(Pending):
                setup.native.flash(address, backup, segments, geometry, snapshot=wrong_segments)
            self.assertEqual(mock.programs, 0)

    def test_flash_start_state_must_match_web_snapshot(self):
        mock = mock_for('hpm6800evk', 'jtag')
        with NativeSetup(board='hpm6800evk', link='jtag', mock=mock) as setup:
            address, backup, segments, geometry = setup.flash_args()
            mock.poke(address, pattern(address, len(backup), 0x99))
            with self.assertRaises(Pending) as caught:
                setup.native.flash(address, backup, segments, geometry,
                                   snapshot=flash_snapshot(backup, segments))
            self.assertIn('快照不一致', str(caught.exception))
            self.assertEqual(mock.programs, 0)

    def test_erase_plan_mismatch_is_pending(self):
        mock = mock_for('hpm6800evk', 'jtag')
        with NativeSetup(board='hpm6800evk', link='jtag', mock=mock) as setup:
            address, backup, segments, geometry = setup.flash_args()
            mock.poke(address, backup)
            geometry = dict(geometry, ranges=[[FLASH_BASE + ERASE, FLASH_BASE + 4 * ERASE]])
            with self.assertRaises(Pending):
                setup.native.flash(address, backup, segments, geometry,
                                   snapshot=flash_snapshot(backup, segments))

    def test_physical_sector_table_mismatch_is_pending(self):
        mock = mock_for('hpm6800evk', 'jtag', sector_size_override=0x2000)
        with NativeSetup(board='hpm6800evk', link='jtag', mock=mock) as setup:
            address, backup, segments, geometry = setup.flash_args()
            mock.poke(address, backup)
            with self.assertRaises(Pending) as caught:
                setup.native.flash(address, backup, segments, geometry,
                                   snapshot=flash_snapshot(backup, segments))
            self.assertIn('物理扇区', str(caught.exception))
            self.assertEqual(mock.programs, 0)

    def test_readback_mismatch_restores_then_reports_failure(self):
        mock = mock_for('hpm6800evk', 'jtag',
                        corrupt_after_program=(FLASH_BASE + ERASE + 0x40, 0x00))
        with NativeSetup(board='hpm6800evk', link='jtag', mock=mock) as setup:
            address, backup, segments, geometry = setup.flash_args()
            mock.poke(address, backup)
            with self.assertRaises(NativeFailure) as caught:
                setup.native.flash(address, backup, segments, geometry,
                                   snapshot=flash_snapshot(backup, segments))
            evidence = caught.exception.evidence
            self.assertGreater(evidence['imageMismatch']['differences'], 0)
            self.assertTrue(evidence['restored'], '失败后必须先恢复扇区')
            self.assertFalse(evidence['verified'])
            self.assertEqual(mock.peek(address, len(backup)), backup)

    def test_restore_blocked_is_unsafe_and_marks_evidence(self):
        mock = mock_for('hpm6800evk', 'jtag', ignore_programs_after=1)
        with NativeSetup(board='hpm6800evk', link='jtag', mock=mock) as setup:
            address, backup, segments, geometry = setup.flash_args()
            mock.poke(address, backup)
            with self.assertRaises(Unsafe):
                setup.native.flash(address, backup, segments, geometry,
                                   snapshot=flash_snapshot(backup, segments))
            on_disk = json.loads((setup.folder / 'native-flash.json').read_text(encoding='utf-8'))
            self.assertFalse(on_disk['verified'])
            self.assertTrue(on_disk['restore'].get('rewritten'))
            self.assertGreater(on_disk['restore']['preRestore']['differences'], 0)

    def test_image_file_snapshot_is_accepted(self):
        mock = mock_for('hpm6800evk', 'jtag')
        with NativeSetup(board='hpm6800evk', link='jtag', mock=mock) as setup:
            address, backup, segments, geometry = setup.flash_args(sectors=2)
            mock.poke(address, backup)
            image = setup.folder / 'image.bin'
            setup.folder.mkdir(parents=True, exist_ok=True)
            image.write_bytes(b'not-an-elf')       # 只按 SHA 确认同一份文件（elf=True 才解析段表）
            snapshot = flash_snapshot(backup, segments, image=image,
                                      image_sha=native_mod.sha_file(image))
            evidence = setup.native.flash(address, backup, segments, geometry, snapshot=snapshot)
            self.assertTrue(evidence['verified'])
            self.assertEqual(evidence['image']['sha256'], native_mod.sha_file(image))
            self.assertFalse(evidence['image']['elfCrossChecked'])
            bad = dict(snapshot, imageSha256='2' * 64)
            with self.assertRaises(Pending):
                setup.native.flash(address, backup, segments, geometry, snapshot=bad)


if __name__ == '__main__':
    unittest.main(verbosity=2)
