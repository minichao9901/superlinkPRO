#!/usr/bin/env python3
"""R2 原生 store builder 适配层（M0/M1：prescan / diagnose）。

原生实现是 ``etm/native/storebuild.cpp``（``etm/build-native/etm_storebuild.exe``）。
本模块只做三件事：

1. **版本握手**：``--version`` 必须报告 ``etm-storebuild/1``、schema/abi=1 且具备
   ``prescan``/``diagnose`` 能力；缺失、旧残留、能力不符都明确报错（不静默回退）。
2. 调一个原生子进程完成 **整批** 工作（native.bin → ranges.bin / 计数 JSON）；
   逐条记录、逐指令处理都在 C++ 内，不逐指令跨 Python 边界。
3. 读回一个 JSON 并做路径/退出码/JSON 可解析性检查。

CLI::

    python -X utf8 etm/tools/native_storebuild.py --version
    python -X utf8 etm/tools/native_storebuild.py prescan --native <native.bin> --out <ranges.bin>
    python -X utf8 etm/tools/native_storebuild.py diagnose --native <native.bin> --static <static-dict.bin>
    python -X utf8 etm/tools/native_storebuild.py static-info --static <static-dict.bin>

设计：docs/etm-r2-native-builder-design.md §1–§4、§7.1、§11；t13 实现 M0/M1。
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # etm/
PROTOCOL = 'etm-storebuild/1'
SCHEMA = 1
ABI = 1
REQUIRED_CAPABILITIES = ('prescan', 'diagnose')
DEFAULT_TIMEOUT = 900.0


class NativeStorebuildError(RuntimeError):
    """原生 builder 失败/结果不可信。"""


class NativeStorebuildUnavailable(NativeStorebuildError):
    """可执行文件缺失或无法运行。"""


class NativeStorebuildMismatch(NativeStorebuildError):
    """可执行文件存在但协议/版本/能力不符（旧残留）。"""


def binary_path(root=None):
    base = Path(root) if root else ROOT
    return base / 'build-native' / 'etm_storebuild.exe'


def _run_binary(path, args, timeout):
    try:
        return subprocess.run([str(path)] + [str(item) for item in args],
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise NativeStorebuildError(
            '原生 builder 超时（%.0fs）：%s' % (timeout, path)) from error
    except OSError as error:
        raise NativeStorebuildUnavailable('原生 builder 无法运行：%s' % error) from error


def probe(path=None, timeout=60.0):
    """版本握手；返回 ``--version`` JSON。缺失/协议不符明确失败。"""
    binary = Path(path) if path else binary_path()
    if not binary.is_file():
        raise NativeStorebuildUnavailable(
            '缺少原生 store builder %s；请先运行 pwsh -NoProfile -File etm/build-decoder.ps1。'
            % binary)
    completed = _run_binary(binary, ['--version'], timeout)
    stderr = completed.stderr.decode('utf-8', 'replace').strip()
    if completed.returncode != 0:
        raise NativeStorebuildMismatch(
            '原生 builder --version 退出码 %d：%s'
            % (completed.returncode, stderr or '(无输出)'))
    lines = completed.stdout.decode('utf-8', 'replace').strip().splitlines()
    if not lines:
        raise NativeStorebuildMismatch('原生 builder --version 没有输出')
    try:
        info = json.loads(lines[-1])
    except json.JSONDecodeError as error:
        raise NativeStorebuildMismatch(
            '原生 builder --version 输出不是 JSON：%r' % lines[-1]) from error
    if info.get('protocol') != PROTOCOL:
        raise NativeStorebuildMismatch(
            '原生 builder 协议不符：期望 %s，实际 %r（%s）；请重新构建。'
            % (PROTOCOL, info.get('protocol'), binary))
    if info.get('schema') != SCHEMA or info.get('abi') != ABI:
        raise NativeStorebuildMismatch(
            '原生 builder schema/abi 不符：%r/%r'
            % (info.get('schema'), info.get('abi')))
    capabilities = set(info.get('capabilities') or ())
    missing = [name for name in REQUIRED_CAPABILITIES if name not in capabilities]
    if missing:
        raise NativeStorebuildMismatch(
            '原生 builder 缺少能力：%s（实际 %r）'
            % (','.join(missing), sorted(capabilities)))
    return info


def provenance(path=None):
    binary = Path(path) if path else binary_path()
    info = probe(binary)
    digest = hashlib.sha256()
    with binary.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    stat = binary.stat()
    return dict(path=str(binary), sha256=digest.hexdigest(), bytes=stat.st_size,
                mtime=time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(stat.st_mtime)),
                version=info, available=True)


def _run_json_command(args, json_path, timeout, binary=None):
    binary = Path(binary) if binary else binary_path()
    probe(binary)                               # 每次调用都握手：拒绝旧残留
    temporary = None
    if json_path is None:
        handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False,
                                             encoding='utf-8')
        handle.close()
        temporary = Path(handle.name)
        json_path = temporary
    json_path = Path(json_path)
    command = [item for item in args]
    command.extend(['--json', str(json_path)])
    started = time.perf_counter()
    completed = _run_binary(binary, command, timeout)
    elapsed = time.perf_counter() - started
    stderr = completed.stderr.decode('utf-8', 'replace').strip()
    try:
        if completed.returncode != 0:
            raise NativeStorebuildError(
                '原生 builder %s 失败（退出码 %d）：%s'
                % (command[0], completed.returncode, stderr or '(无输出)'))
        if not json_path.is_file():
            raise NativeStorebuildError('原生 builder 未生成结果 JSON：%s' % json_path)
        try:
            result = json.loads(json_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as error:
            raise NativeStorebuildError('原生 builder 结果 JSON 不可读：%s' % error) from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
    result.setdefault('wallSeconds', round(elapsed, 3))
    return result


def prescan(native, out=None, json_path=None, max_unique_ranges=None,
            timeout=DEFAULT_TIMEOUT, binary=None):
    """native.bin → 唯一范围表 ranges.bin + 统计；返回 C++ JSON。"""
    native = Path(native)
    if not native.is_file():
        raise NativeStorebuildError('native.bin 不存在：%s' % native)
    args = ['prescan', '--native', str(native)]
    if out is not None:
        args.extend(['--out', str(out)])
    if max_unique_ranges is not None:
        args.extend(['--max-unique-ranges', str(int(max_unique_ranges))])
    return _run_json_command(args, json_path, timeout, binary=binary)


def diagnose(native, static, ranges=None, json_path=None, checkpoint_instructions=4096,
             max_run_instructions=4096, run_log=None, timeout=DEFAULT_TIMEOUT, binary=None):
    """native.bin + static-dict.bin → 计数/分段诊断 JSON。"""
    native = Path(native)
    static = Path(static)
    if not native.is_file():
        raise NativeStorebuildError('native.bin 不存在：%s' % native)
    if not static.is_file():
        raise NativeStorebuildError('static-dict.bin 不存在：%s' % static)
    args = ['diagnose', '--native', str(native), '--static', str(static)]
    if ranges is not None:
        args.extend(['--ranges', str(ranges)])
    args.extend(['--checkpoint-instructions', str(int(checkpoint_instructions)),
                 '--max-run-instructions', str(int(max_run_instructions))])
    if run_log is not None:
        args.extend(['--run-log', str(run_log)])
    return _run_json_command(args, json_path, timeout, binary=binary)


def inspect_static(static, json_path=None, timeout=DEFAULT_TIMEOUT, binary=None):
    static = Path(static)
    if not static.is_file():
        raise NativeStorebuildError('static-dict.bin 不存在：%s' % static)
    args = ['inspect-static', '--static', str(static)]
    return _run_json_command(args, json_path, timeout, binary=binary)


def build_local_index(store_dir, build_result):
    """在 C++ 产出的 store 上做本地索引聚合（不是参考表转写）。

    这是一个过渡实现：用 p1 `trace_store_index.build_index` 扫描当前 store 的
    sequence，生成本地 anchor/57 表/默认视图/v2 payload；57 表的 C++ 同遍版本仍未完成。
    """
    import json
    import time
    import trace_store
    import trace_store_index
    store_dir = Path(store_dir)
    counts = build_result['counts']
    files = build_result['files']
    manifest = dict(
        format='trace-store', formatName='trace-store v1', schemaVersion=1,
        byteOrder='little', state='complete', generation=build_result.get('generation', 'g'),
        generationTag=build_result.get('generationTag', '0x1234'),
        files=[dict(name='store.bin', bytes=files[0]['bytes'],
                    sha256=files[0]['sha256'])],
        counts=counts,
        fieldPresence={'instruction': [
            'kind', 'seq', 'offset', 'pc', 'size', 'bytes', 'mnemonic', 'operands',
            'executed', 'function', 'file', 'line', 'cycleCount', 'cycleSpan',
            'flow', 'conditional', 'executionState', 'executionReason', 'callPath',
            'cycles'], 'event': ['kind', 'seq', 'type', 'offset', 'text', 'payload',
                                 'start', 'end', 'flags']},
        bindings={}, checksums=dict(algorithm='sha256', block='crc32'))
    import os
    import shutil
    generation = str(build_result.get('generation', 'g'))
    parent = store_dir / '_local-index-parent'
    shutil.rmtree(parent, ignore_errors=True)
    generation_dir = parent / 'generations' / generation
    generation_dir.mkdir(parents=True, exist_ok=True)
    os.link(store_dir / 'store.bin', generation_dir / 'store.bin')
    manifest['generation'] = generation
    (parent / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    try:
        started = time.perf_counter()
        store = trace_store.TraceStore(parent, verify='sizes')
        try:
            built = trace_store_index.build_index(store, container='v2', with_nav=True)
        finally:
            store.close()
        elapsed = time.perf_counter() - started
    finally:
        shutil.rmtree(parent, ignore_errors=True)
    payload_path = store_dir / 'index-v2.payload'
    payload_path.write_bytes(built['payload'])
    return dict(payloadBytes=len(built['payload']), tableCount=len(built['arrays']),
                seconds=round(elapsed, 3), buildSeconds=built['buildSeconds'],
                defaultsSeconds=built['defaultsSeconds'], payloadPath=str(payload_path))


def build(native, static, out_dir, generation=None, generation_tag=None, result=None,
          timeout=DEFAULT_TIMEOUT, binary=None, analysis=False, local_index=False):
    """调用原生 build（blocks 1-11；块 12/默认视图留给 t21）。"""
    native = Path(native)
    static = Path(static)
    if not native.is_file():
        raise NativeStorebuildError('native.bin 不存在：%s' % native)
    if not static.is_file():
        raise NativeStorebuildError('static-dict.bin 不存在：%s' % static)
    args = ['build', '--native', str(native), '--static', str(static),
            '--out-dir', str(out_dir)]
    if generation is not None:
        args.extend(['--generation', str(generation)])
    if generation_tag is not None:
        args.extend(['--generation-tag', hex(int(generation_tag))])
    if analysis:
        args.append('--analysis')
    if not local_index:
        return _run_json_command(args, result, timeout, binary=binary)
    first = _run_json_command(args, result, timeout, binary=binary)
    info = build_local_index(out_dir, first)
    args.extend(['--index-payload', info['payloadPath']])
    final = _run_json_command(args, result, timeout, binary=binary)
    phases = final.setdefault('phases', {})
    phases['queryIndex'] = info['seconds']
    phases['defaultViews'] = info['defaultsSeconds']
    final['localIndex'] = {key: info[key] for key in
                           ('payloadBytes', 'tableCount', 'seconds',
                            'buildSeconds', 'defaultsSeconds')}
    if result is not None:
        Path(result).write_text(json.dumps(final, ensure_ascii=False, indent=1),
                                encoding='utf-8')
    return final


def _write_json_or_print(payload, path):
    if path:
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=1) + '\n',
                              encoding='utf-8')
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=1))


def main(argv=None):
    parser = argparse.ArgumentParser(description='R2 原生 store builder（M0/M1）')
    parser.add_argument('--version', action='store_true', help='打印原生协议/能力握手')
    parser.add_argument('--binary', default=None, help='覆盖 etm_storebuild 路径')
    sub = parser.add_subparsers(dest='command')
    prescan_parser = sub.add_parser('prescan', help='预扫描 native.bin 唯一范围')
    prescan_parser.add_argument('--native', required=True)
    prescan_parser.add_argument('--out', default=None)
    prescan_parser.add_argument('--json', default=None)
    prescan_parser.add_argument('--max-unique-ranges', type=int, default=None)
    diagnose_parser = sub.add_parser('diagnose', help='诊断 native.bin 计数/分段')
    diagnose_parser.add_argument('--native', required=True)
    diagnose_parser.add_argument('--static', required=True)
    diagnose_parser.add_argument('--ranges', default=None)
    diagnose_parser.add_argument('--json', default=None)
    diagnose_parser.add_argument('--checkpoint-instructions', type=int, default=4096)
    diagnose_parser.add_argument('--max-run-instructions', type=int, default=4096)
    diagnose_parser.add_argument('--run-log', default=None)
    build_parser = sub.add_parser('build', help='原生 build（blocks 1-11）')
    build_parser.add_argument('--native', required=True)
    build_parser.add_argument('--static', required=True)
    build_parser.add_argument('--out-dir', required=True)
    build_parser.add_argument('--generation', default=None)
    build_parser.add_argument('--generation-tag', default=None)
    build_parser.add_argument('--result', default=None)
    build_parser.add_argument('--analysis', action='store_true')
    build_parser.add_argument('--local-index', action='store_true')
    static_parser = sub.add_parser('static-info', help='读取/校验 static-dict.bin')
    static_parser.add_argument('--static', required=True)
    static_parser.add_argument('--json', default=None)
    args = parser.parse_args(argv)
    try:
        if args.version:
            _write_json_or_print(probe(args.binary), None)
            return 0
        if args.command == 'prescan':
            result = prescan(args.native, out=args.out, json_path=args.json,
                             max_unique_ranges=args.max_unique_ranges,
                             binary=args.binary)
            if args.json is None:
                _write_json_or_print(result, None)
            return 0
        if args.command == 'diagnose':
            result = diagnose(args.native, args.static, ranges=args.ranges,
                              json_path=args.json,
                              checkpoint_instructions=args.checkpoint_instructions,
                              max_run_instructions=args.max_run_instructions,
                              run_log=args.run_log, binary=args.binary)
            if args.json is None:
                _write_json_or_print(result, None)
            return 0
        if args.command == 'build':
            result = build(args.native, args.static, args.out_dir,
                           generation=args.generation,
                           generation_tag=args.generation_tag,
                           result=args.result, binary=args.binary,
                           analysis=args.analysis, local_index=args.local_index)
            if args.result is None:
                _write_json_or_print(result, None)
            return 0
        if args.command == 'static-info':
            result = inspect_static(args.static, json_path=args.json, binary=args.binary)
            if args.json is None:
                _write_json_or_print(result, None)
            return 0
        parser.print_help()
        return 2
    except NativeStorebuildError as error:
        print('native_storebuild: %s' % error, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
