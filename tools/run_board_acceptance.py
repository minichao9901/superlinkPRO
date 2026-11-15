"""Board acceptance entry: offline plan by default, hardware only with --execute.

移植自 ZynqLinkPRO ``tools/run_board_acceptance.py``（54150b31），保留三条边界：

* **plan**（默认）：只展开计划与离线前置，绝不打开设备；连驱动模块都不导入；
* **execute**：必须显式 ``--execute``，并要求 ``--out``（新目录）与 ``--runtime``；
* **review**：只读已完成证据目录，重算计划做逐字比对，不碰硬件。

本仓适配点：

* 板卡/链路/夹具事实在 ``tools/board_acceptance_target.py``，夹具取自
  ``web/tools/target-firmware/board-matrix.json``；
* 七组各五项（35 个实例）代替上游的 8 个 feature；
* 没有 Zynq 位流/PS ELF，因此 ``files`` 只绑定目标夹具；
* flash 需要额外 ``--allow-flash``，人工取证需要 ``--allow-manual``。

退出码：0 通过、1 失败或阻塞、2 仍有待验。
"""

from __future__ import annotations

import argparse
import json
from random import Random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BOARDS = ('f103ze', 'h743', 'hpm6800evk')
GROUPS = ('S', 'F', 'D', 'V', 'R', 'J', 'E')
STATUSES = ('pass', 'fail', 'pending', 'not-applicable', 'blocked', 'manual', 'unsupported')


def utc_now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def write_json(path, value):
    """原子写：被 Ctrl+C 打断时不留半截 JSON。"""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n',
                   encoding='utf-8')
    tmp.replace(path)


def source_state():
    import subprocess
    try:
        revision = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                                           text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(
            ['git', '-C', str(ROOT), 'status', '--porcelain', '--untracked-files=no'],
            text=True, stderr=subprocess.DEVNULL).strip())
    except Exception:
        return {'revision': None, 'sourceDirty': None}
    return {'revision': revision, 'sourceDirty': dirty}


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


def workbench_links(board, links=None):
    from tools.board_acceptance_target import BOARDS, normalize_link
    if not links:
        return [link for link in BOARDS[board]['links'] if link != 'arm-jtag']
    normalized = [normalize_link(board, link) for link in links]
    if 'arm-jtag' in normalized:
        raise ValueError('ARM/JTAG 请用 toolbox.py arm-jtag 或原生转发／采样 CLI 验收')
    return normalized


def build_plan(board, links, groups, seed):
    from tools.board_acceptance import offline_preconditions, plan_cases
    cases, instances = plan_cases(board, workbench_links(board, links), groups, seed)
    for row in instances:
        if 'notApplicable' in row:
            continue
        row['offline'] = offline_preconditions(board, row['link'], row['instance'])
    return cases, instances


def plan_payload(board, links, groups, seed):
    from tools.board_acceptance_target import BOARDS
    cases, instances = build_plan(board, links, groups, seed)
    effective_groups = groups or list(GROUPS)
    chosen_links = sorted({case['link'] for case in cases})
    return {'execute': False, 'board': board, 'label': BOARDS[board]['label'],
            'links': chosen_links,
            'seed': seed, 'groups': effective_groups, 'cases': cases,
            'instances': instances, 'stress': plan_stress(chosen_links, effective_groups, seed),
            'note': '这是计划，不是验收结论。计划投递成功、ack 或 click 成功都不等于通过；'
                    '通过必须以 execute 的证据与 review 的复核为准。'}


def plan_stress(links, groups, seed):
    if not {'D', 'V', 'R', 'J'}.issubset(groups):
        return []
    workflows = ('rtt', 'rttcdc', 'scope', 'dbg')
    modes = ('切页', '空闲断开', '工作中断开', 'USB 数据线拔插')
    rounds = []
    for link in links:
        if link == 'arm-jtag':
            continue
        first = Random(f'{seed}:{link}').sample(workflows, len(workflows))
        for index in range(3):
            order = first[index:] + first[:index]
            endings = {page: modes[(slot + index) % len(modes)] for slot, page in enumerate(first)}
            rounds.append({'link': link, 'round': index + 1, 'order': order, 'endings': endings,
                           'status': 'pending', 'seed': seed})
    return rounds


