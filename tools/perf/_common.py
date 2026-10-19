"""ETM 离线链路基准的共享工具（仅标准库 + psutil）。

约定：
* 只读取系统信息，绝不修改 Windows Defender、电源方案或任何系统设置。
* 所有路径参数由调用方给出；本模块不写死本机绝对路径。
* JSON 结构化输出统一使用 UTF-8。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCHEMA_RUN = 'etm-perf-run/1'
SCHEMA_INPUTS = 'etm-perf-inputs/1'
CHUNK = 8 * 1024 * 1024

# 记录目录里需要指纹化的关键文件（原始输入 + 关键派生/配置；缺失则跳过）。
KEY_RECORD_FILES = (
    'capture.csv', 'capture.kvdat', 'capture.csv.la.json',
    'etm.bin', 'tpiu.bin', 'native-config.txt',
    'firmware.elf', 'session.json', 'sources.json',
)

# 代码指纹排除的目录名（构建产物、第三方源码、缓存、记录）。
_CODE_EXCLUDE_PARTS = {'build-native', 'vendor', '__pycache__', '.git', 'records', 'node_modules'}


# --------------------------------------------------------------------------- 基础
def repo_root(start=None) -> Path:
    """向上寻找仓库根（含 .git）；工作树里的 .git 是文件也算。"""
    here = Path(start or __file__).resolve()
    for parent in (here, *here.parents):
        if (parent / '.git').exists():
            return parent
    for parent in (here, *here.parents):
        if (parent / 'etm' / 'server.py').exists():
            return parent
    return Path.cwd()


def utc_stamp(now=None) -> str:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return now.strftime('%Y%m%dT%H%M%SZ')


def iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def sha256_file(path, chunk=CHUNK) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(chunk), b''):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_entry(path, rel=None) -> dict:
    """文件字节数 + SHA256；rel 是可选的展示用相对路径。"""
    path = Path(path)
    entry = {'bytes': path.stat().st_size, 'sha256': sha256_file(path)}
    if rel is not None:
        entry['path'] = str(rel)
    return entry


def write_json(path, value, indent=2):
    """原子写 JSON（临时文件 + replace），不会留下半截文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=indent, sort_keys=False) + '\n',
                         encoding='utf-8')
    os.replace(temporary, path)


def rel_to(path, root) -> str | None:
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except Exception:
        return None


# --------------------------------------------------------------------- JSON 语义哈希
def canonical_json_sha256(value) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return sha256_bytes(text.encode('utf-8'))


def _drop_dotted(obj, dotted):
    keys = dotted.split('.')
    cursor = obj
    for key in keys[:-1]:
        if not isinstance(cursor, dict) or key not in cursor:
            return
        cursor = cursor[key]
    if isinstance(cursor, dict):
        cursor.pop(keys[-1], None)


def semantic_json_sha256(path, drop=()) -> str:
    """把随运行目录变化的字段（绝对路径等）剔除后再算哈希，供跨 run 比较。"""
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    for dotted in drop:
        _drop_dotted(value, dotted)
    return canonical_json_sha256(value)


def sha256_skip_first_line(path) -> str:
    """sidecar/清单类文件首行常带 mtime 指纹；跨 run 比较时忽略首行。"""
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        stream.readline()
        for block in iter(lambda: stream.read(CHUNK), b''):
            digest.update(block)
    return digest.hexdigest()


_PATH_TOKEN = re.compile(r'(?:[A-Za-z]:[\\/][^\s"\']+|/[^\s"\']+)')


def normalize_paths_text(path) -> str:
    """把文本里的绝对路径替换为文件名，用于 native-config 这类逐 run 变化的清单。"""
    text = Path(path).read_text(encoding='utf-8', errors='replace')
    return _PATH_TOKEN.sub(lambda m: Path(m.group(0)).name, text)


def normalized_text_sha256(path) -> str:
    return sha256_bytes(normalize_paths_text(path).encode('utf-8'))


def directory_manifest(folder, chunk=True) -> dict:
    """目录清单摘要：文件名 + 字节数，再对清单行做 SHA256（不逐个哈希大文件）。"""
    folder = Path(folder)
    rows = []
    for path in sorted(folder.rglob('*')):
        if not path.is_file():
            continue
        rows.append(f'{path.relative_to(folder).as_posix()}\t{path.stat().st_size}')
    text = '\n'.join(rows) + ('\n' if rows else '')
    return {'files': len(rows), 'bytes': sum(int(r.split('\t')[1]) for r in rows),
            'manifestSha256': sha256_bytes(text.encode('utf-8'))}


