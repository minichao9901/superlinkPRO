#!/usr/bin/env python3
"""生成固定输入清单 `tools/perf/inputs.json`（仓库内，可复核）。

清单记录方案 §9.1 的五份只读样本：路径一律相对仓库根（不写死本机绝对路径），
关键文件给字节数与 SHA256，其余派生文件只列大小；目录清单摘要见 directoryManifest。

    python -X utf8 tools/perf/build_inputs.py            # 重新生成 inputs.json
    python -X utf8 tools/perf/build_inputs.py --check    # 只核对现有清单与磁盘是否一致

注意：只读扫描，不改动任何记录文件；大目录不逐个哈希派生的 GB 级 JSONL。
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

# 方案 §9.1 固定输入（路径相对仓库根）。
RECORDS = {
    'steady': {
        'path': 'docs/validation/20261018-ds-review-bb2f8ee-6ff02745/data/records/'
                'capture-be1e78730b7f486a9e27d5911cd4a434',
        'role': '主性能样本 A：H743 200 ms steady（约 368 万条路径指令）',
        'notes': 'Codex 早期 steady 样本；原 CSV 与 KVDAT 都在，可做 CSV/KVDAT 同输入 A/B。',
    },
    'boundary': {
        'path': 'docs/validation/20261018-ds-recheck-0cd4229-a7b4f912/live-data/records/'
                'capture-36431f7c7567446e8bcd6229094d1b28',
        'role': '主性能样本 B：H743 200 ms boundary（约 386 万条指令，含未知/边界）',
        'notes': '原 CSV 与 KVDAT 都在；窗口/导航/未知条件的边界样本。',
    },
    'ds-rerun': {
        'path': 'etm/records/capture-de9b8e0ed24c41c68e5b374f3cb3c9c5',
        'role': 'DS 复跑样本：H743 200 ms（3,675,477 条指令）',
        'notes': '《etm-kvdat-fastpath-rerun-2026-10-19》使用的样本；有条件加入，不替换前两者。',
    },
    'usb-init-10ms': {
        'path': 'etm/records/capture-4395612c313f47ce97af1e1c17e0a0d5',
        'role': '快速 sanity 样本：H743 10 ms USB 初始化（210,706 条指令）',
        'notes': '用于校验基准工具本身可用（全链路小样本），不计入最终性能结论；'
                 '也是 USB 初始化寄存器扫描的业务语义/顺序核对样本。',
    },
    'etm-v3-10ms': {
        'path': 'etm/records/capture-2d6da5829a8c4811b7c5ed2ca9e08cc3',
        'role': 'ETMv3 兼容样本：F103ZE 10 ms（172,596 条指令，ETMv3.4）',
        'notes': 'ETMv3/ETMv4 双协议冒烟；不用于 H743 性能外推。',
    },
}


def _session_load(path: Path) -> dict:
    session_path = path / 'session.json'
    session = json.loads(session_path.read_text(encoding='utf-8')) if session_path.is_file() else {}
    config = session.get('config') or {}
    target = session.get('targetSnapshot') or {}
    summary = session.get('summary') or {}
    clock = session.get('clock')
    data = session.get('data')
    return {
        'name': session.get('name'),
        'protocol': config.get('protocol', 'ETMv4'),
        'target': target.get('name'),
        'core': target.get('core'),
        'etmVersion': target.get('etmVersion'),
        'windowSeconds': session.get('seconds'),
        'sampleRateHz': session.get('rate'),
        'clockChannel': clock,
        'dataChannels': data,
        'fraction': session.get('fraction'),
        'glitchNs': session.get('glitchNs'),
        'traceId': config.get('traceid'),
        'config': config,
        'decodeConfigBasis': session.get('configBasis'),
        'captureScope': session.get('captureScope'),
        'summary': {key: summary.get(key) for key in
                    ('packets', 'ranges', 'instructions', 'executed', 'exceptions',
                     'gaps', 'packetIssues', 'unknownExecution')},
    }


def build(root: Path) -> dict:
    records = {}
    for key, definition in RECORDS.items():
        folder = (root / definition['path']).resolve()
        if not folder.is_dir():
            raise SystemExit(f'固定输入缺失：{key} -> {folder}')
        files = {}
        derived = {}
        for path in sorted(folder.iterdir()):
            if not path.is_file():
                continue
            entry = {'bytes': path.stat().st_size}
            if path.name in perf.KEY_RECORD_FILES:
                entry['sha256'] = perf.sha256_file(path)
                files[path.name] = entry
            else:
                derived[path.name] = entry
        records[key] = {
            'path': definition['path'],
            'role': definition['role'],
            'notes': definition['notes'],
            'load': _session_load(folder),
            'files': files,
            'derivedFiles': derived,
            'directoryManifest': perf.directory_manifest(folder),
        }
    return {
        'schema': perf.SCHEMA_INPUTS,
        'generatedAt': perf.iso_now(),
        'generatedBy': 'tools/perf/build_inputs.py',
        'root': '仓库根（路径均相对它解析）',
        'note': '关键文件（capture.csv/kvdat、etm.bin、firmware.elf、native-config.txt、session.json、'
                'sources.json、tpiu.bin、capture.csv.la.json）含字节数与 SHA256；'
                '派生 GB 级文件只列大小。记录目录保持只读，基准脚本只复制/硬链接副本。',
        'records': records,
    }


def check(root: Path, path: Path) -> int:
    if not path.is_file():
        print(f'缺少 {path}，请先运行 python -X utf8 tools/perf/build_inputs.py', file=sys.stderr)
        return 1
    expected = json.loads(path.read_text(encoding='utf-8'))
    current = build(root)
    failures = []
    for key, entry in current['records'].items():
        old = (expected.get('records') or {}).get(key)
        if old is None:
            failures.append(f'{key}: 清单缺少该记录')
            continue
        if old.get('path') != entry['path']:
            failures.append(f'{key}: 路径变化 {old.get("path")} -> {entry["path"]}')
        for name, item in entry['files'].items():
            old_item = (old.get('files') or {}).get(name)
            if old_item != item:
                failures.append(f'{key}/{name}: 字节数或 SHA256 与清单不一致')
        if (old.get('directoryManifest') or {}).get('manifestSha256') != entry['directoryManifest']['manifestSha256']:
            failures.append(f'{key}: 目录清单摘要变化（可能增删文件）')
    if failures:
        for item in failures:
            print(f'FAIL {item}', file=sys.stderr)
        return 1
    print(f'OK：{len(current["records"])} 份固定输入与 {path} 一致')
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='生成/核对 tools/perf/inputs.json 固定输入清单。')
    parser.add_argument('--check', action='store_true', help='只核对，不写文件')
    parser.add_argument('--root', type=Path, default=None, help='仓库根（缺省自动定位）')
    parser.add_argument('--output', type=Path, default=HERE / 'inputs.json', help='清单输出路径')
    args = parser.parse_args(argv)
    root = (args.root or perf.repo_root(HERE)).resolve()
    if args.check:
        return check(root, args.output)
    payload = build(root)
    perf.write_json(args.output, payload)
    print(f'已写入 {args.output}（{len(payload["records"])} 份记录）')
    for key, entry in payload['records'].items():
        files = entry['files']
        print(f'  {key}: {entry["path"]} files={len(files)} '
              f'instructions={entry["load"]["summary"].get("instructions")}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
