"""Board facts, fixture symbols, offline gates and target access for board acceptance.

移植自 ZynqLinkPRO 的 ``tools/board_acceptance_target.py``（54150b31 引入，
f4b3a552 / 1642ac91 补充性能与时钟证据），按本仓三个目标板重新定标：

* F103ZE / H743：ARM CMSIS-DAP，SWD 与 ARM JTAG 两条链路；
* HPM6800EVK：RISC-V，仅 JTAG。

与上游的差异（有意为之，不是遗漏）：

* 不引入 Zynq PL/PS/OCM/QSPI 协议（上游的 ``J7_PL_*``、``JTAG_TIMING``、
  ``0x4001004c`` 一类寄存器与 QSPI bank 全部丢弃）；
* 夹具一律取自本仓唯一清单 ``web/tools/target-firmware/board-matrix.json``；
* 目标访问复用两条本仓已有通路：浏览器 Web 队列（``toolbox.py`` 的 ``Client``）
  与 ``script_test/`` 的原生 HID/EP83 采样器，不新写 USB 栈；
* 时钟证据来自夹具自报符号或 RCC 回读，两者分别登记，不互相冒充。

本模块只做事实登记与判定辅助；是否接触硬件由 ``plan``/``execute`` 门控决定。
"""

from __future__ import annotations

import hashlib
import json
import struct
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / 'web/tools/target-firmware/board-matrix.json'


# --------------------------------------------------------------------------
# 结果语义（与上游一致）：Pending = 条件不具备；Unsafe = 现场无法确认
# --------------------------------------------------------------------------


class Pending(RuntimeError):
    """必要条件（夹具、CFG、时钟、接线）无法建立，本实例只能记「阻塞」。"""


class Unsafe(RuntimeError):
    """当前操作或恢复无法确认；调用方必须停止后续硬件操作。"""


def checked(test, message):
    if not test:
        raise RuntimeError(message)


def wait_for(get, predicate, timeout, message):
    deadline = time.monotonic() + timeout
    while True:
        value = get()
        if predicate(value):
            return value
        if time.monotonic() >= deadline:
            raise RuntimeError(f'{message}: {value!s:.600}')
        time.sleep(.05)


def sha(data):
    return hashlib.sha256(bytes(data)).hexdigest()


def digest(path):
    """与上游同形：返回 {path, sha256}，供证据比对，不是裸 hex。"""
    with Path(path).open('rb') as stream:
        return {'path': str(path),
                'sha256': hashlib.file_digest(stream, 'sha256').hexdigest()}


def b64(data):
    import base64
    return {'$base64': base64.b64encode(bytes(data)).decode('ascii')}


# --------------------------------------------------------------------------
# 板卡事实
#
# 频点档：docs/板卡验收清单.md「链路怎么选」与本仓 validation 报告。
# 夹具：web/tools/target-firmware/board-matrix.json（唯一清单，CONTRACT.md）。
# 时钟：夹具自报符号（H743/HPM）或 RCC 回读（F103ZE），见 verify_* 注释。
# --------------------------------------------------------------------------

FLASH = {
    # STM32F103ZE 属大容量产品：页 2 KiB（不是中容量的 1 KiB）。
    'f103ze': {'base': 0x08000000, 'bytes': 0x00080000, 'erase': 0x800, 'bank': 'stm32f1'},
    'h743': {'base': 0x08000000, 'bytes': 0x00200000, 'erase': 0x20000, 'bank': 'stm32h7'},
    # HPM 的 XPI0 bank 由官方 SDK cfg 固定：base 0x80000000 / size 0x2000000 / 4 KiB 扇区。
    'hpm6800evk': {'base': 0x80000000, 'bytes': 0x02000000, 'erase': 0x1000, 'bank': 'hpm_xpi'},
}

SRAM_REGION = {
    'f103ze': (0x20000000, 0x20010000),
    'h743': (0x24000000, 0x24080000),
    'hpm6800evk': (0x01200000, 0x01280000),
}

# RISC-V / ARM 调试寄存器
DHCSR = 0xE000EDF0
DEMCR = 0xE000EDFC
FPB = 0xE0002000
S_RESET_ST = 1 << 25
S_RETIRE_ST = 1 << 24
DWT_CTRL = 0xE0001000
ARM_REGISTERS = tuple(range(19)) + (20,)
RV_DCSR = 0x7B0
RV_DCSR_STEP = 1 << 2
RV_REGISTERS = (RV_DCSR, 0x7B1, 0x300, 0x305, 0x340, 0x341, 0x342, 0x343,
                *range(0x1001, 0x1020))

# HPM6800EVK 地址属性由 board_init_pmp() 固定：SBA 读绕过 D-cache，
# 验收窗口必须落在非缓存段，否则读回可能是旧值（script_test/hpm6800evk_scope/README.md）。
HPM_CACHEABLE = (0x01200000, 0x01240000)
HPM_NONCACHEABLE = (0x01240000, 0x01280000)