# ------------------------------------------------------------------------- 命令/环境
def run_cmd(cmd, timeout=30, cwd=None) -> dict:
    """带短超时的只读命令执行；失败也返回结构化结果，不抛异常。"""
    started = time.perf_counter()
    try:
        result = subprocess.run([str(part) for part in cmd], cwd=str(cwd) if cwd else None,
                                capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=timeout)
        return {'command': [str(part) for part in cmd], 'exitCode': result.returncode,
                'stdout': (result.stdout or '').strip(), 'stderr': (result.stderr or '').strip(),
                'seconds': round(time.perf_counter() - started, 3)}
    except Exception as error:  # FileNotFoundError / TimeoutExpired / OSError
        return {'command': [str(part) for part in cmd], 'exitCode': None,
                'stdout': '', 'stderr': f'{type(error).__name__}: {error}',
                'seconds': round(time.perf_counter() - started, 3)}


def _first_line(text) -> str:
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    return lines[0] if lines else ''


def _windows_cpu_model() -> str:
    if os.name != 'nt':
        return platform.processor() or platform.machine()
    try:
        import winreg  # noqa: WPS433 - 仅 Windows
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key:
            return str(winreg.QueryValueEx(key, 'ProcessorNameString')[0]).strip()
    except Exception:
        return platform.processor() or platform.machine()


def _defender_status() -> dict:
    """只读查询 Defender 状态；不修改任何设置。查询失败如实记录。"""
    if os.name != 'nt':
        return {'available': False, 'reason': 'non-windows'}
    script = ('Get-MpComputerStatus | Select-Object AMServiceEnabled,AntivirusEnabled,'
              'RealTimeProtectionEnabled,AntivirusSignatureVersion,AntivirusSignatureLastUpdated,'
              'IsTamperProtected | ConvertTo-Json -Compress')
    result = run_cmd(['powershell', '-NoProfile', '-NonInteractive', '-Command', script], timeout=30)
    if result['exitCode'] != 0 or not result['stdout']:
        return {'available': False, 'error': result['stderr'][-800:] or 'no-output',
                'note': '只读查询失败；未修改任何设置'}
    try:
        value = json.loads(result['stdout'].splitlines()[-1])
    except Exception as error:
        return {'available': False, 'error': f'解析失败：{error}', 'raw': result['stdout'][-800:]}
    value['available'] = True
    value['note'] = '只读查询；基准运行不修改 Defender 设置'
    return value


def _power_scheme() -> dict:
    if os.name != 'nt':
        return {'available': False, 'reason': 'non-windows'}
    result = run_cmd(['powercfg', '/getactivescheme'], timeout=20)
    return {'available': result['exitCode'] == 0, 'output': result['stdout'] or result['stderr'],
            'note': '只读查询；基准运行不修改电源方案'}


