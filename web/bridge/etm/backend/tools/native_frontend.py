#!/usr/bin/env python3
"""原生物理前端适配层（P5b）：调用 `etm/build-native/etm_frontend.exe`。

原生实现（`etm/native/frontend.cpp`）是 `etm/tools/trace_frontend.py`（P5a 参考实现）的
加速版本，语义**由参考实现定义**：本模块只负责

1. 找到二进制并做**版本握手**（`--version` 必须报告 ``trace-frontend-native/1``）——
   缺失、构建失败、版本不符都**明确报错**，绝不静默使用旧残留的可执行文件；
2. 调用它、读回结果 JSON，并把 physical/framing/字节指纹整理成可与参考实现逐字段比较的结构；
3. 提供 `run_reference()`：显式回退到 Python 参考实现（只有调用方主动要求时才回退）。

默认链路不变：`decode_stream.py` / `server.py` 没有引用本模块，原生前端只按需启用。

命令行等价用法::

    etm/build-native/etm_frontend.exe capture.kvdat --chunk-bytes 1048576 \\
        --json out.json --out out/capture
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # etm/
PROTOCOL = 'trace-frontend-native/1'
SCHEMA = 1
DEFAULT_TIMEOUT = 1800.0                            # 秒；1 s 合成输入原生侧也在 10 s 量级


class NativeFrontendError(RuntimeError):
    """原生前端不可用/结果不可信的基类。"""


class NativeFrontendUnavailable(NativeFrontendError):
    """二进制缺失或无法运行。"""


class NativeFrontendMismatch(NativeFrontendError):
    """二进制存在但协议/版本与适配层不一致（旧残留）。"""


def binary_path(root=None):
    """原生前端可执行文件路径（与 build-decoder.ps1 的输出目录一致）。"""
    import decode_trace
    base = Path(root) if root else decode_trace.ROOT
    return base / 'build-native' / 'etm_frontend.exe'


def _run_binary(path, args, timeout):
    completed = subprocess.run([str(path)] + [str(item) for item in args],
                               capture_output=True, timeout=timeout)
    return completed


def probe(path=None, timeout=60.0):
    """版本握手：返回 `--version` 的 JSON；失败抛明确异常。"""
    binary = Path(path) if path else binary_path()
    if not binary.exists():
        raise NativeFrontendUnavailable(
            f'缺少原生物理前端 {binary}；请先运行 pwsh -NoProfile -File etm/build-decoder.ps1。')
    try:
        completed = _run_binary(binary, ['--version'], timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NativeFrontendUnavailable(f'原生前端无法运行：{error}') from error
    if completed.returncode != 0:
        message = completed.stderr.decode('utf-8', 'replace').strip()
        raise NativeFrontendMismatch(
            f'原生前端 --version 退出码 {completed.returncode}：{message or "(无输出)"}')
    text = completed.stdout.decode('utf-8', 'replace').strip().splitlines()
    if not text:
        raise NativeFrontendMismatch('原生前端 --version 没有输出')
    try:
        info = json.loads(text[-1])
    except json.JSONDecodeError as error:
        raise NativeFrontendMismatch(f'原生前端 --version 输出不是 JSON：{text[-1]!r}') from error
    if info.get('protocol') != PROTOCOL:
        raise NativeFrontendMismatch(
            f'原生前端协议不匹配：期望 {PROTOCOL}，实际 {info.get("protocol")!r}'
            f'（{binary}）；请重新运行 build-decoder.ps1，不要使用旧残留二进制。')
    if info.get('schema') != SCHEMA:
        raise NativeFrontendMismatch(
            f'原生前端 schema 不匹配：期望 {SCHEMA}，实际 {info.get("schema")!r}；请重新构建。')
    return info


def provenance(path=None):
    """二进制指纹 + 版本信息（证据用）。"""
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


def available(path=None):
    try:
        probe(path)
        return True
    except NativeFrontendError:
        return False


def _normalize_config(config):
    config = dict(config or {})
    clock = int(config.get('clock', 0))
    data = [int(item) for item in config.get('data', [1, 2, 3, 4])]
    fraction = float(config.get('fraction', 0.25))
    glitch = float(config.get('glitch_ns', 0.0))
    traceid = int(config.get('traceid', 2))
    if not 0 < fraction < 1:
        raise NativeFrontendError('取样位置必须介于 0 和 1 之间。')
    if not 0 <= glitch <= 100:
        raise NativeFrontendError('软件滤波宽度须为 0～100 ns（0 为关闭）')
    if len(data) != 4 or len(set([clock] + data)) != 5:
        raise NativeFrontendError('需要五个不同通道：时钟 + D0/D1/D2/D3。')
    return dict(clock=clock, data=data, fraction=fraction, glitch_ns=glitch, traceid=traceid)


def run(source, config=None, chunk_bytes=1 << 20, chunk_plan=None, out_prefix=None,
        timeout=DEFAULT_TIMEOUT, binary=None, keep_json=False):
    """跑一次原生分块前端，返回与参考实现可比较的结果字典。

    ``out_prefix`` 给出时同时写出 `.tpiu.bin`/`.id<N>.bin`/`.physical.json`/`.framing.json`；
    ``chunk_plan`` 是正整数序列（覆盖块边界用）。
    """
    source = Path(source).resolve()
    if not source.exists():
        raise NativeFrontendError(f'输入不存在：{source}')
    if source.suffix.lower() != '.kvdat':
        raise NativeFrontendError('原生前端只支持 .kvdat 输入；CSV 请用参考实现 trace_frontend.py')
    config = _normalize_config(config)
    binary = Path(binary) if binary else binary_path()
    probe(binary)                      # 每次调用都握手：拒绝旧残留
    args = [str(source),
            '--clock', str(config['clock']),
            '--data', ','.join(str(item) for item in config['data']),
            '--fraction', repr(config['fraction']),
            '--glitch-ns', repr(config['glitch_ns']),
            '--traceid', str(config['traceid']),
            '--chunk-bytes', str(int(chunk_bytes)),
            '--quiet']
    if chunk_plan:
        args.extend(['--chunk-plan', ','.join(str(int(item)) for item in chunk_plan)])
    if out_prefix:
        args.extend(['--out', str(out_prefix)])
    handle = None
    if keep_json:
        json_path = Path(str(out_prefix) + '.native.json') if out_prefix else \
            source.with_suffix('.native.json')
        args.extend(['--json', str(json_path)])
    else:
        handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8')
        handle.close()
        json_path = Path(handle.name)
        args.extend(['--json', str(json_path)])
    started = time.perf_counter()
    try:
        completed = _run_binary(binary, args, timeout)
    except subprocess.TimeoutExpired as error:
        raise NativeFrontendError(f'原生前端超时（{timeout}s）：{source.name}') from error
    elapsed = time.perf_counter() - started
    stderr = completed.stderr.decode('utf-8', 'replace').strip()
    if completed.returncode != 0:
        raise NativeFrontendError(
            f'原生前端失败（退出码 {completed.returncode}）：{stderr or "(无输出)"}')
    try:
        result = json.loads(json_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise NativeFrontendError(f'原生前端结果 JSON 不可读：{error}') from error
    finally:
        if not keep_json:
            try:
                json_path.unlink()
            except OSError:
                pass
    if result.get('protocol') != PROTOCOL:
        raise NativeFrontendMismatch(
            f'原生前端结果协议不匹配：{result.get("protocol")!r}（期望 {PROTOCOL}）')
    result['engine'] = 'native'
    result['wallSeconds'] = elapsed
    if stderr:
        result['stderr'] = stderr
    return result


def run_reference(source, config=None, chunk_bytes=1 << 20):
    """参考实现（Python）跑同一条链路；用于显式回退与逐字段对照。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from trace_frontend import Frontend

    source = Path(source).resolve()
    config = _normalize_config(config)
    started = time.perf_counter()
    frontend = Frontend(source, config, chunk_bytes=chunk_bytes)
    result = frontend.run()
    elapsed = time.perf_counter() - started
    streams = {str(key): value for key, value in result['streams'].items()}
    return dict(engine='python', phase=result['phase'],
                tpiuBytes=len(result['tpiu']),
                tpiuSha256=hashlib.sha256(result['tpiu']).hexdigest(),
                streams={key: len(value) for key, value in streams.items()},
                streamSha256={key: hashlib.sha256(value).hexdigest()
                              for key, value in streams.items()},
                physical=result['physical'], framing=result['framing'],
                counters=dict(result['counters']), wallSeconds=elapsed,
                bytes={key: value for key, value in streams.items()})