BOARDS = {
    'f103ze': {
        'label': 'STM32F103ZE',
        'chip': 'stm32f103',
        'chipOption': 'stm32f1x',
        'core': 'cortex-m3',
        'arch': 'arm',
        'links': {
            'swd': {'requestHz': 60_000_000, 'verifiedHz': [20_000_000, 45_000_000, 60_000_000],
                    'primary': 60_000_000, 'note': '主测档 60 MHz；20/45 MHz 见条件矩阵'},
            # 工具接口沿用产品的 arm-jtag 名称；jtag 只作为命令别名。
            # 本机尚未登记 F103ZE arm-jtag 的已验证档，因此请求档留空，
            # 由页面选择档位、实际值进证据后判定（不猜一个"看起来合理"的频率）。
            'arm-jtag': {'requestHz': None, 'verifiedHz': [], 'primary': None,
                         'attach': True, 'highSpeedBackend': True,
                         'note': '产品按与 SWD 相同的范围支持烧录、调试、Viewer、转发与 '
                                 'J-Scope；请求档未登记，实际时钟以本轮页面回读为准'},
        },
        # 测试窗口：scope 夹具占用 0x20000000..0x20002060 与 g_hole(4096)；
        # 0x20003000 起 20 KiB 空闲。写前整段备份。
        'sram': {'base': 0x20003000, 'bytes': 20 * 1024, 'sentinel': 64,
                 'tarBoundary': 0x400, 'fixture': 'scope'},
        'clock': {'kind': 'stm32f1', 'rcc': 0x40021000, 'cfgr': 0x40021004,
                  'cfgr2': 0x4002102C, 'hseHz': 8_000_000,
                  'expect': {'scope': (72_000_000, None), 'rtt': (72_000_000, None),
                             'debug': (72_000_000, None)}},
        'fixtures': {
            'rtt': {'example': 'rtt',
                    'symbols': {'rtt': '_SEGGER_RTT', 'up': '_acUpBuffer',
                                'down': '_acDownBuffer', 'seq': 'g_bytes',
                                'loops': 'g_loops', 'ms': 'g_ms',
                                'write': 'SEGGER_RTT_Write', 'main': 'main'}},
            'rtt-seq': {'example': 'rtt-seq', 'symbols': {'rtt': '_SEGGER_RTT'}},
            'scope': {'example': 'scope',
                      'symbols': {'block': 'g_lfsr', 'tick': 'g_tick', 'hole': 'g_hole',
                                  'pulse': 'g_pulse', 'pack': 'g_pack', 'ramp': 'g_ramp64',
                                  'square': 'g_sq5k', 'pairA': 'g_pair_a',
                                  'pairB': 'g_pair_b', 'far': 'g_far_cnt'}},
            'debug': {'example': 'dbgstress',
                      'symbols': {'tick': 'g_ticks', 'entry': 'main', 'stage': 'g_stage',
                                  'checksum': 'g_checksum'}},
            'flash': {'example': 'rtt'},
        },
    },
    'h743': {
        'label': 'STM32H743',
        'chip': 'stm32h7',
        'chipOption': 'stm32h7x',
        'core': 'cortex-m7',
        'arch': 'arm',
        'links': {
            'swd': {'requestHz': 60_000_000, 'verifiedHz': [60_000_000], 'primary': 60_000_000,
                    'note': '主测档 60 MHz'},
            'arm-jtag': {'requestHz': 10_000_000, 'verifiedHz': [10_000_000],
                         'primary': 10_000_000, 'attach': True, 'highSpeedBackend': True,
                         'note': '双 TAP 高速后端；主测档 10 MHz 请求档'},
        },        # scope / dbgstress 夹具只占用 0x24000000..0x24002058；0x24020000 起空闲。
        'sram': {'base': 0x24020000, 'bytes': 20 * 1024, 'sentinel': 64,
                 'tarBoundary': 0x400, 'fixture': 'scope'},
        'clock': {'kind': 'stm32h7', 'rcc': 0x58024400, 'hseHz': 25_000_000,
                  'expect': {'rtt': (480_000_000, 240_000_000),
                             'scope': (480_000_000, 240_000_000),
                             'debug': (480_000_000, 240_000_000)}},
        'fixtures': {
            'rtt': {'example': 'rtt',
                    'symbols': {'rtt': '_SEGGER_RTT', 'up': '_acUpBuffer',
                                'down': '_acDownBuffer', 'seq': 'g_bytes',
                                'loops': 'g_loops', 'ms': 'g_ms',
                                'sysclkMHz': 'g_sysclk_mhz', 'clkErr': 'g_clk_err',
                                'write': 'SEGGER_RTT_Write', 'main': 'main'}},
            'rtt-seq': {'example': 'rtt-seq', 'symbols': {'rtt': '_SEGGER_RTT'}},
            'scope': {'example': 'scope',
                      'symbols': {'block': 'g_lfsr', 'tick': 'g_tick', 'hole': 'g_hole',
                                  'pulse': 'g_pulse', 'pack': 'g_pack', 'ramp': 'g_ramp64',
                                  'square': 'g_sq5k', 'pairA': 'g_pair_a',
                                  'pairB': 'g_pair_b', 'far': 'g_far_cnt',
                                  'sysclkHz': 'g_z_sysclk_hz', 'hclkHz': 'g_z_hclk_hz',
                                  'clkSrc': 'g_z_clk_src', 'clkErr': 'g_z_clk_err',
                                  'dcache': 'g_z_dcache'}},
            'debug': {'example': 'dbgstress',
                      'symbols': {'tick': 'g_ticks', 'entry': 'main', 'stage': 'g_stage',
                                  'checksum': 'g_checksum'}},
            'flash': {'example': 'dbgstress'},
        },
    },
    'hpm6800evk': {
        'label': 'HPM6800EVK (HPM6880)',
        'matrix': '6800evk',
        'chip': 'hpm6800evk',
        'chipOption': 'hpm6800evk',
        'core': 'riscv32',
        'arch': 'riscv',
        'links': {
            'jtag': {'requestHz': 8_000_000, 'verifiedHz': [8_000_000], 'primary': 8_000_000,
                     'attach': True, 'highSpeedBackend': True,
                     'note': 'Wasm 建链 8 MHz 请求档；探针内 DMI/SBA 用原固定时序'},
        },
        # SBA 绕过 D-cache：验收窗口落在 0x01240000 起的非缓存段。
        'sram': {'base': 0x01250000, 'bytes': 20 * 1024, 'sentinel': 64,
                 'tarBoundary': None, 'fixture': 'scope'},
        'clock': {'kind': 'fixture', 'mchtmrHz': 24_000_000,
                  'expect': {'scope': (None, None), 'rtt': (None, None), 'debug': (None, None)}},
        'fixtures': {
            'scope': {'example': 'scope',
                      'symbols': {'block': 'g_v', 'tick': 'g_updates',
                                  'magic': 'g_block_magic', 'mchtmrHz': 'g_mchtmr_hz',
                                  'trapCount': 'g_trap_count', 'trapCause': 'g_trap_cause',
                                  'cachedV': 'g_v_cached', 'blockAddr': 'g_block_addr'}},
            'rtt': {'example': 'rtt',
                    'symbols': {'rtt': '_SEGGER_RTT', 'up': '_acUpBuffer',
                                'down': '_acDownBuffer', 'seq': 'g_bytes',
                                'loops': 'g_loops', 'ms': 'g_ms',
                                'hclkMHz': 'g_hclk_mhz', 'cbAddr': 'g_cb_addr'}},
            'rtt-seq': {'example': 'rtt-seq', 'symbols': {'rtt': '_SEGGER_RTT'}},
            'debug': {'example': 'dbgstress',
                      'symbols': {'tick': 'g_ticks', 'entry': 'main', 'stage': 'g_stage',
                                  'checksum': 'g_checksum', 'mchtmrHz': 'g_mchtmr_hz'}},
            'flash': {'example': 'rtt'},
        },
    },
}

