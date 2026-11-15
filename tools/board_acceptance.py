"""Seven-group board acceptance checks for SuperLinkPRO (F103ZE / H743 / HPM6800EVK).

移植自 ZynqLinkPRO ``tools/board_acceptance.py``（54150b31），保留其判定语义：

* 用例通过 = 函数正常返回（全部 ``checked`` 断言成立）；抛 ``Pending`` = 待验（条件不具备）；
  抛 ``Unsafe`` = 阻塞（写入/恢复未确认，之后一律 blocked）；
* 证据先落盘，结论由证据推导；``ack``/计划投递成功都不算通过；
* 每个用例后核对探针/目标回到空闲；不干净就停止后续硬件动作。

按本仓适配的差异：

* 不硬编码 DOM 选择器当页面契约：CLI 路径走页面公开方法（``invoke``），
  Web 路径走真实控件（``set``/``click``）再读 ``state``，两条路径分别计时；
* 七组各五项与 ``docs/板卡验收清单.md`` 逐格对应，35 个实例 ID 为 ``S01``…``E05``；
* 探针高速流沿用 SuperLinkPRO APP 的 ARM JTAG 和 EP83 协议；
* ETM 固定用本仓 Kingst 与 **1 MHz SWD** 控制，HPM6800EVK 记「不适用」；
* 原生 HSS 定速窗复用 ``script_test/scope_hss_test.py``，不新写 USB 栈。

判定口径：没有批准门槛的性能项只报基线（``pending``），不把其他探针或其他板卡的
实测速度当本机门槛；人工取证项没有明确授权时记 ``manual``/``pending``，不假装自动完成。
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import math
import struct
import subprocess
import sys
import time
from pathlib import Path

from tools import board_acceptance_rtt as rtt_evidence
from tools import board_acceptance_scope as scope
from tools import board_acceptance_target as facts
from tools.board_acceptance_target import (
    BOARDS, GROUPS, HPM_NONCACHEABLE, Pending, RATE_BASELINES, RTT_USB_PACKET,
    SCOPE_SHORT_WINDOW_SECONDS, SCOPE_TICK_HZ, SCOPE_WINDOW_SECONDS, SRAM_PIPELINE,
    STATUSES, Unsafe, checked, finish, median, pattern, sha, wait_for)

checked_ = checked

# --------------------------------------------------------------------------
# 用例元数据：与 docs/板卡验收清单.md 的七组各五项逐格一致
# --------------------------------------------------------------------------

ITEM_META = {
    'S01': {'title': 'SRAM 图案写入并独立读回',
            'condition': '停机；独占测试区已备份；写地址相关非重复图案后独立读回',
            'criteria': '逐字节一致；测试区前后哨兵不变',
            'evidence': ['address', 'bytes', 'patternSha256', 'mismatch', 'guards']},
    'S02': {'title': '块读/写测速',
            'condition': '同一地址、同一大小（64 B 与 20 KiB）与同一运行状态，'
                         '分别计时 CLI（页面方法）与 Web（真实控件）两条路径',
            'criteria': '两路径各自给出中位吞吐；有批准下限时才算通过，否则「基线已测，待定标」',
            'evidence': ['blocks', 'cli', 'web', 'comparison', 'baseline']},
    'S03': {'title': '跨 TAR 边界连续访问',
            'condition': 'ARM：覆盖 1 KiB TAR 边界两侧各 32 B 的读写；'
                         'HPM：非缓存段 SBA 连续读写窗口，另读可缓存对照样本',
            'criteria': '跨边界内容逐字节一致；HPM 的可缓存对照样本只作限制说明，'
                        '不能当作正确性证据',
            'evidence': ['address', 'crossed', 'mismatch', 'hpmCachedControl']},
    'S04': {'title': '恢复原值、哨兵与运行状态',
            'condition': '写回备份原始内容；核对哨兵与目标运行状态',
            'criteria': '测试区与备份逐字节一致，哨兵一致，目标回到开始时运行/停机状态',
            'evidence': ['restoredSha256', 'backupSha256', 'guards', 'targetState']},
    'S05': {'title': '1/2/4 B 与非对齐访问',
            'condition': '对齐写入 1/2/4 B；再在基址 +1 处按声明宽度读写',
            'criteria': '支持宽度逐字节一致，相邻字节不被改写；声明拒绝的组合报错且哨兵不变',
            'evidence': ['widths', 'unaligned', 'neighbors']},

    'F01': {'title': 'Web 按钮烧录配套镜像',
            'condition': '已确认备份、镜像与目标板匹配；用烧录页真实控件提交',
            'criteria': '目标身份与写入范围符合镜像段表；页面确认 verified',
            'evidence': ['image', 'segments', 'sectors', 'verified', 'target']},
    'F02': {'title': '独立回读镜像与扇区未指定字节',
            'condition': '从 Flash 地址独立读回；HPM 直接读 XIP 映射区',
            'criteria': '有效镜像逐字节一致；受影响扇区未指定字节与备份一致',
            'evidence': ['readbackSha256', 'verifiedLoadBytes', 'sectorPadding']},
    'F03': {'title': '复位后业务心跳',
            'condition': '烧录后按页面复位方式重启，读取夹具心跳两次',
            'criteria': '心跳前进（业务已运行）；复位方式单列',
            'evidence': ['resetMethod', 'heartbeat', 'advancing']},
    'F04': {'title': '烧录测速',
            'condition': '同 ELF、同范围，分别计 Web 控件路径与原生 CLI（target-flash）总耗时',
            'criteria': '两路径各自给出总耗时；阶段可观测时单列',
            'evidence': ['webSeconds', 'cliSeconds', 'stages', 'bytes']},
    'F05': {'title': '恢复原 Flash 并确认原程序运行',
            'condition': '写回备份扇区；独立回读；复位确认原程序心跳',
            'criteria': '回读与备份 SHA 一致；原程序心跳前进',
            'evidence': ['backupSha256', 'readbackSha256', 'heartbeat']},

    'D01': {'title': '建链与身份核对',
            'condition': '顶栏选定探针，官方/内置 CFG 建链；载入匹配 ELF',
            'criteria': '芯片、DPIDR（SWD）或 TAP ID（JTAG）、核与 ELF 一致',
            'evidence': ['cfg', 'transport', 'identity', 'clock', 'elf']},
    'D02': {'title': '暂停、继续与指令单步',
            'condition': '运行中暂停、继续，再从已知指令单步若干次',
            'criteria': '每次暂停确认停机与 PC 可读；单步后 PC 变化且与指令长度一致',
            'evidence': ['halt', 'resume', 'steps']},
    'D03': {'title': '源码进入、跳过、跳出',
            'condition': '在已知源码行使用进入/跳过/跳出，核对停点',
            'criteria': 'PC 与源码行符合夹具调用结构；调用深度按操作变化',
            'evidence': ['entry', 'into', 'over', 'out', 'backtrace']},
    'D04': {'title': '断点设置、命中与删除',
            'condition': '在已知函数入口设断点并命中，再删除',
            'criteria': '命中 PC 等于断点地址；删除后登记清空；FPB/触发器状态可核对',
            'evidence': ['breakpoints', 'hit', 'removed', 'slots']},
    'D05': {'title': '交互测速',
            'condition': '同一会话重复：建链、暂停、单步、停点后寄存器/变量刷新',
            'criteria': '各项给出中位耗时；没有批准上限时记「基线已测，待定标」',
            'evidence': ['connectSeconds', 'haltSeconds', 'stepSeconds', 'refreshSeconds']},

    'V01': {'title': '原始上行序号连续',
            'condition': '读取配套 SRAM 夹具的有限序号窗',
            'criteria': '序号连续、内容与夹具一致、无重复',
            'evidence': ['controlBlock', 'bytes', 'lost', 'corrupt']},
    'V02': {'title': '下行载荷与 WrOff',
            'condition': '通过 Web 发送二进制标记；独立读回目标下行环',
            'criteria': '下行字节和哨兵一致；WrOff 前进量与发送长度相符',
            'evidence': ['sent', 'readbackBytes', 'wrOffBefore', 'wrOffAfter']},
    'V03': {'title': 'Viewer 吞吐',
            'condition': '固定环大小与轮询档，连续接收若干秒',
            'criteria': '给出有效 B/s 与轮询次数；有批准下限时才判通过，否则待定标',
            'evidence': ['bytes', 'seconds', 'bytesPerSecond', 'polls', 'ringBytes']},
    'V04': {'title': '跨环尾收发',
            'condition': '写入跨越上行/下行环尾的载荷',
            'criteria': '跨尾数据完整、游标正确、无重复或缺口',
            'evidence': ['upWrap', 'downWrap', 'lost']},
    'V05': {'title': 'ELF 定位 _SEGGER_RTT',
            'condition': '用匹配 ELF 定位控制块并实际收数',
            'criteria': '控制块地址与 ELF 符号一致；能收到数据',
            'evidence': ['elfSymbol', 'actualAddress', 'bytes']},

    'R01': {'title': '转发序号流',
            'condition': '按探针序列号匹配 COM；用配套序号 ELF 分别采集 Web 和原生窗口',
            'criteria': '窗口内及跨窗口序号连续，独立 RdOff、实收和 STOP 计数一致',
            'evidence': ['probeSerial', 'webSeconds', 'cliSeconds', 'sequence', 'captureEvidence']},
    'R02': {'title': '转发吞吐',
            'condition': '同一环、同一批次，分别计时 Web Serial 与 CLI 驱动路径',
            'criteria': '两路径各自给出有效 MB/s；有批准下限时才判通过',
            'evidence': ['web', 'cli', 'ringBytes', 'baseline']},
    'R03': {'title': '带载 STOP 排空',
            'condition': '有数据流时停止转发；核对原始实收与环剩余，确认串口归还',
            'criteria': '排空后计数自洽；串口回到 UART 且能再次打开',
            'evidence': ['received', 'ringLeft', 'portReturned', 'probeCounters']},
    'R04': {'title': 'STOP 后立即再启动',
            'condition': '同一页面停止后立刻再次启动',
            'criteria': '按钮状态与实际一致；新窗口不含旧数据',
            'evidence': ['firstWindow', 'secondWindow', 'staleBytes']},
    'R05': {'title': '511/512/513 B 载荷对账',
            'condition': '按 USB 端点整包边界 M=512 分别发送 M-1/M/M+1 字节非重复载荷',
            'criteria': '三档逐字节一致，无重复或额外字节',
            'evidence': ['lengths', 'mismatch']},

    'J01': {'title': 'DEF、地址与波形',
            'condition': '载入配套 scope ELF，只勾 1 个运行态计数器（u32）；页面收到 DEF',
            'criteria': 'DEF 通道地址＝ELF 符号地址；采样随计数器单调推进',
            'evidence': ['selection', 'elfAddresses', 'definition', 'vars', 'plan', 'samples']},
    'J02': {'title': 'STOP 计数与尾差',
            'condition': '取单路 60 秒长窗停止后的整组计数（探针 METRICS）与页面侧对账；'
                         '任一字段缺失按缺口处理，不得判通过',
            'criteria': 'produced>0；STOP 尾差=0；USB 丢样=0；读错=0；DAP 让路=0；'
                        '无效样本=0；包序缺口=0；实际周期 tick 与请求一致',
            'evidence': ['produced', 'skipped', 'usbDrops', 'readErrors', 'dapYields',
                         'invalidSamples', 'tailCountDelta', 'actualPeriodTicks']},
    'J03': {'title': '单路摸高',
            'condition': '固定链路档，从同板历史周期试相邻档。Web 最快合格档和原生历史固定档各跑 60 秒',
            'criteria': '短窗和两条长窗均满足 J02 计数门槛、组率达到理论值 99%、跳拍率低于 0.1%；'
                        '原生窗缺失时待验',
            'evidence': ['selection', 'chosenPeriodUs', 'web', 'cli', 'shortWindows', 'longAttempts']},
    'J04': {'title': '八路摸高',
            'condition': '用 ELF 匹配历史窗口的 8 个不同对齐 DWORD，'
                         '每组 32 B；其余同 J03',
            'criteria': '八路选择互不相同；组/s 与计数门槛同 J03',
            'evidence': ['selection', 'chosenPeriodUs', 'web', 'cli', 'shortWindows', 'longAttempts']},
    'J05': {'title': 'JSP 与 CSV 逐值对账',
            'condition': '对同一采集窗口保存原始 JSP 与 CSV；高速长窗另做小窗导出',
            'criteria': '通道数与样本数一致；时间单调；逐值相等（整数精确）；'
                        '首处不一致可定位；导出缺失按缺口处理',
            'evidence': ['jsp', 'csv', 'channels', 'verdict']},

    'E01': {'title': 'Web 联动打开新记录',
            'condition': 'ETM 页面选择函数并采集一段新记录（Kingst LA，1 MHz SWD 控制）',
            'criteria': '采集完成并生成可打开的记录目录',
            'evidence': ['record', 'summary', 'seconds']},
    'E02': {'title': '硬件窗口、丢帧与解码质量',
            'condition': '离线严格校验该记录',
            'criteria': '记录深度与请求相符；质量计数与未知执行区间分别登记',
            'evidence': ['depth', 'requested', 'overflow', 'unknown', 'verify']},
    'E03': {'title': 'ETM 性能',
            'condition': '固定 1 秒，分计采集、LA 回传/保存、解码/索引、首屏与总时间',
            'criteria': '各阶段耗时单列；没有批准上限时记「基线已测，待定标」',
            'evidence': ['captureSeconds', 'decodeSeconds', 'querySeconds', 'totalSeconds']},
    'E04': {'title': '执行顺序验证',
            'condition': '用已知函数、匹配源码与窗口内事件查询轨迹',
            'criteria': '已知调用路径按预期出现；未知/未执行区间保留',
            'evidence': ['function', 'events', 'unknown']},
    'E05': {'title': '恢复与租约释放',
            'condition': '核对目标运行状态、调试配置、Kingst 租约与原页面接回',
            'criteria': '目标状态与本轮开始时一致；LA 与采集租约空闲；原页面可重新建链',
            'evidence': ['targetState', 'laLease', 'pageReturn']},
}


def item_meta(instance):
    checked(instance in ITEM_META, f'未知实例：{instance}')
    return ITEM_META[instance]


def render_checklist_table():
    """从 ITEM_META 生成主清单的「七组各五项」表格。

    文档与 runner 用同一份事实：改判据只改这里，再重新生成
    ``docs/板卡验收清单.md`` 的对应小节；测试会核对两者不漂移。
    """
    lines = ['| 实例 | 子项 | 测试条件 | 验收标准 | 证据字段 |', '| --- | --- | --- | --- | --- |']
    for instance in facts.INSTANCE_IDS:
        meta = ITEM_META[instance]
        lines.append('| `{}` | {} | {} | {} | {} |'.format(
            instance, meta['title'], meta['condition'], meta['criteria'],
            '、'.join(meta['evidence'])))
    return '\n'.join(lines)


# --------------------------------------------------------------------------
# 计划：把「链路 × 组 × 五项」展开成实例，并标出本仓明确不适用的组合
# --------------------------------------------------------------------------


def applicability(board, link, instance):
    """返回 (是否适用, 原因)。不适用必须写明依据，不能静默跳过。

    ARM/JTAG 的原生用例使用相同编号。工作台 runner 只执行 SWD 与 RISC-V/JTAG。
    """
    profile = BOARDS[board]
    if link not in profile['links']:
        return False, f'{profile["label"]} 不支持 {link} 链路'
    group = instance[0]
    if group == 'E' and board == 'hpm6800evk':
        return False, 'HPM6800EVK（RISC-V）没有 ARM ETM'
    if group == 'E' and link != 'swd':
        return False, ('本仓 ETM 固定用 Kingst 采集与 1 MHz SWD 控制；'
                       f'{link} 链路下不采 ETM')
    return True, None


def plan_cases(board, links=None, groups=None, seed=20261122):
    """展开计划。返回 (cases, instances)；计划必须能被 review 逐字复算。"""
    profile = BOARDS[board]
    chosen = list(links) if links else list(profile['links'])
    groups = list(groups) if groups else list(GROUPS)
    cases, instances = [], []
    for link in chosen:
        checked(link in profile['links'], f'{board} 不支持链路 {link}')
        for group in groups:
            checked(group in GROUPS, f'未知组：{group}')
            ids = [f'{group}0{index}' for index in range(1, 6)]
            verdicts = {instance: applicability(board, link, instance) for instance in ids}
            case = {'id': f'{link}-{group}', 'group': group, 'link': link,
                    'requestedHz': profile['links'][link].get('requestHz'),
                    'instances': ids, 'seed': seed}
            off = {instance: reason for instance, (ok, reason) in verdicts.items() if not ok}
            if len(off) == len(ids):
                case['notApplicable'] = next(iter(off.values()))
            elif off:
                case['instanceNotApplicable'] = off
            if group == 'S' and board == 'h743' and link == 'swd':
                case['pipeline'] = dict(SRAM_PIPELINE)
            if board == 'hpm6800evk':
                case['sba'] = {'nonCacheable': list(HPM_NONCACHEABLE)}
            cases.append(case)
            for instance in ids:
                meta = item_meta(instance)
                row = {'instance': instance, 'group': group, 'case': case['id'],
                       'board': board, 'link': link,
                       'requestedHz': case['requestedHz'],
                       'condition': meta['condition'], 'criteria': meta['criteria'],
                       'evidenceFields': meta['evidence'], 'seed': seed}
                if instance in off:
                    row['notApplicable'] = off[instance]
                instances.append(row)
    return cases, instances


def offline_preconditions(board, link, instance):
    """离线可核对的前置：夹具可解析、符号齐全、基线是否已批准。

    只做文件与事实核对，不打开任何设备。
    """
    group = instance[0]
    ok, missing, pending = [], [], []
    kinds = {'S': ['scope'], 'F': ['flash'], 'D': ['debug'], 'V': ['rtt'],
             'R': ['rtt'], 'J': ['scope'], 'E': []}.get(group, [])
    if instance == 'R01':
        kinds = ['rtt-seq']
    for kind in kinds:
        try:
            evidence = facts.fixture(board, kind)
            ok.append({'fixture': kind, 'path': evidence['path'],
                       'source': evidence['source'], 'sha256': evidence['sha256'],
                       'symbols': {k: hex(v) for k, v in evidence['symbols'].items()}})
        except facts.Pending as error:
            missing.append({'fixture': kind, 'reason': str(error)})
    if group == 'F' and instance in ('F01', 'F04'):
        pending.append({'reason': '烧录会改写目标 Flash，必须显式 --allow-flash'})
    if group == 'J':
        pending.append({'reason': '摸高结论只能用本机实测组率，计划阶段不预填速度门槛'})
    if instance in ('V01', 'V02', 'V04'):
        pending.append({'reason': '需用 --probe-serial 配对 Viewer 与独立 SRAM 回读，H743 需关闭 D-cache'})
    if group == 'E':
        pending.append({'reason': 'ETM 需要 Kingst LA、匹配 CDC 夹具与接线，由操作者确认'})
    if instance in ('S02', 'V03', 'R02'):
        baselines = RATE_BASELINES.get((board, link, 'rtt-forward-fixed-text'))
        if instance == 'S02':
            pending.append({'reason': '块读写下限尚未在 docs 中批准，只能记基线'})
        elif not baselines:
            pending.append({'reason': '本板本链路尚无已批准的吞吐下限，只能记基线'})
    if group in ('V', 'R', 'J', 'E') or instance in ('D03', 'D04'):
        pending.append({'reason': '需要目标已运行匹配夹具并在页面完成一次真实建链'})
    return {'ok': ok, 'missing': missing, 'pending': pending}


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------


class Driver:
    """按组执行五项；所有硬件动作都经 ctx 门控，plan 模式下不会调用本类。"""

    def __init__(self, ctx):
        self.ctx = ctx
        self.board = ctx.board
        self.link = ctx.link
        self.profile = BOARDS[ctx.board]
        self.web = ctx.web
        self.out = ctx.out
        self.owns_debug = False

    # -- 小工具 ----------------------------------------------------------
    @property
    def riscv(self):
        return self.profile['arch'] == 'riscv'

    @property
    def chip_option(self):
        return self.profile['chip']

    def read_flash(self, address, length):
        payload = bytearray()
        for offset in range(0, length, 0x20000):
            count = min(length - offset, 0x20000)
            result = self.web.invoke('flash', 'readFlashBytes', [address + offset, count])
            data = result.get('$bytes') if isinstance(result, dict) else None
            checked(isinstance(data, list) and len(data) == count, '实际 Flash 回读长度不符')
            payload.extend(data)
        return bytes(payload)

    def configure_flash(self, case):
        self.web.set_control('flash', '#f-chip', self.chip_option)
        if case['requestedHz']:
            self.web.set_control('flash', '#f-speed', str(case['requestedHz'] // 1000))

    def match_flash_image(self, case, loads):
        """Prevent writes to an unknown application or to a fixture with a different layout."""
        self.configure_flash(case)
        for address, expected in loads:
            if self.read_flash(address, len(expected)) != expected:
                raise Pending(f'板载 Flash 与配套 ELF 装载段不一致（{address:#x}）')

    def fixture_flash_loads(self, fixture):
        _, segments = facts.elf_contents(Path(facts.ROOT) / fixture['path'])
        geometry = facts.FLASH[self.board]
        base, limit = geometry['base'], geometry['base'] + geometry['bytes']
        loads = sorted((address, data) for address, data, _ in segments if data)
        if not loads or any(address < base or address + len(data) > limit for address, data in loads):
            raise Pending('夹具 ELF 的装载段超出本板 Flash；等待匹配的 Flash 夹具')
        return loads

    def folder(self, name):
        # 证据必须落在本轮 --out 目录里：不允许退回当前工作目录（会把模拟产物写进仓库）。
        if self.out is None:
            raise Pending('execute 必须带 --out；证据不能写到当前工作目录')
        path = Path(self.out) / (self.ctx.case or '') / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_json(self, folder, name, value):
        path = Path(folder) / name
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n',
                       encoding='utf-8')
        tmp.replace(path)
        return path

    def record(self, instance, status, **fields):
        meta = item_meta(instance)
        row = {'title': meta['title'], **fields}
        self.ctx.record(instance, status, **row)
        return {'instance': instance, 'status': status, **row}

    def pending(self, instance, reason, evidence=None, **extra):
        return self.record(instance, 'pending', reason=reason,
                           evidence={**(evidence or {}), **extra})

    def fail(self, instance, reason, evidence=None, **extra):
        return self.record(instance, 'fail', reason=reason,
                           evidence={**(evidence or {}), **extra})

    def manual(self, instance, reason, evidence=None, **extra):
        return self.record(instance, 'manual', reason=reason,
                           evidence={**(evidence or {}), **extra})

    def unsupported(self, instance, reason, evidence=None, **extra):
        """产品/固件不具备该能力：记「不支持」，不能让缺失字段变成通过。"""
        return self.record(instance, 'unsupported', reason=reason,
                           evidence={**(evidence or {}), **extra})

    # -- 采集 ------------------------------------------------------------
    def read(self, address, length):
        return self.web.read_memory(address, length)

    def write(self, address, data):
        self.web.write_memory(address, data)

    def read_timed(self, address, length, rounds=3):
        times, payload = [], b''
        for _ in range(rounds):
            started = time.perf_counter()
            payload = self.read(address, length)
            times.append(time.perf_counter() - started)
        best = median(times)
        return dict(facts.measurement(length, best), rounds=[round(t, 6) for t in times],
                    sha256=sha(payload), data=payload)

    def write_timed(self, address, data, rounds=3):
        times = []
        for _ in range(rounds):
            started = time.perf_counter()
            self.write(address, data)
            times.append(time.perf_counter() - started)
        best = median(times)
        return dict(facts.measurement(len(data), best), rounds=[round(t, 6) for t in times])

    def web_ui_read(self, address, length, chunk=1024):
        """真实控件路径：设地址/长度后点「读内存」，再核对页面实收长度。

        页面把单次长度限制在 1024 B，因此大块按 chunk 分次点击，逐次核对
        ``summary().memLen``，拼接后再计时。
        """
        chunks = math.ceil(length / chunk)
        started = time.perf_counter()
        got = 0
        for index in range(chunks):
            span = min(chunk, length - index * chunk)
            at = address + index * chunk
            previous = self.web.state('dbg')['summary'].get('manualMemReadId', 0)
            self.web.set_control('dbg', '#d-mem-addr', hex(at))
            self.web.set_control('dbg', '#d-mem-len', str(span))
            self.web.click('dbg', '#d-mem-read')
            wait_for(lambda: (self.web.state('dbg') or {}).get('summary', {}),
                     lambda value: value.get('manualMemReadId', 0) > previous and value.get('memLen') == span and
                     int(str(value.get('memAddr', '0')), 0) == at, 10,
                     f'Web 读内存第 {index + 1}/{chunks} 段未达到 {span} 字节')
            got += span
        elapsed = time.perf_counter() - started
        return dict(facts.measurement(length, elapsed), chunks=chunks, chunkBytes=chunk,
                    bytesRead=got, seconds=elapsed)

    def fixture(self, kind):
        return facts.fixture(self.board, kind)

    def heartbeat(self, symbols):
        """读夹具心跳计数（两个 4 B 字的时间差由调用方判）。"""
        address = symbols.get('tick')
        if not address:
            raise Pending('夹具没有登记心跳符号，无法核对业务运行')
        return address, self.web.word(address)

    def wait_heartbeat(self, address, timeout=4.0):
        first = self.web.word(address)
        seen = [first]

        def advanced():
            value = self.web.word(address)
            if value != seen[-1]:
                seen.append(value)
            return len(seen) >= 3
        wait_for(lambda: None, lambda _: advanced(), timeout,
                 '心跳在超时内没有前进（目标可能已停机或夹具不符）')
        return {'address': hex(address), 'first': first, 'samples': seen}

    # -- 状态 ------------------------------------------------------------
    def clock(self, fixture_kind):
        """实际时钟：H743/HPM 用夹具自报符号，F103ZE 用 RCC 回读。"""
        board = self.board
        spec = self.profile['clock']
        if spec['kind'] == 'stm32f1':
            cfgr = self.web.word(spec['cfgr'])
            return facts.decode_stm32f1_clock({'CFGR': cfgr}, spec['hseHz'])
        if spec['kind'] == 'stm32h7':
            base = spec['rcc']
            registers = {'CR': self.web.word(base), 'CFGR': self.web.word(base + 0x10),
                         'D1CFGR': self.web.word(base + 0x18),
                         'PLLCKSELR': self.web.word(base + 0x28),
                         'PLLCFGR': self.web.word(base + 0x2C),
                         'PLL1DIVR': self.web.word(base + 0x30)}
            return facts.decode_stm32h7_clock(registers, spec['hseHz'])
        symbols = self.fixture(fixture_kind)['symbols']
        address = symbols.get('mchtmrHz') or symbols.get('hclkHz')
        if not address:
            raise Pending('该板夹具没有自报时钟符号，需要操作者提供量测值')
        value = self.web.word(address)
        if symbols.get('mchtmrHz'):
            return {'mchtmrHz': value, 'basis': '夹具自报 MCHTMR'}
        return {'hclkHz': value * 1_000_000, 'basis': '夹具自报 HCLK（MHz）'}

    def target_state(self):
        summary = (self.web.state('dbg') or {}).get('summary') or {}
        return {'halted': summary.get('halted'), 'running': summary.get('targetState'),
                'pc': summary.get('pc'), 'backend': summary.get('backend')}

    def check_idle(self):
        """用例后核对：调试/转发页没有仍在进行的任务。"""
        findings = {}
        for page in ('dbg', 'rttcdc', 'scope'):
            try:
                summary = (self.web.state(page) or {}).get('summary') or {}
            except Exception as error:  # 页面未加载不算占用
                findings[page] = f'unavailable: {error}'
                continue
            findings[page] = {key: summary.get(key) for key in
                              ('connected', 'running', 'busy', 'open', 'state') if key in summary}
        return findings

    # ==================================================================
    # S 组：SRAM
    # ==================================================================
    def sram(self, case):
        window = self.profile['sram']
        fixture = self.fixture('scope')
        symbols = fixture['symbols']
        base, size = window['base'], window['bytes']
        guard = window['sentinel']
        start, stop = base - guard, base + size + guard

        # 前置：目标在跑、夹具对得上（心跳前进）。
        tick_address = symbols.get('tick')
        if not tick_address:
            raise Pending('scope 夹具没有心跳符号')
        before_heartbeat = self.wait_heartbeat(tick_address)
        if self.board == 'h743' and self.web.word(0xe000ed14) & (1 << 16):
            raise Pending('H743 AXI SRAM 夹具要求目标 D-cache 关闭')

        backup = self.read(start, stop - start)
        self.write_json(self.folder('sram'),
                        'backup.json', {'address': hex(start), 'bytes': len(backup),
                                        'sha256': sha(backup)})
        (self.folder('sram') / 'backup.bin').write_bytes(backup)
        try:
            results, timings = self.sram_checks(case, window, backup, start, symbols)
        except Unsafe:
            raise
        except Exception:
            self.sram_restore(start, backup, tick_address, before_heartbeat)
            raise
        results['S04'] = self.sram_restore(start, backup, tick_address, before_heartbeat)
        if results['S04']['status'] != 'pass':
            results['S02'] = self.pending('S02', 'Web 后 SRAM 或运行状态未恢复，原生对照停止', web=timings)
        else:
            results['S02'] = self.sram_compare(case, base, backup, timings)
        return results

    def sram_checks(self, case, window, backup, start, symbols):
        base, size, guard = window['base'], window['bytes'], window['sentinel']
        results = {}
        patterns = pattern(2048, 0x5A5A)
        results['S01'] = self.sram_pattern(base, patterns, backup, start, guard)

        timings = self.sram_web_bench(case, base, size)
        results['S03'] = self.sram_cross(base, backup, start, symbols)
        results['S05'] = self.sram_widths(base, start, guard)
        return results, timings

    def sram_web_bench(self, case, base, size):
        compare = bool(self.ctx.probe_serial and case.get('requestedHz'))
        blocks = {}
        try:
            if compare:
                state = self.web.state('dbg')['summary']
                checked((state.get('probeSerial') or '').casefold() == self.ctx.probe_serial.casefold(),
                        'Web SRAM 访问所用探针序列号不符')
                self.web.invoke('dbg', 'halt', target='session')
                checked(self.target_state()['halted'], 'Web SRAM 性能阶段未确认暂停')
            for length in (64, size):
                value = self.read_timed(base, length)
                payload = pattern(length, 0x1234 + length)
                written = self.write_timed(base, payload)
                blocks[str(length)] = {'read': {k: v for k, v in value.items() if k != 'data'},
                                       'write': written, 'byteExact': self.read(base, length) == payload}
            ui = self.web_ui_read(base, size)
        except Unsafe as error:
            self.ctx.uncertain = str(error)
            raise
        finally:
            if compare and not self.ctx.uncertain:
                self.restore_debug_running()
        return {'address': base, 'state': 'halted' if compare else 'running', 'blocks': blocks, 'webUi': ui}

    def sram_compare(self, case, base, backup, web):
        from tools.board_acceptance_native import NativeFailure

        baseline = facts.rate_verdict(RATE_BASELINES.get((self.board, self.link, 'sram-block')),
                                      case['requestedHz'], web['blocks']['64']['read']['mebibytesPerSecond'], 'SRAM 块读写')
        if not all(item['byteExact'] for item in web['blocks'].values()):
            return self.fail('S02', 'Web SRAM 写入未逐字节对齐', web=web)
        if web['state'] != 'halted':
            return self.pending('S02', '原生对照需要 --probe-serial 和确定的链路时钟', web=web, baseline=baseline)
        evidence = {'web': web, 'native': {}, 'baseline': baseline}
        guard = self.profile['sram']['sentinel']
        try:
            self.disconnect_debug()
            for block in web['blocks']:
                length = int(block)
                native = self.native_openocd(case, self.folder('sram') / f'native-{block}')
                row = native.sram(base, pattern(length, 0x1234 + length),
                                  expected=backup[guard:guard + length])
                evidence['native'][block] = row
                if not row.get('verified') or not row.get('restored') or row.get('stateAfter', {}).get('state') != 'running':
                    raise Unsafe('原生 SRAM 独立回读或恢复未确认')
            if profile := case.get('pipeline'):
                from tools.board_acceptance_pipeline import run

                native = self.native_openocd(case, self.folder('sram') / 'native-pipeline')
                window = profile['windowBytes']
                evidence['pipeline'] = run(native, base, backup[:window + 2 * guard], profile)
                try:
                    self.connect_debug(case)
                    state = self.web.state('dbg')['summary']
                    checked((state.get('probeSerial') or '').casefold() == self.ctx.probe_serial.casefold(),
                            '流水测后心跳所用探针不一致')
                    evidence['pipelineHeartbeat'] = self.wait_heartbeat(self.fixture('scope')['symbols']['tick'])
                except Exception as error:
                    self.ctx.fail_unsafe('确认 H743 流水测后原应用心跳', error)
        except Pending as error:
            return self.pending('S02', str(error), evidence=evidence)
        except NativeFailure as error:
            return self.fail('S02', str(error), evidence={**evidence, 'failure': error.evidence})
        return self.pending('S02', '已记录块读写实测数据，本档尚未定标', evidence=evidence)

    def sram_pattern(self, base, payload, backup, start, guard):
        guards = {'before': backup[:guard], 'after': backup[-guard:]}
        self.write(base, payload)
        readback = self.read(base, len(payload))
        mismatch = facts.differ(payload, readback)
        head = self.read(start, guard)
        tail = self.read(start + len(backup) - guard, guard)
        guards_ok = head == guards['before'] and tail == guards['after']
        evidence = {'address': hex(base), 'bytes': len(payload),
                    'patternSha256': sha(payload), 'readSha256': sha(readback),
                    'mismatch': mismatch, 'guards': {'before': head == guards['before'],
                                                     'after': tail == guards['after']}}
        if mismatch:
            return self.fail('S01', f'写后读回不一致：{mismatch}', **evidence)
        if not guards_ok:
            return self.fail('S01', '测试区前后哨兵被改动', **evidence)
        return self.record('S01', 'pass', evidence=evidence)

    def sram_cross(self, base, backup, start, symbols):
        boundary = self.profile['sram'].get('tarBoundary')
        if boundary:
            address = base + boundary - 32
            payload = pattern(64, 0xBEEF)
            self.write(address, payload)
            readback = self.read(address, 64)
            mismatch = facts.differ(payload, readback)
            evidence = {'address': hex(address), 'boundary': hex(base + boundary),
                        'crossed': True, 'mismatch': mismatch,
                        'cachedControl': None,
                        'note': 'TAR 边界两侧各 32 B，覆盖 1 KiB 增量地址增量'}
            if mismatch:
                return self.fail('S03', f'跨 TAR 边界不一致：{mismatch}', **evidence)
            return self.record('S03', 'pass', evidence=evidence)
        # RISC-V：非缓存段连续窗口 + 可缓存对照
        address = base
        payload = pattern(256, 0xF00D)
        self.write(address, payload)
        readback = self.read(address, 256)
        mismatch = facts.differ(payload, readback)
        cached = symbols.get('cachedV')
        control = None
        if cached:
            control = {'address': hex(cached), 'value': self.web.word(cached),
                       'note': '可缓存对照样本：SBA 读可能停在旧值，只作限制说明，'
                               '不能当作正确性证据'}
        evidence = {'address': hex(address), 'crossed': False, 'mismatch': mismatch,
                    'hpmCachedControl': control,
                    'window': {'nonCacheable': [hex(value) for value in HPM_NONCACHEABLE]}}
        if mismatch:
            return self.fail('S03', f'SBA 连续窗口不一致：{mismatch}', **evidence)
        return self.record('S03', 'pass', evidence=evidence)

    def sram_restore(self, start, backup, tick_address, before_heartbeat):
        self.write(start, backup)
        readback = self.read(start, len(backup))
        same = readback == backup
        heartbeat = None
        try:
            heartbeat = self.wait_heartbeat(tick_address, timeout=4)
        except Exception as error:
            heartbeat = {'error': str(error)}
        state = self.target_state()
        evidence = {'restoredSha256': sha(readback), 'backupSha256': sha(backup),
                    'guards': same, 'targetState': state,
                    'heartbeat': heartbeat, 'before': before_heartbeat}
        if not same:
            self.ctx.uncertain = f'SRAM 恢复后读回与备份不一致（{hex(start)}，{len(backup)} B）'
            return self.fail('S04', self.ctx.uncertain, **evidence)
        if heartbeat is not None and 'error' in (heartbeat or {}):
            return self.fail('S04', '恢复后心跳没有前进', **evidence)
        return self.record('S04', 'pass', evidence=evidence)

    def sram_widths(self, base, start, guard):
        evidence = {'widths': {}, 'unaligned': {}, 'neighbors': {}}
        address = base + 0x100
        for width in (1, 2, 4):
            value = bytes(range(0xF0, 0xF0 + width))
            self.write(address, value)
            back = self.read(address, width)
            evidence['widths'][str(width)] = {'sent': value.hex(), 'read': back.hex(),
                                              'equal': back == value}
        offset = address + 1
        neighbor = self.read(offset - 1, 6)
        self.write(offset, bytes(range(0xA0, 0xA4)))
        after = self.read(offset - 1, 6)
        evidence['unaligned'] = {'address': hex(offset),
                                 'value': after[1:5].hex(),
                                 'expected': 'a0a1a2a3'}
        evidence['neighbors'] = {'before': neighbor.hex(), 'after': after.hex(),
                                 'leadingKept': neighbor[0] == after[0],
                                 'trailingKept': neighbor[-1] == after[-1]}
        bad = [key for key, row in evidence['widths'].items() if not row['equal']]
        if bad:
            return self.fail('S05', f'宽度 {bad} 写后读回不一致', **evidence)
        if (after[1:5] != bytes(range(0xA0, 0xA4)) or
                not (evidence['neighbors']['leadingKept'] and evidence['neighbors']['trailingKept'])):
            return self.fail('S05', '非对齐写入改动了相邻字节', **evidence)
        return self.record('S05', 'pass', evidence=evidence)

    # ==================================================================
    # F 组：Flash
    # ==================================================================
    def flash(self, case):
        self.ctx.flash('烧写目标 Flash')
        fixture = self.fixture('flash')
        image = Path(facts.ROOT) / fixture['path']
        loads = self.fixture_flash_loads(fixture)
        geometry = facts.FLASH[self.board]
        self.match_flash_image(case, loads)
        ranges = facts.flash_segment_ranges(geometry, loads)
        symbols = facts.elf_symbols(image)
        tick_address = next((symbols[name] for name in ('g_ticks', 'g_ms', 'g_updates') if name in symbols), None)
        if not tick_address:
            raise Pending('烧录夹具缺少用于复位和恢复核对的已知心跳')
        self.connect_debug(case)
        if self.target_state()['running'] != 'running':
            raise Pending('烧录验收要求匹配的原应用处于运行态')
        self.wait_heartbeat(tick_address)
        self.disconnect_debug()

        start, stop = ranges[0][0], ranges[-1][1]
        backup = self.read_flash(start, stop - start)
        folder = self.folder('flash')
        (folder / 'flash-backup.bin').write_bytes(backup)
        self.write_json(folder, 'flash-backup.json',
                        {'address': hex(start), 'bytes': len(backup), 'sha256': sha(backup),
                         'geometry': {'base': hex(geometry['base']), 'erase': geometry['erase']},
                         'ranges': ranges})

        results = {}
        try:
            results['F01'] = self.flash_web(case, folder, image, loads)
            results['F02'] = self.flash_readback(folder, loads, backup, start)
            results['F03'] = self.flash_boot(case, 'F03', tick_address)
            if results['F02']['status'] == 'pass' and results['F03']['status'] == 'pass':
                results['F04'] = self.flash_compare(case, folder, image, loads, geometry, ranges,
                                                     start, backup, results['F01']['evidence'])
            else:
                results['F04'] = self.pending('F04', 'Web 独立校验或启动未通过，原生对照不执行')
        except Unsafe:
            raise
        except Exception:
            self.flash_restore(case, folder, backup, start, tick_address)
            raise
        results['F05'] = self.flash_restore(case, folder, backup, start, tick_address)
        return results

    def native_openocd(self, case, folder):
        from tools.board_acceptance_native import NativeOpenOcd

        if not self.ctx.probe_serial or not case.get('requestedHz'):
            raise Pending('原生 OpenOCD 对照需要探针序列号及确定的目标链路时钟')
        return NativeOpenOcd(self.board, self.link, case['requestedHz'], self.ctx.probe_serial, folder)

    def flash_compare(self, case, folder, image, loads, geometry, ranges, start, backup, web):
        from tools.board_acceptance_native import NativeFailure, segments_sha

        declared = sum((stop - first) // geometry['erase'] for first, stop in ranges)
        if type(web.get('sectors')) is not int or web['sectors'] != declared:
            return self.pending('F04', 'Web 擦除的扇区数与稀疏计划不一致',
                                webSeconds=web['seconds'], webSectors=web.get('sectors'), expectedSectors=declared)
        try:
            native = self.native_openocd(case, folder / 'native')
            if self.read_flash(start, len(backup)) != backup:
                return self.pending('F04', 'Web 后 Flash 与原始备份不一致，先恢复原扇区', webSeconds=web['seconds'])
            snapshot = {'image': str(image), 'imageSha256': web['image']['sha256'],
                        'segmentsSha256': segments_sha(loads), 'backupSha256': sha(backup),
                        'backupAddress': start, 'elf': True}
            comparison = native.flash(start, backup, loads, {**geometry, 'ranges': ranges},
                                      snapshot=snapshot, web_seconds=web['seconds'])
        except Pending as error:
            return self.pending('F04', str(error), webSeconds=web['seconds'], ranges=ranges)
        except NativeFailure as error:
            return self.fail('F04', str(error), native=error.evidence, webSeconds=web['seconds'])
        if not comparison.get('verified') or not comparison.get('restored'):
            raise Unsafe('原生 Flash 对照未确认扇区校验和恢复')
        stages = comparison.get('stages') or {}
        reset_seconds = (stages.get('reset') or {}).get('processSeconds')
        program_seconds = (stages.get('program') or {}).get('processSeconds')
        if (not isinstance(reset_seconds, (float, int)) or reset_seconds <= 0 or
                not isinstance(program_seconds, (float, int)) or program_seconds <= 0):
            return self.pending('F04', '原生阶段缺少可与 Web 对齐的复位计时', native=comparison)
        return self.record('F04', 'pass', evidence={'image': web['image'], 'webSeconds': web['seconds'],
                                                     'cliSeconds': program_seconds + reset_seconds,
                                                     'cliProgramSeconds': program_seconds,
                                                     'cliResetSeconds': reset_seconds, 'native': comparison})

    def flash_options(self, case, **options):
        clock = case['requestedHz']
        return {'chip': self.chip_option, 'clockKhz': clock // 1000 if clock else None, **options}

    def flash_web(self, case, folder, image, loads):
        payload = facts.b64(image.read_bytes())
        payload['name'] = image.name
        payload['kind'] = 'file'
        started = time.perf_counter()
        value = self.web.invoke('flash', 'programFile', [payload, self.flash_options(case, reset=True)])
        seconds = time.perf_counter() - started
        self.write_json(folder, 'flash-web.json', {'seconds': seconds, 'result': value})
        evidence = {'image': {'path': str(image), 'sha256': facts.digest(image)['sha256']},
                    'segments': [{'address': hex(a), 'bytes': len(d)} for a, d in loads],
                    'sectors': (value or {}).get('sectors'),
                    'verified': (value or {}).get('verified'),
                    'target': (value or {}).get('chip'),
                    'seconds': seconds}
        checked(value and value.get('verified') is True and value.get('reset') is True and
                value.get('chip') == self.chip_option, 'Web 没有确认本次目标、烧录校验与复位')
        return self.record('F01', 'pass', evidence=evidence)

    def flash_readback(self, folder, loads, backup, start):
        readback = self.read_flash(start, len(backup))
        expected = bytearray(backup)
        verified, content_bytes = 0, 0
        for address, data in loads:
            offset = address - start
            expected[offset:offset + len(data)] = data
            content_bytes += len(data)
            if readback[offset:offset + len(data)] == data:
                verified += len(data)
        mismatch = facts.differ(expected, readback)
        evidence = {'readbackSha256': sha(readback),
                    'verifiedLoadBytes': verified,
                    'sectorPadding': {'bytes': len(backup) - content_bytes,
                                      'matchesBackup': not mismatch},
                    'mismatch': mismatch,
                    'mapping': 'XIP 映射回读' if self.board == 'hpm6800evk' else 'Flash 地址回读'}
        self.write_json(folder, 'flash-readback.json', evidence)
        if mismatch:
            return self.fail('F02', f'有效镜像或扇区未指定字节不一致：{mismatch}', **evidence)
        return self.record('F02', 'pass', evidence=evidence)

    def flash_boot(self, case, instance, tick_address):
        heartbeat = self.check_boot(case, tick_address)
        return self.record(instance, 'pass',
                           evidence={'resetMethod': '烧录器确认复位，独立建链核对目标运行和心跳', 'heartbeat': heartbeat})

    def check_boot(self, case, tick_address):
        self.connect_debug(case)
        try:
            checked(self.target_state()['running'] == 'running', '目标没有在运行')
            return self.wait_heartbeat(tick_address, timeout=6)
        finally:
            self.disconnect_debug()

    def flash_restore(self, case, folder, backup, start, tick_address):
        try:
            readback = self.read_flash(start, len(backup))
            rewritten = readback != backup
            if rewritten:
                erase = facts.FLASH[self.board]['erase']
                for offset in range(0, len(backup), erase):
                    sector = backup[offset:offset + erase]
                    if sector == readback[offset:offset + erase]:
                        continue
                    payload = {**facts.b64(sector), 'kind': 'file', 'name': 'flash-backup-sector.bin'}
                    reply = self.web.invoke('flash', 'programFile',
                                            [payload, self.flash_options(case, base=start + offset, reset=False)])
                    checked(reply and reply.get('verified') is True, '烧录器未确认备份扇区写回')
                readback = self.read_flash(start, len(backup))
            checked(readback == backup, 'Flash 与备份不一致')
            self.connect_debug(case)
            if rewritten:
                self.web.invoke('dbg', 'resetRun', target='session')
            heartbeat = self.check_boot(case, tick_address)
        except Exception as error:
            self.ctx.fail_unsafe('恢复 Flash（保留 flash-backup.bin）', error)
        evidence = {'backupSha256': sha(backup), 'readbackSha256': sha(readback),
                    'rewritten': rewritten, 'heartbeat': heartbeat}
        self.write_json(folder, 'flash-restore.json', evidence)
        return self.record('F05', 'pass', evidence=evidence)

    def connect_debug(self, case):
        state = self.target_state()
        if state['running'] in ('running', 'halted'):
            if not self.owns_debug:
                raise Pending('调试器已有会话，请结束原会话后再执行')
            return
        self.web.set_control('dbg', '#d-backend', 'riscv' if self.riscv else 'webusb')
        self.web.set_control('dbg', '#d-hpm-target' if self.riscv else '#d-arm-target', self.chip_option)
        if case['requestedHz']:
            self.web.set_control('dbg', '#d-clock', str(case['requestedHz'] // 1000))
        result = self.web.invoke('dbg', 'connect')
        self.owns_debug = self.target_state()['running'] in ('running', 'halted')
        checked(result is not False and self.owns_debug, '调试建链未确认')

    def disconnect_debug(self):
        if self.owns_debug:
            self.web.invoke('dbg', 'disconnect')
            checked(self.target_state()['running'] == 'disconnected', '调试会话尚未关闭')
            self.owns_debug = False

    # ==================================================================
    # D 组：调试
    # ==================================================================
    def debug(self, case):
        fixture = self.fixture('debug')
        symbols = fixture['symbols']
        self.match_flash_image(case, self.fixture_flash_loads(fixture))
        payload = {**facts.b64((facts.ROOT / fixture['path']).read_bytes()), 'kind': 'buffer'}
        loaded = self.web.invoke('dbg', 'loadElfBuffer', [payload, Path(fixture['path']).name])
        checked(loaded is not False, '配套 ELF 未载入')
        started = time.perf_counter()
        self.connect_debug(case)
        import_seconds = time.perf_counter() - started
        summary = (self.web.state('dbg') or {}).get('summary') or {}
        if summary.get('targetState') != 'running':
            raise Pending('调试验收需要匹配应用处于运行态')
        results = {}
        try:
            results['D01'] = self.debug_identity(fixture, summary, import_seconds)
            results['D02'] = self.debug_run_stop(symbols)
            results['D03'] = self.debug_source(symbols)
            results['D04'] = self.debug_breakpoints(symbols)
            results['D05'] = self.debug_speed(import_seconds)
        except Unsafe:
            raise
        except Exception:
            self.restore_debug_running()
            raise
        self.restore_debug_running()
        return results

    def restore_debug_running(self):
        try:
            if self.target_state()['halted']:
                self.web.invoke('dbg', 'run', target='session')
            checked(self.target_state()['running'] == 'running', '调试结束后目标运行态无法确认')
        except Exception as error:
            self.ctx.fail_unsafe('恢复调试运行状态', error)

    def debug_identity(self, fixture, summary, seconds):
        target = {'backend': summary.get('backend'), 'connected': summary.get('connected'),
                  'clockKhz': summary.get('clockKhz'), 'regs': summary.get('regs'),
                  'pc': hex(summary.get('pc') or 0)}
        identity = summary.get('idcode')
        evidence = {'cfg': summary.get('backend'), 'transport': self.link,
                    'identity': identity, 'clock': target,
                    'elf': {'path': fixture['path'], 'sha256': fixture['sha256']},
                    'connectSeconds': seconds}
        if not summary.get('connected'):
            return self.fail('D01', '建链后页面仍报未连接', **evidence)
        expected = self.profile['clock'].get('expect', {}).get(
            'debug' if 'debug' in self.profile['clock'].get('expect', {}) else 'rtt')
        if expected:
            try:
                measured = self.clock('debug')
                verdict = facts.verify_target_clock(measured, *expected)
            except facts.Pending as error:
                return self.pending('D01', str(error), **evidence)
            evidence['targetClock'] = verdict
        if not identity or identity == 0xffffffff or not summary.get('elf'):
            return self.pending('D01', '缺少实际目标 IDCODE 或已载入 ELF 标识', **evidence)
        return self.record('D01', 'pass', evidence=evidence)

    def debug_run_stop(self, symbols):
        halt = []
        for _ in range(3):
            started = time.perf_counter()
            self.web.invoke('dbg', 'runLine', ['halt'])
            halt.append(time.perf_counter() - started)
        state = self.target_state()
        pcs = []
        steps = []
        for _ in range(3):
            started = time.perf_counter()
            self.web.invoke('dbg', 'runLine', ['step'])
            steps.append(time.perf_counter() - started)
            after = self.target_state()
            pcs.append({'pc': after.get('pc'), 'halted': after.get('halted')})
        self.web.invoke('dbg', 'runLine', ['c'])
        time.sleep(0.2)
        resumed = self.target_state()
        evidence = {'halt': {'medianSeconds': median(halt), 'rounds': [round(x, 4) for x in halt]},
                    'halted': state, 'steps': steps and pcs,
                    'stepSeconds': median(steps), 'resume': resumed}
        if not (state.get('halted') or state.get('targetState') == 'halted'):
            return self.fail('D02', '暂停后页面未确认停机', **evidence)
        if any(row.get('pc') is None for row in pcs):
            return self.pending('D02', '单步后没有读到 PC', **evidence)
        return self.record('D02', 'pass', evidence=evidence)

    def debug_source(self, symbols):
        entry = symbols.get('entry')
        if not entry:
            raise Pending('debug 夹具没有登记入口函数符号')
        evidence = {'entry': hex(entry), 'into': None, 'over': None, 'out': None,
                    'backtrace': None}
        try:
            into = self.web.invoke('dbg', 'stepInto', [])
            over = self.web.invoke('dbg', 'stepOver', [])
            out = self.web.invoke('dbg', 'stepOut', [])
            frames = self.web.invoke('dbg', 'runLine', ['bt'])
        except Exception as error:
            return self.pending('D03', f'源码步进接口不可用：{error}', **evidence)
        summary = (self.web.state('dbg') or {}).get('summary') or {}
        evidence.update(into=into, over=over, out=out, backtrace=(frames or {}).get('text'),
                        source=summary.get('src'))
        if not (summary.get('src') or {}).get('ready'):
            return self.pending('D03', '页面没有源码索引，无法核对停点所在行', **evidence)
        return self.record('D03', 'pass', evidence=evidence)

    def debug_breakpoints(self, symbols):
        entry = symbols.get('entry')
        if not entry:
            raise Pending('debug 夹具没有登记入口函数符号')
        folder = self.folder('debug')
        before = self.web.invoke('dbg', 'bpList', [], target='session') or []
        self.web.invoke('dbg', 'bpAdd', [entry, 'acceptance'], target='session')
        installed = self.web.invoke('dbg', 'bpList', [], target='session') or []
        self.write_json(folder, 'breakpoints.json',
                        {'before': before, 'installed': installed})
        # 命中：复位运行到入口（runTo 走断点/单步兜底，由页面实现决定）
        hit = None
        try:
            hit = self.web.invoke('dbg', 'runTo', [entry], target='session')
        except Exception as error:
            hit = {'error': str(error)}
        state = self.target_state()
        self.web.invoke('dbg', 'bpDel', [entry], target='session')
        after = self.web.invoke('dbg', 'bpList', [], target='session') or []
        evidence = {'breakpoints': {'before': before, 'installed': installed, 'after': after},
                    'hit': hit, 'pc': state.get('pc'), 'halted': state.get('halted'),
                    'slots': (self.web.state('dbg') or {}).get('summary', {}).get('bpCap'),
                    'removed': not any((bp.get('addr') if isinstance(bp, dict) else bp) == entry
                                       for bp in after)}
        if not evidence['removed']:
            self.ctx.uncertain = f'断点删除后仍登记在 {hex(entry)}'
            return self.fail('D04', self.ctx.uncertain, **evidence)
        if state.get('pc') != entry:
            return self.pending('D04', f'断点未在预期地址命中（PC={state.get("pc")}）',
                                **evidence)
        return self.record('D04', 'pass', evidence=evidence)

    def debug_speed(self, connect_seconds):
        halts, refreshes = [], []
        for _ in range(5):
            started = time.perf_counter()
            self.web.invoke('dbg', 'runLine', ['halt'])
            halts.append(time.perf_counter() - started)
            started = time.perf_counter()
            self.web.invoke('dbg', 'refreshAll', [])
            refreshes.append(time.perf_counter() - started)
        evidence = {'connectSeconds': connect_seconds, 'haltSeconds': median(halts),
                    'refreshSeconds': median(refreshes), 'rounds': len(halts),
                    'note': '三项均为本机实测；没有批准上限，故只作基线'}
        return self.record('D05', 'pending',
                           reason='交互耗时没有批准上限，本轮只登记基线',
                           evidence=evidence)

    # ==================================================================
    # V 组：RTT Viewer
    # ==================================================================
    def rtt_viewer(self, case):
        fixture = self.fixture('rtt')
        self.match_flash_image(case, self.fixture_flash_loads(fixture))
        mode = {'swd': 'swd', 'jtag': 'riscv'}[self.link]
        self.web.set_control('rtt', '#r-target', mode)
        self.web.set_control('rtt', '#r-chip', self.chip_option)
        if case['requestedHz']:
            self.web.set_control('rtt', '#r-usb-clock', str(case['requestedHz'] // 1000))
        self.web.set_control('rtt', '#r-record-auto', False)
        self.web.set_control('rtt', '#r-poll', '5')
        checked(self.web.invoke('rtt', 'loadElfFile', [self._elf_file(fixture)]) is True,
                'RTT Viewer 未接受配套 ELF')
        self.web.click('rtt', '#r-clear')
        try:
            self.web.invoke('rtt', 'connectProbe', [])
            results = self.rtt_viewer_window(case, fixture)
        finally:
            self.disconnect_viewer()
        results.update(self.viewer_ring_checks(case, fixture))
        return results

    def disconnect_viewer(self):
        try:
            self.web.invoke('rtt', 'disconnect')
            checked(self.web.state('rtt')['summary'].get('connected') is False, 'Viewer 尚未释放探针')
        except Exception as error:
            self.ctx.fail_unsafe('关闭 RTT Viewer', error)

    def viewer_ring_checks(self, case, fixture):
        ids = ('V01', 'V02', 'V04')
        serial = self.ctx.probe_serial
        if not serial:
            return {instance: self.pending(instance, '环尾夹具需要 --probe-serial 配对独立回读')
                    for instance in ids}
        size = len(rtt_evidence.viewer_fixture(0)[0])
        try:
            address = rtt_evidence.boundary_address(self.board, facts.ROOT / fixture['path'], size)
        except ValueError as error:
            raise Pending(str(error)) from error
        image, _, payload = rtt_evidence.viewer_fixture(address)
        folder = self.folder('viewer')
        previous = {selector: self.web.read('rtt', selector)['value']
                    for selector in ('#r-addr', '#r-hexsend', '#r-eol', '#r-tx')}
        try:
            with self.rtt_scratch(case, serial, address, image, folder):
                self.web.set_control('rtt', '#r-addr', hex(address))
                self.web.click('rtt', '#r-clear')
                try:
                    self.web.invoke('rtt', 'connectProbe')
                    summary = wait_for(lambda: self.web.state('rtt')['summary'],
                                       lambda state: state.get('controlBlock') == address and
                                       state.get('stats', {}).get('bytes', 0) >= len(payload),
                                       10, 'Viewer 有限上行载荷未收齐')
                    checked((summary.get('probeSerial') or '').casefold() == serial.casefold(),
                            'Viewer 探针与独立回读探针不一致')
                    checked(not summary['stats'].get('lost') and not summary['stats'].get('corrupt'),
                            'Viewer 报告上行丢失或损坏')
                    raw = self.viewer_raw()
                    (folder / 'up.bin').write_bytes(raw)
                    self.web.set_control('rtt', '#r-hexsend', True)
                    self.web.set_control('rtt', '#r-eol', 'none')
                    self.web.set_control('rtt', '#r-tx', rtt_evidence.VIEWER_DOWN.hex(' '))
                    self.web.click('rtt', '#r-send')
                finally:
                    self.disconnect_viewer()
                self.connect_debug(case)
                state = self.web.state('dbg')['summary']
                checked((state.get('probeSerial') or '').casefold() == serial.casefold(),
                        'Viewer 跨尾读回所用探针不一致')
                (folder / 'ring-after.bin').write_bytes(self.read(address - 64, size + 128))
        finally:
            if not self.ctx.uncertain:
                for selector, value in previous.items():
                    self.web.set_control('rtt', selector, value)
        files = {'before': 'sram-before.bin', 'restored': 'sram-after.bin',
                 'readback': 'ring-after.bin', 'upstream': 'up.bin'}
        artifacts = {key: {'path': (folder / name).relative_to(self.out).as_posix(),
                           'sha256': sha((folder / name).read_bytes())} for key, name in files.items()}
        proof = rtt_evidence.check_viewer_fixture(
            address, *((folder / name).read_bytes() for name in files.values()))
        evidence = {**proof, 'lost': summary['stats']['lost'], 'corrupt': summary['stats']['corrupt'],
                    'probeSerial': serial, 'artifacts': artifacts}
        return {instance: self.record(instance, 'pass', evidence=evidence.copy()) for instance in ids}

    def rtt_viewer_window(self, case, fixture):
        symbols = fixture['symbols']
        wait_for(lambda: ((self.web.state('rtt') or {}).get('summary') or {}).get('stats', {})
                 .get('bytes', 0), lambda value: value > 0, 10,
                 'RTT Viewer 在超时内没有收到任何字节')
        first = self.web.state('rtt')['summary']['stats']
        started = time.perf_counter()
        time.sleep(2)
        summary = (self.web.state('rtt') or {}).get('summary') or {}
        elapsed = time.perf_counter() - started
        stats = summary.get('stats') or {}
        control = summary.get('controlBlock')
        expected = symbols.get('rtt')
        evidence = {'actualAddress': control, 'elfSymbol': hex(expected) if expected else None,
                    'bytes': stats.get('bytes'), 'lost': stats.get('lost'),
                    'corrupt': stats.get('corrupt'), 'rate': stats.get('rate'),
                    'clockKhz': summary.get('clockKhz'), 'ringBytes': summary.get('ringBytes')}
        serial = self.ctx.probe_serial
        if serial:
            checked((summary.get('probeSerial') or '').casefold() == serial.casefold(),
                    'Viewer 吞吐窗口所用探针与 --probe-serial 不一致')
        results = {}
        if not expected or control != expected or not summary.get('running') or summary.get('backend') != 'openocd':
            results['V05'] = self.record('V05', 'fail', evidence=evidence)
        else:
            results['V05'] = self.record('V05', 'pass', evidence=evidence)
        results['V03'] = self.rtt_viewer_rate(case, stats, summary, first, elapsed)
        return results

    def viewer_raw(self):
        existing = {entry['id'] for entry in self.web.downloads('rtt')}
        status = self.web.invoke('rtt', 'saveRaw')
        entries = [entry for entry in self.web.downloads('rtt') if entry['id'] not in existing]
        checked(len(entries) == 1, '未确认本轮 Viewer 原始导出')
        raw = self.web.fetch_download('rtt', entries[0])
        checked(status.get('bytes') == len(raw) and status.get('truncated') is False, 'Viewer 原始导出不完整')
        return raw

    def rtt_viewer_rate(self, case, stats, summary, first, seconds):
        count = stats['bytes'] - first['bytes']
        rate = count / seconds
        polls = stats['polls'] - first['polls']
        baselines = RATE_BASELINES.get((self.board, self.link, 'rtt-viewer-fixed-text'))
        verdict = facts.rate_verdict(baselines, case['requestedHz'],
                                     (rate or 0) / (1 << 20), 'Viewer 上行')
        evidence = {'bytes': count, 'seconds': seconds,
                    'bytesPerSecond': rate, 'polls': polls,
                    'ringBytes': summary.get('ringBytes'), 'pollingMs': summary.get('pollingMs'),
                    'displaySuppressed': summary.get('displaySuppressed'),
                    'baseline': verdict}
        if count <= 0 or polls <= 0 or not summary.get('ringBytes') or stats.get('lost') or stats.get('corrupt'):
            return self.fail('V03', 'Viewer 性能窗口缺少有效进度、环大小或数据完整性证据', evidence=evidence)
        return self.record('V03', 'pending' if verdict['status'] == 'pending' else verdict['status'],
                           reason=verdict.get('reason'), evidence=evidence)

    # ==================================================================
    # R 组：RTT 转发
    # ==================================================================
    def rtt_forward(self, case):
        serial = self.ctx.probe_serial
        if not serial:
            raise Pending('RTT 转发执行需要 --probe-serial 绑定 HID 与 CDC')
        try:
            from serial.tools.list_ports import comports
            port = rtt_evidence.probe_port(comports(), serial)
        except (ImportError, ValueError) as error:
            raise Pending(str(error)) from error
        self.open_rtt_hid(serial)
        checked(self.web.invoke('rttcdc', 'disconnect', target='hid') is True, 'RTT 预检后 HID 未释放')
        kind, fixture, wire_pattern = self.match_rtt_fixture(case)
        address = fixture['symbols']['rtt']
        folder = self.folder('rtt-forward')
        peer = {'probeSerial': serial, 'com': port, 'targetElf': kind,
                'elf': {'path': fixture['path'], 'sha256': fixture['sha256']}}
        seconds = (rtt_evidence.RTT_SEQUENCE_SECONDS[self.board] if wire_pattern == 'seq'
                   else rtt_evidence.RTT_FLOOD_SECONDS)
        before = self.rtt_ring(case, serial, address)
        captures = ((0, seconds), (0, rtt_evidence.RTT_FLOOD_SECONDS))
        web_windows, host_data = self.rtt_web_windows(case, serial, address, folder, captures)
        after = self.rtt_ring(case, serial, address)
        for row, raw in zip(web_windows, host_data):
            row['payload'] = rtt_evidence.check_payload(raw, wire_pattern)
            checked(row['payload']['exact'], 'Web 串口载荷不连续')
        whole = b''.join(host_data)
        total_stop = {**web_windows[-1]['stop'], 'moved': sum(row['stop']['moved'] for row in web_windows)}
        web_counts = rtt_evidence.check_window(before, after, len(whole), total_stop)
        web_stop = web_windows[-1]['stop']
        native = self.rtt_native_window(case, peer, address, web_stop, wire_pattern, folder, seconds)
        native_after = self.rtt_ring(case, serial, address)
        native_data = (folder / 'native' / 'capture.bin').read_bytes()
        native_counts = rtt_evidence.check_window(after, native_after, len(native_data), native['stop'])
        native['payload'] = rtt_evidence.check_payload(native_data, wire_pattern)
        checked(native['payload']['exact'], '原生串口载荷不连续')
        evidence = {**peer, 'ring': before, 'web': web_windows, 'cli': native,
                    'ringAfterWeb': after, 'ringAfterNative': native_after,
                    'webCounts': web_counts, 'nativeCounts': native_counts, 'format': wire_pattern}
        proof = self.write_json(folder, 'comparison.json', evidence)
        peer['captureEvidence'] = {'path': proof.relative_to(self.out).as_posix(), 'sha256': facts.digest(proof)['sha256']}

        results = {}
        sequence = rtt_evidence.check_payload(whole, wire_pattern)
        if wire_pattern == 'seq':
            checked(sequence['exact'], 'Web 重开前后的原始序号不连续')
            combined = rtt_evidence.sequence_run(self.board, web_windows[0]['seconds'], native['seconds'],
                                                 whole, native_data)
            results['R01'] = self.record('R01', 'pass', evidence={**peer, 'sequence': combined,
                                         'webSeconds': web_windows[0]['seconds'], 'cliSeconds': native['seconds']})
        else:
            results['R01'] = self.pending('R01', '此 ELF 是固定文本夹具，连续序号用例需序号夹具', **peer)
        if wire_pattern == 'hello':
            baseline = RATE_BASELINES.get((self.board, self.link, 'rtt-forward-fixed-text'))
            measured = min(median(row['bytesPerSecond'] for row in web_windows), native['bytesPerSecond'])
            verdict = facts.rate_verdict(baseline, case['requestedHz'], measured / 1_000_000, 'RTT 转发吞吐')
            results['R02'] = self.record('R02', verdict['status'], reason=verdict.get('reason'),
                                         evidence={**evidence, **peer, 'baseline': verdict,
                                                   'matchedWindowSeconds': seconds})
        else:
            results['R02'] = self.pending('R02', '吞吐摸高使用同板固定文本夹具', **peer)
        results['R03'] = self.record('R03', 'pass', evidence={**peer, 'web': web_counts, 'cli': native_counts,
                                     'portReturned': True})
        try:
            boundary = self.rtt_boundary(case, serial, fixture, folder / 'boundaries')
        except Pending as error:
            boundary = None
            reason = str(error)
        if wire_pattern == 'seq' or boundary is not None:
            results['R04'] = self.record('R04', 'pass', evidence={**peer, 'staleBytes': 0,
                                         'sameSession': True, 'joinedSequence': sequence, 'boundaryRestart': boundary})
        else:
            results['R04'] = self.pending('R04', '同页已连续起停，固定文本无法排除完整旧记录重发', **peer)
        results['R05'] = (self.record('R05', 'pass', evidence={**peer, 'boundaries': boundary})
                          if boundary is not None else self.pending('R05', reason, **peer))
        return results

    def match_rtt_fixture(self, case):
        variants = [('rtt', 'hello'), ('rtt-seq', 'seq')]
        for kind, wire_pattern in variants:
            try:
                fixture = self.fixture(kind)
                self.match_flash_image(case, self.fixture_flash_loads(fixture))
            except Pending:
                continue
            return kind, fixture, wire_pattern
        raise Pending('目标 Flash 与已登记的 RTT 夹具均不一致')

    def rtt_ring(self, case, serial, address):
        self.connect_debug(case)
        try:
            state = self.web.state('dbg')['summary']
            if (state.get('probeSerial') or '').casefold() != serial.casefold():
                raise Pending('目标独立回读所用 USB 与 RTT 转发探针不一致')
            if state.get('targetState') != 'running':
                raise Pending('RTT 上行测速需要运行中的目标')
            return rtt_evidence.ring_state(self.read(address, 48), address)
        finally:
            self.disconnect_debug()

    def rtt_web_windows(self, case, serial, address, folder, captures=((0, 8), (0, 8))):
        page = 'rttcdc'
        self.web.page(page, 'show')
        self.open_rtt_hid(serial)
        windows, data = [], []
        try:
            self.open_rtt_serial(case, address)
            for index, (channel, seconds) in enumerate(captures, 1):
                self.web.set_control(page, '#h-chan', str(channel))
                existing = {entry['id'] for entry in self.web.downloads(page)}
                window = self.web.invoke(page, 'captureForwardWindow', [{'seconds': seconds, 'maxMiB': 64, 'drainMs': 4000}])
                entries = [entry for entry in self.web.downloads(page)
                           if entry['id'] not in existing and entry.get('name') == window.get('name')]
                checked(len(entries) == 1, 'Web RTT 原始下载缺失或重复')
                raw = self.web.fetch_download(page, entries[0])
                self.web.page(page, 'discard', id=entries[0]['id'])
                (folder / f'web-{index}.bin').write_bytes(raw)
                window['sha256'] = sha(raw)
                self.write_json(folder, f'web-{index}.json', window)
                stop = window.get('stop') or {}
                start = window.get('start') or {}
                expected_type = {'swd': 0, 'jtag': 1}[self.link]
                checked(window.get('complete') and window.get('canRestart') and
                        window.get('seconds', 0) >= seconds - rtt_evidence.WINDOW_CLOCK_TOLERANCE and
                        (seconds < 1 or window.get('bytesPerSecond', 0) > 0) and
                        window.get('bytes') == len(raw) == stop.get('moved') and
                        stop.get('running') is False and stop.get('rdErr') == stop.get('wrErr') == 0 and
                        start.get('cbAddr') == address and start.get('channel') == channel and
                        start.get('targetType') == stop.get('targetType') == expected_type,
                        'Web RTT STOP、控制块或尾部计数未确认')
                windows.append(window)
                data.append(raw)
            checked(len({(row['stop'].get('chunkBytes'), row['stop'].get('swdMhz')) for row in windows}) == 1,
                    '两次 Web 转发的搬运块或时钟变化')
        except Unsafe as error:
            self.ctx.uncertain = str(error)
            raise
        finally:
            if not getattr(self.ctx, 'uncertain', None):
                try:
                    closed = self.web.invoke(page, 'disconnect', target='hid')
                    checked(closed is True, 'Web HID 停止或释放未确认')
                    self.web.invoke(page, 'close', target='session')
                    checked(not self.web.invoke(page, 'summary')['open'], 'Web 串口未释放')
                except Exception as error:
                    self.ctx.fail_unsafe('释放 RTT 转发', error)
        return windows, data

    def open_rtt_serial(self, case, address):
        page = 'rttcdc'
        self.web.invoke(page, 'refreshPorts')
        choices = [p for p in self.web.invoke(page, 'forwardPortOptions')
                   if (p['vendorId'], p['productId']) == (0x0d28, 0x0204)]
        if len(choices) != 1:
            raise Pending('浏览器须已有唯一授权的 akaLinkPro CDC 端口')
        mode = {'swd': 'swd', 'jtag': 'riscv'}[self.link]
        controls = {'#h-target': mode, '#h-addr': hex(address), '#h-chan': '0',
                    '#c-port': str(choices[0]['index']), '#c-record-auto': False}
        if self.link == 'swd' and case['requestedHz']:
            controls['#h-clock'] = str(case['requestedHz'])
        for selector, value in controls.items():
            self.web.set_control(page, selector, value)
        self.web.invoke(page, 'connect')
        checked(self.web.invoke(page, 'summary')['open'], 'Web Serial 打开失败')

    def open_rtt_hid(self, serial):
        page = 'rttcdc'
        previous = self.web.invoke(page, 'summary', target='hid')
        if previous.get('connected'):
            raise Pending('工作台已有 HID 会话，执行须使用独立、空闲的会话')
        if not self.web.invoke(page, 'reconnect', [{'silent': True}], target='hid'):
            raise Pending('未能复用已授权 HID')
        try:
            state = self.web.invoke(page, 'refresh', [{'strict': True}], target='hid')
        except Exception as error:
            self.ctx.fail_unsafe('RTT 探针空闲状态', error)
        # -100 also describes a probe that has never started a transfer.
        if not state or state.get('running') is not False or state.get('startRc') is None:
            self.ctx.fail_unsafe('RTT 探针空闲状态', '探针仍有活动或未确认的转发')
        if (state.get('probeSerial') or '').casefold() != serial.casefold():
            self.web.invoke(page, 'disconnect', target='hid')
            raise Pending('已授权工作台 HID 与 --probe-serial 不一致')

    def rtt_native_window(self, case, peer, address, web_stop, wire_pattern, folder, seconds):
        chunk = web_stop.get('chunkBytes')
        checked(type(chunk) is int and 64 <= chunk <= 4096, 'Web 搬运块未读回')
        clock = 0 if self.riscv else (web_stop.get('swdMhz') or 0) * 1_000_000
        checked(self.riscv or clock > 0, 'Web 探针时钟档未读回')
        command = [sys.executable, '-X', 'utf8', str(facts.ROOT / 'tools/validate_f103_streams.py'),
                   'rtt', '--out', str(folder / 'native'), '--probe-serial', peer['probeSerial'],
                   '--com', peer['com'], '--target', self.link, '--address', hex(address),
                   '--speed', str(clock), '--chunk', str(chunk), '--pattern', wire_pattern, '--seconds', str(seconds)]
        try:
            with (folder / 'native.log').open('wb') as stream:
                process = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=seconds + 32)
            path = folder / 'native' / 'result.json'
            result = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            self.ctx.fail_unsafe('原生 RTT 进程状态和 STOP', error)
        if not result or result.get('stopError') or result.get('targetRestored') is not True:
            raise Unsafe('原生 RTT STOP 或探针模式恢复未确认')
        stop = result.get('rttStop') or {}
        checked(process.returncode == 0 and result['serial'].casefold() == peer['probeSerial'].casefold() and
                result['com'].casefold() == peer['com'].casefold() and
                result['seconds'] >= seconds - rtt_evidence.WINDOW_CLOCK_TOLERANCE and
                (stop.get('chunkBytes'), stop.get('targetType'), stop.get('swdMhz')) ==
                (chunk, web_stop.get('targetType'), web_stop.get('swdMhz')) and
                result.get('rttStart') == {'running': True, 'cbAddr': address, 'channel': 0,
                                           'targetType': web_stop.get('targetType')},
                '原生 RTT 与 Web 的设备、通道或时序不一致')
        return {'stop': {**stop, 'rdErr': stop['readErrors'], 'wrErr': stop['writeErrors']},
                'bytesPerSecond': result['activeBytes'] / result['seconds'], 'seconds': result['seconds'],
                'sha256': result['sha256'], 'serial': result['serial'], 'com': result['com']}

    def rtt_boundary(self, case, serial, fixture, folder):
        image_size = len(rtt_evidence.boundary_fixture(0, RTT_USB_PACKET)[0])
        try:
            address = rtt_evidence.boundary_address(self.board, facts.ROOT / fixture['path'], image_size)
        except ValueError as error:
            raise Pending(str(error)) from error
        image, payloads = rtt_evidence.boundary_fixture(address, RTT_USB_PACKET)
        folder.mkdir(parents=True, exist_ok=False)
        with self.rtt_scratch(case, serial, address, image, folder) as guard:
            captures = tuple((channel, .35) for channel in range(len(payloads)))
            windows, raw = self.rtt_web_windows(case, serial, address, folder, captures)
            self.connect_debug(case)
            actual = self.read(address - 64, len(image) + 128)
            checked(actual[:64] == guard[:64] and actual[-64:] == guard[-64:], '边界夹具 SRAM 哨兵已修改')
            rows = []
            for channel, payload in enumerate(payloads):
                checked(raw[channel] == payload, f'{len(payload)} B 原始载荷不一致')
                length = 48 + 24 * channel
                before = rtt_evidence.ring_state(image[:length], address, channel)
                after = rtt_evidence.ring_state(actual[64:64 + length], address, channel)
                counts = rtt_evidence.check_window(before, after, len(payload), windows[channel]['stop'])
                checked(counts['ringLeft'] == 0, '有限 RTT 窗仍有未搬运目标字节')
                rows.append({'channel': channel, 'bytes': len(payload), 'sha256': sha(payload), **counts})
        summary = self.write_json(folder, 'comparison.json', {'rows': rows, 'restored': True, 'address': address})
        return {'path': summary.relative_to(self.out).as_posix(), 'sha256': facts.digest(summary)['sha256'], 'rows': rows}

    @contextmanager
    def rtt_scratch(self, case, serial, address, image, folder):
        self.connect_debug(case)
        state = self.web.state('dbg')['summary']
        if (state.get('probeSerial') or '').casefold() != serial.casefold() or state.get('targetState') != 'running':
            raise Pending('RTT 边界夹具写入要求配对 USB 与运行中的目标')
        backup = None
        start = address - 64
        try:
            self.web.invoke('dbg', 'halt', target='session')
            checked(self.target_state()['halted'], '边界夹具写前目标未停机')
            if self.board == 'h743' and self.web.word(0xe000ed14) & (1 << 16):
                raise Pending('H743 SRAM 夹具需要目标 D-cache 关闭')
            sp = self.web.invoke('dbg', 'readReg', ['sp'], target='session')
            if type(sp) is not int or (self.riscv and start - 4096 <= sp <= address + len(image) + 64 or
                                      not self.riscv and not address + len(image) + 4096 < sp <= facts.SRAM_REGION[self.board][1]):
                raise Pending('目标 SP 与预计边界夹具安全区不匹配')
            backup = self.read(start, len(image) + 128)
            (folder / 'sram-before.bin').write_bytes(backup)
            self.write(address, image)
            checked(self.read(address, len(image)) == image, '有限 RTT 夹具写后读回不符')
            self.restore_debug_running()
            self.disconnect_debug()
            yield backup
        except Unsafe as error:
            self.ctx.uncertain = str(error)
            raise
        finally:
            if not self.ctx.uncertain:
                try:
                    if backup is not None:
                        self.connect_debug(case)
                        state = self.web.state('dbg')['summary']
                        checked((state.get('probeSerial') or '').casefold() == serial.casefold(), '恢复所用探针不一致')
                        self.web.invoke('dbg', 'halt', target='session')
                        checked(self.target_state()['halted'], '恢复边界夹具前目标未停机')
                        self.write(start, backup)
                        restored = self.read(start, len(backup))
                        checked(restored == backup, '恢复边界夹具 SRAM 后独立回读不符')
                        (folder / 'sram-after.bin').write_bytes(restored)
                    self.restore_debug_running()
                    self.disconnect_debug()
                except Exception as error:
                    self.ctx.fail_unsafe('恢复 RTT 边界夹具', error)

    # ==================================================================
    # J 组：J-Scope（页面公开方法 + 原生 scope_hss_test 取证）
    # ==================================================================
    #
    # 判据来源：本仓 scope 页面（protocol.js / view.js）与夹具契约
    # （stm32f103_scope / stm32h743_scope / hpm6800evk_scope 的 g_tick 契约）。
    # 所有结论都来自页面真实公开方法与真实导出，缺字段一律报缺口，不填 pass。
    J_ONE = ('word0',)
    J_EIGHT = tuple(f'word{index}' for index in range(8))

    def jscope(self, case):
        fixture = self.fixture('scope')
        symbols = fixture['symbols']
        folder = self.folder('jscope')
        results = {}
        prepare = self._scope_prepare(fixture, case)
        if prepare.get('status') != 'ready':
            for instance in ('J01', 'J02', 'J03', 'J04', 'J05'):
                results[instance] = self.pending(instance, prepare['reason'],
                                                 evidence=prepare.get('evidence', {}))
            return results
        one = dict(zip(self.J_ONE, scope.sweep_addresses(self.board, symbols, 1)))
        single = self._scope_channel_run('one', self.J_ONE, one, case, folder)
        results['J01'] = single['definition']
        results['J02'] = single['stop']
        results['J03'] = single['sweep']
        export_window = single.get('longWindow')
        if export_window and export_window['summary'].get('samples', 0) > 500_000:
            period = results['J03']['evidence']['chosenPeriodUs']
            export_window = self._scope_window(period, min(2, SCOPE_SHORT_WINDOW_SECONDS), 'web')
        results['J05'] = self._scope_export_check(export_window, self.J_ONE, folder)
        eight_addresses = scope.sweep_addresses(self.board, symbols, 8)
        eight = self._scope_channel_run('eight', self.J_EIGHT, dict(zip(self.J_EIGHT, eight_addresses)), case, folder)
        results['J04'] = eight['sweep']
        return results

    # -- 前置 ------------------------------------------------------------
    def _scope_prepare(self, fixture, case):
        """选目标类型、载入匹配 ELF、打开原始包记录。"""
        link_value = {'swd': 'swd', 'jtag': 'riscv'}.get(self.link)
        if not link_value:
            return {'status': 'pending', 'reason': f'未登记的链路：{self.link}'}
        try:
            self.web.set_control('scope', '#sc-target', link_value)
            if case['requestedHz'] and not self.riscv:
                self.web.set_control('scope', '#sc-clock', str(case['requestedHz'] // 1000))
            payload = self._elf_file(fixture)
            self.web.invoke('scope', 'loadElfFile', [payload, None])
            self.web.set_control('scope', '#sc-raw', True)
            self.web.set_control('scope', '#sc-batch', True)
            self.web.set_control('scope', '#sc-cdcoff', True)
            self.web.invoke('scope', 'clearTrigger')
        except Unsafe:
            raise
        except Exception as error:
            return {'status': 'pending', 'reason': f'J-Scope 准备失败：{error}'}
        summary = self.web.state('scope').get('summary') or {}
        evidence = {'target': link_value, 'elf': {'path': fixture['path'],
                                                  'sha256': fixture['sha256']},
                    'elfState': summary.get('elf'), 'mode': summary.get('mode')}
        if summary.get('mode') == 'mock':
            return {'status': 'pending',
                    'reason': 'J-Scope 当前是假探针（mock），不能作为验收证据',
                    'evidence': evidence}
        if not summary.get('elf'):
            return {'status': 'pending', 'reason': 'ELF 没有载入到 J-Scope 页面',
                    'evidence': evidence}
        self.write_json(self.folder('jscope'), 'prepare.json', evidence)
        return {'status': 'ready', 'evidence': evidence}

    def _elf_file(self, fixture):
        payload = facts.b64((facts.ROOT / fixture['path']).read_bytes())
        payload['kind'] = 'file'
        payload['name'] = Path(fixture['path']).name
        return payload

    def _scope_select(self, roles, symbols, scalars=None):
        """按角色勾选通道：先清空再逐个打开（1 个 vs 8 个不同符号）。"""
        current = (self.web.state('scope').get('summary') or {}).get('vars') or []
        for entry in current:
            name, _, kind_addr = str(entry).rpartition(':')
            scalar, _, address = kind_addr.partition('@')
            self.web.invoke('scope', 'toggleVar', [{'name': name, 'addr': int(address, 0),
                                                    'scalar': scalar}, False])
        for role, scalar in zip(roles, scalars or ('u32',) * len(roles)):
            address = symbols.get(role)
            checked(address is not None, f'夹具缺少符号角色 {role}')
            self.web.invoke('scope', 'toggleVar', [{'name': role, 'addr': address,
                                                    'scalar': scalar, 'size': 4}, True])
        summary = self.web.state('scope').get('summary') or {}
        return {'requested': list(roles), 'selected': summary.get('vars'),
                'count': len(roles)}

    # -- 一个通道组合 ----------------------------------------------------
    def _scope_channel_run(self, label, roles, symbols, case, folder):
        scalars = scope.sweep_scalars(self.board, len(roles))
        selection = self._scope_select(roles, symbols, scalars)
        addresses = sorted(symbols[role] for role in roles)
        faster, reference, slower = scope.PERIODS[(self.board, self.link, len(roles))]
        rows = []

        def short(ticks):
            period_us = scope.us_for_ticks(ticks)
            window = self._scope_window(period_us, SCOPE_SHORT_WINDOW_SECONDS, 'web')
            row = self._scope_judge(window, period_us)
            row['periodUs'] = period_us
            rows.append(row)
            self.write_json(folder, f'{label}-sweep.json', {'selection': selection,
                                                            'periods': rows})
            return row['clean']

        if short(reference):
            short(faster)
        else:
            short(slower)
        definition = self._scope_definition_row(label, roles, symbols, selection) if label == 'one' else None
        candidates = sorted(row['requestedTicks'] for row in rows if row['clean'])
        if not candidates:
            sweep = self.record('J03' if label == 'one' else 'J04', 'fail',
                                reason='所有短窗周期都有计数问题，没有合格档',
                                evidence={'selection': selection, 'periods': rows})
            return {'definition': definition, 'stop': self._scope_stop_row(rows[-1]) if label == 'one' else None,
                    'sweep': sweep, 'longWindow': None}
        long_attempts = []
        for ticks in candidates + ([slower] if slower not in candidates else []):
            if ticks not in candidates and not short(ticks):
                break
            chosen = scope.us_for_ticks(ticks)
            long_window = self._scope_window(chosen, SCOPE_WINDOW_SECONDS, 'web')
            long_row = self._scope_judge(long_window, chosen)
            self._scope_raw_evidence(long_row, addresses, scalars, folder / f'{label}-web-{ticks}.jsp')
            long_attempts.append(long_row)
            if long_row['clean']:
                break
        native_row = (self._scope_native_row(roles, symbols, scope.us_for_ticks(reference), case, folder, label)
                      if long_row['clean'] else {'status': 'pending', 'clean': False})
        self.write_json(folder, f'{label}-long.json',
                        {'chosenPeriodUs': chosen, 'web': long_row, 'native': native_row, 'selection': selection})
        instance = 'J03' if label == 'one' else 'J04'
        evidence = {'selection': selection, 'chosenPeriodUs': chosen,
                    'web': long_row, 'cli': native_row,
                    'shortWindows': rows, 'longAttempts': long_attempts, 'channels': len(roles)}
        paths = [long_row, native_row]
        status = ('fail' if not long_row['clean'] or native_row['status'] == 'fail' else
                  'pass' if native_row['clean'] else 'pending')
        reason = None
        if status == 'fail':
            reason = '长窗计数不干净：' + '；'.join(sorted({
                problem for row in paths for problem in row.get('problems', [])}))[:400]
        if status == 'pending':
            reason = native_row['reason']
        sweep = self.record(instance, status, reason=reason, evidence=evidence)
        return {'definition': definition, 'stop': self._scope_stop_row(long_row) if label == 'one' else None,
                'sweep': sweep, 'longWindow': long_window}

    def _scope_native_row(self, roles, symbols, period_us, case, folder, label):
        serial = getattr(self.ctx, 'native_scope_serial', None)
        if not serial:
            return {'status': 'pending', 'path': 'native',
                    'reason': '需要 --native-scope 与配对探针的 --probe-serial',
                    'clean': False}
        observed = (self.web.state('scope')['summary'].get('probeSerial') or '').casefold()
        if observed != serial.casefold():
            raise Pending('原生请求序列号与 Web J-Scope 探针不一致或未确认')
        self.web.invoke('scope', 'disconnect')
        summary = self.web.state('scope')['summary']
        checked(not summary['running'] and summary['transport'] is None, '原生采样前 Web 尚未释放数据接口')
        addresses = sorted(symbols[role] for role in roles)
        scalars = scope.sweep_scalars(self.board, len(roles))
        native_out = folder / f'{label}-native'
        flags = '0xe1' if self.riscv else '0xa1'
        command = [sys.executable, '-X', 'utf8', str(facts.ROOT / 'tools/validate_f103_streams.py'),
                   'scope', '--out', str(native_out), '--target', self.link, '--probe-serial', serial,
                   '--speed', str(case['requestedHz'] or 0), '--flags', flags,
                   '--period', str(period_us), '--seconds', str(SCOPE_WINDOW_SECONDS)]
        for address, scalar in zip(addresses, scalars):
            command.extend(('--variable-address', hex(address), '--variable-type', scalar))
        try:
            child = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=110)
        except subprocess.TimeoutExpired as error:
            self.ctx.fail_unsafe('原生 J-Scope 退出与 STOP', error)
        (folder / f'{label}-native.log').write_text(child.stdout + child.stderr, encoding='utf-8')
        report = native_out / 'result.json'
        if not report.is_file():
            raise Unsafe('原生 J-Scope 未产生结果，无法确认探针状态')
        value = json.loads(report.read_text(encoding='utf-8'))
        if value.get('stopError') or value.get('targetRestored') is not True:
            raise Unsafe('原生 J-Scope 的 STOP 或探针目标类型恢复未确认')
        if child.returncode:
            return {'status': 'fail', 'clean': False, 'problems': [value.get('error') or '原生采集失败']}
        result = scope.native_verdict(value, native_out / 'capture.bin', addresses, period_us, self.link, scalars)
        return {'status': 'fail' if result['problems'] else 'measured', 'path': 'native',
                'clean': not result['problems'], **result, 'addresses': addresses,
                'requestedPeriodUs': period_us, 'raw': self._scope_file(native_out / 'capture.bin'),
                'report': self._scope_file(report)}

    def _scope_file(self, path):
        return {'path': path.relative_to(self.out).as_posix(), 'sha256': facts.digest(path)['sha256']}

    def _scope_raw_evidence(self, row, addresses, scalars, path):
        try:
            existing = {entry['id'] for entry in self.web.downloads('scope')}
            self.web.invoke('scope', 'saveRaw')
            entries = [entry for entry in self.web.downloads('scope') if entry['id'] not in existing]
            checked(len(entries) == 1, '未取得本次长窗原始 JSP')
            path.write_bytes(self.web.fetch_download('scope', entries[0]))
            self.web.page('scope', 'discard', id=entries[0]['id'])
            row['raw'] = self._scope_file(path)
            row['addresses'] = addresses
            raw = scope.read_raw(path)
            scope.check_raw(raw, path.stat().st_size, row.get('rawStopMetrics') or {}, addresses,
                            row['requestedTicks'], self.link, scalars)
        except Unsafe:
            raise
        except (OSError, ValueError, RuntimeError, Pending) as error:
            row['problems'].append(str(error))
            row['clean'] = False

    def _scope_window(self, period_us, seconds, path):
        """从真实 Web 控件采样，用探针快照计量并独立核对 STOP。"""
        self.web.set_control('scope', '#sc-period', str(period_us))
        self.web.set_control('scope', '#sc-seconds', str(seconds + 4))
        started = time.perf_counter()
        self.web.click('scope', '#sc-start')
        try:
            wait_for(lambda: (self.web.state('scope').get('summary') or {}).get('running'),
                     bool, 10, 'J-Scope 没有进入采样状态')
            before = self._scope_metrics()
            time.sleep(seconds)
            window_end = self._scope_metrics()
        except Unsafe:
            raise
        except Exception:
            self.web.invoke('scope', 'stop', ['acceptance'])
            raise
        try:
            self.web.click('scope', '#sc-stop')
            wait_for(lambda: self.web.state('scope').get('summary') or {},
                     lambda state: not state.get('running') and not state.get('stopping'),
                     20, 'J-Scope STOP 尚未完成')
        except Exception as error:
            self.ctx.fail_unsafe('J-Scope STOP', error)
        stopped = self._scope_metrics()
        repeated = self._scope_metrics()
        summary = self.web.state('scope').get('summary') or {}
        return {'path': path, 'seconds': seconds, 'requestedPeriodUs': period_us,
                'wallSeconds': time.perf_counter() - started, 'summary': summary,
                'windowStartMetrics': before,
                'windowEndMetrics': window_end, 'stopMetrics': stopped,
                'repeatedMetrics': repeated}

    def _scope_metrics(self):
        """取一次原始整组计数（`readScopeMetrics({fresh:true})`）。"""
        try:
            value = self.web.invoke('scope', 'readScopeMetrics', [{'fresh': True}])
        except Unsafe:
            raise
        except Exception:
            return None
        return value if isinstance(value, dict) else None

    def _scope_judge(self, window, period_us):
        summary = window['summary']
        before, after = window.get('windowStartMetrics'), window.get('windowEndMetrics')
        final, repeated = window.get('stopMetrics'), window.get('repeatedMetrics')
        ticks = scope.ticks_for_us(period_us)
        result = scope.counter_quality(before, after, final, repeated, ticks, window['seconds'])
        metrics = final or {}
        received = summary.get('receivedSamples')
        produced = metrics.get('produced')
        tail = received - produced if type(received) is int and type(produced) is int else None
        if tail is None or type(summary.get('lostGap')) is not int:
            result['gaps'].append('页面缺少实收、包序或 STOP 尾差证据')
        elif tail != 0 or summary.get('tailCountDelta') != 0:
            result['problems'].append(f'STOP 尾差 {tail}，页面 {summary.get("tailCountDelta")}')
        if summary.get('lostGap') or summary.get('metricsError'):
            result['problems'].append('页面报告包序缺口或采样计数错误')
        return {**result, 'path': window['path'], 'requestedSeconds': window['seconds'],
                'requestedPeriodUs': period_us, 'requestedTicks': ticks,
                'actualPeriodTicks': metrics.get('periodTicks'),
                'produced': produced, 'receivedSamples': received, 'tailCountDelta': tail,
                'derivedTail': tail, 'rawWindowStartMetrics': before, 'rawWindowEndMetrics': after,
                'rawStopMetrics': final, 'rawRepeatedMetrics': repeated,
                'skipped': metrics.get('skipped'), 'usbDrops': metrics.get('usb'),
                'readErrors': metrics.get('errors'), 'dapYields': metrics.get('yields'),
                'invalidSamples': metrics.get('invalidSamples'), 'lostGap': summary.get('lostGap'),
                'clean': not result['problems'] and not result['gaps']}

    def _scope_definition_row(self, label, roles, symbols, selection):
        summary = self.web.state('scope').get('summary') or {}
        definition = summary.get('definitionVariables')
        addresses = [symbols[role] for role in roles]
        evidence = {'selection': selection, 'elfAddresses': [hex(a) for a in addresses],
                    'definition': definition, 'vars': summary.get('vars'),
                    'plan': summary.get('plan'), 'samples': summary.get('samples'),
                    'mode': summary.get('mode')}
        instance = 'J01'
        if not definition:
            return self.pending(instance, '页面没有收到 DEF（通道定义）', **evidence)
        if len(addresses) != 1:
            return self.pending(instance, 'J01 只核对单通道 DEF', **evidence)
        if ([row.get('addr') for row in definition] != addresses or
                any(row.get('scalar') != 'u32' or row.get('size') != 4 for row in definition)):
            return self.fail(instance, '硬件 DEF 与目标 ELF 的地址或类型不一致', **evidence)
        return self.record(instance, 'pass', evidence=evidence)

    def _scope_stop_row(self, row):
        if not row:
            return self.pending('J02', '没有可判定的窗口')
        evidence = {key: row.get(key) for key in
                    ('produced', 'skipped', 'usbDrops', 'readErrors', 'dapYields',
                     'invalidSamples', 'tailCountDelta', 'derivedTail',
                     'receivedSamples', 'windowEndProduced', 'repeatProduced',
                     'rawStopMetrics', 'rawWindowStartMetrics', 'rawWindowEndMetrics', 'rawRepeatedMetrics',
                     'lostGap', 'actualPeriodTicks', 'requestedTicks', 'requestedSeconds', 'raw', 'addresses')}
        if row.get('gaps'):
            return self.unsupported('J02', '；'.join(row['gaps']), evidence=evidence)
        if row.get('problems'):
            return self.fail('J02', '；'.join(row['problems']), evidence=evidence)
        return self.record('J02', 'pass', evidence=evidence)

    def _scope_export_check(self, window, roles, folder):
        """同一窗口导出 JSP 与 CSV，用独立解析器逐值对账。"""
        if not window:
            return self.pending('J05', '没有可导出的窗口（前面没有跑成长窗）')
        summary = window.get('summary') or {}
        expected = summary.get('receivedSamples') or summary.get('samples')
        if not expected:
            return self.pending('J05', '窗口内没有样本，无法对账', evidence={})
        try:
            existing = {entry['id'] for entry in self.web.downloads('scope')}
            self.web.invoke('scope', 'saveRaw', [])
            self.web.invoke('scope', 'exportCsv', [])
            entries = [entry for entry in self.web.downloads('scope') if entry['id'] not in existing]
        except Unsafe:
            raise
        except Exception as error:
            return self.pending('J05', f'导出接口不可用：{error}', evidence={})
        jsp = self._fetch_named(entries, '.jsp')
        csv_entry = self._fetch_named(entries, '.csv')
        if not jsp['data'] or not csv_entry['data']:
            missing = [name for name, item in (('JSP', jsp), ('CSV', csv_entry))
                       if not item['data']]
            return self.pending('J05', f'缺少导出文件：{"、".join(missing)}',
                                evidence={'downloads': entries})
        try:
            scope.count_raw_packets(jsp['data'])
            packed = scope.parse_packet_stream(jsp['data'])
            table = scope.csv_rows(csv_entry['data'].decode('utf-8', 'replace'))
            verdict = scope.compare_jsp_csv(packed, table, len(roles))
        except Exception as error:
            return self.pending('J05', str(error), evidence={'downloads': entries})
        (Path(folder) / 'window.jsp').write_bytes(jsp['data'])
        (Path(folder) / 'window.csv').write_bytes(csv_entry['data'])
        # 同窗口：两份导出的样本数必须与 J02 那一窗的实收样本一致
        same_window = {
            'expectedSamples': expected,
            'jspSamples': len(packed['samples']),
            'csvRows': len(table['rows']),
            'consistent': len(packed['samples']) == len(table['rows']) == expected,
        }
        evidence = {'jsp': {'name': jsp['name'], 'bytes': len(jsp['data']),
                            **self._scope_file(Path(folder) / 'window.jsp')},
                    'csv': {'name': csv_entry['name'], 'bytes': len(csv_entry['data']),
                            **self._scope_file(Path(folder) / 'window.csv')},
                    'channels': len(roles), 'sameWindow': same_window,
                    'verdict': verdict}
        if not same_window['consistent']:
            return self.fail('J05', f'导出与窗口样本数不一致：{same_window}', evidence=evidence)
        if not verdict['ok']:
            return self.fail('J05', verdict.get('reason') or
                             f'首个不一致：{verdict.get("firstMismatch")}', evidence=evidence)
        return self.record('J05', 'pass', evidence=evidence)

    def _fetch_named(self, entries, suffix):
        for entry in entries or []:
            if str(entry.get('name', '')).lower().endswith(suffix):
                data = self.web.fetch_download('scope', entry)
                self.web.page('scope', 'discard', id=entry['id'])
                return {'name': entry['name'], 'data': data}
        return {'name': None, 'data': b''}

    # ==================================================================
    # E 组：ETM（Kingst + 1 MHz SWD）
    # ==================================================================
    def etm(self, case):
        # ETM uses a different ELF, clock and Kingst configuration from the
        # generic debug fixture. The operator supplies matching ETM evidence.
        return {instance: self.pending(instance, '按本板 ETM 专项配置采集并附记录、匹配 ELF 和记时证据')
                for instance in case['instances']}

    # ==================================================================
    # 分派
    # ==================================================================
    METHODS = {'S': 'sram', 'F': 'flash', 'D': 'debug', 'V': 'rtt_viewer',
               'R': 'rtt_forward', 'J': 'jscope', 'E': 'etm'}

    def run(self, case):
        checked(self.link != 'arm-jtag', 'ARM/JTAG 请使用原生 CLI')
        group = case['group']
        self.ctx.hardware(case['id'])
        method = getattr(self, self.METHODS[group])
        if group == 'S':
            self.match_flash_image(case, self.fixture_flash_loads(self.fixture('scope')))
            self.connect_debug(case)
        if group == 'J':
            self.match_flash_image(case, self.fixture_flash_loads(self.fixture('scope')))
        try:
            results = method(case)
        except Unsafe as error:
            self.ctx.uncertain = str(error)
            raise
        except Exception:
            self.disconnect_debug()
            raise
        finally:
            if group == 'J' and not self.ctx.uncertain:
                try:
                    self.web.invoke('scope', 'disconnect')
                    state = self.web.state('scope')['summary']
                    checked(not state.get('running') and not state.get('stopping') and not state.get('transport'),
                            'J-Scope 工作台数据接口未释放')
                except Exception as error:
                    self.ctx.fail_unsafe('释放 J-Scope 验收会话', error)
        self.disconnect_debug()
        # 每个用例后核对空闲；不干净就锁定，后续不再碰硬件。
        try:
            idle = self.check_idle()
        except Exception as error:
            self.ctx.uncertain = f'用例后无法确认页面空闲：{error}'
            raise Unsafe(self.ctx.uncertain) from error
        self.ctx.event('idle', case=case['id'], findings=idle)
        return results


# --------------------------------------------------------------------------
# 汇总与复核（离线：只读证据，不碰硬件）
# --------------------------------------------------------------------------


def counts(rows):
    result = {status: 0 for status in STATUSES}
    for row in rows:
        result[row['status']] = result.get(row['status'], 0) + 1
    return result


def overall_status(rows, instances):
    """整体状态：fail > blocked > pending/manual > pass。"""
    present = {row['status'] for row in rows}
    if 'fail' in present:
        return 'fail'
    if 'blocked' in present:
        return 'fail'
    concluded = {(row.get('case'), row['instance']) for row in rows
                 if row['status'] in ('pass', 'not-applicable')}
    if any(key not in concluded for key in instances):
        return 'incomplete'
    if present & {'pending', 'manual'}:
        return 'incomplete'
    return 'pass'


def exit_code(status):
    return {'pass': 0, 'fail': 1, 'incomplete': 2}.get(status, 2)
