"""原生 OpenOCD 证据路径：SRAM 与 Flash 的对照验证（由验收 Runner 调用）。

移植自 ZynqLinkPRO ``tools/board_acceptance.py`` 的原生 SRAM / F04 同扇区计划与
``native_flash_config`` / ``native_flash`` / ``flash_recovery``（f4b3a552、1642ac91、
8d8a2045），按本仓三个目标板重新定标，并复用本仓事实模块
（``board_acceptance_target``）的板卡登记、Flash 几何与稀疏擦除计划：

* **只做「原生」这一半**：Web 队列路径在原模块；本类在同一个目标、同一条链路上用
  本机 OpenOCD（``openocd.exe`` + 官方/本仓 CFG）独立复核，供 Runner 填 S02（块读写
  原生对照）、S04（恢复核对）、F04（同 ELF 同范围烧录测速）的原生证据。Runner 约定：
  **Web 侧测速完成后先恢复 SRAM/Flash 并释放 DAP，再调用本类**；本类每次操作自己起
  短命 OpenOCD 进程，不接管浏览器会话。
* **前置只读、不暂停错误标靶**：``probe()`` 只做 ``init`` + 只读命令（``curstate``、
  DPIDR/idcode、``scan_chain``、``mdw`` 读 DEV_ID）+ ``shutdown``，**不主动 ``halt``**；
  身份核对不通过时保留目标状态并报 ``Pending``。
* **目标身份与探针绑定**：CFG 里 ``adapter serial <sn>`` 把探针钉死在指定序列号上；
  身份按**链路**核对：SWD 用 DPIDR（F103ZE 0x1ba01477 / H743 0x6ba02477，F103ZE 还要
  DBGMCU DEV_ID=0x414，避免同 DPIDR 的其它 STM32 冒充）；ARM JTAG 按登记的 CPU TAP
  idcode（F103ZE 0x3ba00477 / H743 0x6ba00477）和边界 TAP；HPM6800EVK 必须
  回读到 0x1000563d。
* **不假装通过**：工具/探针缺失、快照与现场不一致、物理扇区几何不符 → ``Pending``；
  已经动过目标却无法确认恢复 → ``Unsafe``；逐字节对账不符且恢复已确认 → ``NativeFailure``
  （异常上带 ``evidence``）。每条路径都写阶段计时、SHA256 与文件证据。

计量口径（S02/F04 都按这个读）：``stages`` 里每个操作同时给两种时间——
``*ProcessSeconds`` 是**整条 OpenOCD CLI 进程**的墙钟（含启动、init、shutdown），
``*TclMs`` 是 TCL 里 ``[clock milliseconds]`` 夹住的**真正读写/擦写校验**时间。
两个口径不会混报；没有 TCL 计时就只留进程时间并显式标注。

``sram()`` 负责 OpenOCD 普通块读写与回读。H743/SWD 的交替流水由
``board_acceptance_pipeline`` 使用独立的原生 Bulk 会话执行。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from tools import board_acceptance_target as facts
from tools.board_acceptance_target import Pending, Unsafe, checked

ROOT = facts.ROOT

DEFAULT_SDK_ENV = Path(r'E:\sdk_env_v1.11.0')
SDK_OPENOCD = Path('tools/openocd/openocd.exe')
SDK_SCRIPTS = Path('tools/openocd/tcl')
SDK_BOARDS = Path('hpm_sdk/boards/openocd')

# 本仓官方 OpenOCD 脚本快照（烧录/调试页与 CLI 用的是同一份；JTAG 用它的 interface/cmsis-dap.cfg）
OFFICIAL_DIR = ROOT / 'web/app/flash/openocd-cfgs'
LOCAL_CFG = {('hpm6800evk', 'jtag'): ROOT / 'script_test/openocd_hpm6800evk_dap.cfg'}
ARM_TARGET = {'f103ze': 'stm32f1x.cfg', 'h743': 'stm32h7x_dual_bank.cfg'}
TARGET_NAME = {'f103ze': 'stm32f1x.cpu', 'h743': 'stm32h7x.cpu0', 'hpm6800evk': 'hpm6880.cpu0'}
DAP_CREATE_COMPAT = '''
# HPM SDK OpenOCD still names the DAP argument -chain-position.
if {[string match {*-chain-position*} [capture {help dap create}]]} {
    rename {dap create} slp_dap_create
    proc {dap create} {name flag position args} {
        if {$flag eq "-tap"} {set flag -chain-position}
        slp_dap_create $name $flag $position {*}$args
    }
}
'''
H7_EXAMINE = '''
proc slp_h7_examine_start {original} {
    set ::slp_h7_clocks_ready 0
    set ap stm32h7x.ap2
    set mask 0x0060003f
    if {![$ap was_examined]} {$ap arp_examine}
    set current [$ap read_memory 0xe00e1004 32 1]
    if {($current & $mask) != $mask} {
        $ap mww 0xe00e1004 [expr {$current | $mask}]
        if {([$ap read_memory 0xe00e1004 32 1] & $mask) != $mask} {
            error "STM32H743 DBGMCU debug clocks not confirmed"
        }
    }
    set ::slp_h7_clocks_ready 1
    if {$original ne ""} {uplevel #0 $original}
}
set slp_h7_clocks_ready 0
stm32h7x.cpu0 configure -event examine-start [list slp_h7_examine_start [stm32h7x.cpu0 cget -event examine-start]]
'''

# 按链路登记的目标身份（调试口 TAP/DPIDR）；arm-jtag 的 CPU TAP idcode 见审查结论。
EXPECTED_ID = {
    ('f103ze', 'swd'): ('dpidr', 0x1ba01477),
    ('f103ze', 'arm-jtag'): ('idcode', 0x3ba00477),
    ('h743', 'swd'): ('dpidr', 0x6ba02477),
    ('h743', 'arm-jtag'): ('idcode', 0x6ba00477),
    ('hpm6800evk', 'jtag'): ('idcode', 0x1000563d),
}
# F103ZE 的 DPIDR 与多颗 STM32 相同，必须再用 DBGMCU_IDCODE 的 DEV_ID 确认是 0x414。
STM32F1_DBGMCU_IDCODE = 0xE0042000
STM32F1_DEVICE_ID = 0x414
ARM_BOUNDARY_TAP = {'f103ze': 0x06414041, 'h743': 0x06450041}
# HPM 原版 CFG 的脚本依赖（缺一个就不能开原生会话）。
HPM_CFG_DEPENDENCIES = ('interface/cmsis-dap.cfg',
                        'boards/openocd/soc/hpm6880.cfg',
                        'boards/openocd/boards/hpm6800evk.cfg')
ARCH_MACHINE = {'arm': 'EM_ARM', 'riscv': 'EM_RISCV'}

# OpenOCD 日志里能明确归到「工具 / 探针 / 身份」的失败：这类只能记 Pending。
PENDING_PATTERNS = (
    'unable to find a matching cmsis-dap device',
    'no cmsis-dap device found',
    "couldn't open device",
    'libusb_error',
    'unable to open',
    'no device found',
    'idcode mismatch',
)


def sha(data):
    return hashlib.sha256(bytes(data)).hexdigest()


def sha_file(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def segments_sha(segments):
    """ELF 段表指纹：段地址、长度、内容一起算，用来确认「同一份 ELF 内容」。"""
    body = json.dumps([[address, len(payload), sha(payload)] for address, payload in segments],
                      sort_keys=True).encode('utf-8')
    return sha(body)


def differ(expected, actual):
    """逐字节对账摘要：长度、错字节数、首个差异偏移（0 = 完全一致）。"""
    expected, actual = bytes(expected), bytes(actual)
    if len(expected) != len(actual):
        return {'length': [len(expected), len(actual)], 'first': None,
                'differences': abs(len(expected) - len(actual))}
    first, count = None, 0
    for index, (left, right) in enumerate(zip(expected, actual)):
        if left != right:
            count += 1
            if first is None:
                first = index
    return {'length': [len(expected), len(actual)], 'first': first, 'differences': count}


def sector_ranges(base, erase, backup_address, end, segments):
    """**按段合并**的稀疏擦除计划：只在真正被镜像覆盖的扇区上擦写。

    不能用 ``low..high`` 一把梭：那会把两段之间根本没被镜像覆盖的空扇区也擦掉
    （ZynqLinkPRO 8d8a2045 的反面教材）。相邻或重叠的扇区合并；空档保持原样。
    """
    ranges = []
    for address, payload in sorted(segments, key=lambda item: item[0]):
        first = base + ((address - base) // erase) * erase
        last = base + (((address + len(payload) - base + erase - 1) // erase)) * erase
        checked(backup_address <= first < last <= end,
                f'镜像段 0x{address:x} 的扇区超出备份范围 0x{backup_address:x}..0x{end:x}')
        if ranges and first <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(last, ranges[-1][1]))
        else:
            ranges.append((first, last))
    checked(ranges, '镜像段为空')
    return ranges


def tcl(value):
    """Tcl 词：路径/序列号带空格也安全（花括号内不做替换，Windows 反斜杠照原样）。

    只拒绝会真正破坏 CFG 的字符：花括号、换行、NUL。
    """
    text = value.as_posix() if isinstance(value, Path) else str(value)
    checked(not any(char in text for char in '{}') and '\n' not in text
            and '\r' not in text and '\0' not in text, 'Tcl 参数含不支持字符')
    return '{' + text + '}'


class NativeFailure(RuntimeError):
    """逐字节对账失败（目标已恢复到可确认状态）；完整证据挂在 ``evidence`` 上。"""

    def __init__(self, message, evidence=None):
        super().__init__(message)
        self.evidence = evidence or {}


class OpenOcdStageError(RuntimeError):
    """OpenOCD 阶段失败（非零退出或超时）。

    与 Pending 分开：``Pending`` 只用于「进程根本没起来 / 工具或探针缺失」这类**确定没写过**
    的情形；一旦阶段可能已经动过目标 Flash，就只能进失败通道，由调用方只读复核并尝试恢复。
    """

    def __init__(self, message, *, log=None, script=None, exit_code=None, timed_out=False):
        super().__init__(message)
        self.log = log
        self.script = script
        self.exit_code = exit_code
        self.timed_out = timed_out


def parse_flash_sectors(text):
    """按真实 native OpenOCD ``flash info <bank> sectors`` 行格式解析扇区表。

    形如::

        # 0 : stm32h7x at 0x08000000, size 0x00100000, buswidth 0, chipwidth 0
            #  0: 0x00000000 (0x00020000 128kB) not protected
        # 1 : stm32h7x at 0x08100000, size 0x00100000, buswidth 0, chipwidth 0
            #  0: 0x00000000 (0x00020000 128kB) not protected

    行里的地址是**相对 bank 基址**的偏移（H743 双 bank 各从 0 起），这里换算成绝对地址。
    """
    banks, sectors = [], []
    current = None
    for line in text.splitlines():
        header = re.search(r'^#\s*(\d+)\s*:\s*(\S+)\s+at\s+(0x[0-9a-f]+),\s*size\s+(0x[0-9a-f]+)',
                           line, re.I)
        if header:
            current = {'index': int(header.group(1)), 'driver': header.group(2),
                       'address': int(header.group(3), 16), 'size': int(header.group(4), 16),
                       'sectors': []}
            banks.append(current)
            continue
        row = re.search(r'^\s*#\s*(\d+):\s*(0x[0-9a-f]+)\s+\((0x[0-9a-f]+)', line, re.I)
        if row and current is not None:
            sector = {'index': int(row.group(1)),
                      'address': current['address'] + int(row.group(2), 16),
                      'size': int(row.group(3), 16)}
            current['sectors'].append(sector)
            sectors.append(sector)
    return {'banks': banks, 'sectors': sectors}


# --------------------------------------------------------------------------
# 工具与 CFG 选择（只登记文件系统事实，不访问硬件）
# --------------------------------------------------------------------------


def resolve_tools(*, openocd=None, scripts=None, hpm_env=None):
    """确定本机 OpenOCD / 脚本树 / HPM SDK 环境，来源逐项登记。"""
    env_dir = Path(hpm_env or os.environ.get('HPM_SDK_ENV_DIR') or DEFAULT_SDK_ENV)
    exe = Path(openocd) if openocd else Path(os.environ.get('OPENOCD_EXE') or env_dir / SDK_OPENOCD)
    script_dir = Path(scripts) if scripts else Path(os.environ.get('OPENOCD_SCRIPTS') or env_dir / SDK_SCRIPTS)
    boards = env_dir / SDK_BOARDS
    return {
        'openocd': str(exe), 'openocdSource': 'explicit' if openocd else 'env-or-sdk-default',
        'scripts': str(script_dir), 'scriptsSource': 'explicit' if scripts else 'env-or-sdk-default',
        'hpmSdkEnv': str(env_dir), 'hpmSdkEnvSource': 'explicit' if hpm_env else 'env-or-default',
        'hpmBoards': str(boards),
        'openocdPresent': exe.is_file(), 'scriptsPresent': script_dir.is_dir(),
        'hpmBoardsPresent': boards.is_dir(),
    }


def hpm_dependency_report(tools):
    """HPM 原版脚本依赖：手写 CFG 会 source 的 SoC/board 文件必须真的在。"""
    env_dir = Path(tools['hpmSdkEnv'])
    script_dir = Path(tools['scripts'])
    missing = []
    for relative in HPM_CFG_DEPENDENCIES:
        if relative.startswith('interface/'):
            candidate = script_dir / relative
        else:
            candidate = env_dir / 'hpm_sdk' / relative
        if not candidate.is_file():
            missing.append({'required': relative, 'path': str(candidate)})
    return {'checked': [{'required': relative} for relative in HPM_CFG_DEPENDENCIES],
            'missing': missing}


def cfg_for(board, link, *, cfg=None, expected_id=None):
    """Select the same official ARM target scripts as the Web workbench."""
    if cfg:
        path = Path(cfg)
        if not path.is_file():
            raise Pending(f'指定的 OpenOCD CFG 不存在：{path}')
        return path.read_text(encoding='utf-8', errors='replace'), [], \
            {'kind': 'explicit', 'path': str(path)}
    local = LOCAL_CFG.get((board, link))
    if local is not None:
        if not local.is_file():
            raise Pending(f'缺少本仓 OpenOCD CFG：{local.relative_to(ROOT).as_posix()}')
        return local.read_text(encoding='utf-8', errors='replace'), [], \
            {'kind': 'repo', 'path': str(local)}
    if board in ARM_TARGET and link in ('swd', 'arm-jtag'):
        interface = OFFICIAL_DIR / 'interface/cmsis-dap.cfg'
        target = OFFICIAL_DIR / 'target' / ARM_TARGET[board]
        if not interface.is_file() or not target.is_file():
            raise Pending('缺少官方 OpenOCD 脚本快照（web/app/flash/openocd-cfgs）')
        device_id = expected_id or EXPECTED_ID[(board, link)][1]
        body = '\n'.join([
            f'source {tcl(interface)}',
            f'transport select {"jtag" if link == "arm-jtag" else "swd"}',
            f'set CPUTAPID 0x{device_id:x}',
            DAP_CREATE_COMPAT,
            f'source {tcl(target)}',
            f'{TARGET_NAME[board]} configure -work-area-backup 1',
        ])
        if board == 'h743':
            body += '\n' + H7_EXAMINE
        return body, [str(OFFICIAL_DIR)], \
            {'kind': 'official', 'interface': str(interface), 'target': str(target),
             'cpuId': device_id}
    raise Pending(f'没有 {board}/{link} 的原生 OpenOCD CFG 选择')


def geometry_tuple(geometry):
    """把三种几何写法归一成 (erase, base, size)。"""
    if isinstance(geometry, dict):
        base, size, erase = int(geometry['base']), int(geometry['bytes']), int(geometry['erase'])
    else:
        base, size, erase = (int(value) for value in geometry)
    checked(erase > 0 and size > 0, 'Flash 几何无效')
    return erase, base, size


# --------------------------------------------------------------------------
# 默认 transport：本机 OpenOCD 子进程
# --------------------------------------------------------------------------


class OpenOcdRunner:
    """把一个操作翻译成一条 OpenOCD 命令行；每次调用一个短命进程，失败留全量日志。

    ``NativeOpenOcd`` 只依赖下面这组方法（可以直接用 mock 替换）。返回的计时统一给
    ``processSeconds``（整条进程墙钟）与 ``tclMs``（TCL 内真正的读写/擦写校验耗时）；
    读类方法的 ``data`` 是原始字节。
    """

    def __init__(self, native):
        self.native = native
        self.logs = []

    # -- 命令行：每个阶段一个真实 Tcl 脚本 --------------------------------
    def _write_script(self, label, lines):
        """把阶段脚本落盘（证据）；Tcl 变量一律走 ``[$zn_... ...]`` 参数语法。"""
        self.native.folder.mkdir(parents=True, exist_ok=True)
        path = self.native.folder / f'native-{label}.tcl'
        if self.native.board == 'h743' and self.native.cfg_source['kind'] == 'official':
            lines = [statement for line in lines for statement in (
                [line, 'if {!$slp_h7_clocks_ready || ![stm32h7x.cpu0 was_examined]} {error "H743 examination failed"}']
                if line == 'init' else [line])]
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return path

    def _argv(self, script_path=None):
        native = self.native
        argv = [native.tools['openocd']]
        for directory in native.script_dirs:          # 官方快照 / HPM board 脚本等
            argv += ['-s', str(directory)]
        if native.board == 'hpm6800evk':
            argv += ['-s', native.tools['hpmBoards'],
                     '-c', f'set HPM_SDK_BASE {tcl(native.tools["hpmSdkEnv"] + "/hpm_sdk")}']
        argv += ['-s', native.tools['scripts']]
        argv += ['-f', str(native.cfg_path)]
        if script_path is not None:
            argv += ['-f', str(script_path)]
        return argv

    def _run(self, label, lines, *, timeout=None, may_write=False, stay_open=False):
        """跑一个阶段脚本。

        ``may_write=True``（program / write）：**任何**进程级失败（非零退出、超时）都抛
        ``OpenOcdStageError``，绝不在这里降级成 Pending —— 这个阶段可能已经写进目标
        Flash，必须由调用方只读复核并尝试恢复。只有「进程根本没起来」（OSError）才是
        确定没写过的 Pending。
        """
        native = self.native
        script_path = self._write_script(label, lines)
        argv = self._argv(script_path)
        native.folder.mkdir(parents=True, exist_ok=True)
        log_path = native.folder / f'native-{label}.log'
        temporary = native.folder / 'tmp'
        temporary.mkdir(parents=True, exist_ok=True)
        # 原生子进程不得依赖系统 TEMP（本仓 ISSUE-029 的现场）；环境也把 SDK 基址带出去。
        environment = dict(os.environ)
        environment.update(TEMP=str(temporary), TMP=str(temporary),
                           HPM_SDK_BASE=native.tools['hpmSdkEnv'] + '/hpm_sdk',
                           HPM_SDK_ENV_DIR=native.tools['hpmSdkEnv'])
        creation = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
        started = time.perf_counter()
        try:
            with log_path.open('wb') as log:
                launch = dict(cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT, **creation)
                if stay_open:
                    with subprocess.Popen(argv, **launch) as child:
                        try:
                            while child.poll() is None:
                                time.sleep(.1)
                        finally:
                            if child.poll() is None:
                                child.terminate()
                else:
                    child = subprocess.run(argv, timeout=timeout or native.timeout, **launch)
        except subprocess.TimeoutExpired as error:
            seconds = time.perf_counter() - started
            text = log_path.read_text(encoding='utf-8', errors='replace') if log_path.is_file() else ''
            self.logs.append({'stage': label, 'argv': [str(arg) for arg in argv],
                              'script': str(script_path), 'seconds': seconds,
                              'timedOut': True, 'error': repr(error)})
            raise OpenOcdStageError(
                f'OpenOCD {label} 超时（{timeout or native.timeout}s）：见 {log_path}',
                log=str(log_path), script=str(script_path), timed_out=True) from error
        except OSError as error:
            self.logs.append({'stage': label, 'argv': [str(arg) for arg in argv],
                              'seconds': time.perf_counter() - started, 'error': repr(error)})
            # 进程没起来 = 确定没写过目标，可以安全记 Pending
            raise Pending(f'OpenOCD 无法启动（{label}）：{error}；见 {log_path}') from error
        text = log_path.read_text(encoding='utf-8', errors='replace')
        seconds = time.perf_counter() - started
        self.logs.append({'stage': label, 'argv': [str(arg) for arg in argv],
                          'script': str(script_path),
                          'exit': child.returncode, 'seconds': seconds})
        if child.returncode != 0:
            if may_write:
                raise OpenOcdStageError(
                    f'OpenOCD {label} 失败（exit {child.returncode}）：见 {log_path}',
                    log=str(log_path), script=str(script_path), exit_code=child.returncode)
            self._classify(label, text, log_path, script_path)
            raise OpenOcdStageError(
                f'OpenOCD {label} 失败（exit {child.returncode}）：见 {log_path}',
                log=str(log_path), script=str(script_path), exit_code=child.returncode)
        return {'text': text, 'seconds': seconds, 'log': str(log_path),
                'script': str(script_path)}

    @staticmethod
    def _classify(label, text, log_path, script_path=None):
        """把「工具/探针/身份」类失败收敛成 Pending；其余交给调用方按现场判定。"""
        low = text.lower()
        for pattern in PENDING_PATTERNS:
            if pattern in low:
                raise Pending(f'OpenOCD {label} 不满足原生复核条件（{pattern}）：见 {log_path}')
        bad = re.search(r'(expected-id[^\n]{0,80}|idcode mismatch[^\n]{0,80}|'
                        r'tap/device found:\s*0x0+)', text, re.I)
        if bad:
            raise Pending(f'OpenOCD {label} 目标身份不符：{bad.group(1)[:120]}；见 {log_path}')

    # -- 身份 / 状态（只读！不主动 halt）---------------------------------
    def probe_script(self):
        """前置只读脚本：**不含 halt**，只回读状态/身份/链上 TAP。"""
        native = self.native
        lines = ['set zn_target ' + tcl(native.target), 'init',
                 'if {[$zn_target curstate] eq "unknown"} {poll}',
                 'echo "ZN_STATE [$zn_target curstate]"']
        if native.link != 'swd':
            # JTAG：链上 TAP 与 idcode（边界核对；HPM 也走这条，必须看到 0x1000563d）
            lines.append('puts [capture {scan_chain}]')
        if native.board == 'f103ze' and native.link == 'swd':
            # DPIDR 与其它 STM32 相同，必须补一条 DBGMCU_IDCODE 只读回读
            lines.append(f'mdw 0x{STM32F1_DBGMCU_IDCODE:x} 1')
        lines.append('shutdown')
        return lines

    def probe(self):
        run = self._run('probe', self.probe_script())
        return self._identity_from_log(self.native, run['text'], run['log'])

    def run_commands(self, commands, *, stay_open=False):
        lines = ['init', *commands]
        if not stay_open:
            lines.append('shutdown')
        return self._run('commands', lines, may_write=True, stay_open=stay_open)

    @staticmethod
    def _identity_from_log(native, text, log_path):
        state = re.search(r'ZN_STATE\s+(running|halted|reset|unknown|debug)', text)
        dpidr = re.search(r'SWD DPIDR 0x([0-9a-fA-F]+)', text)
        idcodes = [int(value, 16) for value in
                   re.findall(r'(?:idcode\s*=\s*|tap/device found:\s*)0x([0-9a-fA-F]+)', text)]
        chain = [[int(code, 16)] for code in
                 re.findall(r'^\s*\d+\s+\S+\s+[YN]\s+0x([0-9a-fA-F]+)', text, re.M)]
        dev_id = re.search(r'0xe0042000:\s*([0-9a-fA-F]{8})', text)
        return {
            'probeSerial': native.probe_serial,
            'dpidr': int(dpidr.group(1), 16) if dpidr else None,
            # JTAG 的 idcode 既可能来自 tap/device found，也可能只在 scan_chain 表里
            'idcode': (chain[0][0] if chain else (idcodes[0] if idcodes else None)),
            'idcodes': idcodes or [entry[0] for entry in chain],
            'scanChain': chain,
            'debugIdcode': ((int(dev_id.group(1), 16) & 0xFFF)
                            if dev_id else None),
            'state': state.group(1) if state else None,
            'haltedByOpenOcd': state is not None and state.group(1) == 'halted',
            'log': log_path,
        }

    def halt(self):
        return self._run('halt', ['set zn_target ' + tcl(self.native.target),
                                  'init', 'halt', 'shutdown'])

    def resume(self):
        return self._run('resume', ['set zn_target ' + tcl(self.native.target),
                                    'init', 'resume', 'shutdown'])

    def reset_run(self):
        # 与 Web 烧录后的复位等价：HPM 走 DM 的 ndmreset（cfg 里 reset_config none）。
        run = self._run('reset-run', ['set zn_target ' + tcl(self.native.target),
                                      'init', 'reset run', 'shutdown'])
        return {'processSeconds': run['seconds'], 'log': run['log']}

    # -- 几何 ------------------------------------------------------------
    def geometry(self):
        """按真实 ``flash info <bank> sectors`` 回读物理扇区表。

        H743 有 bank1(0x08000000)/bank2(0x08100000) 两个 bank，都要探测；bank1 不存在时
        ``flash probe 1`` 会报错，用 ``catch`` 吞掉并记 ``ZN_NO_BANK 1``。
        """
        native = self.native
        banks = (0, 1) if native.board == 'h743' else (0,)
        lines = ['init', 'flash probe 0', 'puts [capture {flash info 0 sectors}]']
        for index in banks[1:]:
            lines += [f'if {{[catch {{flash probe {index}}} zn_bank_err]}} '
                      f'{{ puts "ZN_NO_BANK {index}" }} '
                      f'{{ puts [capture {{flash info {index} sectors}}] }}']
        lines.append('shutdown')
        run = self._run('geometry', lines)
        parsed = parse_flash_sectors(run['text'])
        return {'banks': parsed['banks'], 'sectors': parsed['sectors'],
                'probedBanks': list(banks), 'processSeconds': run['seconds'],
                'log': run['log']}

    # -- 读写（带 TCL 实测计时）------------------------------------------
    def read(self, address, length):
        native = self.native
        native.folder.mkdir(parents=True, exist_ok=True)
        path = native.folder / f'native-read-{int(address):08x}-{int(length)}.bin'
        if path.exists():
            path.unlink()
        run = self._run(f'read-{int(address):08x}', [
            'set zn_addr ' + hex(int(address)),
            'set zn_len ' + str(int(length)),
            'set zn_file ' + tcl(path),
            'init',
            'set zn_t0 [clock milliseconds]',
            'dump_image $zn_file $zn_addr $zn_len',
            'set zn_t1 [clock milliseconds]',
            'echo "ZN_TCL read_ms=[expr {$zn_t1 - $zn_t0}]"',
            'shutdown',
        ])
        checked(path.is_file(), f'dump_image 没有产出文件：{path}')
        data = path.read_bytes()
        checked(len(data) == int(length),
                f'独立回读长度不符：请求 {length}，实收 {len(data)}')
        return {'data': data, 'processSeconds': run['seconds'],
                'tclMs': _tcl_ms(run['text'], 'read_ms'), 'log': run['log']}

    def write(self, address, data):
        native = self.native
        native.folder.mkdir(parents=True, exist_ok=True)
        path = native.folder / f'native-write-{int(address):08x}.bin'
        path.write_bytes(bytes(data))
        run = self._run(f'write-{int(address):08x}', [
            'set zn_addr ' + hex(int(address)),
            'set zn_file ' + tcl(path),
            'init', 'halt',
            'set zn_t0 [clock milliseconds]',
            'load_image $zn_file $zn_addr bin',
            'set zn_t1 [clock milliseconds]',
            'echo "ZN_TCL write_ms=[expr {$zn_t1 - $zn_t0}]"',
            'shutdown',
        ], may_write=True)
        return {'processSeconds': run['seconds'],
                'tclMs': _tcl_ms(run['text'], 'write_ms'), 'log': run['log']}

    def program(self, ranges):
        native = self.native
        native.folder.mkdir(parents=True, exist_ok=True)
        lines, images = ['init', 'halt', 'set zn_t0 [clock milliseconds]'], []
        for index, (address, payload) in enumerate(ranges):
            path = native.folder / f'native-program-{index}-{int(address):08x}.bin'
            path.write_bytes(bytes(payload))
            images.append(path)
            lines += ['set zn_addr ' + hex(int(address)), 'set zn_file ' + tcl(path),
                      'flash write_image erase $zn_file $zn_addr bin']
        lines.append('set zn_t1 [clock milliseconds]')
        for index, (address, _) in enumerate(ranges):
            lines += ['set zn_addr ' + hex(int(address)),
                      'set zn_file ' + tcl(images[index]),
                      'verify_image $zn_file $zn_addr bin']
        lines += ['set zn_t2 [clock milliseconds]',
                  'echo "ZN_STAGES program_ms=[expr {$zn_t1 - $zn_t0}] '
                  'verify_ms=[expr {$zn_t2 - $zn_t1}]"',
                  'shutdown']
        run = self._run('program', lines, may_write=True)
        stages = re.search(r'ZN_STAGES\s+program_ms=(\d+)\s+verify_ms=(\d+)', run['text'])
        return {'processSeconds': run['seconds'],
                'programMs': int(stages.group(1)) if stages else None,
                'verifyMs': int(stages.group(2)) if stages else None,
                'bytes': sum(len(payload) for _, payload in ranges),
                'ranges': [[int(address), len(payload)] for address, payload in ranges],
                'log': run['log']}


def _tcl_ms(text, name):
    match = re.search(rf'ZN_TCL\s+{re.escape(name)}=(\d+)', text)
    return int(match.group(1)) if match else None


# --------------------------------------------------------------------------
# 主类
# --------------------------------------------------------------------------


class NativeOpenOcd:
    """本机 OpenOCD 的原生复核入口；Runner 在 execute 阶段调用 ``sram`` / ``flash``。

    构造即确定板卡、链路、时钟、**探针序列号**与证据目录；工具与 CFG 的选择也在此落地
    （``self.tools`` / ``self.script_dirs`` / ``self.cfg_source`` / ``self.cfg_digest``），
    Runner 可先把它们写进计划证据再决定是否真的执行。
    """

    def __init__(self, board, link, clock_hz, probe_serial, folder, *,
                 openocd=None, scripts=None, hpm_env=None, cfg=None, runner=None,
                 timeout=240, expected=None):
        checked(board in facts.BOARDS, f'未知板卡：{board}')
        self.board = board
        self.link = facts.normalize_link(board, link)
        checked(isinstance(clock_hz, int) and clock_hz > 0, f'时钟须为正整数 Hz：{clock_hz!r}')
        self.clock_hz = clock_hz
        checked(isinstance(probe_serial, str) and probe_serial.strip(),
                '原生复核必须给探针序列号（不接受「随便哪只在线探针」）')
        self.probe_serial = probe_serial.strip()
        self.folder = Path(folder)
        self.timeout = timeout
        self.profile = facts.BOARDS[board]
        self.target = TARGET_NAME[board]
        self.tools = resolve_tools(openocd=openocd, scripts=scripts, hpm_env=hpm_env)
        registered = EXPECTED_ID[(board, self.link)]
        if expected is not None:
            kind = 'idcode' if self.link in ('arm-jtag', 'jtag') else 'dpidr'
            registered = (kind, int(expected))
        self.expected_identity = registered
        self.cfg_text, self.script_dirs, self.cfg_source = cfg_for(
            board, self.link, cfg=cfg, expected_id=registered[1]
            if registered[0] == 'idcode' else None)
        self.cfg_digest = sha(self.cfg_text.encode('utf-8'))
        self.cfg_path = self.folder / 'native-openocd.cfg'
        self.cfg_lines = self._config_lines()
        self.identity = None
        self.runner = runner if runner is not None else self._default_runner()
        if runner is not None:
            self.tools = {**self.tools, 'runner': 'injected-mock'}

    # -- 构造辅助 --------------------------------------------------------
    def _default_runner(self):
        """没有注入 mock 时才要求本机工具真的在：缺了就 Pending，不静默降级。"""
        missing = [name for name, present in
                   (('openocd', self.tools['openocdPresent']),
                    ('scripts', self.tools['scriptsPresent'])) if not present]
        if self.board == 'hpm6800evk' and not self.tools['hpmBoardsPresent']:
            missing.append('hpm-sdk-boards')
        if missing:
            raise Pending('本机原生工具缺失（' + '、'.join(missing) + '）：'
                          '用 --openocd / OPENOCD_EXE、--openocd-scripts / OPENOCD_SCRIPTS、'
                          f'HPM_SDK_ENV_DIR 指定，或先安装 HPM SDK 环境（默认 {DEFAULT_SDK_ENV}）')
        return OpenOcdRunner(self)

    def _config_lines(self):
        head = ['gdb port disabled', 'telnet port disabled', 'tcl port disabled']
        body = ([f'source {tcl(self.cfg_source["path"])}']
                if self.cfg_source['kind'] in ('repo', 'explicit')
                else self.cfg_text.splitlines())
        tail = [f'adapter speed {self.clock_hz // 1000}',
                f'adapter serial {tcl(self.probe_serial)}',
                'reset_config none']
        return head + body + tail

    def prepare(self):
        """把生成的 CFG 落盘（证据的一部分）；幂等。"""
        self.folder.mkdir(parents=True, exist_ok=True)
        self.cfg_path.write_text('\n'.join(self.cfg_lines) + '\n', encoding='utf-8')
        return self.cfg_path

    # -- 小工具 ----------------------------------------------------------
    def _evidence_file(self, name, value):
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / name
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n',
                        encoding='utf-8')
        return path

    def tools_evidence(self):
        evidence = {'tools': dict(self.tools),
                    'cfg': {'source': self.cfg_source, 'sha256': self.cfg_digest,
                            'path': str(self.cfg_path), 'lines': self.cfg_lines},
                    'scriptDirs': [str(directory) for directory in self.script_dirs],
                    'folder': str(self.folder)}
        if self.board == 'hpm6800evk':
            evidence['hpmDependencies'] = hpm_dependency_report(self.tools)
        return evidence

    # -- 身份（只读，不 halt）--------------------------------------------
    def preflight(self, *, require_running=True):
        """Identify the target before issuing any halt, resume or write."""
        self.prepare()
        if self.tools.get('runner') != 'injected-mock':
            missing = [name for name, present in (('openocd', self.tools['openocdPresent']),
                                                  ('scripts', self.tools['scriptsPresent']))
                       if not present]
            if self.board == 'hpm6800evk' and not self.tools['hpmBoardsPresent']:
                missing.append('hpm-sdk-boards')
            if missing:
                raise Pending('本机原生工具缺失：' + '、'.join(sorted(missing)))
            if self.board == 'hpm6800evk':
                report = hpm_dependency_report(self.tools)
                if report['missing']:
                    raise Pending('HPM 原版脚本依赖缺失：' + '、'.join(
                        item['required'] for item in report['missing']))
        probe = self.runner.probe()
        kind, expected = self.expected_identity
        observed = probe.get(kind)
        identity = {
            'board': self.board, 'link': self.link, 'clockHz': self.clock_hz,
            'probeSerial': self.probe_serial,
            'probeBinding': f'adapter serial {tcl(self.probe_serial)}',
            'expected': {'kind': kind, 'value': expected},
            'dpidr': probe.get('dpidr'), 'idcode': probe.get('idcode'),
            'idcodes': probe.get('idcodes'), 'scanChain': probe.get('scanChain'),
            'stateBefore': probe.get('state'), 'haltedByOpenOcd': probe.get('haltedByOpenOcd'),
            'source': '本仓/官方 OpenOCD CFG + adapter serial 绑定；身份按链路登记值比对',
        }
        problem = None
        if observed is None:
            problem = (f'原生 OpenOCD 未回读 {kind}（DPIDR/idcode），不能确认接的是 '
                       f'{self.profile["label"]}')
        elif observed != expected:
            problem = (f'原生 OpenOCD 回读的 {kind} 0x{observed:08x} 与登记的 '
                       f'{self.profile["label"]} 0x{expected:08x} 不符：不接默认目标')
        if problem is None and self.board == 'f103ze' and self.link == 'swd':
            device = probe.get('debugIdcode')
            identity['debugIdcode'] = device
            if device != STM32F1_DEVICE_ID:
                problem = (f'F103ZE 需要 DBGMCU_IDCODE 的 DEV_ID=0x{STM32F1_DEVICE_ID:03x}，'
                           f'回读为 {device if device is None else hex(device)}：'
                           '同 DPIDR 的其它 STM32 不能当这块板')
        if problem is None and self.link != 'swd':
            chain = [entry[0] for entry in (probe.get('scanChain') or [])]
            boundary = ARM_BOUNDARY_TAP.get(self.board)
            wanted = [expected] if boundary is None else [expected, boundary]
            if boundary is not None and len(chain) == 2:
                chain[1] &= 0x0fffffff
            if chain != wanted:
                problem = f'JTAG 链边界不符：期望 {[hex(code) for code in wanted]}，实际 {[hex(code) for code in chain]}'
        states = ('running',) if require_running else ('running', 'halted')
        if problem is None and probe.get('state') not in states:
            problem = f'目标状态未确认，期望 {" / ".join(states)}，实际 {probe.get("state")}'
        if problem is not None:
            identity['failure'] = problem
            self.identity = identity
            self._evidence_file('native-identity.json', identity)
            raise Pending(problem)
        self.identity = identity
        self._evidence_file('native-identity.json', identity)
        return identity

    # ==================================================================
    # SRAM
    # ==================================================================
    def sram(self, address, payload, *, expected=None, restore=True, pipeline=None):
        """SRAM 图案写入 + 独立回读 + 恢复原值；返回带双口径阶段计时与 SHA256 的证据。

        ``expected``：Web 路径测速**完成后、释放 DAP 之前**采到的 SRAM 快照。给了就先
        独立读一遍现场并逐字节比对——现场与快照不一致（别人还在跑/复位过）就 Pending，
        不动手。H743 流水由验收 runner 另开原生 Bulk 会话。
        """
        began = time.perf_counter()
        address = int(address)
        payload = bytes(payload)
        low, high = facts.SRAM_REGION[self.board]
        checked(payload, 'SRAM 图案为空')
        checked(low <= address and address + len(payload) <= high,
                f'SRAM 窗口超出 {self.board} 登记区间 0x{low:08x}..0x{high:08x}')
        if pipeline is not None:
            raise Pending('请由验收 runner 执行 H743 独立 Bulk 流水窗口')
        identity = self.preflight()
        evidence = {'kind': 'sram', 'address': address, 'bytes': len(payload),
                    'sha256': sha(payload), 'identity': identity, 'region': [low, high],
                    'pipeline': None,
                    'pipelineNote': 'OpenOCD 普通块读写',
                    **self.tools_evidence()}
        stages = {}
        backup = self.runner.read(address, len(payload))
        stages['backup'] = _stage(backup)
        backup_bytes = backup['data']
        evidence.update({'backupSha256': sha(backup_bytes), 'stages': stages})
        if expected is not None:
            snapshot = differ(bytes(expected), backup_bytes)
            evidence['snapshot'] = {'sha256': sha(bytes(expected)),
                                    'differences': snapshot['differences'],
                                    'first': snapshot['first']}
            if snapshot['differences']:
                raise Pending('现场 SRAM 与 Web 快照不一致（可能还在跑/被复位过）：'
                              f'{snapshot}；不承认快照，也不动手')

        failure = None
        try:
            wrote = self.runner.write(address, payload)
            stages['write'] = _stage(wrote)
            readback = self.runner.read(address, len(payload))
            stages['read'] = _stage(readback)
            write_diff = differ(payload, readback['data'])
            evidence.update({'readbackSha256': sha(readback['data']),
                             'writeMismatch': write_diff,
                             'writeBytesPerSecond': _rate(len(payload), stages['write']),
                             'readBytesPerSecond': _rate(len(readback['data']), stages['read'])})
            if write_diff['differences']:
                failure = NativeFailure(f'SRAM 写入后独立回读不一致：{write_diff}', evidence)
        except Exception as error:  # noqa: BLE001 - 先恢复，再按现场结论分类
            failure = error

        if failure is not None:
            # 已经动过 SRAM：先写回备份并独立核对，恢复不了就只能是 Unsafe。
            record = {'preRestore': {'readbackSha256': evidence.get('readbackSha256')}}
            try:
                wrote = self.runner.write(address, backup_bytes)
                stages['restore'] = _stage(wrote)
                restore_read = self.runner.read(address, len(backup_bytes))
                stages['restoreRead'] = _stage(restore_read)
                restore_diff = differ(backup_bytes, restore_read['data'])
                record['postRestore'] = {'sha256': sha(restore_read['data']),
                                         'differences': restore_diff['differences']}
                record['verified'] = not restore_diff['differences']
            except Exception as restore_error:  # noqa: BLE001
                evidence.update({'stages': stages, 'verified': False, 'restore': record,
                                 'recoveryError': repr(restore_error),
                                 'stateAfter': self._restore_running(),
                                 'totalSeconds': time.perf_counter() - began})
                self._evidence_file('native-sram.json', evidence)
                raise Unsafe(f'SRAM 复核失败且恢复无法确认：{restore_error!r}；'
                             f'见 {self.folder}') from failure
            evidence['restore'] = record
            evidence.update({'stages': stages, 'verified': False,
                             'stateAfter': self._restore_running(),
                             'totalSeconds': time.perf_counter() - began})
            self._evidence_file('native-sram.json', evidence)
            if not record['verified']:
                raise Unsafe(f'SRAM 写回备份后仍不一致，保留 {self.folder}：'
                             f'{record["postRestore"]}') from failure
            if isinstance(failure, (Pending, Unsafe)):
                raise failure
            if isinstance(failure, NativeFailure):
                raise failure
            raise RuntimeError(f'SRAM 复核失败：{failure}') from failure

        if restore:
            wrote = self.runner.write(address, backup_bytes)
            stages['restore'] = _stage(wrote)
            restore_read = self.runner.read(address, len(backup_bytes))
            stages['restoreRead'] = _stage(restore_read)
            restore_diff = differ(backup_bytes, restore_read['data'])
            record = {'preRestore': {'readbackSha256': sha(readback['data'])},
                      'postRestore': {'sha256': sha(restore_read['data']),
                                      'differences': restore_diff['differences']},
                      'verified': not restore_diff['differences']}
            evidence['restore'] = record
            evidence['restored'] = record['verified']
            if restore_diff['differences']:
                evidence.update({'stages': stages, 'verified': False,
                                 'stateAfter': self._restore_running(),
                                 'totalSeconds': time.perf_counter() - began})
                self._evidence_file('native-sram.json', evidence)
                raise Unsafe(f'SRAM 恢复无法确认，保留 {self.folder}：{restore_diff}')
        else:
            evidence['restored'] = None

        state = self._restore_running()
        evidence.update({'stateAfter': state, 'stages': stages, 'verified': True,
                         'totalSeconds': time.perf_counter() - began})
        self._evidence_file('native-sram.json', evidence)
        return evidence

    def _restore_running(self):
        """让目标回到运行态并回读确认（SRAM 成功/失败路径都要走）。"""
        try:
            self.runner.resume()
            probe = self.runner.probe()
            return {'resumed': True, 'state': probe.get('state')}
        except Exception as error:  # noqa: BLE001 - 状态恢复失败要如实登记
            return {'resumed': False, 'error': repr(error)}

    def _force_running(self):
        """读状态，不是 running 就 resume 再读一次；从不抛异常（只用于证据判定）。"""
        try:
            probe = self.runner.probe()
            if probe.get('state') == 'running':
                return {'state': 'running', 'resumed': False}
            self.runner.resume()
            again = self.runner.probe()
            return {'state': again.get('state'), 'resumed': True}
        except Exception as error:  # noqa: BLE001
            return {'state': None, 'resumed': False, 'error': repr(error)}

    # ==================================================================
    # Flash
    # ==================================================================
    def flash(self, backup_address, backup, segments, geometry, *, snapshot=None,
              web_seconds=None, after_readback=None, reset=True):
        """同扇区计划的 Flash 原生对照：擦除+编程+校验 → 独立回读 → 验证后恢复 → 复位。

        * ``segments`` = 与 Web 同一份 ELF 的加载段 ``[(地址, 字节)]``；擦除计划**按段合并**
          生成，段与段之间的空扇区不擦（与 Web 的稀疏计划一致）；
        * ``geometry`` = 本仓 ``board_acceptance_target.FLASH`` 条目（``base``/``bytes``/``erase``；
          也接受 ``(base, size, erase)``）；可选 ``geometry['ranges']`` 用来逐字核对擦除计划；
        * ``snapshot`` = **同一 ELF 与备份扇区的快照确认**（``image``/``imageSha256``/
          ``segmentsSha256``/``backupSha256``/可选 ``elf=True``）；缺了或对不上就是 ``Pending``
          —— 阶段 F04 的原生路径只认快照，不认「大概同一个镜像」；
        * ``web_seconds``：Web 控件路径的实测总耗时，随证据回给 Runner 做 F04 对照；
        * ``reset=True``（默认）：恢复验证后发 ``reset run``，与 Web 烧录后的复位等价；
          明确不需要复位时置 ``False``，证据里 ``reset.included`` 会如实为 False。

        返回证据（``verified``/``restored`` 同时为真才算通过）：
        ``cliSeconds``（TCL 实测擦除+编程+校验，不含 OpenOCD 启动）、``cliProcessSeconds``
        （含启动的进程墙钟）、``stages``、``bytes``、``sectors``、``ranges``、``readbackSha256``、
        ``expectedSha256``、``backupSha256``、``restore``、``reset``、``mapping``、``identity``。
        """
        began = time.perf_counter()
        backup_address = int(backup_address)
        backup = bytes(backup)
        segments = [(int(address), bytes(payload)) for address, payload in segments]
        erase, base, size = geometry_tuple(geometry)
        end = backup_address + len(backup)
        evidence = {'kind': 'flash', 'backupAddress': backup_address, 'backupBytes': len(backup),
                    'backupSha256': sha(backup),
                    'geometry': {'base': base, 'bytes': size, 'erase': erase},
                    'segments': [[address, len(payload)] for address, payload in segments],
                    **self.tools_evidence()}
        if not segments:
            raise Pending('原生 Flash 对照缺少镜像段')
        checked(backup_address % erase == 0, f'备份起点未按 {erase:#x} 扇区对齐')
        checked(base <= backup_address < end <= base + size, '备份范围超出登记的 Flash 区间')
        checked(len(backup) % erase == 0, '备份长度不是整数个扇区')
        low = min(address for address, _ in segments)
        high = max(address + len(payload) for address, payload in segments)
        checked(low >= backup_address and high <= end, '镜像段超出备份扇区（原生不能写备份之外的范围）')
        ranges = sector_ranges(base, erase, backup_address, end, segments)
        covered = sum(stop - start for start, stop in ranges)
        gap_bytes = (high - low) - sum(len(payload) for _, payload in segments)
        planned = [[start, stop] for start, stop in ranges]
        if 'ranges' in geometry:
            provided = [[int(value) for value in pair] for pair in geometry['ranges']]
            if provided != planned:
                raise Pending(f'与 Web 的擦除计划不一致：给的是 {provided}，本类是 {planned}')
        evidence.update({'ranges': planned, 'bytes': covered,
                         'sectors': sum((stop - start) // erase for start, stop in ranges),
                         'segmentSpanBytes': high - low, 'segmentGapBytes': gap_bytes})

        image = self._snapshot(backup, segments, snapshot, backup_address)
        evidence['image'] = image
        evidence['identity'] = self.preflight()
        evidence['physical'] = self._flash_geometry_check(erase, backup_address, end)

        expected = bytearray(backup)
        for address, payload in segments:
            offset = address - backup_address
            expected[offset:offset + len(payload)] = payload
        expected = bytes(expected)
        evidence['expectedSha256'] = sha(expected)

        stages = {}
        # 与 Web 同起始状态：现场独立回读必须逐字节等于 Web 路径交来的备份快照。
        current = self.runner.read(backup_address, len(backup))
        stages['preReadback'] = _stage(current)
        evidence['preReadbackSha256'] = sha(current['data'])
        same = differ(backup, current['data'])
        if same['differences']:
            raise Pending('现场 Flash 与 Web 快照不一致（可能被别的烧录/复位改过）：'
                          f'{same}；不承认这份备份，也不动手')

        payloads = [(start, expected[start - backup_address:stop - backup_address])
                    for start, stop in ranges]
        failure, written = None, False
        try:
            written = True
            program = self.runner.program(payloads)
            stages['program'] = _stage(program)
            stages['programTclMs'] = {'programMs': program.get('programMs'),
                                      'verifyMs': program.get('verifyMs')}
            evidence['program'] = {'bytes': program.get('bytes'),
                                   'ranges': program.get('ranges'),
                                   'processSeconds': program.get('processSeconds'),
                                   'log': program.get('log')}
            readback = self.runner.read(backup_address, len(backup))
            stages['readback'] = _stage(readback)
            mismatch = differ(expected, readback['data'])
            evidence.update({'readbackSha256': sha(readback['data']), 'imageMismatch': mismatch,
                             'mapping': 'XIP 映射回读' if self.board == 'hpm6800evk'
                                        else 'Flash 地址回读'})
            if mismatch['differences']:
                raise NativeFailure(f'原生烧录后扇区独立回读不一致：{mismatch}', evidence)
            if after_readback is not None:
                cross = differ(backup, bytes(after_readback))
                evidence['crossCheck'] = {'differences': cross['differences'],
                                          'sha256': sha(after_readback)}
                if cross['differences']:
                    raise NativeFailure('原生回读与 Web 侧独立回读不一致', evidence)
        except BaseException as error:  # noqa: BLE001
            # 关键：program 一旦开始就可能写过 Flash。非零退出、超时、transport 报错
            # 都在这里进失败通道，**不再允许**以「安全 Pending」跳过复核与恢复。
            failure = error
        finally:
            evidence['stages'] = stages
            # F04 对照耗时：TCL 实测擦除+编程+校验；拿不到 TCL 计时才退回进程墙钟，
            # 进程墙钟单独列在 cliProcessSeconds，两个口径不混报。
            tcl_ms = stages.get('programTclMs') or {}
            evidence['cliProcessSeconds'] = (stages.get('program') or {}).get('processSeconds')
            evidence['cliSeconds'] = (round((tcl_ms.get('programMs') + tcl_ms.get('verifyMs')) / 1000, 6)
                                      if tcl_ms.get('programMs') is not None
                                      and tcl_ms.get('verifyMs') is not None
                                      else evidence['cliProcessSeconds'])
            if web_seconds is not None:
                evidence['webSeconds'] = float(web_seconds)

        # 无论成功还是异常：只要可能写过，就先只读复核，再按需重写备份并复核。
        started = time.perf_counter()
        restore = {'writtenDuringRun': written, 'differences': None}
        try:
            if written:
                current = self.runner.read(backup_address, len(backup))
                restore['preRestore'] = {'readbackSha256': sha(current['data']),
                                         'differences': differ(backup, current['data'])['differences']}
                if restore['preRestore']['differences']:
                    restore['rewritten'] = True
                    self.runner.program([(start, backup[start - backup_address:stop - backup_address])
                                         for start, stop in ranges])
                    current = self.runner.read(backup_address, len(backup))
                restore['postRestore'] = {'readbackSha256': sha(current['data']),
                                          'differences': differ(backup, current['data'])['differences']}
                stages['restore'] = {'processSeconds': round(time.perf_counter() - started, 6)}
        except Exception as error:  # noqa: BLE001 - 恢复步骤自身失败同样锁存
            restore['error'] = repr(error)
            restore['seconds'] = time.perf_counter() - started
            restore['verified'] = False
            evidence.update({'restore': restore, 'restored': False, 'verified': False,
                             'stateAfter': self._force_running(),
                             'totalSeconds': time.perf_counter() - began})
            self._evidence_file('native-flash.json', evidence)
            raise Unsafe(f'Flash 恢复无法确认：{error!r}；见 {self.folder}') from error
        restore['seconds'] = time.perf_counter() - started
        restore['verified'] = restore['postRestore']['differences'] == 0
        evidence['restore'] = restore
        evidence['restored'] = restore['verified']
        evidence['totalSeconds'] = time.perf_counter() - began
        if not restore['verified']:
            evidence['verified'] = False
            evidence['stateAfter'] = self._force_running()
            self._evidence_file('native-flash.json', evidence)
            raise Unsafe(f'Flash 扇区未能恢复成备份（保留 {self.folder}）；'
                         '不宣称通过，后续硬件操作必须停止')

        # 复位：与 Web 烧录后的复位等价（reset run）；显式关闭或本次对照已失败时如实登记。
        reset_failure = None
        if reset and failure is None:
            started = time.perf_counter()
            try:
                run = self.runner.reset_run()
                stages['reset'] = {'processSeconds': run.get('processSeconds'),
                                   'method': 'reset run'}
                evidence['reset'] = {'included': True,
                                     'method': 'reset run（与 Web 烧录后的复位等价）'}
            except Exception as error:  # noqa: BLE001
                reset_failure = error
                stages['reset'] = {'processSeconds': round(time.perf_counter() - started, 6),
                                   'error': repr(error)}
                evidence['reset'] = {'included': True, 'error': repr(error)}
        elif reset:
            # 对照本身失败：不引入复位副作用，只把目标放回运行态。
            stages['reset'] = {'processSeconds': None, 'method': 'skipped-after-failure'}
            evidence['reset'] = {'included': False,
                                 'method': '本次对照失败，未发复位（只放回运行态）'}
        else:
            # 明确不复位时也要把目标放回运行态，不能留一个 halted 给下一位。
            stages['reset'] = {'processSeconds': None, 'method': 'none'}
            evidence['reset'] = {'included': False,
                                 'method': '本原生阶段不含复位（由 Runner 显式声明）'}

        # 无论复位成功、失败还是被显式关闭，最终状态都必须是「运行中」才配得上 verified。
        state = self._force_running()
        evidence['stateAfter'] = state
        running = state.get('state') == 'running'
        if reset_failure is not None:
            evidence['reset']['resume'] = state
            if not running:
                evidence.update({'verified': False, 'stages': stages,
                                 'totalSeconds': time.perf_counter() - began})
                self._evidence_file('native-flash.json', evidence)
                raise Unsafe(f'原生复位失败且目标未确认回到运行态（保留 {self.folder}）：'
                             f'{reset_failure!r}') from reset_failure
            evidence['verified'] = False
            evidence['stages'] = stages
            evidence['totalSeconds'] = time.perf_counter() - began
            self._evidence_file('native-flash.json', evidence)
            raise NativeFailure(f'原生复位未完成（目标已恢复运行，verified=False）：'
                                f'{reset_failure!r}', evidence) from reset_failure
        if not running:
            evidence.update({'verified': False, 'stages': stages,
                             'totalSeconds': time.perf_counter() - began})
            self._evidence_file('native-flash.json', evidence)
            raise Unsafe(f'Flash 对照后目标未确认运行（保留 {self.folder}）：{state}')

        evidence['verified'] = failure is None
        evidence['stages'] = stages
        evidence['totalSeconds'] = time.perf_counter() - began
        self._evidence_file('native-flash.json', evidence)
        if failure is not None:
            if isinstance(failure, Unsafe):
                raise failure
            if isinstance(failure, NativeFailure):
                raise failure
            if isinstance(failure, Pending):
                # 进程根本没起来（OSError）：确定没写过，上面的复核/恢复是空操作。
                raise failure
            raise NativeFailure(f'原生 Flash 对照失败（扇区已复核恢复到备份）：'
                                f'{failure!r}', evidence) from failure
        if not evidence['verified']:
            raise Unsafe('原生 Flash 对照结论未确认运行态')
        return evidence

    # -- flash 辅助 -------------------------------------------------------
    def _snapshot(self, backup, segments, snapshot, backup_address):
        """确认「同一 ELF 内容 + 同一备份扇区」快照；缺项/不符都是 Pending。"""
        if not snapshot:
            raise Pending('阶段 F04 的原生路径必须先确认快照：同一 ELF（imageSha256/segmentsSha256）'
                          '与备份扇区（backupSha256）；未确认不做原生烧录对照')
        snapshot = dict(snapshot)
        if snapshot.get('backupSha256') is None:
            raise Pending('快照缺少备份扇区指纹（backupSha256）')
        if sha(backup) != str(snapshot['backupSha256']).lower():
            raise Pending('备份扇区与快照指纹不一致：这份 backup 不是 Web 路径核对过的那份')
        if snapshot.get('backupAddress') is not None and int(snapshot['backupAddress']) != backup_address:
            raise Pending('备份地址与快照不一致')
        fingerprint = segments_sha(segments)
        recorded = snapshot.get('segmentsSha256')
        if recorded is not None and str(recorded).lower() != fingerprint:
            raise Pending('镜像段表与快照指纹不一致：不是 Web 路径用的那份 ELF')
        image, elf_checked = snapshot.get('image'), False
        if image is not None:
            path = Path(image)
            if not path.is_file():
                raise Pending(f'快照里的 ELF 不存在：{path}')
            if snapshot.get('imageSha256') is not None and \
                    sha_file(path) != str(snapshot['imageSha256']).lower():
                raise Pending('ELF 文件与快照指纹不一致')
            if snapshot.get('elf'):
                machine, parsed = facts.elf_contents(path)
                if machine != ARCH_MACHINE[self.profile['arch']]:
                    raise Pending(f'快照 ELF 架构与 {self.board} 不符：{machine}')
                parsed = [(int(address), bytes(payload)) for address, payload, _ in parsed]
                if parsed != segments:
                    raise Pending('快照 ELF 的加载段与本类收到的 segments 不一致')
                elf_checked = True
        if image is None and snapshot.get('imageSha256') is None and recorded is None:
            raise Pending('快照没有 ELF 侧指纹（imageSha256 或 segmentsSha256）')
        return {'path': str(image) if image is not None else None,
                'sha256': snapshot.get('imageSha256'), 'segmentsSha256': fingerprint,
                'elfCrossChecked': elf_checked}

    def _flash_geometry_check(self, erase, backup_address, end):
        """物理扇区表必须覆盖备份范围且与登记擦除单元一致；不符即 Pending。

        扇区表来自真实 ``flash info <bank> sectors``（H743 双 bank 各自回读），所以这里
        同时还核对「备份范围落在某个已探测 bank 内」——跨 bank 或跑到未探测 bank 都会
        Pending，而不是拿另一个 bank 的扇区蒙混。
        """
        table = self.runner.geometry()
        sectors = table.get('sectors') or []
        if not sectors:
            raise Pending('原生 OpenOCD 没有回读 Flash 扇区表，不能确认擦除计划')
        have = {int(item['address']): int(item['size']) for item in sectors}
        for address in range(backup_address, end, erase):
            if address not in have:
                raise Pending(f'物理 Flash 扇区表缺少 {address:#x}：与备份几何不符')
            if have[address] != erase:
                raise Pending(f'{address:#x} 的物理扇区 {have[address]:#x} != 登记的 {erase:#x}')
        banks = table.get('banks') or []
        covering = [bank for bank in banks
                    if int(bank['address']) <= backup_address
                    and end <= int(bank['address']) + int(bank['size'])]
        if not covering:
            raise Pending(f'备份范围 0x{backup_address:x}..0x{end:x} 没有落在任何已探测的 Flash bank 内：'
                          f'{[hex(int(bank["address"])) for bank in banks]}')
        return {'reported': [[address, size] for address, size in sorted(have.items())],
                'banks': [[hex(int(bank['address'])), int(bank['size'])] for bank in banks]}


def _stage(result):
    """把 transport 返回的计时统一成 (进程墙钟, TCL 实测) 双口径。"""
    if isinstance(result, dict):
        return {'processSeconds': result.get('processSeconds'),
                'tclMs': result.get('tclMs')}
    return {'processSeconds': None, 'tclMs': None}


def _rate(length, stage):
    """块速率：优先用 TCL 实测时间；没有 TCL 计时才用进程墙钟（并在证据里注明口径）。"""
    if stage.get('tclMs'):
        return length / (stage['tclMs'] / 1000)
    if stage.get('processSeconds'):
        return length / stage['processSeconds']
    return 0.0