# 七组各五项：与 docs/板卡验收清单.md「七组各五项」逐格对应。
GROUPS = {
    'S': ('SRAM', ('SRAM 图案写入并独立读回',
                   '块读/写测速（同地址、大小、运行状态对照 Web 与 CLI）',
                   '跨 1 KiB TAR 边界的连续访问（HPM 为 SBA 连续读写窗口）',
                   '恢复原值、前后哨兵和运行状态',
                   '1/2/4 B 及受支持的非对齐访问检查相邻字节')),
    'F': ('Flash', ('真实 Web 按钮烧录配套镜像并核对目标与写入范围',
                    '独立回读有效镜像与受影响扇区未指定字节（HPM 读 XIP 映射）',
                    '复位后确认业务心跳或串口标记',
                    '烧录测速（同 ELF、同范围对照 Web 与 CLI，可观测阶段单列）',
                    '恢复原 Flash、独立回读并确认原程序运行')),
    'D': ('调试', ('顶栏选探针、官方/内置 CFG 建链并核对芯片、DPIDR/TAP 与 ELF',
                   '暂停、继续与指令单步，核对 PC 与状态',
                   '源码进入、跳过、跳出及回栈，核对停点',
                   '设置/命中/删除断点，核对后端登记与 FPB（ARM）或触发器（RISC-V）',
                   '交互测速：建链、暂停、单步与停点后变量刷新耗时')),
    'V': ('RTT Viewer', ('原始上行序号连续', '二进制下行并独立读回内容与下行 WrOff',
                         'Viewer 吞吐，记录轮询、环大小与实际渲染状态',
                         '上下行各跨一次环尾，核对数据与游标',
                         '用匹配 ELF 的 _SEGGER_RTT 定位并实收')),
    'R': ('RTT 转发', ('按序列号匹配探针与 COM 并收到目标序号流',
                       '转发吞吐（同环、同批次对照 Web Serial 与原生 CLI）',
                       '带载 STOP 排空：原始实收、moved、目标 RdOff、环剩余与 CDC 回 UART',
                       '同页 STOP 后立即再启动，核对按钮与旧数据残留',
                       '511/512/513 B 非重复载荷逐字节对账')),
    'J': ('J-Scope', ('配套 ELF 选运行态计数器，核对 DEF、地址与波形',
                      '解析原始 JSP，STOP 后逐帧对账包序、载荷与终态计数',
                      '单路摸高（固定链路档，只调采样周期）',
                      '八路摸高（固定链路档，报组/s）',
                      'JSP 与 CSV 逐值对账：字段、时间戳与数量')),
    'E': ('ETM', ('Web 普通联动：从选择函数到打开新采记录',
                  '核对实际硬件窗口、丢帧、解码质量与未知执行区间',
                  'ETM 性能：固定 1 秒，分计采集、LA 回传/保存、解码/索引、首屏与总时间',
                  '用已知函数、匹配源码和窗口内事件验证执行顺序',
                  '核对目标与调试配置恢复、Kingst 与采集租约空闲、原页面能接回')),
}