def plan_summary(payload):
    """控制台只打摘要：完整计划写到 --out/plan.json，避免刷满终端。"""
    rows = payload['instances']
    applicable = [row for row in rows if not row.get('notApplicable')]
    reasons, missing = {}, {}
    for row in applicable:
        offline = row.get('offline') or {}
        for item in offline.get('missing') or []:
            missing.setdefault(item['reason'], []).append(row['instance'])
        for item in offline.get('pending') or []:
            reasons.setdefault(item['reason'], []).append(row['instance'])
    return {'execute': False, 'board': payload['board'], 'label': payload['label'],
            'links': payload['links'], 'seed': payload['seed'], 'groups': payload['groups'],
            'cases': [{'id': case['id'], 'instances': case['instances'],
                       'notApplicable': case.get('notApplicable')}
                      for case in payload['cases']],
            'instances': len(rows), 'applicable': len(applicable),
            'notApplicable': len(rows) - len(applicable),
            'missingFixtures': [{'reason': reason, 'instances': sorted(set(ids))}
                                for reason, ids in missing.items()],
            'pendingPreconditions': [{'reason': reason, 'instances': sorted(set(ids))}
                                     for reason, ids in reasons.items()],
            'note': payload['note']}


def render_plan_markdown(payload):
    lines = [f"# {payload['label']} 板卡验收计划", '',
             f"板卡：`{payload['board']}`　种子：`{payload['seed']}`　组：{'、'.join(payload['groups'])}", '',
             '> 这是计划，不是验收结论。计划或任务 ACK 不等于成功。', '']
    for case in payload['cases']:
        lines.append(f"## 用例 {case['id']}")
        if 'notApplicable' in case:
            lines.append(f"不适用：{case['notApplicable']}")
            lines.append('')
            continue
        lines.append(f"链路：`{case['link']}`　请求档：`{case['requestedHz']}` Hz")
        lines.append('')
        lines.append('| 实例 | 子项 | 条件 | 判定 | 证据字段 | 离线前置 |')
        lines.append('|---|---|---|---|---|---|')
        for row in payload['instances']:
            if row['case'] != case['id']:
                continue
            offline = row.get('offline') or {}
            if row.get('notApplicable'):
                state = f"不适用：{row['notApplicable']}"
            elif offline.get('missing'):
                state = '缺夹具：' + '；'.join(item['reason'] for item in offline['missing'])
            elif offline.get('pending'):
                state = '有前置待确认'
            else:
                state = '夹具与符号已核对'
            lines.append('| `{}` | {} | {} | {} | {} | {} |'.format(
                row['instance'], GROUPS_TITLE.get(row['instance'][0], row['instance'][0]),
                row['condition'], row['criteria'], '、'.join(row['evidenceFields']), state))
        lines.append('')
    return '\n'.join(lines)


GROUPS_TITLE = {'S': 'S · SRAM', 'F': 'F · 烧录', 'D': 'D · 调试', 'V': 'V · RTT Viewer',
                'R': 'R · RTT 转发', 'J': 'J · J-Scope', 'E': 'E · ETM'}


# --------------------------------------------------------------------------
# execute
# --------------------------------------------------------------------------


