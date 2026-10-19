#!/usr/bin/env python3
"""比较两次（或多次）bench_pipeline.py 运行：输入/代码指纹、产物哈希、阶段耗时。

用法：
    python -X utf8 tools/perf/compare_runs.py <run 或 variant 目录> <run 或 variant 目录> \
        [--json <输出.json>] [--label A --label B] [--require-artifact-match]

每个参数可以是：
  * 单个 run 目录（含 run.json），或 run.json 文件本身；
  * 一个变体目录（<variant>/runs/<runId>/run.json 若干），按多次重复统计中位数/范围；
  * bench 的 --output 根目录（包含多个 <variant>/runs/…）。
默认只输出摘要并以 0 退出；--require-artifact-match 时产物哈希不一致返回 2。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import _common as perf  # noqa: E402

METRIC_PATHS = (
    'decodeAndIndexSeconds',
    'resources.peakTreeRssBytes',
    'resources.treeCpuSeconds',
    'artifactBytes',
    'artifactBytesComparable',
    'api.overview.seconds',
    'api.page.seconds',
    'api.timeline.seconds',
    'api.window.seconds',
    'api.firstDisplaySeconds',
    'api.windowCached.seconds',
    'api.queries.p95Seconds',
    'api.queries.navigationP95Seconds',
    'api.queries.searchP95Seconds',
)


def _load_runs(argument) -> list:
    path = Path(argument).resolve()
    if path.is_file():
        if path.name != 'run.json':
            raise SystemExit(f'不是 run.json：{path}')
        return [json.loads(path.read_text(encoding='utf-8'))]
    if not path.is_dir():
        raise SystemExit(f'路径不存在：{path}')
    direct = path / 'run.json'
    if direct.is_file():
        return [json.loads(direct.read_text(encoding='utf-8'))]
    candidates = sorted(path.glob('runs/*/run.json'))
    if not candidates:
        candidates = sorted(path.glob('*/runs/*/run.json'))
    if not candidates:
        candidates = sorted(path.glob('*/run.json'))
    if not candidates:
        raise SystemExit(f'{path} 下没有找到 run.json（既不是 run 目录也不是变体目录）')
    return [json.loads(item.read_text(encoding='utf-8')) for item in candidates]


def _dig(value, dotted):
    cursor = value
    for key in dotted.split('.'):
        if not isinstance(cursor, dict) or key not in cursor:
            return None
        cursor = cursor[key]
    return cursor


def _input_digest(run):
    data = run.get('input') or {}
    files = {name: {'bytes': item.get('bytes'), 'sha256': item.get('sha256')}
             for name, item in (data.get('files') or {}).items()}
    return perf.canonical_json_sha256({'raw': data.get('raw'), 'kind': data.get('kind'),
                                       'files': files,
                                       'manifest': (data.get('directoryManifest') or {}).get('manifestSha256')})


def _group_summary(label, argument, runs) -> dict:
    ok = [run for run in runs if run.get('status') == 'ok']
    code = (runs[0].get('code') or {})
    git = code.get('git') or {}
    binaries = code.get('binaries') or {}
    metrics = {}
    for dotted in METRIC_PATHS:
        metrics[dotted] = perf.spread([_dig(run, dotted) for run in ok])
    stage_labels = set()
    for run in ok:
        stage_labels.update((run.get('stages') or {}).keys())
    for stage in sorted(stage_labels):
        metrics[f'stage.{stage}.wallSeconds'] = perf.spread(
            [_dig(run, f'stages.{stage}.wallSeconds') for run in ok])
    decoded_summaries = {perf.canonical_json_sha256(run.get('decodedSummary'))
                         for run in ok if run.get('decodedSummary') is not None}
    return {
        'label': label,
        'path': str(Path(argument).resolve()),
        'runs': len(runs),
        'ok': len(ok),
        'failed': [{'runId': run.get('runId'), 'error': run.get('error')}
                   for run in runs if run.get('status') != 'ok'],
        'runIds': [run.get('runId') for run in runs],
        'input': {
            'digest': _input_digest(runs[0]),
            'recordKey': (runs[0].get('input') or {}).get('recordKey'),
            'raw': (runs[0].get('input') or {}).get('raw'),
            'kind': (runs[0].get('input') or {}).get('kind'),
            'recordPath': (runs[0].get('input') or {}).get('recordPathRelative')
                           or (runs[0].get('input') or {}).get('recordPath'),
            'directoryManifestSha256': ((runs[0].get('input') or {}).get('directoryManifest') or {}).get('manifestSha256'),
            'catalogMatched': ((runs[0].get('input') or {}).get('catalog') or {}).get('catalogMatched'),
            'files': {name: {'bytes': item.get('bytes'), 'sha256': item.get('sha256')}
                      for name, item in ((runs[0].get('input') or {}).get('files') or {}).items()},
        },
        'code': {
            'path': runs[0].get('codePath'),
            'revision': git.get('revision'),
            'describe': git.get('describe'),
            'dirty': git.get('dirty'),
            'dirtyCount': git.get('dirtyCount'),
            'sourceDigest': code.get('sourceDigest'),
            'sourceFileCount': code.get('sourceFileCount'),
            'decoderSha256': (binaries.get('etm_decode.exe') or binaries.get('etm_decode') or {}).get('sha256'),
            'indexerSha256': (binaries.get('etm_index.exe') or binaries.get('etm_index') or {}).get('sha256'),
            'build': code.get('build'),
        },
        'metrics': metrics,
        'decodedSummaryDigest': next(iter(decoded_summaries)) if len(decoded_summaries) == 1 else None,
        'decodedSummaryDigests': sorted(decoded_summaries),
    }


def _artifact_comparison(groups, runs_by_label) -> tuple:
    names = set()
    for runs in runs_by_label.values():
        for run in runs:
            names.update((run.get('artifacts') or {}).keys())
    result = {}
    comparable = matching = 0
    volatile = []
    differing = []
    unhashed = []
    for name in sorted(names):
        per_group = {}
        values = {}
        for label, runs in runs_by_label.items():
            entries = [(run.get('artifacts') or {}).get(name) for run in runs]
            entries = [entry for entry in entries if entry]
            if not entries:
                per_group[label] = {'missing': True}
                continue
            hashed = [entry for entry in entries if entry.get('hashed', True) and entry.get('sha256')]
            if not hashed:
                per_group[label] = {'bytes': entries[0].get('bytes'), 'skipped': True,
                                    'hashed': False}
                continue
            compare_values = sorted({entry.get('compareSha256') for entry in hashed
                                     if entry.get('compareSha256') is not None})
            raw_values = sorted({entry.get('sha256') for entry in hashed if entry.get('sha256')})
            is_volatile = all(entry.get('volatile') for entry in hashed)
            state = 'volatile' if is_volatile else ('stable' if len(compare_values) == 1 else 'unstable')
            per_group[label] = {'bytes': entries[0].get('bytes'), 'sha256': entries[0].get('sha256'),
                                'compareSha256': entries[0].get('compareSha256'),
                                'uniqueCompareValues': len(compare_values),
                                'uniqueRawValues': len(raw_values), 'state': state, 'hashed': True}
            if not is_volatile:
                values[label] = compare_values[0] if len(compare_values) == 1 else None
        if all('missing' in per_group.get(label, {}) for label in runs_by_label):
            continue
        present = [label for label in runs_by_label if not per_group.get(label, {}).get('missing')]
        if any(per_group[label].get('skipped') for label in present if label in per_group):
            unhashed.append(name)
            result[name] = {'comparable': False, 'volatile': False, 'skipped': True,
                            'match': False, 'groups': per_group}
            continue
        is_volatile = any(per_group[label].get('state') == 'volatile' for label in present)
        match = (not is_volatile and len(present) == len(runs_by_label)
                 and len({values.get(label) for label in present}) == 1
                 and all(values.get(label) is not None for label in present))
        if is_volatile:
            volatile.append(name)
        else:
            comparable += 1
            if match:
                matching += 1
            else:
                differing.append(name)
        result[name] = {'comparable': not is_volatile, 'volatile': is_volatile,
                        'match': match, 'groups': per_group}
    return result, {'comparable': comparable, 'matching': matching, 'differing': differing,
                    'volatile': volatile, 'unhashed': unhashed, 'allMatch': matching == comparable}


def _metric_table(groups):
    names = set()
    for group in groups:
        names.update(group['metrics'].keys())
    table = {}
    for name in sorted(names):
        row = {}
        for group in groups:
            row[group['label']] = group['metrics'].get(name, perf.spread([]))
        labels = [group['label'] for group in groups]
        if len(labels) >= 2:
            first = row[labels[0]].get('median')
            last = row[labels[1]].get('median')
            row['deltaMedian'] = None if (first is None or last is None) else round(last - first, 6)
            row['ratioMedian'] = None if not first else round(last / first, 4)
        table[name] = row
    return table


def _format_number(value):
    if value is None:
        return 'n/a'
    if isinstance(value, float):
        return f'{value:.3f}'
    if isinstance(value, int) and value >= 1024 * 1024:
        return f'{value / (1024 * 1024):.1f}MB'
    return str(value)


def _print_summary(groups, checks, artifacts, metrics):
    print('== etm-perf compare ==')
    for group in groups:
        code = group['code']
        print(f"[{group['label']}] runs={group['ok']}/{group['runs']} "
              f"input={group['input'].get('recordKey') or Path(group['input'].get('recordPath') or '?').name} "
              f"kind={group['input'].get('kind')} "
              f"revision={code.get('revision')} dirty={code.get('dirty')} "
              f"sourceDigest={str(code.get('sourceDigest'))[:16]} "
              f"decoder={str(code.get('decoderSha256'))[:16]}")
    print(f"inputFingerprintMatch={checks['inputFingerprintMatch']} "
          f"codeSourceMatch={checks['codeSourceMatch']} "
          f"decodedSummaryMatch={checks['decodedSummaryMatch']}")
    print(f"artifacts: comparable={artifacts['comparable']} match={artifacts['matching']} "
          f"differ={artifacts['differing'] or '[]'} volatile={artifacts['volatile'] or '[]'} "
          f"unhashed={artifacts['unhashed'] or '[]'}")
    print('metrics (median [min..max]):')
    for name, row in metrics.items():
        labels = [group['label'] for group in groups]
        parts = []
        for label in labels:
            spread = row.get(label) or {}
            parts.append(f"{label}={_format_number(spread.get('median'))} "
                         f"[{_format_number(spread.get('min'))}..{_format_number(spread.get('max'))}]")
        delta = '' if row.get('deltaMedian') is None else f" delta={_format_number(row['deltaMedian'])}"
        ratio = '' if row.get('ratioMedian') is None else f" ratio={row['ratioMedian']}"
        print(f'  {name}: ' + '  '.join(parts) + delta + ratio)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog='compare_runs.py',
        description='比较两次（或多次）ETM 基准 run：输入/代码指纹、产物哈希一致性、'
                    '各阶段耗时中位数与范围；输出 JSON 与可读摘要。',
        epilog='参数可以是 run 目录、run.json、变体目录（含 runs/）或 bench --output 根目录。')
    parser.add_argument('runs', nargs='+', metavar='RUN', help='run.json / run 目录 / 变体目录 / output 根目录')
    parser.add_argument('--label', action='append', metavar='NAME', help='分组名；缺省用变体名或目录名')
    parser.add_argument('--json', type=Path, metavar='PATH', help='把完整比较结果写入 JSON')
    parser.add_argument('--require-artifact-match', action='store_true',
                        help='产物哈希不一致时以退出码 2 结束（默认只报告）')
    args = parser.parse_args(argv)
    if len(args.runs) < 2:
        raise SystemExit('至少需要两个 RUN 参数。')
    runs_by_label = {}
    labels = list(args.label or [])
    if labels and len(labels) != len(args.runs):
        raise SystemExit('--label 数量必须与 RUN 数量一致。')
    groups = []
    for index, argument in enumerate(args.runs):
        runs = _load_runs(argument)
        label = labels[index] if labels else (runs[0].get('variant') or Path(argument).name)
        if label in runs_by_label:
            label = f'{label}-{index + 1}'
        runs_by_label[label] = runs
        groups.append(_group_summary(label, argument, runs))
    checks = {
        'inputFingerprintMatch': len({group['input']['digest'] for group in groups}) == 1,
        'codeSourceMatch': len({group['code'].get('sourceDigest') for group in groups}) == 1,
        'decodedSummaryMatch': len({group['decodedSummaryDigest'] for group in groups}) == 1
                               and all(group['decodedSummaryDigest'] for group in groups),
    }
    artifacts, artifact_summary = _artifact_comparison(groups, runs_by_label)
    metrics = _metric_table(groups)
    payload = {
        'schema': 'etm-perf-compare/1',
        'generatedAt': perf.iso_now(),
        'argv': sys.argv,
        'runs': [str(Path(argument).resolve()) for argument in args.runs],
        'checks': {**checks, 'artifactMatch': artifact_summary['allMatch'],
                   'artifactComparable': artifact_summary['comparable'],
                   'artifactMatching': artifact_summary['matching'],
                   'artifactDiffering': artifact_summary['differing'],
                   'artifactVolatile': artifact_summary['volatile'],
                   'artifactUnhashed': artifact_summary['unhashed']},
        'groups': groups,
        'artifacts': artifacts,
        'metrics': metrics,
    }
    _print_summary(groups, checks, artifact_summary, metrics)
    if args.json:
        perf.write_json(args.json, payload)
        print(f'JSON：{Path(args.json).resolve()}')
    if args.require_artifact_match and not artifact_summary['allMatch']:
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