INSTANCE_IDS = tuple(f'{group}0{index}' for group in GROUPS for index in range(1, 6))

# 结果状态。``ack``/``plan`` 明确不是结论：计划与任务投递成功都不等于验收通过。
STATUSES = ('pass', 'fail', 'blocked', 'pending', 'manual', 'unsupported', 'not-applicable')
CONCLUSIONS = ('pass', 'fail', 'blocked', 'unsupported', 'not-applicable')


# --------------------------------------------------------------------------
# 性能基线：只登记本仓已批准的数字；没有批准门槛就标「待定标」，
# 不拿其他探针或其他板卡的实测速度当本机门槛（上游 f4b3a552 的教训）。
#
# F103ZE 的 45/60 MHz 转发下限来自本仓 web/tools/selftest/hw-campaign.mjs 的口径
# （60 MHz 判 >2.5 MB/s，45 MHz 判 >2.0 MB/s）；其它组合尚无批准下限。
# --------------------------------------------------------------------------

RATE_BASELINES = {
    ('f103ze', 'swd', 'rtt-forward-fixed-text'): {45_000_000: 2.0, 60_000_000: 2.5},
    ('f103ze', 'swd', 'rtt-viewer-fixed-text'): None,
    ('f103ze', 'arm-jtag', 'rtt-forward-fixed-text'): None,
    ('h743', 'swd', 'rtt-forward-fixed-text'): None,
    ('h743', 'arm-jtag', 'rtt-forward-fixed-text'): None,
    ('hpm6800evk', 'jtag', 'rtt-forward-fixed-text'): None,
    # 块读写（S02）与烧录（F04）各自的条件门槛；未批准前只能是 None。
    ('f103ze', 'swd', 'sram-block'): None,
    ('f103ze', 'arm-jtag', 'sram-block'): None,
    ('h743', 'swd', 'sram-block'): None,
    ('h743', 'arm-jtag', 'sram-block'): None,
    ('hpm6800evk', 'jtag', 'sram-block'): None,
    ('f103ze', 'swd', 'flash-program'): None,
    ('f103ze', 'arm-jtag', 'flash-program'): None,
    ('h743', 'swd', 'flash-program'): None,
    ('h743', 'arm-jtag', 'flash-program'): None,
    ('hpm6800evk', 'jtag', 'flash-program'): None,
}

# 命令别名：产品工具接口用 arm-jtag；jtag 是同一个物理链路的简写。
LINK_ALIASES = {'jtag': 'arm-jtag'}


def normalize_link(board, link):
    """把命令行的链路写法归一成板卡登记的名字；不支持的组合直接报错。"""
    if link is None:
        return None
    canonical = LINK_ALIASES.get(link, link)
    links = BOARDS[board]['links']
    if canonical in links:
        return canonical
    if link in links:
        return link
    raise Pending(f'{BOARDS[board]["label"]} 不支持链路 {link}'
                  f'（可用：{"、".join(links)}）')

SRAM_PIPELINE = {'maxWords': 256, 'maxDepth': 4, 'windowBytes': 4096,
                 'loops': 64, 'repeats': 3, 'soakSeconds': 10, 'fixture': 'h743'}
SCOPE_TICK_HZ = 24_000_000
SCOPE_WINDOW_SECONDS = 60
SCOPE_SHORT_WINDOW_SECONDS = 8
SCOPE_SKIP_CEILING = 0.001        # 跳拍率 < 0.1%
SCOPE_RATE_FLOOR = 0.99           # 短窗下限 = 理论组率的 99%（本仓 review 稿口径）
RTT_USB_PACKET = 512              # EP 整包边界 M；R05 用 M-1/M/M+1


# --------------------------------------------------------------------------
# 夹具解析：唯一清单 web/tools/target-firmware/board-matrix.json
# --------------------------------------------------------------------------


def _matrix():
    if not MATRIX.is_file():
        raise Pending(f'缺少夹具清单：{MATRIX.relative_to(ROOT).as_posix()}')
    return json.loads(MATRIX.read_text(encoding='utf-8'))


def _matrix_example(board, example):
    data = _matrix()
    # 夹具清单的板卡 id 与验收板卡 id 不总是同名（清单用 6800evk）。
    key = BOARDS[board].get('matrix', board)
    entry = data.get('boards', {}).get(key)
    if not entry:
        raise Pending(f'夹具清单没有板卡 {key}（验收板卡 {board}）')
    item = (entry.get('examples') or {}).get(example)
    if not item:
        raise Pending(f'{key} 的夹具清单没有例程 {example}')
    return item