def execute(args):
    from tools.board_acceptance import Driver, counts, exit_code, overall_status
    from tools.board_acceptance_stress import BoardStress
    from tools.board_acceptance_target import (BOARDS, Pending, RunContext, Unsafe,
                                               WebTarget, digest, fixture)
    if sys.flags.optimize:
        raise SystemExit('验收依赖断言：不要用 python -O 运行')
    if args.out is None or args.runtime is None:
        raise SystemExit('--execute 需要 --out 与 --runtime')
    out = Path(args.out).resolve()
    if out.exists():
        raise SystemExit('--out 必须是新目录（证据不可覆盖）')
    cases, instances = build_plan(args.board, args.link, args.group, args.seed)
    web = WebTarget(args.runtime, timeout=args.timeout)
    web.ping()
    for page, flags in {'dbg': ('connected',), 'rtt': ('connected',),
                        'rttcdc': ('open',), 'scope': ('running', 'stopping')}.items():
        state = web.state(page).get('summary')
        if not isinstance(state, dict) or any(state.get(flag) for flag in flags):
            raise ValueError(f'{page} 未确认空闲，请使用本轮独立工作台会话')
    bridge = web.invoke('rttcdc', 'summary', target='hid')
    if not isinstance(bridge, dict) or bridge.get('running'):
        raise ValueError('探针 RTT 转发尚未确认空闲')
    ctx = RunContext(args.board, None, 'execute', out, web=web,
                     allow_flash=args.allow_flash, allow_manual=args.allow_manual)
    out.mkdir(parents=True, exist_ok=False)
    web.out = out
    ctx.probe_serial = args.probe_serial
    ctx.native_scope_serial = args.probe_serial if args.native_scope else None
    files = {}
    for kind in ('rtt', 'rtt-seq', 'scope', 'debug', 'flash'):
        try:
            evidence = fixture(args.board, kind)
        except Exception:
            continue
        files[kind] = {'path': evidence['path'], 'sha256': evidence['sha256']}
    metadata = {'board': args.board, 'label': BOARDS[args.board]['label'],
                'seed': args.seed, 'plan': cases, 'instances': instances,
                'files': files, 'startedAt': utc_now(), 'mode': 'execute',
                **source_state()}
    metadata['stress'] = plan_stress(sorted({case['link'] for case in cases}),
                                    {case['group'] for case in cases}, args.seed)
    summary = dict(metadata, results=[], status='running', accepted=False)
    write_json(out / 'summary.json', summary)
    stop_reason = None
    cover = {case['id']: [row['instance'] for row in instances if row['case'] == case['id']]
             for case in cases}
    for case in cases:
        ids = cover[case['id']]
        ctx.case, ctx.link = case['id'], case['link']
        if 'notApplicable' in case:
            for instance in ids:
                ctx.record(instance, 'not-applicable', reason=case['notApplicable'])
            continue
        if stop_reason:
            for instance in ids:
                ctx.record(instance, 'blocked', reason=stop_reason)
            continue
        driver = Driver(ctx)
        started = time.monotonic()
        try:
            driver.run(case)
        except Unsafe as error:
            stop_reason = str(error)
            done = {row['instance'] for row in ctx.records if row['case'] == case['id']}
            for instance in ids:
                if instance not in done:
                    ctx.record(instance, 'blocked', reason=stop_reason)
        except Pending as error:
            pending_ids = {row['instance'] for row in ctx.records if row['case'] == case['id']}
            for instance in ids:
                if instance not in pending_ids:
                    ctx.record(instance, 'pending', reason=str(error))
        except (Exception, KeyboardInterrupt) as error:
            stop_reason = f'{type(error).__name__}: {error}'
            done = {row['instance'] for row in ctx.records if row['case'] == case['id']}
            for instance in ids:
                if instance not in done:
                    ctx.record(instance, 'fail', reason=stop_reason)
        summary.update(results=ctx.records, seconds=round(time.monotonic() - started, 3),
                       updated=utc_now())
        write_json(out / 'summary.json', summary)
    for index, plan in enumerate(summary['stress']):
        if stop_reason:
            result = {**plan, 'status': 'blocked', 'reason': stop_reason}
        else:
            case = next(item for item in cases if item['link'] == plan['link'] and item['group'] == 'D')
            ctx.link, ctx.case = plan['link'], f"{plan['link']}-stress-{plan['round']}"
            try:
                result = BoardStress(Driver(ctx), case).run(plan)
            except Unsafe as error:
                stop_reason = str(error)
                result = {**plan, 'status': 'blocked', 'reason': stop_reason}
        summary['stress'][index] = result
        write_json(out / 'summary.json', summary)
    for row in ctx.records:
        kind = {'S': 'scope', 'F': 'flash', 'D': 'debug', 'V': 'rtt',
                'R': 'rtt', 'J': 'scope'}.get(row['instance'][0])
        if kind in files:
            row.setdefault('evidence', {}).setdefault('targetElf', kind)
    status = overall_status(ctx.records, [(row['case'], row['instance']) for row in instances])
    stress_states = {row['status'] for row in summary['stress']}
    if stress_states & {'fail', 'blocked'}:
        status = 'fail'
    elif status == 'pass' and 'pending' in stress_states:
        status = 'incomplete'
    summary.update(results=ctx.records, status=status, accepted=status == 'pass',
                   counts=counts(ctx.records), finishedAt=utc_now(), digest=ctx.digest())
    write_json(out / 'summary.json', summary)
    ctx.write_jsonl('results.jsonl')
    print(json.dumps({'execute': True, 'board': args.board, 'status': status,
                      'accepted': status == 'pass', 'counts': summary['counts'],
                      'out': str(out)}, ensure_ascii=False, indent=2))
    return exit_code(status)


# --------------------------------------------------------------------------
# review（离线复核）
# --------------------------------------------------------------------------