def environment(anchor=None) -> dict:
    """采集一次环境快照。anchor 用于磁盘容量（通常是输出目录）。"""
    try:
        import psutil
    except Exception as error:  # psutil 不可用时仍返回基本环境
        psutil = None
        psutil_error = str(error)
    else:
        psutil_error = None
    anchor = Path(anchor or Path.cwd()).resolve()
    while not anchor.exists() and anchor != anchor.parent:
        anchor = anchor.parent
    disk = shutil.disk_usage(anchor)
    value = {
        'capturedAt': iso_now(),
        'os': {
            'platform': platform.platform(),
            'system': platform.system(),
            'release': platform.release(),
            'version': platform.version(),
            'machine': platform.machine(),
            'win32': ' '.join(part for part in platform.win32_ver() if part) if os.name == 'nt' else None,
        },
        'cpu': {
            'model': _windows_cpu_model(),
            'logicalCores': os.cpu_count(),
            'physicalCores': None,
            'frequencyMHz': None,
        },
        'memory': {'totalBytes': None, 'availableBytes': None},
        'disk': {'anchor': str(anchor), 'totalBytes': disk.total, 'freeBytes': disk.free},
        'python': {
            'version': platform.python_version(),
            'fullVersion': sys.version.replace('\n', ' '),
            'implementation': platform.python_implementation(),
            'executable': sys.executable,
            'compiler': platform.python_compiler(),
            'unicode': {'utf8Mode': bool(sys.flags.utf8_mode), 'filesystemEncoding': sys.getfilesystemencoding()},
        },
        'tools': {
            'git': _first_line(run_cmd(['git', '--version'], timeout=20)['stdout']),
            'cmake': _first_line(run_cmd(['cmake', '--version'], timeout=20)['stdout']),
            'ninja': _first_line(run_cmd(['ninja', '--version'], timeout=20)['stdout']),
            'gcc': _first_line(run_cmd([shutil.which('gcc') or 'gcc', '--version'], timeout=20)['stdout']),
            'g++': _first_line(run_cmd([shutil.which('g++') or 'g++', '--version'], timeout=20)['stdout']),
        },
        'environmentVariables': {name: os.environ.get(name) for name in
                                 ('ETM_DECODE_ENGINE', 'ETM_PYTHON_INDEX', 'PYTHONHASHSEED',
                                  'OMP_NUM_THREADS', 'TEMP', 'TMP', 'PYTHONUTF8')},
        'defender': _defender_status(),
        'powerScheme': _power_scheme(),
        'psutilError': psutil_error,
    }
    if psutil is not None:
        try:
            value['cpu']['physicalCores'] = psutil.cpu_count(logical=False)
            frequency = psutil.cpu_freq()
            if frequency:
                value['cpu']['frequencyMHz'] = round(frequency.max or frequency.current, 1)
        except Exception:
            pass
        try:
            memory = psutil.virtual_memory()
            value['memory'].update(totalBytes=memory.total, availableBytes=memory.available)
        except Exception:
            pass
        try:
            value['resourcesNow'] = {
                'bootTime': psutil.boot_time(),
                'loadPercent': psutil.cpu_percent(interval=0.1),
            }
        except Exception:
            pass
    return value


# ------------------------------------------------------------------------- 代码指纹
def _read_cmake_cache(build_dir) -> dict | None:
    path = Path(build_dir) / 'CMakeCache.txt'
    if not path.is_file():
        return None
    wanted = {'CMAKE_BUILD_TYPE', 'CMAKE_C_COMPILER', 'CMAKE_CXX_COMPILER', 'CMAKE_GENERATOR',
              'CMAKE_MAKE_PROGRAM', 'CMAKE_C_FLAGS_RELEASE', 'CMAKE_CXX_FLAGS_RELEASE',
              'CMAKE_C_COMPILER_VERSION', 'CMAKE_CXX_COMPILER_VERSION', 'CMAKE_C_COMPILER_ID',
              'CMAKE_CXX_COMPILER_ID'}
    values = {}
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        if line.startswith('//') or '=' not in line:
            continue
        key, _, val = line.partition('=')
        base = key.split(':', 1)[0]
        if base in wanted or base.startswith('SQLite3_'):
            values[base] = val
    return values