def fixture_path(board, example):
    """按清单取夹具：优先 buildArtifact，回退 publishedArtifact。

    清单路径相对 ``web/``（见 board-matrix.mjs 的 ROOT）。返回 (path, 来源)。
    """
    item = _matrix_example(board, example)
    base = ROOT / 'web'
    for key in ('buildArtifact', 'publishedArtifact'):
        value = item.get(key)
        if not value:
            continue
        path = base / value
        if path.is_file():
            return path, key
    raise Pending(f'{board}/{example} 的夹具缺失：清单里 buildArtifact 与 '
                  f'publishedArtifact 都找不到文件（{item.get("buildArtifact")} / '
                  f'{item.get("publishedArtifact")}）')


def elf_symbols(path):
    """返回 {符号名: 地址}。不做符号猜测：缺符号由调用方报 Pending。"""
    from elftools.elf.elffile import ELFFile
    with Path(path).open('rb') as stream:
        elf = ELFFile(stream)
        table = elf.get_section_by_name('.symtab')
        if table is None:
            raise Pending(f'夹具 ELF 没有符号表：{path}')
        return {symbol.name: int(symbol['st_value']) for symbol in table.iter_symbols()
                if symbol.name and symbol['st_info']['type'] != 'STT_SECTION'}


def elf_contents(path):
    """返回 (e_machine, [(paddr, bytes, flags)])；只取有文件内容的 PT_LOAD。"""
    from elftools.elf.elffile import ELFFile
    with Path(path).open('rb') as stream:
        elf = ELFFile(stream)
        checked(elf.elfclass == 32 and elf.little_endian, f'需要小端 ELF32 夹具：{path}')
        segments = [(int(seg['p_paddr']), seg.data(), int(seg['p_flags']))
                    for seg in elf.iter_segments()
                    if seg['p_type'] == 'PT_LOAD' and seg['p_filesz']]
        return elf['e_machine'], segments


def fixture(board, kind):
    """解析某板某角色的夹具：返回 {path, source, sha256, symbols, segments}。"""
    profile = BOARDS[board]['fixtures'].get(kind)
    if profile is None:
        raise Pending(f'{board} 没有登记 {kind} 夹具')
    path, source = fixture_path(board, profile['example'])
    wanted = profile.get('symbols') or {}
    table = elf_symbols(path)
    symbols = {}
    for role, name in wanted.items():
        if name not in table:
            raise Pending(f'夹具 {path.relative_to(ROOT).as_posix()} 缺少符号 {name}'
                          f'（角色 {role}）；先重建该例程再验收')
        symbols[role] = table[name]
    evidence = {'path': path.relative_to(ROOT).as_posix(), 'source': source,
                'sha256': digest(path)['sha256'], 'symbols': symbols}
    return evidence


# --------------------------------------------------------------------------
# 时钟判定
# --------------------------------------------------------------------------

_STM32_BUS_DIV = (1, 1, 1, 1, 1, 1, 1, 1, 2, 4, 8, 16, 64, 128, 256, 512)
_F1_PCLK_DIV = (1, 1, 1, 1, 2, 4, 8, 16)