def review(paths, out=None):
    """复核已完成证据：不接受未完成会话、脏源码或与计划不符的结果。"""
    from tools.board_acceptance import plan_cases
    from tools.board_acceptance_target import sha as hash_bytes
    reports = []
    revision = None
    board_files = {}
    by_board = {}
    stress_by_board = {}
    for path in paths:
        path = Path(path)
        summary_path = path / 'summary.json'
        if not summary_path.is_file():
            raise ValueError(f'缺少 summary.json：{path}')
        report = json.loads(summary_path.read_text(encoding='utf-8'))
        if report.get('status') not in ('pass', 'fail', 'incomplete') or not report.get('finishedAt'):
            raise ValueError(f'不能复核未完成的会话：{path}')
        if report.get('mode') != 'execute':
            raise ValueError(f'不是 execute 产物：{path}')
        if report.get('sourceDirty'):
            raise ValueError(f'复核要求干净源码；{path} 记录为脏工作区')
        rows = report.get('results')
        if not isinstance(rows, list) or report.get('digest') != hash_bytes(
                json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')):
            raise ValueError(f'报告结果摘要不匹配：{path}')
        journal = path / 'results.jsonl'
        if not journal.is_file() or [json.loads(line) for line in journal.read_text(encoding='utf-8').splitlines()] != rows:
            raise ValueError(f'验收结果与独立 JSONL 台账不一致：{path}')
        board = report['board']
        current = report.get('revision')
        if not current or revision not in (None, current):
            raise ValueError(f'多份报告的源码提交不一致：{path}')
        revision = current
        for kind, entry in (report.get('files') or {}).items():
            key = (board, kind)
            sha = entry.get('sha256')
            if not sha or board_files.setdefault(key, sha) != sha:
                raise ValueError(f'同板同夹具的指纹不一致：{path} / {kind}')
        expected_cases, expected_instances = plan_cases(
            board, list({case['link'] for case in report['plan']}),
            list({case['group'] for case in report['plan']}), report['seed'])
        cases = {case['id']: case for case in report['plan']}
        if cases != {case['id']: case for case in expected_cases}:
            raise ValueError(f'计划与按种子重算的结果不一致（不能事后改计划）：{path}')
        expected_stress = plan_stress(sorted({case['link'] for case in expected_cases}),
                                      {case['group'] for case in expected_cases}, report['seed'])
        stress_rows = report.get('stress', [])
        if len(stress_rows) != len(expected_stress):
            raise ValueError(f'跨页压力计划缺项：{path}')
        for stress_row, expected in zip(stress_rows, expected_stress):
            if any(stress_row.get(key) != value for key, value in expected.items() if key != 'status'):
                raise ValueError(f'跨页压力调度与执行计划不一致：{path}')
            state = stress_row.get('status')
            if state not in ('pass', 'fail', 'blocked', 'pending'):
                raise ValueError(f'跨页压力结果状态无效：{path}')
            if state == 'pass':
                verify_stress_steps(path, stress_row)
            slot = (board, stress_row['link'], stress_row['round'])
            key = (report['finishedAt'], str(path))
            if slot not in stress_by_board or key > stress_by_board[slot][0]:
                stress_by_board[slot] = (key, stress_row)
        allowed = {(row['case'], row['instance']): row for row in expected_instances}
        seen = set()
        for row in rows:
            key = (row.get('case'), row.get('instance'))
            expected = allowed.get(key)
            if (not expected or key in seen or row.get('board') != board or row.get('link') != expected['link'] or
                    row.get('status') not in STATUSES or
                    (row['status'] == 'not-applicable') != bool(expected.get('notApplicable'))):
                raise ValueError(f'非法结果行：{path} / {row.get("instance")}')
            seen.add(key)
        for row in rows:
            if row['status'] != 'pass':
                continue
            evidence = row.get('evidence') or {}
            kind = evidence.get('targetElf')
            sha = (report.get('files') or {}).get(kind, {}).get('sha256') if kind else None
            actual = (evidence.get('elf') or {}).get('sha256') or \
                     (evidence.get('image') or {}).get('sha256') or \
                     (evidence.get('target') or {}).get('sha256')
            if not sha or (actual and actual != sha):
                raise ValueError(
                    f'通过行缺少匹配的夹具证据：{path} / {row["instance"]}')
            if row['instance'] == 'F02' and not (evidence.get('verifiedLoadBytes') or 0) > 0:
                raise ValueError(f'F02 通过但 verifiedLoadBytes 为 0：{path}')
            if row['instance'] in ('V01', 'V02', 'V04'):
                verify_viewer_result(path, row)
            if row['instance'] in ('J02', 'J03', 'J04', 'J05'):
                verify_scope_result(path, row)
            if row['instance'].startswith('R'):
                verify_rtt_result(path, row)
        reports.append(report)
        key = (report['finishedAt'], str(path))
        for row in rows:
            slot = (board, row['link'], row['instance'])
            prior = by_board.get(slot)
            order = (not pending_rtt_fixture(row), *key)
            if prior is None or order > prior[0]:
                by_board[slot] = (order, row, path)
    boards = sorted({report['board'] for report in reports})
    instances = []
    stress = []
    for board in boards:
        seed = next(report['seed'] for report in reports if report['board'] == board)
        links = workbench_links(board)
        _, expected = plan_cases(board, links, None, seed)
        for row in plan_stress(sorted(links), GROUPS, seed):
            saved = stress_by_board.get((board, row['link'], row['round']))
            stress.append({'board': board, **(saved[1] if saved else row)})
        for row in expected:
            slot = (board, row['link'], row['instance'])
            chosen = by_board.get(slot)
            if row.get('notApplicable'):
                instances.append({'board': board, 'link': row['link'], 'instance': row['instance'],
                                  'status': 'not-applicable', 'reason': row['notApplicable']})
            elif chosen is None:
                instances.append({'board': board, 'link': row['link'], 'instance': row['instance'],
                                  'status': 'pending',
                                  'reason': 'No completed run covering this case'})
            else:
                _, result, source = chosen
                instances.append({'board': board, 'link': row['link'], 'instance': row['instance'],
                                  'status': result['status'],
                                  'source': str(source),
                                  'reason': result.get('reason'),
                                  'evidence': result.get('evidence')})
    present = {row['board'] for row in instances}
    statuses = [row['status'] for row in instances]
    stress_states = [row['status'] for row in stress]
    if any(value in ('fail', 'blocked') for value in statuses + stress_states):
        status = 'fail'
    elif any(value in ('pending', 'manual', 'unsupported') for value in statuses + stress_states):
        status = 'incomplete'
    else:
        status = 'pass'
    counts = {value: statuses.count(value) for value in STATUSES}
    result = {'review': True, 'boards': boards, 'status': status,
              'accepted': status == 'pass' and len(present) == len(boards),
              'counts': counts, 'instances': instances, 'stress': stress,
              'reports': [str(Path(path)) for path in paths],
              'identity': {'revision': revision},
              'finishedAt': utc_now(),
              'note': '复核只读证据；通过要求每个实例都有结论且夹具指纹一致'}
    if out:
        out = Path(out)
        if out.exists():
            raise ValueError('复核输出目录必须是新目录')
        out.mkdir(parents=True)
        write_json(out / 'review.json', result)
    print(json.dumps({key: value for key, value in result.items() if key not in ('instances', 'stress')},
                     ensure_ascii=False, indent=2))
    return {'pass': 0, 'fail': 1, 'incomplete': 2}[status]


def checked_evidence(root, reference):
    from tools.board_acceptance_target import digest

    if not isinstance(reference, dict) or not reference.get('path') or not reference.get('sha256'):
        raise ValueError('缺少证据路径或 SHA-256')
    path = (root / reference['path']).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file() or digest(path)['sha256'] != reference['sha256']:
        raise ValueError(f'证据路径不在执行目录内、文件缺失或 SHA-256 不匹配：{path}')
    return path


def pending_rtt_fixture(row):
    """A different RTT fixture cannot invalidate an earlier measured result."""
    expected_other = {'R01': 'rtt', 'R02': 'rtt-seq'}.get(row['instance'])
    return (expected_other is not None and row['status'] == 'pending' and
            (row.get('evidence') or {}).get('targetElf') == expected_other)


def verify_viewer_result(root, row):
    from tools.board_acceptance_rtt import check_viewer_fixture

    evidence = row['evidence']
    artifacts = evidence.get('artifacts') or {}
    address = evidence.get('controlBlock')
    if type(address) is not int or address <= 0 or not evidence.get('probeSerial'):
        raise ValueError('Viewer 通过行缺少目标地址或配对探针')
    keys = ('before', 'restored', 'readback', 'upstream')
    files = [checked_evidence(root, artifacts.get(key)).read_bytes() for key in keys]
    recomputed = check_viewer_fixture(address, *files)
    if any(evidence.get(key) != value for key, value in recomputed.items()):
        raise ValueError('Viewer 通过行计数或游标与原始证据不一致')
    if evidence.get('lost') != 0 or evidence.get('corrupt') != 0:
        raise ValueError('Viewer 通过行缺少零丢失与损坏计数')


def verify_scope_result(root, row):
    from tools import board_acceptance_scope as scope
    from tools.board_acceptance_target import SCOPE_WINDOW_SECONDS, fixture

    evidence = row['evidence']
    instance = row['instance']
    if instance == 'J05':
        jsp = checked_evidence(root, evidence.get('jsp')).read_bytes()
        csv = checked_evidence(root, evidence.get('csv')).read_text(encoding='utf-8')
        scope.count_raw_packets(jsp)
        packed = scope.parse_packet_stream(jsp)
        verdict = scope.compare_jsp_csv(packed, scope.csv_rows(csv), evidence.get('channels'))
        if not verdict['ok'] or verdict['rows'] != evidence.get('sameWindow', {}).get('expectedSamples'):
            raise ValueError('J05 的 JSP、CSV 或本窗样本数不一致')
        return
    web = evidence if instance == 'J02' else evidence['web']
    channels = 8 if instance == 'J04' else 1
    addresses = web.get('addresses') or []
    scalars = scope.sweep_scalars(row['board'], channels)
    ticks = web.get('requestedTicks')
    grid = scope.PERIODS[(row['board'], row['link'], channels)]
    expected_addresses = scope.sweep_addresses(row['board'], fixture(row['board'], 'scope')['symbols'], channels)
    if (addresses != expected_addresses or ticks not in grid or
            web.get('requestedSeconds') != SCOPE_WINDOW_SECONDS):
        raise ValueError('J-Scope Web 长窗的通道、周期或时长不符')
    counters = (web.get('rawWindowStartMetrics'), web.get('rawWindowEndMetrics'),
                web.get('rawStopMetrics'), web.get('rawRepeatedMetrics'))
    quality = scope.counter_quality(*counters, ticks, SCOPE_WINDOW_SECONDS)
    raw = checked_evidence(root, web.get('raw'))
    scope.check_raw(scope.read_raw(raw), raw.stat().st_size, counters[2] or {}, addresses, ticks, row['link'], scalars)
    if quality['problems'] or quality['gaps'] or web.get('lostGap') != 0 or web.get('tailCountDelta') != 0:
        raise ValueError('J-Scope Web 长窗硬件计数或 STOP 不符合门槛')
    if instance == 'J02':
        return
    native = evidence['cli']
    report = json.loads(checked_evidence(root, native.get('report')).read_text(encoding='utf-8'))
    raw = checked_evidence(root, native.get('raw'))
    fixed_period = scope.us_for_ticks(grid[1])
    outcome = scope.native_verdict(report, raw, addresses, fixed_period, row['link'], scalars)
    if native.get('requestedPeriodUs') != fixed_period or outcome['problems']:
        raise ValueError('J-Scope 原生历史固定档的原始证据不通过：' + '；'.join(outcome['problems']))


def verify_stress_steps(root, result):
    from tools.board_acceptance_stress import IDLE_EXPECTED

    steps = result.get('steps') or []
    if [step.get('page') for step in steps] != result['order']:
        raise ValueError('跨页压力的实际步骤缺项或顺序不符')
    for index, step in enumerate(steps):
        page = step['page']
        ending = result['endings'][page]
        if step.get('ending') != ending or step.get('status') != 'pass':
            raise ValueError('跨页压力实际结束方式与计划不符')
        before = json.loads(checked_evidence(root, step.get('before')).read_text(encoding='utf-8'))
        after = json.loads(checked_evidence(root, step.get('after')).read_text(encoding='utf-8'))
        verify_stress_progress(root, page, before)
        if after.get('idle') != IDLE_EXPECTED:
            raise ValueError('跨页压力退出后没有释放会话')
        if ending == '切页':
            following = result['order'][(index + 1) % len(steps)]
            if after.get('nextPage') != following:
                raise ValueError('跨页压力未切入调度指定页面')
            verify_stress_stop(root, page, after.get('stopped', {}))
            continue
        if after.get('idleBeforeReconnect') != IDLE_EXPECTED:
            raise ValueError('跨页压力重连前存在残留会话')
        again = after.get('reconnected') or {}
        verify_stress_progress(root, page, again)
        if again.get('serial') != before.get('serial') or after.get('reconnectSeconds', -1) < 0:
            raise ValueError('未按原序列号重新连接或缺重连计时')
        verify_stress_stop(root, page, after.get('final', {}))
        stopped = after.get('stopped') or {}
        if ending == '空闲断开':
            verify_stress_stop(root, page, stopped)
        elif ending == '工作中断开' and stopped.get('activeDisconnect') is not True:
            raise ValueError('缺少工作中顶栏断开证据')
        elif ending == 'USB 数据线拔插' and stopped != {
                'serial': before['serial'], 'removed': True, 'reappeared': True}:
            raise ValueError('缺少指定探针消失与重枚举证据')


def verify_stress_progress(root, page, state):
    from tools import board_acceptance_rtt as rtt, board_acceptance_scope as scope

    if state.get('active') is not True or not state.get('serial'):
        raise ValueError(f'{page} 缺少业务处于活动状态的证据')
    if page in ('rtt', 'rttcdc'):
        raw = checked_evidence(root, state.get('raw')).read_bytes()
        pattern = (state.get('payload') or {}).get('format')
        if state.get('bytes', 0) <= 0 or pattern not in ('hello', 'seq') or not rtt.check_payload(raw, pattern)['exact']:
            raise ValueError(f'{page} 缺少真实且连续的原始 RTT 流')
    elif page == 'scope':
        raw = checked_evidence(root, state.get('raw')).read_bytes()
        parsed = scope.count_raw_packets(raw)
        if parsed['received'] < 8 or [var['address'] for var in parsed['definition']['vars']] != [state.get('address')]:
            raise ValueError('J-Scope 样本或 DEF 与当前 ELF 不符')
    elif (not state.get('halted') or type(state.get('pc')) is not int or
          type(state.get('steppedPc')) is not int or state['pc'] == state['steppedPc']):
        raise ValueError('调试页缺少停机与真实 PC 单步进展')


def verify_stress_stop(root, page, state):
    from tools import board_acceptance_scope as scope

    if page == 'rttcdc':
        passes = state.get('moved', 0) > 0 and state['moved'] == state.get('received')
    elif page == 'scope':
        raw = checked_evidence(root, state.get('raw')).read_bytes()
        passes = state.get('tailCountDelta') == 0 and state.get('received') == scope.count_raw_packets(raw)['received']
    elif page == 'dbg':
        passes = state.get('targetState') == 'running'
    else:
        passes = state.get('lost') == state.get('corrupt') == 0
    if not passes:
        raise ValueError(f'{page} 正常停止或恢复状态有误')


def verify_rtt_result(root, row):
    from tools import board_acceptance_rtt as rtt
    from tools.board_acceptance_target import RTT_USB_PACKET, sha

    evidence = row['evidence']
    instance = row['instance']
    comparison = checked_evidence(root, evidence.get('captureEvidence'))
    cards = json.loads(comparison.read_text(encoding='utf-8'))
    folder = comparison.parent
    if cards.get('probeSerial') != evidence.get('probeSerial') or cards.get('targetElf') != evidence.get('targetElf'):
        raise ValueError('RTT 采集证据的探针或 ELF 与用例不一致')
    if len(cards['web']) != 2:
        raise ValueError('RTT 复核要求同页的两个 Web 窗口')
    host = []
    for index, window in enumerate(cards['web'], 1):
        raw = checked_evidence(root, {'path': (folder / f'web-{index}.bin').relative_to(root.resolve()).as_posix(),
                                      'sha256': window['sha256']}).read_bytes()
        if (window.get('complete') is not True or window.get('canRestart') is not True or
                len(raw) != window['stop']['moved'] or window['stop'].get('running') is not False or
                any(window['stop'].get(field) != 0 for field in ('rdErr', 'wrErr')) or
                not rtt.check_payload(raw, cards['format'])['exact']):
            raise ValueError('Web RTT 原始窗口不满足载荷或 STOP 条件')
        host.append(raw)
    native = checked_evidence(root, {'path': (folder / 'native/capture.bin').relative_to(root.resolve()).as_posix(),
                                     'sha256': cards['cli']['sha256']}).read_bytes()
    if not rtt.check_payload(native, cards['format'])['exact'] or cards['cli']['stop'].get('stableMoved') is not True:
        raise ValueError('原生 RTT 原始载荷或 STOP 终态计数不满足条件')
    web_stop = {**cards['web'][-1]['stop'], 'moved': sum(w['stop']['moved'] for w in cards['web'])}
    rtt.check_window(cards['ring'], cards['ringAfterWeb'], sum(map(len, host)), web_stop)
    rtt.check_window(cards['ringAfterWeb'], cards['ringAfterNative'], len(native), cards['cli']['stop'])
    if instance == 'R01' or (instance == 'R04' and cards['format'] == 'seq'):
        web_bytes = b''.join(host)
        if cards['format'] != 'seq' or not rtt.check_payload(web_bytes, 'seq')['exact']:
            raise ValueError('RTT 连续序号证据缺失或跨窗口断开')
        if instance == 'R01':
            first_seconds, native_seconds = cards['web'][0].get('seconds'), cards['cli'].get('seconds')
            actual = rtt.sequence_run(row['board'], first_seconds, native_seconds, web_bytes, native)
            if (actual != evidence.get('sequence') or first_seconds != evidence.get('webSeconds') or
                    native_seconds != evidence.get('cliSeconds')):
                raise ValueError('RTT 序号结果与原始证据或窗口长度不符')
    if instance == 'R05' or (instance == 'R04' and cards['format'] != 'seq'):
        path = checked_evidence(root, evidence.get('boundaries') or evidence.get('boundaryRestart'))
        boundary = json.loads(path.read_text(encoding='utf-8'))
        _, expected = rtt.boundary_fixture(boundary['address'], RTT_USB_PACKET)
        before = (path.parent / 'sram-before.bin').read_bytes()
        after = (path.parent / 'sram-after.bin').read_bytes()
        if boundary.get('restored') is not True or not before or before != after or len(boundary['rows']) != 3:
            raise ValueError('RTT 边界夹具恢复证据不完整')
        for index, (payload, result) in enumerate(zip(expected, boundary['rows']), 1):
            raw = (path.parent / f'web-{index}.bin').read_bytes()
            if raw != payload or result['sha256'] != sha(raw) or result['ringLeft'] != 0 or result['tailBytes'] != 0:
                raise ValueError(f'RTT 整包边界载荷或收尾不匹配：{len(payload)} B')


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parser():
    p = argparse.ArgumentParser(
        description='三板板卡验收：默认离线 plan，--execute 才接触硬件，--review 只复核证据')
    p.add_argument('--board', choices=BOARDS, help='目标板卡')
    p.add_argument('--link', action='append', choices=('swd', 'jtag'),
                   help='工作台链路；jtag 适用于 RISC-V')
    p.add_argument('--group', action='append', choices=GROUPS,
                   help='只跑指定组（七组各五项）；省略时全跑')
    p.add_argument('--seed', type=int, default=20261122, help='计划种子，复核时按它重算')
    p.add_argument('--out', type=Path, help='证据目录；execute 要求新目录')
    p.add_argument('--runtime', type=Path, help='已打开执行页的 toolbox 运行时目录')
    p.add_argument('--timeout', type=float, default=180, help='单条队列请求等待秒数')
    p.add_argument('--execute', action='store_true', help='真正访问硬件（默认只出计划）')
    p.add_argument('--allow-flash', action='store_true', help='允许烧写目标 Flash')
    p.add_argument('--allow-manual', action='store_true', help='允许记录人工取证事件')
    p.add_argument('--native-scope', action='store_true',
                   help='额外跑原生 EP83 定速窗（需要 --probe-serial）；'
                        '不加时 J 组把原生路径明确记成缺口')
    p.add_argument('--probe-serial', help='RTT 验收及原生访问所用的 USB/HID 探针序列号')
    p.add_argument('--review', nargs='+', type=Path, metavar='SUMMARY_DIR',
                   help='离线复核已完成证据目录（与 --execute 互斥）')
    p.add_argument('--json', action='store_true', help='（保留）plan 模式始终输出 JSON')
    p.add_argument('--print-plan', action='store_true',
                   help='把完整计划打到 stdout（默认只打摘要，全量写 --out/plan.json）')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.review:
        if args.execute:
            raise SystemExit('--review 是离线复核；不要和 --execute 一起用')
        return review(args.review, args.out)
    if not args.board:
        raise SystemExit('需要 --board（或用 --review 复核已完成证据）')
    if args.native_scope and not args.probe_serial:
        raise SystemExit('--native-scope 需要 --probe-serial')
    if args.execute:
        return execute(args)
    payload = plan_payload(args.board, args.link, args.group, args.seed)
    if args.out:
        out = Path(args.out)
        if out.exists():
            raise SystemExit('--out 必须是新目录')
        out.mkdir(parents=True)
        write_json(out / 'plan.json', payload)
        (out / 'plan.md').write_text(render_plan_markdown(payload) + '\n', encoding='utf-8')
    print(json.dumps(payload if args.print_plan else plan_summary(payload),
                     ensure_ascii=False, indent=2, default=str))
    missing = [row['instance'] for row in payload['instances']
               if (row.get('offline') or {}).get('missing')]
    return 0 if not missing else 2


if __name__ == '__main__':
    raise SystemExit(main())