def _same_path(left, right):
    return os.path.normcase(os.path.realpath(str(left))) == os.path.normcase(
        os.path.realpath(str(right)))


def compare(native, reference, strict_input=True):
    """逐字节/逐字段比较原生结果与参考结果。

    ``physical['input']`` 是绝对路径字符串（两个实现分别做过路径规范化），
    默认按 ``normcase(realpath)`` 归一后比较；其余字段（含浮点）要求**完全相等**。
    """
    diffs = []
    checks = {}
    checks['phase'] = native.get('phase') == reference.get('phase')
    checks['tpiuBytes'] = native.get('tpiuBytes') == reference.get('tpiuBytes')
    checks['tpiuSha256'] = native.get('tpiuSha256') == reference.get('tpiuSha256')
    checks['streams'] = {str(k): v for k, v in (native.get('streams') or {}).items()} == \
                        {str(k): v for k, v in (reference.get('streams') or {}).items()}
    checks['streamSha256'] = {str(k): v for k, v in (native.get('streamSha256') or {}).items()} == \
                             {str(k): v for k, v in (reference.get('streamSha256') or {}).items()}
    checks['framing'] = native.get('framing') == reference.get('framing')
    native_physical = dict(native.get('physical') or {})
    reference_physical = dict(reference.get('physical') or {})
    if 'input' in native_physical and 'input' in reference_physical:
        if strict_input:
            checks['input'] = _same_path(native_physical.pop('input'),
                                         reference_physical.pop('input'))
        else:
            native_physical.pop('input')
            reference_physical.pop('input')
    checks['physical'] = native_physical == reference_physical
    for field in checks:
        if not checks[field]:
            diffs.append(field)
    detail = {}
    if not checks['physical']:
        for field in sorted(set(native_physical) | set(reference_physical)):
            if native_physical.get(field) != reference_physical.get(field):
                detail.setdefault('physical', {})[field] = dict(
                    native=native_physical.get(field), reference=reference_physical.get(field))
    if not checks['framing']:
        framing_native = native.get('framing') or {}
        framing_reference = reference.get('framing') or {}
        for field in sorted(set(framing_native) | set(framing_reference)):
            if framing_native.get(field) != framing_reference.get(field):
                detail.setdefault('framing', {})[field] = dict(
                    native=framing_native.get(field), reference=framing_reference.get(field))
    return dict(passed=all(checks.values()), checks=checks, diffs=diffs, detail=detail)