def code_fingerprint(code_root) -> dict:
    """etm 源码快照指纹：Python/C++/构建脚本哈希 + 原生二进制 + 构建参数 + git 状态。"""
    code = Path(code_root).resolve()
    if not (code / 'server.py').is_file() and not (code / 'tools').is_dir():
        raise SystemExit(f'--code 不像 etm 源码快照根（缺少 tools/ 或 server.py）：{code}')
    files = []
    for path in sorted(code.rglob('*.py')):
        parts = set(path.relative_to(code).parts)
        if parts & _CODE_EXCLUDE_PARTS:
            continue
        files.append({'path': path.relative_to(code).as_posix(), 'bytes': path.stat().st_size,
                      'sha256': sha256_file(path)})
    extra = ['CMakeLists.txt', 'build-decoder.ps1', 'requirements.txt']
    for name in extra:
        path = code / name
        if path.is_file():
            files.append({'path': name, 'bytes': path.stat().st_size, 'sha256': sha256_file(path)})
    native = code / 'native'
    if native.is_dir():
        for path in sorted(native.glob('*')):
            if path.suffix.lower() in ('.cpp', '.h', '.hpp', '.c'):
                files.append({'path': path.relative_to(code).as_posix(), 'bytes': path.stat().st_size,
                              'sha256': sha256_file(path)})
    text = ''.join(f"{item['path']}\t{item['bytes']}\t{item['sha256']}\n" for item in files)
    binaries = {}
    build_dir = code / 'build-native'
    for name in ('etm_decode.exe', 'etm_index.exe', 'etm_decode', 'etm_index'):
        path = build_dir / name
        if path.is_file():
            binaries[name] = {'path': rel_to(path, code), 'bytes': path.stat().st_size,
                              'sha256': sha256_file(path),
                              'mtime': _dt.datetime.fromtimestamp(path.stat().st_mtime,
                                                                  _dt.timezone.utc).isoformat()}
    git = {}
    for key, args in (('revision', ['rev-parse', 'HEAD']),
                      ('toplevel', ['rev-parse', '--show-toplevel']),
                      ('describe', ['describe', '--always', '--dirty', '--tags'])):
        result = run_cmd(['git', '-C', str(code), *args], timeout=20)
        git[key] = result['stdout'] if result['exitCode'] == 0 else None
    status = run_cmd(['git', '-C', str(code), 'status', '--porcelain', '--', '.'], timeout=20)
    dirty = [line for line in status['stdout'].splitlines() if line.strip()] if status['exitCode'] == 0 else []
    return {
        'root': str(code),
        'sourceDigest': sha256_bytes(text.encode('utf-8')),
        'sourceFileCount': len(files),
        'sourceFiles': files,
        'binaries': binaries,
        'build': _read_cmake_cache(build_dir),
        'git': {'revision': git.get('revision'), 'describe': git.get('describe'),
                'toplevel': git.get('toplevel'), 'dirty': bool(dirty), 'dirtyPaths': dirty[:200],
                'dirtyCount': len(dirty)},
    }


# --------------------------------------------------------------------------- 输入清单
def load_inputs(path, root) -> dict | None:
    path = Path(path)
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding='utf-8'))
    value['_file'] = str(path.resolve())
    value['_root'] = str(Path(root).resolve())
    return value


def resolve_record(raw, root, catalog) -> tuple:
    """--record 支持目录路径，也支持 inputs.json 里的键名。返回 (key|None, Path)。"""
    raw = str(raw)
    if catalog:
        records = catalog.get('records') or {}
        if raw in records and records[raw].get('path'):
            return raw, (Path(root) / records[raw]['path']).resolve()
        current = Path(raw)
        if not current.is_dir():
            for key, entry in records.items():
                candidate = (Path(root) / entry.get('path', '')).resolve()
                if candidate.is_dir() and (candidate.name == raw or str(candidate) == raw):
                    return key, candidate
    return None, Path(raw).resolve()


def find_inputs_entry(record_path, catalog):
    if not catalog:
        return None, None
    record_path = Path(record_path).resolve()
    for key, entry in (catalog.get('records') or {}).items():
        candidate = (Path(catalog['_root']) / entry.get('path', '')).resolve()
        if candidate == record_path:
            return key, entry
    return None, None


def catalog_fingerprint_check(record_path, catalog) -> dict:
    """把当前记录目录与 inputs.json 的固定指纹对照（缺失/不一致如实列出，不修改文件）。"""
    key, entry = find_inputs_entry(record_path, catalog)
    if entry is None:
        return {'catalogKey': None, 'catalogMatched': None,
                'note': '当前记录不在 inputs.json 中，未做固定指纹对照'}
    checks = {}
    for name, expected in (entry.get('files') or {}).items():
        path = Path(record_path) / name
        if not path.is_file():
            checks[name] = {'match': False, 'reason': 'missing'}
            continue
        actual = {'bytes': path.stat().st_size, 'sha256': sha256_file(path)}
        checks[name] = {'match': actual['bytes'] == expected.get('bytes') and
                        actual['sha256'] == expected.get('sha256'),
                        'actualBytes': actual['bytes'], 'actualSha256': actual['sha256']}
    return {'catalogKey': key, 'catalogMatched': all(item['match'] for item in checks.values()),
            'files': checks}


# ----------------------------------------------------------------------------- 统计
def spread(values) -> dict:
    values = [float(value) for value in values if value is not None]
    if not values:
        return {'n': 0, 'median': None, 'min': None, 'max': None, 'mean': None}
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    median = ordered[middle] if count % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return {'n': count, 'median': round(median, 6), 'min': round(ordered[0], 6),
            'max': round(ordered[-1], 6), 'mean': round(sum(ordered) / count, 6)}
