"""trace-store 查询适配器：让 `browse_store()` 用旧 `RecordStore` 的同一契约读新格式记录。

背景（方案 §6、§8 P4）
----------------------
P3 的融合写出器把记录写成 `trace-store v1`（`store/manifest.json` +
`store/generations/g-*/store.bin`），不再生成 `events.jsonl` / `browse-v3.sqlite`。
本适配器把 `trace_store.TraceStore` 的读取能力包装成与 `record_store.RecordStore`
**同名同义**的公开 API：

    signature() / ready() / ensure()
    overview() / page() / function_timeline() / window()
    event() / source() / navigate() / issues()

语义来源
--------
* 展开（逐指令字段、callPath、执行状态、周期）由 `trace_store.TraceStore` 完成，
  它与旧事件流逐字段等价；P1 的逐接口对照与 P3 的逐块字节对照已经证明这一点。
* 本模块只做三件事：后端选择（`backend_for`）、陈旧/损坏检测（manifest 指纹与
  CRC 校验）、查询结果缓存（`window`/`function_timeline` 与旧后端一样按区间缓存）。
* 旧 `RecordStore` 的公开行为不变；本模块不修改 `record_store.py`。

允许的表示差异（白名单，逐项在 P4 报告证明）
--------------------------------------------
* W-P4-1：`overview()` 多一个 `traceStore{format,generation,runs,instructions}` 概要键
  （P1 W1 同源；前端忽略未知键）。
* P3 W-P3-1：若 store 由融合写出器（native.bin 路径）生成，`overview().streamed`
  的 `eventsFile='store.bin'` / `nativeJsonl='native.bin'` 与旧记录不同——这是事件源
  的事实描述；其它字段一致。
* P3 W-P3-2：融合写出器生成的 store 不保存 OpenCSD 包说明文本，
  `page(view='packets').text` 为空；参考写入器（从 events.jsonl 写）的 store 保留文本。

完整性与缓存
------------
`ensure()` 用 `manifest.json` 的 `files[].sha256` 与块 CRC 校验存储本身，并把
`bindings.inputs` 里**决定事件流**的输入（`STALENESS_INPUTS`：etm.bin / native.bin /
native-config.txt / firmware.elf / physical.json / framing.json / tpiu.bin，以及
`image-*.bin`）按**内容**与当前目录对照：凡 manifest 记了 `sha256` 就流式整份核验，
不再按体积放宽。R0-A（2026-10-20）修的就是旧的 `MAX_HASH_BYTES=64 MiB` 阈值——它让
1 s 的 native.bin 只比尺寸，声明错误的 sha256 也会被放过。
不一致时明确报“store 与当前记录输入不一致”，绝不返回拼凑结果；`verified / skipped /
逐项耗时` 记录在 `input_verification()`，并写进 `overview().traceStore.inputHash`。
`--no-hash-inputs` 产物（`bindings.inputHash.skipped=true`，输入只有尺寸没有 sha256）
可以正常浏览（诊断用），但**显式标注“未核验”**（`verified=false`），不得当成已核验
的最终用户路径。`session.json` / `sources.json` 也按内容核验（报告
`metadata`/`metadataChanged`），但它们会被服务端在解码后继续改写（decodeStatus、
captureStart、源码快照），因此**不参与**陈旧拒绝；`decoded.json` 与事件/包文本等派生
输入只列进报告的 `excluded`，不按内容核验（GB 级重读会破坏打开时延）。

缓存按 store 代隔离：`window` / `function_timeline` 的缓存键以**实际 store 代**
（`store.generation` + 格式版本）打头，`ensure()` 换代或 `close()` 时清掉不属于当前代
的条目；在途查询先登记再计算，写缓存前比对当前代，旧代结果不会落进新代缓存。换代时
旧 store **延迟关闭**（最后一个在途查询释放后才 `close()`），不会被读到一半关掉。

`adapter()` 提供与 `record_store.record_store()` 相同的“按记录目录缓存最多 4 个实例”
的工厂；`backend_for()` 决定某个记录目录应使用新后端还是旧后端：

* 有 `store/manifest.json` 且（decoded.json 带 `traceStore` 标记 或 没有 events.jsonl）
  → `trace-store`；
* 有 manifest、但 decoded.json 没有标记且 events.jsonl 仍在（旧记录目录里手工生成过
  store 的情形）→ `legacy`，避免旧记录被陈旧 store 抢先；
* 没有 manifest 但 decoded.json 声明 `traceStore` → 明确报错（不静默回退到必然失败的
  旧后端）。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

import trace_store

STORE_DIR_NAME = 'store'
MANIFEST_NAME = trace_store.MANIFEST_NAME
BACKEND_TRACE = 'trace-store'
BACKEND_LEGACY = 'legacy'
# 陈旧判断只比对“决定事件流”的输入；服务端解码后会改写 session.json/decoded.json，
# 而 sources.json 由 attach_sources 刷新后适配器会覆盖读取，都不参与陈旧判断。
STALENESS_INPUTS = ('etm.bin', 'native.bin', 'native-config.txt', 'firmware.elf',
                    'physical.json', 'framing.json', 'tpiu.bin')
# 构建后仍会被服务端改写的元数据：按内容核验并报告，但不参与“陈旧拒绝”
# （decodeStatus/captureStart/源码快照在 store 发布之后仍会重写）。
AUXILIARY_INPUTS = ('session.json', 'sources.json')
# 流式核验分块：R0-A 之后**没有**“超过多少字节只比尺寸”的体积阈值，声明了 sha256 就整份核验。
HASH_CHUNK_BYTES = 1 << 20
_STORES = OrderedDict()
_STORES_LOCK = threading.Lock()


def store_dir(folder):
    return Path(folder).resolve() / STORE_DIR_NAME


def manifest_path(folder):
    return store_dir(folder) / MANIFEST_NAME


def store_available(folder):
    return manifest_path(folder).is_file()


def _read_small_json(path, limit=16 << 20):
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return None
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def record_declares_store(folder):
    decoded = _read_small_json(Path(folder).resolve() / 'decoded.json')
    return isinstance(decoded, dict) and bool(decoded.get('traceStore'))


def backend_for(folder):
    """按记录目录内容选择后端；返回 ``'trace-store'`` 或 ``'legacy'``。

    明确的“声明了新格式但 store 缺失/损坏”不静默回退，而是抛出可读错误；
    旧 JSONL/内联记录（没有 manifest、也没有 traceStore 标记）返回 ``legacy``。
    """
    folder = Path(folder).resolve()
    manifest = manifest_path(folder)
    declared = record_declares_store(folder)
    if manifest.is_file():
        if declared or not (folder / 'events.jsonl').is_file():
            return BACKEND_TRACE
        # 旧记录目录里可能有历史上手工生成的 store；没有 traceStore 标记时仍走旧后端。
        return BACKEND_LEGACY
    if declared:
        raise ValueError('记录声明了 trace-store，但缺少已发布的 store/manifest.json；'
                         '请重新解码、重新导入完整记录包，或删除过期的 decoded.json')
    return BACKEND_LEGACY


def _stat_key(path):
    try:
        stat = Path(path).stat()
        return '%d:%d' % (stat.st_size, stat.st_mtime_ns)
    except OSError:
        return '-'


def signature(folder):
    """记录目录指纹：store manifest 代 + 关键输入文件的大小/时间戳。

    与 `RecordStore.signature()` 同一用途（判断派生数据是否对应当前记录），
    但新格式的“派生数据”是 manifest 指向的 store.bin。
    """
    folder = Path(folder).resolve()
    manifest = manifest_path(folder)
    parts = ['trace-store', _stat_key(manifest)]
    declared = _read_small_json(manifest) or {}
    index = declared.get('queryIndex') or {}
    parts.append('query=%s:%s:%s' % (index.get('version') or 0, index.get('cellSize') or 0,
                                     index.get('payloadBytes') or 0))
    engine = declared.get('engine') or (declared.get('bindings') or {}).get('engine') or {}
    parts.append('engine=%s:%s:%s' % (engine.get('name') or 'python',
                                      engine.get('version') or 0,
                                      engine.get('fallback') or '-'))
    parts.append('container=%s' % (index.get('container') or 'v1'))
    for name in STALENESS_INPUTS:
        parts.append(name + '=' + _stat_key(folder / name))
    decoded = folder / 'decoded.json'
    parts.append('decoded=' + _stat_key(decoded))
    for path in sorted(folder.glob('image-*.bin')):
        parts.append(path.name + '=' + _stat_key(path))
    return '|'.join(parts)


def _hash_file_stream(path, chunk=HASH_CHUNK_BYTES):
    """流式 sha256：返回 ``(sha256, 实际字节数, 秒)``；不按体积跳过、不整体读进内存。"""
    digest = hashlib.sha256()
    total = 0
    started = time.perf_counter()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(chunk), b''):
            total += len(block)
            digest.update(block)
    return digest.hexdigest(), total, time.perf_counter() - started


def _sha256(path):
    return _hash_file_stream(path)[0]


def verify_inputs(folder, manifest):
    """按 `manifest.bindings.inputs` 核验**决定事件流**的输入；返回报告字典（不抛异常）。

    R0-A 修复要点（Codex 2026-10-20 §2.4）：

    * 删掉旧的 `MAX_HASH_BYTES=64 MiB` 静默降级——凡 manifest 声明了 `sha256` 的绑定
      输入都按内容整份核验（流式分块，记录每项耗时/字节数），因此“同长度改写 + mtime
      不变”也会被拒绝（旧实现只看 size，会放过）；
    * `bindings.inputHash.skipped=true`（`--no-hash-inputs` 产物）或输入缺少 sha256 时
      不让它“默认已核验”：这些名字进 `unverified`，`verified=false`，`reason` 写明原因；
    * 只核验 `STALENESS_INPUTS` + `image-*.bin`（P4 既有口径：这些才决定事件流）；
      `session.json` / `sources.json`（构建后仍会被服务端改写）也按内容核验，但结果只写进
      `metadata`/`metadataChanged`，不参与拒绝；旧布局的派生文本（events/elements/packets
      JSONL、capture.csv 等）列进 `excluded`——它们不是 store 的事件源（事件源是 store.bin，
      每次打开都做 sha256+CRC 全量校验），且单份可达 1.7 GB。

    返回字段：``verified / skipped / reason / declared / present / blockingHashed / hashed /
    hashedBytes / hashSeconds / unverified / unbound / excluded / metadata / metadataChanged /
    inputs / problems / elapsedSeconds``。``problems`` 非空即“store 与当前记录输入不一致”，
    调用方必须拒绝。
    """
    folder = Path(folder)
    bindings = manifest.get('bindings') or {}
    inputs = bindings.get('inputs') or {}
    marker = bindings.get('inputHash') or {}
    skipped = bool(marker.get('skipped'))
    started = time.perf_counter()
    report = dict(verified=False, skipped=skipped, reason='', declared=len(inputs),
                  present=0, blockingHashed=0, hashed=0, hashedBytes=0, hashSeconds=0.0,
                  unverified=[], unbound=[], excluded=[], metadata=[], metadataChanged=[],
                  inputs=[], problems=[], elapsedSeconds=0.0)
    names = [name for name in STALENESS_INPUTS if (folder / name).is_file()]
    names += [path.name for path in sorted(folder.glob('image-*.bin'))]
    for name in names:
        path = folder / name
        recorded = inputs.get(name)
        if not recorded:
            # 文件在记录里、但 manifest 没绑定（旧 store，或建成后才补进来的输入）。
            report['unbound'].append(name)
            continue
        report['present'] += 1
        stat = path.stat()
        expected = recorded.get('sha256')
        entry = dict(name=name, bytes=stat.st_size, expectedSha256=expected,
                     sha256=None, seconds=0.0)
        if stat.st_size != recorded.get('bytes'):
            entry['status'] = 'size-mismatch'
            report['problems'].append('%s（尺寸 %d≠%s）'
                                      % (name, stat.st_size, recorded.get('bytes')))
            report['inputs'].append(entry)
            continue
        if not expected:
            entry['status'] = 'not-hashed'
            report['unverified'].append(name)
            report['inputs'].append(entry)
            continue
        digest, bytes_read, seconds = _hash_file_stream(path)
        report['blockingHashed'] += 1
        report['hashed'] += 1
        report['hashedBytes'] += bytes_read
        report['hashSeconds'] += seconds
        entry.update(sha256=digest, seconds=round(seconds, 6))
        if digest != expected:
            entry['status'] = 'mismatch'
            report['problems'].append('%s（sha256 不符）' % name)
        else:
            entry['status'] = 'ok'
        report['inputs'].append(entry)
    # 元数据输入（session.json/sources.json）：同样按内容核验，但构建后被服务端改写是正常的，
    # 因此只记录不拒绝。
    for name in AUXILIARY_INPUTS:
        recorded = inputs.get(name)
        path = folder / name
        if not recorded or not path.is_file():
            continue
        stat = path.stat()
        expected = recorded.get('sha256')
        entry = dict(name=name, bytes=stat.st_size, expectedSha256=expected,
                     sha256=None, seconds=0.0)
        if stat.st_size != recorded.get('bytes'):
            entry['status'] = 'size-mismatch'
        elif not expected:
            entry['status'] = 'not-hashed'
        else:
            digest, bytes_read, seconds = _hash_file_stream(path)
            report['hashed'] += 1
            report['hashedBytes'] += bytes_read
            report['hashSeconds'] += seconds
            entry.update(sha256=digest, seconds=round(seconds, 6))
            entry['status'] = 'ok' if digest == expected else 'changed-after-build'
            if entry['status'] != 'ok':
                report['metadataChanged'].append(name)
        report['metadata'].append(entry)
    blocking = set(STALENESS_INPUTS) | set(AUXILIARY_INPUTS)
    for name in sorted(inputs):
        if name in blocking or name.startswith('image-'):
            continue
        if (folder / name).is_file():
            report['excluded'].append(name)
    reasons = []
    if report['problems']:
        reasons.append('输入与 manifest 记录不一致')
    if skipped:
        reasons.append('manifest.bindings.inputHash.skipped=true：%s'
                       % (marker.get('reason') or '构建时未哈希输入内容'))
    if report['unverified']:
        reasons.append('manifest 未记录 sha256，未按内容核验：%s'
                       % '、'.join(report['unverified'][:6]))
    if report['present'] == 0:
        reasons.append('没有可比对的绑定输入（manifest 未记录，或输入已不在记录目录）')
    elif report['blockingHashed'] == 0 and not report['unverified'] and not report['problems']:
        reasons.append('没有按内容核验的输入')
    if report['metadataChanged']:
        reasons.append('元数据输入构建后被改写（不参与拒绝）：%s'
                       % '、'.join(report['metadataChanged']))
    report['verified'] = bool(not report['problems'] and not skipped
                              and not report['unverified'] and report['present'] > 0
                              and report['blockingHashed'] > 0)
    report['reason'] = '；'.join(reasons)
    report['hashSeconds'] = round(report['hashSeconds'], 6)
    report['elapsedSeconds'] = round(time.perf_counter() - started, 6)
    return report


def _stale_inputs(folder, manifest):
    """兼容包装：只返回不一致项（既有调用/复现脚本沿用这个形状）。"""
    return verify_inputs(folder, manifest)['problems']


def capture_start_from_store(folder_or_store, metadata):
    """与 `server.capture_start_info()` 同义的采集入口回读（不依赖 events.jsonl）。"""
    entry = (metadata or {}).get('captureEntry')
    if entry is None:
        return None
    try:
        entry = int(entry)
    except (TypeError, ValueError):
        return None
    store_path = folder_or_store
    if Path(store_path).name != STORE_DIR_NAME and (Path(store_path) / STORE_DIR_NAME).is_dir():
        store_path = Path(store_path) / STORE_DIR_NAME
    store = trace_store.TraceStore(store_path, verify='sizes')
    try:
        first_seq = (store.summary or {}).get('firstSeq')
        first_pc = None
        if isinstance(first_seq, int):
            first_pc = store.event(first_seq)['event'].get('pc')
        found = store.navigate(0, 1, 'instruction', entry)['seq'] is not None
        return dict(selectedPC=entry, firstRecoveredPC=first_pc, entryRecovered=found,
                    firstMatchesEntry=bool(first_pc is not None and first_pc == entry))
    finally:
        store.close()


def read_sources(folder):
    """源码文本：以记录目录的 sources.json 为准，回退到 store 写入时的 SOURCES 快照。

    旧后端把源码存进 SQLite；新格式把写入时快照存进 store 的 SOURCES 块。
    `attach_sources` 更新的是 sources.json（不重建 store），因此读取时优先用前者，
    保证“刷新源码快照”对新格式记录同样生效。
    """
    folder = Path(folder).resolve()
    overlay = _read_small_json(folder / 'sources.json')
    values = dict(overlay) if isinstance(overlay, dict) else {}
    try:
        if manifest_path(folder).is_file():
            store = trace_store.TraceStore(store_dir(folder), verify='sizes')
            try:
                for name, text in (store.sources or {}).items():
                    values.setdefault(name, text)
            finally:
                store.close()
    except (trace_store.TraceStoreError, OSError):
        pass
    return values


class TraceStoreAdapter:
    """`RecordStore` 同契约的 trace-store 只读视图（服务端 `browse_store()` 使用）。"""

    backend = BACKEND_TRACE

    def __init__(self, folder, loader=None):
        self.folder = Path(folder).resolve()
        self.path = store_dir(self.folder)
        self.loader = loader
        self._store = None
        self._signature = None
        self._verified = False
        self._lock = threading.RLock()
        # 缓存键 = (代令牌, *参数)；代令牌绑定实际 store.generation + 格式版本（R0-A）。
        self._windows = OrderedDict()
        self._timelines = OrderedDict()
        # 在途查询计数与“延迟关闭”的旧代：换代/close 时不打断正在读的请求。
        self._inflight = {}
        self._retired = {}
        # 桩/其它不可写属性的 store 的核验报告后备位（正常实现挂在 store 对象上）。
        self._fallback_verification = None

    # -- 生命周期 ----------------------------------------------------------------------------
    def signature(self):
        return signature(self.folder)

    def ready(self):
        """派生 store 是否存在且看起来对应当前记录（不抛异常、不做全量哈希）。

        注意：这里只做尺寸级检查；“输入内容是否与 store 绑定一致”在 `ensure()` 打开时
        由 `verify_inputs()` 按 sha256 核验，结果见 `input_verification()`。
        """
        try:
            current = self.signature()
            if self._store is not None and self._signature == current:
                return True
            manifest_file = manifest_path(self.folder)
            if not manifest_file.is_file():
                return False
            manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
            if manifest.get('state') != 'complete':
                return False
            files = manifest.get('files') or []
            if not files:
                return False
            generation = manifest.get('generation')
            if not isinstance(generation, str) or '/' in generation or '\\' in generation:
                return False
            for entry in files:
                path = self.path / 'generations' / generation / str(entry.get('name'))
                if not path.is_file() or path.stat().st_size != entry.get('bytes'):
                    return False
            return True
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def _store_token(self, store):
        """缓存代令牌：实际 store 代 + 格式版本；换代必然换令牌。"""
        generation = getattr(store, 'generation', None)
        if generation is None:                       # 测试桩/异常实现：退回对象身份
            generation = 'store-%d' % id(store)
        return '%s|schema-%s' % (generation, trace_store.SCHEMA_VERSION)

    def _open(self):
        """打开并校验 store（manifest sha256 + 块 CRC），并按内容核验绑定输入。"""
        started = time.perf_counter()
        try:
            store = trace_store.TraceStore(self.path, verify='full')
        except trace_store.TraceStoreError as error:
            raise ValueError('trace-store 不可用：%s' % error)
        try:
            store_check_seconds = time.perf_counter() - started
            report = verify_inputs(self.folder, store.manifest)
            if report['problems']:
                raise ValueError(
                    'trace-store 与当前记录输入不一致（%s）；请重新解码或重新导入完整记录包，'
                    '本适配器不会把旧 store 当成当前执行历史'
                    % '、'.join(report['problems'][:6]))
            # 触发一次摘要/静态/索引块读取，尽早暴露块 CRC 或结构损坏。
            store.overview()
            store.stats()
        except Exception:
            store.close()
            raise
        # 打开耗时：store 自身校验（sha256/CRC）+ 输入内容核验，分开记录。
        # 挂在 store 对象上（而不是只挂适配器属性），保证 overview/诊断读到的报告
        # 一定属于当前借出的这一代，不会与并发的换代错配。
        report = dict(
            report, generation=getattr(store, 'generation', None),
            storeCheckSeconds=round(store_check_seconds, 6),
            openSeconds=round(time.perf_counter() - started, 6))
        try:
            store.verification = report
        except AttributeError:                      # 桩/其它实现不可写属性时退回适配器属性
            self._fallback_verification = report
        return store

    def _retire_locked(self, store):
        """关闭旧 store；仍有在途查询时先挂起，等最后一个使用者释放后再关。"""
        token = id(store)
        if self._inflight.get(token, 0) > 0:
            self._retired[token] = store
            return
        try:
            store.close()
        except Exception:
            pass

    def _purge_caches_locked(self, token):
        """清掉不属于当前代的缓存条目（token=None 时全清）。"""
        for cache in (self._windows, self._timelines):
            for key in [key for key in cache if token is None or key[0] != token]:
                cache.pop(key, None)

    def _ensure_locked(self):
        current = self.signature()
        if self._store is not None and self._signature == current and self._verified:
            return self._store
        store = self._open()                       # 失败时保留原 store，由调用方看到错误
        old = self._store
        token = self._store_token(store)
        self._store = store
        self._signature = current
        self._verified = True
        self._purge_caches_locked(token)           # 换代：旧代 window/timeline 缓存作废
        if old is not None and old is not store:
            self._retire_locked(old)
        return store

    def ensure(self):
        """确保 store 已发布、未损坏、未陈旧、输入已核验；必要时重新打开（新 generation）。"""
        current = self.signature()
        if self._store is not None and self._signature == current and self._verified:
            return
        with self._lock:
            self._ensure_locked()

    def _release(self, token):
        with self._lock:
            remaining = self._inflight.get(token, 1) - 1
            if remaining > 0:
                self._inflight[token] = remaining
                return
            self._inflight.pop(token, None)
            retired = self._retired.pop(token, None)
            if retired is not None:
                try:
                    retired.close()
                except Exception:
                    pass

    @contextmanager
    def _leased(self):
        """借出当前 store：登记在途计数，保证查询期间不会被换代/close 关掉。"""
        with self._lock:
            store = self._ensure_locked()
            token = id(store)
            self._inflight[token] = self._inflight.get(token, 0) + 1
        try:
            yield store
        finally:
            self._release(token)

    def close(self):
        with self._lock:
            store, self._store = self._store, None
            self._verified = False
            self._fallback_verification = None
            self._purge_caches_locked(None)        # 关闭：window/timeline 缓存全部作废
            if store is not None:
                self._retire_locked(store)

    def _active(self):
        with self._lock:
            return self._ensure_locked()

    def input_verification(self):
        """最近一次打开 store 的输入核验报告（含 skipped/耗时）；未打开时返回 None。

        报告字段见 `verify_inputs()`，`_open()` 另加 `generation / storeCheckSeconds /
        openSeconds`。`verified=False` 时必须当成“未核验”，不能作为最终用户路径的依据。
        """
        with self._lock:
            return self._verification_of(self._store)

    def _verification_of(self, store):
        """取某一代 store 的核验报告（读 store 对象的属性，避免与并发换代错配）。"""
        if store is None:
            return None
        report = getattr(store, 'verification', None)
        if report is None:
            report = self._fallback_verification
        return dict(report) if report else None

    def _input_hash_summary(self, store):
        """写进 overview 的精简视图（前端忽略未知键；测试与验收按这里的 verified 判定）。"""
        report = self._verification_of(store)
        if not report:
            return None
        summary = dict(verified=bool(report['verified']), skipped=bool(report['skipped']),
                       reason=report['reason'], declared=report['declared'],
                       present=report['present'], blockingHashed=report['blockingHashed'],
                       hashed=report['hashed'], hashedBytes=report['hashedBytes'],
                       hashSeconds=report['hashSeconds'],
                       unverified=list(report['unverified']),
                       metadataChanged=list(report['metadataChanged']),
                       excluded=list(report['excluded']))
        if report.get('generation'):
            summary['generation'] = report['generation']
        return summary

    # -- RecordStore 契约 --------------------------------------------------------------------
    def overview(self):
        with self._leased() as store:
            result = store.overview()
            generation = getattr(store, 'generation', None)
            summary = self._input_hash_summary(store)
            stats = store.stats()
            manifest = dict(store.manifest or {})
        result = dict(result)
        # W-P4-1 白名单键：traceStore 概要；这里补上实际代、输入核验状态、R1 查询索引状态
        # 与 R2 引擎标识/构建成本（python 参考或 native；回退原因显式带出）。
        trace = dict(result.get('traceStore') or {})
        if generation is not None:
            trace['generation'] = generation
        if summary is not None:
            trace['inputHash'] = summary
        query_index = dict(stats.get('queryIndex') or {})
        declared_index = manifest.get('queryIndex') or {}
        if declared_index.get('container'):
            query_index['container'] = declared_index.get('container')
        trace['queryIndex'] = query_index
        engine = manifest.get('engine') or (manifest.get('bindings') or {}).get('engine') or {}
        trace['engine'] = dict(name=engine.get('name') or 'python',
                               version=engine.get('version'),
                               fallback=engine.get('fallback'),
                               reason=engine.get('reason'),
                               buildCosts=engine.get('buildCosts'))
        result['traceStore'] = trace
        performance = _read_small_json(self.folder / 'decode-performance.json')
        if isinstance(performance,dict) and performance.get('generation') == generation:
            result['performance'] = performance
        return result

    def page(self, view='events', offset=0, limit=150, search='', hide_skipped=False,
             start=None, end=None):
        with self._leased() as store:
            return store.page(view, offset, limit, search, hide_skipped, start, end)

    def event(self, seq):
        with self._leased() as store:
            return store.event(seq)

    def iter_events(self, start=None, end=None):
        """流式展开事件（导出/诊断用；不属于 RecordStore 契约）。

        生成器在整个迭代期间持有借用计数：中途遇到换代/close 也不会把 store 关掉。
        """
        with self._leased() as store:
            yield from store.events(start, end)

    def export_execution(self,result,path,kind,start=None,end=None):
        """Keep the verified generation alive while its native projection runs."""
        import native_export
        with self._leased() as store:
            if native_export.prefer_prepared_small_range(store,start,end):return False
            return native_export.write(store,result,path,kind,start,end)

    def export_reading(self,result,path,mapping,manifest,kind,start=None,end=None,config=None,names=None):
        import reading_projection
        with self._leased() as store:
            return reading_projection.write(store,result,self.folder,path,mapping,manifest,kind,start,end,config,names)

    def sources(self):
        with self._leased() as store:
            overlay = _read_small_json(self.folder / 'sources.json')
            if isinstance(overlay, dict) and len(overlay) >= len(store.sources or {}):
                return dict(overlay)
            values = dict(overlay) if isinstance(overlay, dict) else {}
            for name, text in (store.sources or {}).items():
                values.setdefault(name, text)
            return values

    def source(self, file):
        return dict(file=file, text=self.sources().get(file, ''))

    def navigate(self, seq=0, direction=1, mode='instruction', pc=None, start=None, end=None):
        with self._leased() as store:
            return store.navigate(seq, direction, mode, pc, start, end)

    def issues(self, offset=0, limit=100):
        with self._leased() as store:
            return store.issues(offset, limit)

    def _cached(self, cache, token, key, compute, limit=8):
        """按代令牌缓存；在途计算结果写回前比对当前代，旧代结果不得污染新代缓存。"""
        full = (token,) + tuple(key)
        with self._lock:
            if full in cache:
                cache.move_to_end(full)
                return cache[full]
        value = compute()
        with self._lock:
            if self._store is None or self._store_token(self._store) != token:
                return value                        # 旧代在途结果：丢弃，不写入新代缓存
            cache[full] = value
            while len(cache) > limit:
                cache.popitem(last=False)
        return value

    def function_timeline(self, start=None, end=None, search='', hide_skipped=False, bins=1600,
                          max_functions=6):
        with self._leased() as store:
            token = self._store_token(store)
            key = (start, end, search or '', bool(hide_skipped), int(bins), int(max_functions))
            return self._cached(self._timelines, token, key,
                                lambda: store.function_timeline(start, end, search, hide_skipped,
                                                                bins, max_functions))

    def window(self, start=None, end=None, bins=1200):
        with self._leased() as store:
            token = self._store_token(store)
            key = (start, end, int(bins))
            return self._cached(self._windows, token, key,
                                lambda: store.window(start, end, bins))

    # 诊断用（不进入页面 JSON）
    def trace_store_stats(self):
        with self._leased() as store:
            value = dict(generation=store.generation, counts=store.counts, stats=store.stats())
            value['verification'] = self._verification_of(store)
        return value


def adapter(folder, loader=None):
    """按记录目录缓存适配器（与 `record_store.record_store()` 同规模、同语义）。"""
    key = Path(folder).resolve()
    with _STORES_LOCK:
        existing = _STORES.pop(key, None)
        if existing is None:
            existing = TraceStoreAdapter(key, loader)
        else:
            existing.loader = loader
        _STORES[key] = existing
        while len(_STORES) > 4:
            _, dropped = _STORES.popitem(last=False)
            try:
                dropped.close()
            except Exception:
                pass
        return existing


def forget(folder):
    """测试/诊断用：从缓存移除并关闭适配器。"""
    key = Path(folder).resolve()
    with _STORES_LOCK:
        store = _STORES.pop(key, None)
    if store is not None:
        store.close()
