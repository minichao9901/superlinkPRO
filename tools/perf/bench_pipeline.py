#!/usr/bin/env python3
"""ETM 离线链路基准入口（仓库内可复用、参数化、不覆盖历史）。

完整链路：取样（CSV/KVDAT）→ TPIU 解复用 → OpenCSD 原生解码 → 事件展开 →
SQLite 浏览索引 → overview / page / timeline / window。

用法（--help 有完整说明）：

    python -X utf8 tools/perf/bench_pipeline.py \
        --record <记录目录或 inputs.json 键名> --code <etm 源码快照根> \
        --kind csv|kvdat --variant <名字> --repeat N --output <输出根> [--window]

设计要点：
* 每个样本在**独立子进程**里冷启动执行，互不共享模块状态；样本目录独占，
  绝不覆盖旧 run。--repeat 与多 --record/--code 组合按轮次交错，便于旧/新 A/B。
* 每次运行写 run.json：输入指纹、代码源文件哈希（含 git revision/dirty 清单）、
  构建参数、环境、每阶段墙钟/CPU、进程树峰值 RSS、产物字节数与 SHA256、
  overview/page/timeline/window 耗时。
* 100 ms 采样线程记录进程树（含 OpenCSD 子进程）峰值 RSS 与 CPU 时间。
* 失败保留原始日志（bench.log / error.log / process.log）并以非零码退出。
* 只读环境信息；不修改 Defender、电源方案或任何系统设置。
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import _common as perf  # noqa: E402

ARTIFACT_FILES = (
    'etm.bin', 'tpiu.bin', 'native.jsonl', 'native.log', 'native-config.txt',
    'framing.json', 'physical.json', 'decoded.json', 'demux-errors.json',
    'events.jsonl', 'packets.jsonl', 'elements.jsonl',
    'browse-columns.tsv', 'browse-v3.sqlite',
    'sources.json', 'session.json',
)
# 字节内容随 run 目录/mtime 变化、不适合跨 run 做字节比较的产物。
VOLATILE_ARTIFACTS = {'browse-v3.sqlite'}


# --------------------------------------------------------------------------- 日志
class Logger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open('a', encoding='utf-8')
        self.started = time.perf_counter()

    def __call__(self, message):
        line = f'[{time.strftime("%H:%M:%S")}] +{time.perf_counter() - self.started:8.3f}s {message}'
        print(line, flush=True)
        self.handle.write(line + '\n')
        self.handle.flush()

    def close(self):
        try:
            self.handle.close()
        except Exception:
            pass


# --------------------------------------------------------------- 进程树资源采样
class ResourceSampler(threading.Thread):
    """100 ms 采样本进程 + 子孙进程（OpenCSD 解码器）的 RSS 与 CPU 时间。"""

    def __init__(self, interval_ms=100):
        super().__init__(name='etm-perf-resource-sampler', daemon=True)
        self.interval = max(20, int(interval_ms)) / 1000.0
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self.peak_rss = 0
        self.peak_tree_rss = 0
        self.peak_processes = 0
        self.samples = 0
        self.error = None
        self.last = {'treeCpuSeconds': 0.0, 'parentCpuSeconds': 0.0, 'treeRssBytes': 0,
                     'at': 0.0}

    def run(self):
        try:
            import psutil
            parent = psutil.Process(os.getpid())
        except Exception as error:  # psutil 缺失时如实降级，不影响基准本身
            self.error = f'{type(error).__name__}: {error}'
            return
        while not self._stop_event.is_set():
            try:
                processes = [parent]
                try:
                    processes.extend(parent.children(recursive=True))
                except Exception:
                    pass
                rss = cpu = 0
                alive = 0
                for process in processes:
                    try:
                        rss += process.memory_info().rss
                        times = process.cpu_times()
                        cpu += times.user + times.system
                        alive += 1
                    except Exception:
                        continue
                try:
                    own = parent.memory_info().rss
                    own_times = parent.cpu_times()
                    own_cpu = own_times.user + own_times.system
                except Exception:
                    own, own_cpu = 0, 0.0
                with self._lock:
                    self.samples += 1
                    self.peak_rss = max(self.peak_rss, own)
                    self.peak_tree_rss = max(self.peak_tree_rss, rss)
                    self.peak_processes = max(self.peak_processes, alive)
                    self.last = {'treeCpuSeconds': cpu, 'parentCpuSeconds': own_cpu,
                                 'treeRssBytes': rss, 'at': time.perf_counter()}
            except Exception as error:
                self.error = f'{type(error).__name__}: {error}'
                break
            self._stop_event.wait(self.interval)

    def stop(self):
        self._stop_event.set()
        # 早失败时线程可能尚未 start（threading.join 会拒绝 join 未启动线程）。
        if self.is_alive():
            self.join(timeout=3)

    def snapshot(self):
        with self._lock:
            return dict(self.last)

    def summary(self):
        with self._lock:
            return {'intervalMs': int(self.interval * 1000), 'samples': self.samples,
                    'peakRssBytes': self.peak_rss, 'peakTreeRssBytes': self.peak_tree_rss,
                    'peakProcessCount': self.peak_processes,
                    'treeCpuSeconds': round(self.last.get('treeCpuSeconds', 0.0), 6),
                    'parentCpuSeconds': round(self.last.get('parentCpuSeconds', 0.0), 6),
                    'error': self.error}


# --------------------------------------------------------------- 阶段计时（插桩）
class StageRecorder:
    """对指定函数做精确计时；可被多次调用（例如 demux 逐相位）累加。"""

    def __init__(self, sampler):
        self.sampler = sampler
        self.stages = {}
        self.missing = []
        self._lock = threading.Lock()

    def _record(self, label, wall, cpu, before, after):
        child = max(0.0, (after.get('treeCpuSeconds', 0.0) - before.get('treeCpuSeconds', 0.0)) - cpu)
        with self._lock:
            item = self.stages.setdefault(label, {'calls': 0, 'wallSeconds': 0.0,
                                                  'procCpuSeconds': 0.0,
                                                  'childCpuSecondsApprox': 0.0})
            item['calls'] += 1
            item['wallSeconds'] += wall
            item['procCpuSeconds'] += cpu
            item['childCpuSecondsApprox'] += child

    def _wrap(self, label, original):
        @functools.wraps(original)
        def wrapped(*args, **kwargs):
            before = self.sampler.snapshot()
            wall_start = time.perf_counter()
            cpu_start = time.process_time()
            try:
                return original(*args, **kwargs)
            finally:
                wall = time.perf_counter() - wall_start
                cpu = time.process_time() - cpu_start
                self._record(label, wall, cpu, before, self.sampler.snapshot())
        return wrapped

    def wrap_module(self, module, name, label):
        if not hasattr(module, name):
            self.missing.append(f'{getattr(module, "__name__", module)}.{name}')
            return False
        setattr(module, name, self._wrap(label, getattr(module, name)))
        return True

    def wrap_class_method(self, cls, name, label):
        if not hasattr(cls, name):
            self.missing.append(f'{cls.__name__}.{name}')
            return False
        setattr(cls, name, self._wrap(label, getattr(cls, name)))
        return True

    def report(self):
        with self._lock:
            result = {}
            for label, item in self.stages.items():
                wall = item['wallSeconds']
                proc = item['procCpuSeconds']
                child = item['childCpuSecondsApprox']
                result[label] = {
                    'calls': item['calls'],
                    'wallSeconds': round(wall, 6),
                    'procCpuSeconds': round(proc, 6),
                    'childCpuSecondsApprox': round(child, 6),
                    'totalCpuSecondsApprox': round(proc + child, 6),
                }
            return dict(sorted(result.items()))


# ------------------------------------------------------------------------- 参数解析
def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog='bench_pipeline.py',
        description='ETM 离线链路基准：取样 → TPIU 解复用 → OpenCSD 原生解码 → 事件展开 → '
                    'SQLite 索引 → overview/page/timeline/window。每次运行写入独立 run 目录，绝不覆盖历史。',
        epilog='提示：--record 可重复（也可直接写 inputs.json 里的键名，如 steady），'
               '--code 可重复；多个组合按轮次交错执行，便于同一输入做旧/新 A/B。',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--record', action='append', metavar='DIR|KEY',
                        help='记录目录；也接受 tools/perf/inputs.json 里的键名。可重复。')
    parser.add_argument('--code', action='append', metavar='DIR',
                        help='etm 源码快照根（含 tools/、native/、build-native/）。可重复做 A/B。')
    parser.add_argument('--kind', action='append', choices=['csv', 'kvdat'],
                        help='原始输入格式；单个值作用于全部记录，或与 --record 一一对应。缺省 csv。')
    parser.add_argument('--variant', action='append', metavar='NAME',
                        help='变体名（输出子目录名）；单个值作用于全部 --code，或与 --code 一一对应。')
    parser.add_argument('--repeat', type=int, default=1, metavar='N',
                        help='每个组合重复次数；多组合时交错执行。')
    parser.add_argument('--output', type=Path, metavar='DIR',
                        help='输出根目录；每个样本写入 <output>/<variant>/runs/<runId>/，绝不覆盖。')
    parser.add_argument('--window', action='store_true',
                        help='除整段窗口外，再测量缓存命中的重复请求与半区间窗口。')
    parser.add_argument('--inputs', type=Path, default=HERE / 'inputs.json',
                        help='固定输入清单 JSON；默认仓库内 tools/perf/inputs.json。')
    parser.add_argument('--sample-interval-ms', type=int, default=100,
                        help='进程树 RSS/CPU 采样间隔（毫秒）。')
    parser.add_argument('--run-timeout', type=float, default=1800.0,
                        help='单个样本的硬超时（秒）；超时后保留日志并按失败记录。')
    parser.add_argument('--internal-run', type=Path, default=None,
                        help=argparse.SUPPRESS)  # 子进程内部模式：执行单个样本
    return parser.parse_args(argv)


# ------------------------------------------------------------------- 组合与指纹
def _dedupe(labels):
    seen = {}
    result = []
    for label in labels:
        count = seen.get(label, 0)
        seen[label] = count + 1
        result.append(label if count == 0 else f'{label}-{count + 1}')
    return result


def _build_combos(args, root, catalog) -> list:
    records = [perf.resolve_record(raw, root, catalog) for raw in (args.record or [])]
    codes = [Path(raw).resolve() for raw in (args.code or [])]
    kinds = list(args.kind or ['csv'])
    if len(kinds) == 1:
        kinds = kinds * len(records)
    elif len(kinds) != len(records):
        raise SystemExit('--kind 的数量必须为 1，或与 --record 数量一致。')
    if args.variant:
        variants = list(args.variant)
        if len(variants) == 1 and len(codes) == 1:
            labels = variants
        elif len(variants) == len(codes):
            labels = variants
        elif len(variants) == 1:
            labels = [f'{variants[0]}-c{index + 1}' for index in range(len(codes))]
        else:
            raise SystemExit('--variant 的数量必须为 1，或与 --code 数量一致。')
    else:
        labels = []
        for code in codes:
            name = code.name
            if name.lower() == 'etm':
                parent = code.parent.name
                name = parent if parent and parent != 'tmp' else 'baseline'
            labels.append(name)
    labels = _dedupe(labels)
    combos = []
    for record_index, (record_key, record_path) in enumerate(records):
        if not record_path.is_dir():
            raise SystemExit(f'记录目录不存在：{record_path}')
        for code_index, code_path in enumerate(codes):
            if not code_path.is_dir():
                raise SystemExit(f'--code 目录不存在：{code_path}')
            combos.append({
                'recordKey': record_key,
                'recordPath': record_path,
                'kind': kinds[record_index],
                'codePath': code_path,
                'variant': labels[code_index],
                'comboIndex': len(combos),
            })
    return combos


def _input_fingerprint(record_path, kind, root, catalog) -> dict:
    record_path = Path(record_path).resolve()
    raw = f'capture.{kind}'
    if not (record_path / raw).is_file():
        raise SystemExit(f'记录目录缺少 {raw}：{record_path}')
    files = {}
    for name in perf.KEY_RECORD_FILES:
        path = record_path / name
        if path.is_file():
            files[name] = perf.file_entry(path, rel=name)
    session_path = record_path / 'session.json'
    session = json.loads(session_path.read_text(encoding='utf-8')) if session_path.is_file() else {}
    return {
        'recordKey': perf.find_inputs_entry(record_path, catalog)[0],
        'recordPath': str(record_path),
        'recordPathRelative': perf.rel_to(record_path, root),
        'raw': raw,
        'kind': kind,
        'files': files,
        'directoryManifest': perf.directory_manifest(record_path),
        'catalog': perf.catalog_fingerprint_check(record_path, catalog),
        'session': {
            'name': session.get('name'),
            'seconds': session.get('seconds'),
            'rate': session.get('rate'),
            'clock': session.get('clock'),
            'data': session.get('data'),
            'fraction': session.get('fraction'),
            'glitchNs': session.get('glitchNs'),
            'config': session.get('config'),
            'summary': session.get('summary'),
            'targetSnapshotName': (session.get('targetSnapshot') or {}).get('name'),
            'etmVersion': (session.get('targetSnapshot') or {}).get('etmVersion'),
        },
    }


def _unique_run_dir(variant_root, run_id) -> Path:
    candidate = variant_root / 'runs' / run_id
    counter = 1
    while True:
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            counter += 1
            candidate = variant_root / 'runs' / f'{run_id}-{counter}'


# ------------------------------------------------------------------- 子进程单样本
def _safe_sid(record_path) -> str:
    name = Path(record_path).name
    return re.sub(r'[^A-Za-z0-9_-]', '-', name) or 'record'


def _link_or_copy(source, dest) -> str:
    source, dest = Path(source), Path(dest)
    if dest.exists():
        dest.unlink()
    try:
        os.link(source, dest)
        return 'hardlink'
    except OSError:
        shutil.copy2(source, dest)
        return 'copy'


def _prepare_record(spec, run_dir) -> Path:
    source = Path(spec['recordPath'])
    raw = f"capture.{spec['kind']}"
    if not (source / raw).is_file():
        raise SystemExit(f'记录缺少原始输入 {raw}：{source}')
    dest = run_dir / 'records' / spec['sid']
    dest.mkdir(parents=True, exist_ok=True)
    prepared = {}
    for name in (raw, 'firmware.elf', 'sources.json', 'capture.csv.la.json'):
        item = source / name
        if not item.is_file():
            if name == 'firmware.elf':
                raise SystemExit(f'记录缺少 firmware.elf：{source}')
            continue
        mode = _link_or_copy(item, dest / name)
        prepared[name] = {'mode': mode, 'bytes': item.stat().st_size}
    session = json.loads((source / 'session.json').read_text(encoding='utf-8'))
    session['input'] = raw  # 只改副本，源记录保持只读
    payload = json.dumps(session, ensure_ascii=False)
    (dest / 'session.json').write_text(payload, encoding='utf-8')
    prepared['session.json'] = {'mode': 'rewritten', 'bytes': len(payload.encode('utf-8'))}
    return dest


def _load_pipeline(code_root):
    code = Path(code_root).resolve()
    for candidate in (code / 'tools', code / 'tools' / 'la', code):
        path = str(candidate)
        if path not in sys.path:
            sys.path.insert(0, path)
    import server  # noqa: E402 - 运行期按 --code 注入
    import decode_stream  # noqa: E402
    import decode_trace  # noqa: E402
    import trace_port  # noqa: E402
    import tpiu_demux  # noqa: E402
    from record_store import RecordStore  # noqa: E402
    return {'server': server, 'decode_stream': decode_stream, 'decode_trace': decode_trace,
            'trace_port': trace_port, 'tpiu_demux': tpiu_demux, 'RecordStore': RecordStore}


def _install_instrumentation(modules, recorder):
    recorder.wrap_module(modules['trace_port'], 'extract', 'sample')
    recorder.wrap_module(modules['tpiu_demux'], 'demux', 'demux')
    recorder.wrap_module(modules['decode_stream'], '_run_native', 'nativeDecode')
    recorder.wrap_module(modules['decode_stream'], '_expand_fast', 'expand')
    recorder.wrap_class_method(modules['RecordStore'], 'ensure', 'index')
    recorder.wrap_class_method(getattr(modules['decode_trace'], 'Image'), '__init__', 'elfImage')


def _pipeline(modules, sid, logger) -> dict:
    """与 server._decode_job 的流式分支同序：解码 → 写 decoded.json → 发布 → 建索引。"""
    server, decoder = modules['server'], modules['decode_stream']
    folder = server.directory(sid)
    metadata = server.meta(sid)
    sources_path = folder / 'sources.json'
    sources = json.loads(sources_path.read_text(encoding='utf-8')) if sources_path.is_file() else None
    raw = folder / metadata['input']
    job = server.new_job(sid, 'decoding', kind='decode')
    marker = folder / 'decode-in-progress.json'
    server.save_json(marker, dict(started=time.time(), job=job))
    logger(f'decode start sid={sid} input={raw.name} bytes={raw.stat().st_size}')
    try:
        if raw.suffix.lower() in ('.csv', '.kvdat'):
            result = decoder.decode_capture_stream(
                raw, folder / 'firmware.elf', folder, metadata['config'], sources,
                fraction=metadata.get('fraction', .25), clock=metadata.get('clock', 0),
                data=metadata.get('data', [1, 2, 3, 4]), glitch_ns=metadata.get('glitchNs', 0))
        else:
            result = decoder.decode_stream(raw.read_bytes(), folder / 'firmware.elf', folder,
                                           metadata['config'], sources)
            result['capture'] = {'name': raw.name, 'sha256': server.file_hash(raw)}
        result['targetSnapshot'] = metadata.get('targetSnapshot')
        result['captureScope'] = metadata.get('captureScope', 'unfiltered-window')
        result['captureStart'] = server.capture_start_info(
            server.attach_streams(dict(result), folder)['events'], metadata)
        result['laFilter'] = metadata.get('laFilter')
        server.save_json(folder / 'decoded.json', result)
        if 'sources' in result:
            server.save_json(folder / 'sources.json', result['sources'])
        metadata['summary'] = result['summary']
        metadata.pop('error', None)
        metadata.pop('errorStage', None)
        metadata['decodeStatus'] = 'done'
        server.save(sid, metadata)
        marker.unlink(missing_ok=True)
        server.change(job, state='done', summary=result['summary'])
        logger('decode done; building browse index')
        server.browse_store(sid).ensure()  # 幂等：streamed 分支已建好时几乎为 0
        logger('browse index ready')
        return result['summary']
    except (Exception, SystemExit) as error:
        server.record_failure(sid, error, 'decode')
        server.change(job, state='error', error=str(error))
        raise


def _timed_call(callable_obj, *args, **kwargs):
    start = time.perf_counter()
    value = callable_obj(*args, **kwargs)
    return value, time.perf_counter() - start


def _write_api(run_dir, name, value, call_seconds):
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    encoded = text.encode('utf-8')
    relative = f'api/{name}.json'
    path = run_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    path.write_bytes(encoded)
    serialize = time.perf_counter() - start
    return {'path': relative, 'callSeconds': round(call_seconds, 6),
            'serializeSeconds': round(serialize, 6),
            'seconds': round(call_seconds + serialize, 6),
            'bytes': len(encoded), 'sha256': perf.sha256_bytes(encoded)}


def _measure_api(store, run_dir, spec, logger) -> dict:
    api = {}
    overview, seconds = _timed_call(store.overview)
    api['overview'] = _write_api(run_dir, 'overview', overview, seconds)
    page, seconds = _timed_call(store.page)
    api['page'] = _write_api(run_dir, 'page', page, seconds)
    timeline, seconds = _timed_call(store.function_timeline)
    api['timeline'] = _write_api(run_dir, 'timeline', timeline, seconds)
    window, seconds = _timed_call(store.window)
    api['window'] = _write_api(run_dir, 'window', window, seconds)
    logger(f"api overview={api['overview']['seconds']:.3f}s page={api['page']['seconds']:.3f}s "
           f"timeline={api['timeline']['seconds']:.3f}s window={api['window']['seconds']:.3f}s")
    if spec.get('window'):
        _, seconds = _timed_call(store.window)
        api['windowCached'] = {'seconds': round(seconds, 6), 'note': '同一整段窗口的第二次请求（应命中缓存）'}
        total = overview.get('eventCount') or page.get('total') or 0
        first = overview.get('firstSeq') or 0
        if total > 2:
            start, end = first + total // 2, first + total - 1
            try:
                half, seconds = _timed_call(store.window, start, end)
                api['windowHalf'] = _write_api(run_dir, 'window-half', half, seconds)
                api['windowHalf'].update(fromSeq=start, toSeq=end)
            except Exception as error:
                api['windowHalf'] = {'error': f'{type(error).__name__}: {error}'}
    return api


def _json_normalized_sha256(path, prefix) -> str:
    """把 JSON 里位于 prefix 之下的绝对路径替换成 <record>，供跨 run/跨变体比较。"""
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    token = str(Path(prefix).resolve()).replace('\\', '/')

    def walk(node):
        if isinstance(node, str):
            text = node.replace('\\', '/')
            if token and text.startswith(token):
                return '<record>' + text[len(token):]
            return node
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {key: walk(item) for key, item in node.items()}
        return node

    return perf.canonical_json_sha256(walk(value))


def _artifact_compare_sha(path, name, record_dir):
    """跨 run 可比较的产物哈希；字节天然不稳定的产物返回 None。"""
    if name in VOLATILE_ARTIFACTS:
        return None
    if name == 'browse-columns.tsv':
        return perf.sha256_skip_first_line(path)
    if name == 'native-config.txt':
        return perf.normalized_text_sha256(path)
    if name in ('decoded.json', 'physical.json', 'framing.json') or name.startswith('api/'):
        return _json_normalized_sha256(path, record_dir)
    return perf.sha256_file(path)


def _collect_artifacts(run_dir, spec) -> tuple:
    folder = Path(run_dir) / 'records' / spec['sid']
    artifacts = {}
    total = 0
    comparable_total = 0
    for name in ARTIFACT_FILES:
        path = folder / name
        if not path.is_file():
            continue
        entry = {'bytes': path.stat().st_size, 'sha256': perf.sha256_file(path)}
        entry['compareSha256'] = _artifact_compare_sha(path, name, folder)
        entry['volatile'] = entry['compareSha256'] is None
        entry['pathDependent'] = name in ('decoded.json', 'physical.json', 'native-config.txt',
                                          'browse-columns.tsv', 'framing.json')
        artifacts[name] = entry
        total += entry['bytes']
        if not entry['pathDependent']:
            comparable_total += entry['bytes']
    for path in sorted((Path(run_dir) / 'api').glob('*.json')):
        relative = f'api/{path.name}'
        entry = {'bytes': path.stat().st_size, 'sha256': perf.sha256_file(path),
                 'compareSha256': _json_normalized_sha256(path, folder),
                 'volatile': False, 'pathDependent': True}
        artifacts[relative] = entry
        total += entry['bytes']
    return artifacts, total, comparable_total


def _reference_matches(run_dir, spec) -> dict:
    source = Path(spec['recordPath'])
    folder = Path(run_dir) / 'records' / spec['sid']
    matches = {}
    for name in ('etm.bin', 'tpiu.bin', 'native.jsonl', 'sources.json', 'events.jsonl',
                 'packets.jsonl', 'elements.jsonl'):
        expected, actual = source / name, folder / name
        if not expected.is_file() or not actual.is_file():
            continue
        try:
            expected_sha = _artifact_compare_sha(expected, name, source)
            actual_sha = _artifact_compare_sha(actual, name, folder)
        except Exception as error:
            matches[name] = {'error': f'{type(error).__name__}: {error}'}
            continue
        matches[name] = {'sourceBytes': expected.stat().st_size,
                         'actualBytes': actual.stat().st_size,
                         'match': expected_sha == actual_sha,
                         'sourceCompareSha256': expected_sha, 'actualCompareSha256': actual_sha}
    return matches


def _run_single(spec_path) -> int:
    spec = json.loads(Path(spec_path).read_text(encoding='utf-8'))
    run_dir = Path(spec['runDir'])
    logger = Logger(run_dir / 'bench.log')
    sampler = ResourceSampler(spec.get('sampleIntervalMs', 100))
    recorder = None
    code = spec.get('codeFingerprint') or {}
    result = {
        'schema': perf.SCHEMA_RUN,
        'runId': spec['runId'],
        'variant': spec['variant'],
        'comboIndex': spec.get('comboIndex'),
        'repeatIndex': spec.get('repeatIndex'),
        'kind': spec['kind'],
        'status': 'error',
        'startedAt': perf.iso_now(),
        'argv': sys.argv,
        'cwd': str(Path.cwd()),
        'recordKey': spec.get('recordKey'),
        'sid': spec['sid'],
        'codePath': spec.get('codePath'),
        'code': code,
        'input': spec.get('inputFingerprint'),
        'build': code.get('build'),
        'decoderBinaries': code.get('binaries'),
        'environment': spec.get('environment'),
        'instrumentation': None,
    }
    try:
        _prepare_record(spec, run_dir)
        logger(f"record prepared at {run_dir / 'records' / spec['sid']}")
        modules = _load_pipeline(spec['codePath'])
        # 记录目录重定向到本次 run 的副本；源记录始终只读。
        modules['server'].RECORDS = run_dir / 'records'
        logger(f'pipeline modules: server={modules["server"].__file__} '
               f'records={modules["server"].RECORDS}')
        recorder = StageRecorder(sampler)
        _install_instrumentation(modules, recorder)
        result['instrumentation'] = {'wrapped': ['trace_port.extract', 'tpiu_demux.demux',
                                                 'decode_stream._run_native',
                                                 'decode_stream._expand_fast',
                                                 'RecordStore.ensure', 'decode_trace.Image.__init__']}
        sampler.start()
        started, cpu_started = time.perf_counter(), time.process_time()
        summary = _pipeline(modules, spec['sid'], logger)
        result['decodeAndIndexSeconds'] = round(time.perf_counter() - started, 6)
        result['decodeAndIndexProcCpuSeconds'] = round(time.process_time() - cpu_started, 6)
        result['decodedSummary'] = summary
        store = modules['server'].browse_store(spec['sid'])
        result['api'] = _measure_api(store, run_dir, spec, logger)
        result['status'] = 'ok'
    except BaseException as error:  # 包括 KeyboardInterrupt：仍要落盘日志和 run.json
        result['error'] = f'{type(error).__name__}: {error}'
        result['traceback'] = traceback.format_exc()
        logger('ERROR ' + result['traceback'])
        try:
            (run_dir / 'error.log').write_text(result['traceback'], encoding='utf-8')
        except Exception:
            pass
    finally:
        sampler.stop()
        if recorder is not None:
            result['stages'] = recorder.report()
            result['instrumentationMissing'] = list(recorder.missing)
        result['resources'] = sampler.summary()
        try:
            result['artifacts'], result['artifactBytes'], result['artifactBytesComparable'] = \
                _collect_artifacts(run_dir, spec)
        except Exception as error:
            result['artifactError'] = f'{type(error).__name__}: {error}'
        try:
            result['referenceMatches'] = _reference_matches(run_dir, spec)
        except Exception as error:
            result['referenceError'] = f'{type(error).__name__}: {error}'
        result['finishedAt'] = perf.iso_now()
        try:
            if result['status'] == 'ok':
                duration = (time.perf_counter() - logger.started)
                result['runWallSeconds'] = round(duration, 6)
            perf.write_json(run_dir / 'run.json', result)
        except Exception as error:
            logger(f'无法写 run.json：{error}')
        logger(f"run finished status={result['status']}")
        logger.close()
    return 0 if result['status'] == 'ok' else 1


# ------------------------------------------------------------------------- 编排
def _summarize(runs, combos) -> dict:
    groups = {}
    for run in runs:
        groups.setdefault(run['variant'], []).append(run)
    summary = {}
    stage_labels = set()
    for run in runs:
        stage_labels.update((run.get('stages') or {}).keys())
    for variant, items in groups.items():
        ok = [run for run in items if run['status'] == 'ok']
        metrics = {
            'decodeAndIndexSeconds': perf.spread([run.get('decodeAndIndexSeconds') for run in ok]),
            'overviewSeconds': perf.spread([(run.get('api') or {}).get('overview', {}).get('seconds')
                                            for run in ok]),
            'pageSeconds': perf.spread([(run.get('api') or {}).get('page', {}).get('seconds') for run in ok]),
            'timelineSeconds': perf.spread([(run.get('api') or {}).get('timeline', {}).get('seconds')
                                            for run in ok]),
            'windowSeconds': perf.spread([(run.get('api') or {}).get('window', {}).get('seconds')
                                          for run in ok]),
            'peakTreeRssBytes': perf.spread([(run.get('resources') or {}).get('peakTreeRssBytes')
                                             for run in ok]),
            'treeCpuSeconds': perf.spread([(run.get('resources') or {}).get('treeCpuSeconds')
                                           for run in ok]),
            'artifactBytes': perf.spread([run.get('artifactBytes') for run in ok]),
            'artifactBytesComparable': perf.spread([run.get('artifactBytesComparable') for run in ok]),
        }
        for label in sorted(stage_labels):
            metrics[f'stage:{label}'] = perf.spread(
                [((run.get('stages') or {}).get(label) or {}).get('wallSeconds') for run in ok])
        summary[variant] = {
            'runs': len(items), 'ok': len(ok),
            'failed': [{'runId': run['runId'], 'error': run.get('error')} for run in items
                       if run['status'] != 'ok'],
            'runIds': [run['runId'] for run in items],
            'codeRevision': (items[0].get('code') or {}).get('git', {}).get('revision'),
            'codeSourceDigest': (items[0].get('code') or {}).get('sourceDigest'),
            'decoderSha256': ((items[0].get('code') or {}).get('binaries') or {}).get(
                'etm_decode.exe', {}).get('sha256'),
            'recordKeys': sorted({combo['recordKey'] or Path(combo['recordPath']).name for combo in combos
                                  if combo['variant'] == variant}),
            'metrics': metrics,
        }
    return summary


def main(argv=None) -> int:
    args = _parse_args(argv)
    if args.internal_run:
        return _run_single(args.internal_run)
    if not args.record or not args.code or not args.output:
        raise SystemExit('缺少必填参数：--record / --code / --output（见 --help）。')
    if args.repeat < 1:
        raise SystemExit('--repeat 必须 >= 1。')
    root = perf.repo_root()
    catalog = perf.load_inputs(args.inputs, root) if args.inputs else None
    combos = _build_combos(args, root, catalog)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    environment = perf.environment(output)
    invocation_stamp = perf.utc_stamp()
    invocation = {
        'schema': 'etm-perf-invocation/1',
        'invocationId': f'{invocation_stamp}-{os.getpid()}',
        'startedAt': perf.iso_now(),
        'argv': sys.argv,
        'cwd': str(Path.cwd()),
        'repoRoot': str(root),
        'inputsFile': str(args.inputs.resolve()) if args.inputs else None,
        'repeat': args.repeat,
        'window': args.window,
        'combos': [{'comboIndex': combo['comboIndex'], 'recordKey': combo['recordKey'],
                    'recordPath': str(combo['recordPath']), 'kind': combo['kind'],
                    'codePath': str(combo['codePath']), 'variant': combo['variant']}
                   for combo in combos],
        'environment': environment,
        'runs': [],
    }
    input_fingerprints = {}
    code_fingerprints = {}
    print(f'bench_pipeline: {len(combos)} combo(s) x {args.repeat} repeat(s) -> {output}', flush=True)
    for repeat in range(args.repeat):
        for combo in combos:
            key = (str(combo['recordPath']), combo['kind'])
            if key not in input_fingerprints:
                print(f'计算输入指纹：{combo["recordPath"]} ({combo["kind"]})', flush=True)
                input_fingerprints[key] = _input_fingerprint(
                    combo['recordPath'], combo['kind'], root, catalog)
            code_key = str(combo['codePath'])
            if code_key not in code_fingerprints:
                print(f'计算代码指纹：{combo["codePath"]}', flush=True)
                code_fingerprints[code_key] = perf.code_fingerprint(combo['codePath'])
            variant_root = output / combo['variant']
            run_id = f'{invocation_stamp}-c{combo["comboIndex"] + 1:02d}-r{repeat + 1:02d}'
            run_dir = _unique_run_dir(variant_root, run_id)
            spec = {
                'schema': 'etm-perf-run-spec/1',
                'runId': run_dir.name,
                'variant': combo['variant'],
                'comboIndex': combo['comboIndex'],
                'repeatIndex': repeat,
                'recordKey': combo['recordKey'],
                'recordPath': str(combo['recordPath']),
                'sid': _safe_sid(combo['recordPath']),
                'kind': combo['kind'],
                'codePath': str(combo['codePath']),
                'runDir': str(run_dir),
                'window': bool(args.window),
                'sampleIntervalMs': args.sample_interval_ms,
                'inputsFile': str(args.inputs.resolve()) if args.inputs else None,
                'inputFingerprint': input_fingerprints[key],
                'codeFingerprint': code_fingerprints[code_key],
                'environment': environment,
            }
            spec_path = run_dir / 'run-spec.json'
            perf.write_json(spec_path, spec)
            command = [sys.executable, '-X', 'utf8', str(Path(__file__).resolve()),
                       '--internal-run', str(spec_path)]
            print(f'[{repeat + 1}/{args.repeat}] {combo["variant"]} {run_dir.name}', flush=True)
            started = time.perf_counter()
            stdout = stderr = ''
            try:
                completed = subprocess.run(command, cwd=str(root), capture_output=True, text=True,
                                           encoding='utf-8', errors='replace',
                                           timeout=args.run_timeout)
                stdout, stderr, exit_code = completed.stdout or '', completed.stderr or '', completed.returncode
            except subprocess.TimeoutExpired as error:
                stdout = (error.stdout or b'').decode('utf-8', 'replace') if isinstance(error.stdout, bytes) else (error.stdout or '')
                stderr = (error.stderr or b'').decode('utf-8', 'replace') if isinstance(error.stderr, bytes) else (error.stderr or '')
                exit_code = 'timeout'
            except Exception as error:
                stderr = f'{type(error).__name__}: {error}'
                exit_code = 'spawn-error'
            duration = time.perf_counter() - started
            (run_dir / 'process.log').write_text(
                f'command: {" ".join(command)}\nexitCode: {exit_code}\nseconds: {duration:.3f}\n'
                f'--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n', encoding='utf-8')
            run_json = run_dir / 'run.json'
            if not run_json.is_file():
                stub = {
                    'schema': perf.SCHEMA_RUN, 'runId': run_dir.name, 'variant': combo['variant'],
                    'status': 'error', 'startedAt': perf.iso_now(), 'finishedAt': perf.iso_now(),
                    'error': f'子进程未写出 run.json（exitCode={exit_code}）',
                    'process': {'exitCode': exit_code, 'seconds': round(duration, 6),
                                'stdoutTail': stdout[-4000:], 'stderrTail': stderr[-4000:]},
                }
                perf.write_json(run_json, stub)
            try:
                run = json.loads(run_json.read_text(encoding='utf-8'))
            except Exception as error:
                run = {'runId': run_dir.name, 'variant': combo['variant'], 'status': 'error',
                       'error': f'run.json 解析失败：{error}'}
            run['process'] = {'exitCode': exit_code, 'seconds': round(duration, 6)}
            invocation['runs'].append({
                'runId': run.get('runId'), 'variant': run.get('variant'),
                'status': run.get('status'), 'runDir': str(run_dir),
                'decodeAndIndexSeconds': run.get('decodeAndIndexSeconds'),
                'error': run.get('error'),
            })
            with (variant_root / 'runs.jsonl').open('a', encoding='utf-8') as handle:
                handle.write(json.dumps({'runId': run.get('runId'), 'variant': run.get('variant'),
                                         'status': run.get('status'), 'runDir': str(run_dir),
                                         'decodeAndIndexSeconds': run.get('decodeAndIndexSeconds'),
                                         'peakTreeRssBytes': (run.get('resources') or {}).get('peakTreeRssBytes'),
                                         'finishedAt': run.get('finishedAt')},
                                        ensure_ascii=False) + '\n')
            print(f"  -> status={run.get('status')} decodeAndIndex="
                  f"{run.get('decodeAndIndexSeconds')}s wall={duration:.2f}s", flush=True)
    invocation['finishedAt'] = perf.iso_now()
    invocation['summary'] = _summarize([json.loads((Path(item['runDir']) / 'run.json').read_text(encoding='utf-8'))
                                        for item in invocation['runs']
                                        if (Path(item['runDir']) / 'run.json').is_file()], combos)
    invocations = output / 'invocations'
    invocations.mkdir(parents=True, exist_ok=True)
    invocation_path = invocations / f'{invocation["invocationId"]}.json'
    perf.write_json(invocation_path, invocation)
    print(f'调用摘要：{invocation_path}', flush=True)
    for variant, group in invocation['summary'].items():
        metrics = group['metrics']
        print(f'[{variant}] ok={group["ok"]}/{group["runs"]} '
              f'decodeAndIndex median={metrics["decodeAndIndexSeconds"]["median"]} '
              f'range={metrics["decodeAndIndexSeconds"]["min"]}..{metrics["decodeAndIndexSeconds"]["max"]} '
              f'peakTreeRssMB={_mb(metrics["peakTreeRssBytes"]["max"])} '
              f'revision={group["codeRevision"]}', flush=True)
    failures = sum(1 for item in invocation['runs'] if item['status'] != 'ok')
    return 0 if failures == 0 else 1


def _mb(value):
    return None if value is None else round(value / (1024 * 1024), 1)


if __name__ == '__main__':
    sys.exit(main())