def decode_stm32f1_clock(registers, hse_hz=8_000_000):
    """按 RCC_CFGR 的 **SWS**（实际档，不是 SW 请求档）还原 SYSCLK/HCLK/PCLK。

    F103ZE 属大容量产品，SSM 不切 PREDIV1（CFGR2），因此这里只用 CFGR：
    PLLSRC/PLLXTPRE/PLLMUL 与 HPRE/PPRE1/PPRE2 都是可核对的整数分频。
    """
    cfgr = registers['CFGR']
    sws = (cfgr >> 2) & 3
    if sws == 0:
        sysclk, basis = 8_000_000, 'HSI'
    elif sws == 1:
        sysclk, basis = hse_hz // (2 if cfgr >> 17 & 1 else 1), 'HSE'
    elif sws == 2:
        pll_in = (hse_hz // (2 if cfgr >> 17 & 1 else 1)) if cfgr >> 16 & 1 else 8_000_000
        multiplier = ((cfgr >> 18) & 15) + 2
        sysclk, basis = pll_in * multiplier, f'PLL({multiplier}x)'
    else:
        raise Pending('RCC_CFGR.SWS 为保留值，无法判定实际 SYSCLK')
    hclk = sysclk // _STM32_BUS_DIV[(cfgr >> 4) & 15]
    pclk1 = hclk // _F1_PCLK_DIV[(cfgr >> 8) & 7]
    pclk2 = hclk // _F1_PCLK_DIV[(cfgr >> 11) & 7]
    return {'sysclkHz': sysclk, 'hclkHz': hclk, 'pclk1Hz': pclk1, 'pclk2Hz': pclk2,
            'sws': sws, 'basis': f'STM32F1 RCC_CFGR.SWS（{basis}）', 'raw': {'CFGR': cfgr}}


def decode_stm32h7_clock(registers, hse_hz=25_000_000):
    """H743 时钟判定：25 MHz HSE + 整数 PLL1P，SWS 取自 RCC_CFGR[5:3]。

    与上游 1642ac91 同口径：要求 HSERDY|PLL1RDY、SWS=PLL1、整数 PLL1（FRACEN 清零），
    再校验 25 MHz×N 同时整除 CPU 与 HCLK 分频，得到 480/240 MHz。
    """
    cr = registers['CR']
    cfgr = registers['CFGR']
    d1cfgr = registers['D1CFGR']
    pllckselr = registers.get('PLLCKSELR', 0)
    pllcfgr = registers.get('PLLCFGR', 0)
    pll1divr = registers.get('PLL1DIVR', 0)
    ready = (1 << 17) | (1 << 25)                  # HSERDY | PLL1RDY
    if cr & ready != ready:
        raise Pending('H743 需要已就绪的 HSE 与 PLL1（HSERDY/PLL1RDY 未置位）')
    if (cfgr >> 3) & 7 != 3:
        raise Pending('H743 实际 SYSCLK 不是 PLL1（RCC_CFGR.SWS≠3）')
    if pllckselr & 3 != 2:
        raise Pending('H743 PLL1 时钟源不是 HSE（PLLCKSELR.PLLSRC≠2）')
    if not pllcfgr & (1 << 16) or pllcfgr & 1:
        raise Pending('H743 需要整数 PLL1（FRACEN 清零、DIVP1EN 置位）')
    m = pllckselr >> 4 & 63
    if not m:
        raise Pending('H743 PLL1 DIVM 为 0，无法判定')
    n, p = (pll1divr & 0x1FF) + 1, ((pll1divr >> 9) & 127) + 1
    cpu_divisor = m * p * _STM32_BUS_DIV[d1cfgr >> 8 & 15]
    hclk_divisor = cpu_divisor * _STM32_BUS_DIV[d1cfgr & 15]
    source_hz = hse_hz * n
    if source_hz % cpu_divisor or source_hz % hclk_divisor:
        raise Pending('H743 PLL 分频不产生整数时钟')
    return {'sysclkHz': source_hz // cpu_divisor, 'hclkHz': source_hz // hclk_divisor,
            'basis': 'H743 25 MHz HSE 与 RCC PLL1P/D1 回读',
            'raw': {'CR': cr, 'CFGR': cfgr, 'D1CFGR': d1cfgr, 'PLLCKSELR': pllckselr,
                    'PLLCFGR': pllcfgr, 'PLL1DIVR': pll1divr}}


def verify_clock(actual, requested, tolerance=0.001):
    """生效链路时钟与请求档核对；只有请求档时不能判定。"""
    if actual is None:
        raise Pending('缺少生效链路时钟，只有请求档，不能判定')
    delta = abs(actual - requested)
    checked(delta <= max(10, requested * tolerance),
            f'生效链路 {actual:.0f} Hz 与请求档 {requested} Hz 不符')
    return {'requestedHz': requested, 'actualHz': actual, 'deltaHz': actual - requested}


def verify_target_clock(measured, expected_sysclk, expected_hclk=None):
    """目标时钟核对：H743 用 CPU/HCLK 双门槛；F103ZE 只有 SYSCLK 门槛。"""
    if expected_sysclk is None:
        return {'status': 'pending',
                'reason': '本板本夹具没有登记的期望时钟，只有实测值',
                'measured': measured}
    if measured.get('sysclkHz') != expected_sysclk:
        raise Pending(f'目标 SYSCLK {measured.get("sysclkHz")} Hz，期望 {expected_sysclk} Hz')
    if expected_hclk is not None and measured.get('hclkHz') != expected_hclk:
        raise Pending(f'目标 HCLK {measured.get("hclkHz")} Hz，期望 {expected_hclk} Hz')
    return {'status': 'pass', 'measured': measured}


# --------------------------------------------------------------------------
# 性能判定辅助（纯函数，离线可回归）
# --------------------------------------------------------------------------


def scope_theory_hz(period_ticks, tick_hz=SCOPE_TICK_HZ):
    """按实际 tick 算理论组率；用于短窗下限与摸高比较。"""
    checked(period_ticks > 0, '采样周期 tick 必须为正')
    return tick_hz / period_ticks


def scope_rate_floor(period_ticks, tick_hz=SCOPE_TICK_HZ):
    return scope_theory_hz(period_ticks, tick_hz) * SCOPE_RATE_FLOOR


def scope_window(group_rate, produced, skipped, usb_drops, period_ticks,
                 tick_hz=SCOPE_TICK_HZ):
    """J-Scope 窗口判定：跳拍率 <0.1%、USB 丢样为 0、组率不低于该档下限。"""
    total = produced + skipped
    fraction = skipped / total if total else float('inf')
    floor = scope_rate_floor(period_ticks, tick_hz)
    return {'groupRate': group_rate, 'theoryHz': scope_theory_hz(period_ticks, tick_hz),
            'floorHz': floor, 'produced': produced, 'skipped': skipped,
            'usbDrops': usb_drops, 'skipFraction': fraction,
            'skipOk': fraction < SCOPE_SKIP_CEILING, 'usbOk': usb_drops == 0,
            'rateOk': group_rate >= floor}


def native_scope_rate_matches(row, margin=0.0011):
    """原生定速窗自洽：实得组率与标称组率的相对差不超过容差。"""
    nominal = row.get('nominalGroupsPerSecond')
    measured = row.get('groupsPerSecond')
    if not nominal or measured is None:
        return False
    return abs(measured - nominal) <= nominal * margin


def rate_verdict(baselines, clock_hz, measured, label='吞吐'):
    """按本仓批准下限判定；本档没有批准门槛时返回「待定标」。"""
    if not baselines:
        return {'status': 'pending', 'reason': f'{label}：本板本链路没有已批准的下限'}
    floor = baselines.get(clock_hz)
    if floor is None:
        return {'status': 'pending',
                'reason': f'{label}：{clock_hz} Hz 档没有批准门槛，'
                          f'不能借用其他档位或其他探针的速度'}
    return {'status': 'pass' if measured >= floor else 'fail', 'floor': floor,
            'measured': measured, 'ratio': measured / floor if floor else None}


def measurement(bytes_moved, seconds):
    if seconds <= 0:
        raise ValueError('计时必须为正')
    return {'bytes': bytes_moved, 'seconds': seconds,
            'mebibytesPerSecond': bytes_moved / seconds / (1 << 20)}


def median(values):
    ordered = sorted(values)
    if not ordered:
        raise ValueError('没有样本')
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def pattern(length, seed=0x5A5AA5A5):
    """地址相关的非重复图案：避免「写相同字节」造成的假通过。"""
    out = bytearray()
    state = seed & 0xFFFFFFFF
    while len(out) < length:
        state = (state * 1103515245 + 12345) & 0xFFFFFFFF
        out += struct.pack('<I', state)
    return bytes(out[:length])


def differ(expected, actual):
    if len(expected) != len(actual):
        return {'lengthMismatch': [len(expected), len(actual)]}
    index = next((i for i, (a, b) in enumerate(zip(expected, actual)) if a != b), None)
    return {} if index is None else {'firstMismatch': index,
                                     'expected': expected[index], 'actual': actual[index]}


def flash_ranges(base, size, erase, address, length):
    """稀疏擦除计划：只覆盖受影响的扇区，返回 [(start, stop)]。

    上游 8d8a2045 的规则：原生与 Web 必须用同一份擦除计划，否则时长不可比。
    """
    checked(erase > 0 and size > 0, 'Flash 几何无效')
    checked(base <= address and address + length <= base + size, '写入范围超出 bank')
    start = base + ((address - base) // erase) * erase
    stop = base + ((address + length - base + erase - 1) // erase) * erase
    return [(start, min(stop, base + size))]


def flash_segment_ranges(geometry, segments):
    """Merge only adjacent or overlapping erase sectors touched by ELF segments."""
    spans = [span for address, payload in segments if payload
             for span in flash_ranges(geometry['base'], geometry['bytes'], geometry['erase'], address, len(payload))]
    result = []
    for start, stop in sorted(spans):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(stop, result[-1][1]))
        else:
            result.append((start, stop))
    return result


def finish(statuses):
    """把一组状态收敛成实例结论：fail 优先，其次 blocked/unsupported/manual/pending。"""
    order = ('fail', 'blocked', 'unsupported', 'not-applicable', 'manual', 'pending', 'pass')
    present = set(statuses)
    for status in order:
        if status in present:
            return status
    return 'pending'


# --------------------------------------------------------------------------
# 浏览器 Web 队列访问（复用 toolbox.Client，不新写传输）
# --------------------------------------------------------------------------


class WebTarget:
    """工作台队列的最小封装：只调用本仓页面确实公开的操作。"""

    def __init__(self, runtime, timeout=180, out=None):
        from toolbox import Client  # 延迟导入：plan 模式不需要队列
        self.client = Client(runtime, timeout=timeout)
        self.runtime = runtime
        self.out = Path(out) if out else None
        self.steps = []

    def _log(self, row):
        self.steps.append(row)
        if self.out:
            self.out.mkdir(parents=True, exist_ok=True)
            with (self.out / 'steps.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')

    @staticmethod
    def brief(payload):
        def walk(item):
            if isinstance(item, dict) and '$base64' in item:
                return {'$base64': f'<{len(item["$base64"])} chars>'}
            if isinstance(item, dict):
                return {k: walk(v) for k, v in item.items()}
            if isinstance(item, list):
                return [walk(v) for v in item]
            return item
        return walk(payload)

    def request(self, payload):
        started = time.perf_counter()
        try:
            result = self.client.request(payload)
        except (RuntimeError, TimeoutError) as error:
            if isinstance(error, TimeoutError) or '操作可能仍在执行' in str(error) or '是否已排队未知' in str(error):
                raise Unsafe(str(error)) from error
            raise
        self._log({'request': self.brief(payload), 'ok': bool(result.get('ok')),
                   'job': result.get('job'), 'seconds': round(time.perf_counter() - started, 4)})
        # Results are copied into the run's evidence. A long run must not fill the completed-job queue.
        if result.get('job'):
            self.ack(result['job'])
        if not result.get('ok'):
            raise RuntimeError(result.get('error') or str(result))
        value = result.get('value')
        if isinstance(value, dict) and value.get('returned'):
            checked(not value.get('errors'), f'页面报错：{value["errors"]}')
            return value.get('value')
        return value

    def ping(self):
        return self.request({'action': 'ping'})

    def page(self, page, op, **fields):
        return self.request({'action': 'workbench', 'page': page, 'op': op, **fields})

    def state(self, page):
        return self.page(page, 'state')

    def invoke(self, page, method, args=None, target='view'):
        return self.page(page, 'invoke', method=method, args=args or [], target=target)

    def read(self, page, selector):
        return self.page(page, 'read', selector=selector)

    def set_control(self, page, selector, value):
        return self.request({'action': 'workbench', 'page': page, 'op': 'set',
                             'selector': selector, 'value': value})

    def click(self, page, selector):
        return self.request({'action': 'workbench', 'page': page, 'op': 'click',
                             'selector': selector})

    # -- 页面导出（下载）--------------------------------------------------
    def downloads(self, page):
        return self.page(page, 'downloads') or []

    def fetch_download(self, page, entry, chunk=1 << 20):
        """按页面 download 分块接口取回暂存 Blob（JSP/CSV 等）。"""
        import base64
        total = int(entry.get('bytes') or 0)
        data = bytearray()
        while len(data) < total:
            part = self.request({'action': 'workbench', 'page': page, 'op': 'download',
                                 'id': entry['id'], 'offset': len(data),
                                 'length': min(chunk, total - len(data))})
            raw = base64.b64decode(part['base64'])
            if not raw:
                break
            data += raw
        return bytes(data)

    def ack(self, job):
        self.client.http('ack/' + job, {})

    # -- 目标内存 --------------------------------------------------------
    def read_memory(self, address, length):
        value = self.invoke('dbg', 'memRead', [address, length], target='session')
        raw = value.get('$bytes') if isinstance(value, dict) else value
        checked(isinstance(raw, list) and len(raw) == length,
                f'内存读回长度不符：请求 {length}，实收 '
                f'{len(raw) if isinstance(raw, list) else raw}')
        return bytes(raw)

    def write_memory(self, address, data):
        payload = b64(data)
        payload['kind'] = 'bytes'
        self.invoke('dbg', 'memWrite', [address, payload], target='session')

    def word(self, address):
        return struct.unpack('<I', self.read_memory(address, 4))[0]

    def halfword(self, address):
        return struct.unpack('<H', self.read_memory(address, 2))[0]

    def byte(self, address):
        return self.read_memory(address, 1)[0]

    def run_command(self, text):
        value = self.invoke('dbg', 'runLine', [text]) or {}
        lines = value.get('lines') or []
        errors = [line.get('t') for line in lines if line.get('c') == 'err']
        if value.get('error'):
            errors.append(value['error'])
        return {'text': '\n'.join(str(line.get('t')) for line in lines),
                'errors': errors, 'lines': lines}


# --------------------------------------------------------------------------
# plan / execute 门控
# --------------------------------------------------------------------------


class RunContext:
    """一次验收的上下文：模式、输出目录、链路、目标与证据台账。"""

    def __init__(self, board, link, mode, out, web=None, allow_flash=False,
                 allow_manual=False):
        checked(board in BOARDS, f'未知板卡：{board}')
        if link is not None:
            link = normalize_link(board, link)
        checked(mode in ('plan', 'execute'), f'未知模式：{mode}')
        self.board = board
        self.link = link
        self.case = None
        self.mode = mode
        self.out = Path(out) if out else None
        self.web = web
        self.allow_flash = allow_flash
        self.allow_manual = allow_manual
        self.probe_serial = None
        self.profile = BOARDS[board]
        self.uncertain = None
        self.records = []
        self.events = []

    # -- 门控 ------------------------------------------------------------
    def hardware(self, what):
        """任何接触硬件的动作都必须先过这里：默认 plan 只登记、不下发。"""
        if self.mode != 'execute':
            raise Pending(f'plan 模式不执行硬件动作（{what}）；确认条件后使用 execute')
        if self.uncertain:
            raise Unsafe(self.uncertain)
        if self.web is None:
            raise Pending('execute 需要 --runtime 指向已打开执行页的运行时目录')

    def flash(self, what):
        self.hardware(what)
        if not self.allow_flash:
            raise Pending(f'{what} 会改写目标 Flash；需要显式 --allow-flash')

    def manual(self, what):
        """人工取证：没有授权就记待验，不能假装自动完成。"""
        if not self.allow_manual:
            raise Pending(f'{what} 需要人工取证；授权后使用 --allow-manual 记录操作事件')
        return self.event('operator', action=what)

    def fail_unsafe(self, what, error):
        self.uncertain = f'{what} 无法确认：{error}'
        raise Unsafe(self.uncertain) from error

    # -- 证据 ------------------------------------------------------------
    def event(self, kind, **fields):
        row = {'event': kind, 'board': self.board, 'link': self.link, **fields,
               'time': time.strftime('%Y-%m-%dT%H:%M:%S')}
        self.events.append(row)
        if self.out:
            self.out.mkdir(parents=True, exist_ok=True)
            with (self.out / 'events.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
        return row

    def record(self, instance, status, **fields):
        checked(status in STATUSES, f'未知状态：{status}')
        row = {'instance': instance, 'board': self.board, 'link': self.link,
               'case': self.case, 'status': status, **fields}
        self.records.append(row)
        self.event('result', **row)
        return row

    def write_jsonl(self, name='results.jsonl'):
        if not self.out:
            return None
        self.out.mkdir(parents=True, exist_ok=True)
        path = self.out / name
        with path.open('x', encoding='utf-8') as stream:
            for row in self.records:
                stream.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
        return path

    def digest(self):
        return sha(json.dumps(self.records, ensure_ascii=False, sort_keys=True,
                              default=str).encode('utf-8'))
