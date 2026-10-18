"""长窗（一次采集 + 自动分窗）记录的合并工具。

`server.export_slices()` 逐片导出/解码后，各片是**独立解码**的：
  · `seq` 从 0 起（每片独立编号）；
  · `cycles` / `cycleCount` 是**每元素**量（区间周期记在区间末指令上），不是累计时间；
  · events.jsonl 里没有片号。

合并 = 按片序把事件接到记录级 `events.jsonl`：`seq` 按输出顺序重编、`slice` 标注片号、
`cycles` **原样保留**。随后 `aggregate_slices()` 把各片的 summary/quality/analysis
（函数、热点、火焰、周期）合成"整段"，避免整段分析仍是第一片。

纯逻辑、无 IO 依赖硬件，单测见 `tools/test_slices.py`。
"""
import json
import pathlib

# 各片直接相加的统计量（都是"自身量"，不含跨片累计语义）
ADDITIVE = ('instructions', 'knownExecuted', 'unknownExecution', 'notTaken', 'calls',
            'closedCalls', 'tailTransfers', 'interruptEntries', 'selfCycles', 'totalCycles',
            'cycleRanges')

# summary/quality 里可以逐片相加的键
SUMMARY_KEYS = ('packets', 'ranges', 'instructions', 'executed', 'sourceMapped', 'exceptions',
                'gaps', 'packetIssues', 'unknownExecution')
QUALITY_KEYS = ('inputBytes', 'processedBytes', 'unprocessedBytes', 'overlapBytes',
                'unaccountedBytes', 'unsynchronizedBytes', 'unsynchronizedRecords',
                'validPackets', 'packetIssues', 'overflowPackets', 'unknownExecution',
                'initialPhysicalBytes', 'unknownSourceBytes', 'trailingFrameBytes',
                'trailingSyncBytes')


def merge_slice_events(dest, slice_dir, index, seq_off=0):
    """把一片的 events.jsonl 合并进 dest；返回该片之后的 seq 偏移。

    `cycles` 原样保留：它是每元素量（`flow_analysis.mark_execution` 把区间周期写到区间末指令的
    `cycles` 上）。旧实现按"累计时间"取每片最大值再加偏移 —— 既把整段周期放大成片数的倍数
    （1 s 记录实测 912，实际约 3,600 万），又会让后片事件带上假的周期偏移。

    `seq` 一律按输出顺序重编：旧实现用"片内最大 seq"推偏移，片内 seq 从 0 起时，后片第一条
    会与上一片最后一条**撞号**（1 s 记录实测片边界出现重复 seq，浏览索引因此建不起来）。
    """
    written = 0
    mode = 'w' if index == 0 else 'a'
    src = pathlib.Path(slice_dir) / 'events.jsonl'
    with pathlib.Path(dest).open(mode, encoding='utf8') as out, src.open(encoding='utf8') as fh:
        for line in fh:
            if not line.startswith('{'):
                continue
            e = json.loads(line)
            e['seq'] = seq_off + written
            e['slice'] = index
            out.write(json.dumps(e, ensure_ascii=False) + '\n')
            written += 1
    return seq_off + written


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _add(target, source, keys):
    for key in keys:
        if _number(source.get(key)):
            target[key] = target.get(key, 0) + source[key]


def _merge_functions(parts, total_cycles):
    merged = {}
    for part in parts:
        for fn in part.get('functions') or []:
            name = fn.get('function') or '(未知函数)'
            item = merged.setdefault(name, dict(function=name, instructions=0, knownExecuted=0,
                                                unknownExecution=0, notTaken=0, calls=0,
                                                closedCalls=0, tailTransfers=0,
                                                interruptEntries=0, selfCycles=0,
                                                totalCycles=0, cycleRanges=0))
            _add(item, fn, ADDITIVE)
    total_calls = sum(int(s['calls']) for s in merged.values())
    for s in merged.values():
        s['openCalls'] = max(0, s['calls'] - s['closedCalls'])
        s['callPercent'] = 100 * s['calls'] / total_calls if total_calls else 0
        s['cyclePercent'] = 100 * s['selfCycles'] / total_cycles if total_cycles else 0
        s['cpi'] = (s['selfCycles'] / s['instructions']) if (s['instructions'] and s['selfCycles']) else None
    return sorted(merged.values(), key=lambda s: s['instructions'], reverse=True)


def _merge_hotspots(parts):
    merged = {}
    for part in parts:
        for hot in part.get('hotspots') or []:
            key = (hot.get('function'), hot.get('file'), hot.get('line'))
            item = merged.setdefault(key, dict(function=hot.get('function'), file=hot.get('file'),
                                               line=hot.get('line'), records=0, unknown=0,
                                               cycles=0, pc=hot.get('pc')))
            for name in ('records', 'unknown', 'cycles'):
                if _number(hot.get(name)):
                    item[name] = item.get(name, 0) + hot[name]
    return sorted(merged.values(), key=lambda h: (h['cycles'], h['records']), reverse=True)