def main():
    import argparse
    parser = argparse.ArgumentParser(description='原生物理前端适配层（P5b）')
    parser.add_argument('source', type=Path, help='capture.kvdat')
    parser.add_argument('--clock', type=int, default=0)
    parser.add_argument('--data', nargs=4, type=int, default=[1, 2, 3, 4], metavar='CH')
    parser.add_argument('--fraction', type=float, default=0.25)
    parser.add_argument('--glitch-ns', type=float, default=0.0)
    parser.add_argument('--traceid', type=int, default=2)
    parser.add_argument('--chunk-bytes', type=int, default=1 << 20)
    parser.add_argument('--out', type=Path, help='输出前缀')
    parser.add_argument('--compare', action='store_true', help='同时跑参考实现并逐字段对照')
    args = parser.parse_args()
    config = dict(clock=args.clock, data=list(args.data), fraction=args.fraction,
                  glitch_ns=args.glitch_ns, traceid=args.traceid)
    try:
        info = probe()
        print(f'原生前端：{info}')
        result = run(args.source, config, chunk_bytes=args.chunk_bytes, out_prefix=args.out)
    except NativeFrontendError as error:
        print(f'原生前端不可用：{error}', file=sys.stderr)
        return 2
    physical = result['physical']
    print(f"相位 {result['phase']}：tpiu {result['tpiuBytes']} B "
          f"（{result['tpiuSha256'][:16]}…），ETM {result['streams']}")
    print(f"时钟 {physical['clock_edges']} 条跳变 / {physical['nibbles']} nibble，"
          f"耗时 {result['timing']['total']:.2f} s")
    if args.compare:
        reference = run_reference(args.source, config, chunk_bytes=args.chunk_bytes)
        report = compare(result, reference)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report['passed'] else 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