def _merge_flame(node, other):
    """把一片的火焰树并进 node（children 在 analysis 里已序列化成列表）。"""
    for key in ('self', 'value', 'selfCycles', 'valueCycles'):
        if _number(other.get(key)):
            node[key] = node.get(key, 0) + other[key]
    by_name = {child.get('name'): child for child in (node.get('children') or [])}
    for child in other.get('children') or []:
        name = child.get('name')
        if name not in by_name:
            by_name[name] = dict(name=name, self=0, value=0, selfCycles=0, valueCycles=0, children=[])
            node.setdefault('children', []).append(by_name[name])
        _merge_flame(by_name[name], child)


def _merge_analysis(parts):
    if not parts:
        return {}
    timing = {}
    for part in parts:
        _add(timing, part.get('timing') or {}, ('totalCycles', 'cycleRanges',
                                                'executedInstructions', 'instructions',
                                                'cycleElements'))
    total_cycles = int(timing.get('totalCycles') or 0)
    executed = int(timing.get('executedInstructions') or 0)
    functions = _merge_functions(parts, total_cycles)
    timing['cpi'] = (total_cycles / executed) if (total_cycles and executed) else None
    timing['functions'] = [dict(function=s['function'], selfCycles=s['selfCycles'],
                                cyclePercent=s['cyclePercent'], instructions=s['instructions'],
                                cpi=s['cpi'], calls=s['calls'])
                           for s in functions if s['selfCycles']]
    timing['timeFlameAvailable'] = bool(total_cycles)
    timing['basis'] = ('周期计数来自各片的 ETMv4 cycle-count 元素（区间末指令记录，可求和）；'
                       '本记录由 ' + str(len(parts)) + ' 片合成，跨片调用关系不计入'
                       if total_cycles else
                       '本记录没有周期计数数据（目标未启用，或 ETMv3/F103 不支持），时间轴不可用')
    flame = dict(name='已恢复执行区间', self=0, value=0, selfCycles=0, valueCycles=0, children=[])
    edges = {}
    observed = 0
    restarts = 0
    for part in parts:
        if isinstance(part.get('flame'), dict):
            _merge_flame(flame, part['flame'])
        for edge in part.get('edges') or []:
            key = (edge.get('caller'), edge.get('callee'), edge.get('kind'))
            item = edges.setdefault(key, dict(caller=key[0], callee=key[1], kind=key[2], count=0))
            if _number(edge.get('count')):
                item['count'] += edge['count']
        if _number(part.get('observedCalls')):
            observed += part['observedCalls']
        if _number(part.get('unknownStackRestarts')):
            restarts += part['unknownStackRestarts']
    return dict(functions=functions, hotspots=_merge_hotspots(parts), flame=flame,
                observedCalls=observed, unknownStackRestarts=restarts,
                edges=sorted(edges.values(), key=lambda e: (e['kind'], e['caller'], e['callee'])),
                timing=timing,
                weight=('路径指令记录数（含未成立/未知状态）；周期列才是耗时，指令数不是'
                        if total_cycles else '路径指令记录数（含未成立/未知状态），不是耗时'),
                callsPerSecond=None,
                rateReason=('缺少覆盖可信执行区间的时间基准，暂不计算调用/秒' if not total_cycles
                            else '可用周期计数折算时间（coreHz 已知），但调用/秒仍取决于覆盖完整度'))


def aggregate_slices(base, slices, step, la_filter=None, parts=None):
    """把逐片结果合成记录级 decoded.json（summary/quality/analysis + slices[]）。

    `parts` 是各片解出来的 decoded.json（含 analysis）；缺省时只能聚合 summary 的
    指令/包/包问题，analysis 仍沿用 `base`（第一片）。
    """
    out = dict(base or {})
    parts = [p or {} for p in (parts or [])]
    summary = dict(out.get('summary') or {})
    totals = {}
    for part in parts:
        _add(totals, part.get('summary') or {}, SUMMARY_KEYS)
    if parts:
        summary.update({key: int(totals.get(key, 0)) for key in SUMMARY_KEYS})
        functions = {}
        for part in parts:
            for name, count in ((part.get('summary') or {}).get('functions') or {}).items():
                if _number(count):
                    functions[name] = functions.get(name, 0) + count
        summary['functions'] = functions
    else:
        summary.update(instructions=sum(int(s.get('instructions') or 0) for s in slices),
                       packets=sum(int(s.get('packets') or 0) for s in slices))
    # 周期以各片 analysis.timing.totalCycles 为准（每元素 cycles 不能求和当累计量）
    measured = sum(int((s.get('cycles') or 0)) for s in slices)
    if parts:
        measured = sum(int(((p.get('analysis') or {}).get('timing') or {}).get('totalCycles') or 0)
                       for p in parts) or measured
    summary['totalCycles'] = measured

    quality = dict(out.get('quality') or {})
    qtotals = {}
    for part in parts:
        _add(qtotals, part.get('quality') or {}, QUALITY_KEYS)
    if parts:
        for key in QUALITY_KEYS:
            if key in qtotals:
                quality[key] = int(qtotals[key])
    quality['packetIssues'] = sum(int(s.get('packetIssues') or 0) for s in slices)

    out.update(summary=summary, quality=quality, slices=slices, sliceSeconds=step,
               captureScope='sliced-window', laFilter=la_filter)
    if parts:
        out['analysis'] = _merge_analysis([p.get('analysis') or {} for p in parts])
    return out
