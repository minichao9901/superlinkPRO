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
`bindings.inputs` 里**决定事件流**的输入（etm.bin / firmware.elf / native.bin /
native-config.txt / image-*.bin / sources.json 等）与当前目录对照：不一致时明确报
“store 与当前记录输入不一致”，绝不返回拼凑结果。`session.json` / `decoded.json`
会被服务端在解码后继续改写（decodeStatus、captureStart 等），因此**不参与**陈旧判断。

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

import json
import threading
from collections import OrderedDict
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
MAX_HASH_BYTES = 64 << 20          # 超过 64 MiB 的输入只比尺寸，避免每次打开记录都重读大文件
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
    for name in STALENESS_INPUTS:
        parts.append(name + '=' + _stat_key(folder / name))
    decoded = folder / 'decoded.json'
    parts.append('decoded=' + _stat_key(decoded))
    for path in sorted(folder.glob('image-*.bin')):
        parts.append(path.name + '=' + _stat_key(path))
    return '|'.join(parts)


def _sha256(path):
    return trace_store._sha256_file(path)


def _stale_inputs(folder, manifest):
    """manifest.bindings.inputs 与当前目录对照；返回不一致项列表。"""
    folder = Path(folder)
    problems = []
    inputs = ((manifest.get('bindings') or {}).get('inputs') or {})
    for name in STALENESS_INPUTS:
        recorded = inputs.get(name)
        path = folder / name
        if not recorded or not path.is_file():
            continue
        stat = path.stat()
        if stat.st_size != recorded.get('bytes'):
            problems.append('%s（尺寸 %d≠%s）' % (name, stat.st_size, recorded.get('bytes')))
            continue
        expected = recorded.get('sha256')
        if expected and stat.st_size <= MAX_HASH_BYTES and _sha256(path) != expected:
            problems.append('%s（sha256 不符）' % name)
    for path in sorted(folder.glob('image-*.bin')):
        recorded = inputs.get(path.name)
        if not recorded or not path.is_file():
            continue
        if path.stat().st_size != recorded.get('bytes'):
            problems.append('%s（尺寸不符）' % path.name)
        elif recorded.get('sha256') and _sha256(path) != recorded['sha256']:
            problems.append('%s（sha256 不符）' % path.name)
    return problems


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
        self._windows = OrderedDict()
        self._timelines = OrderedDict()
        self._cache_lock = threading.Lock()

    # -- 生命周期 ----------------------------------------------------------------------------
    def signature(self):
        return signature(self.folder)

    def ready(self):
        """派生 store 是否存在且看起来对应当前记录（不抛异常、不做全量哈希）。"""
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

    def _open(self):
        """打开并校验 store（manifest sha256 + 块 CRC），并对照输入指纹。"""
        try:
            store = trace_store.TraceStore(self.path, verify='full')
        except trace_store.TraceStoreError as error:
            raise ValueError('trace-store 不可用：%s' % error)
        try:
            manifest = store.manifest
            problems = _stale_inputs(self.folder, manifest)
            if problems:
                raise ValueError(
                    'trace-store 与当前记录输入不一致（%s）；请重新解码或重新导入完整记录包，'
                    '本适配器不会把旧 store 当成当前执行历史' % '、'.join(problems[:6]))
            # 触发一次摘要/静态/索引块读取，尽早暴露块 CRC 或结构损坏。
            store.overview()
            store.stats()
        except Exception:
            store.close()
            raise
        return store

    def ensure(self):
        """确保 store 已发布、未损坏、未陈旧；必要时重新打开（新 generation）。"""
        current = self.signature()
        if self._store is not None and self._signature == current and self._verified:
            return
        with self._lock:
            current = self.signature()
            if self._store is not None and self._signature == current and self._verified:
                return
            store = self._open()
            old = self._store
            self._store = store
            self._signature = current
            self._verified = True
            if old is not None and old is not store:
                try:
                    old.close()
                except Exception:
                    pass

    def close(self):
        with self._lock:
            store, self._store = self._store, None
            self._verified = False
            if store is not None:
                store.close()

    def _active(self):
        self.ensure()
        return self._store

    # -- RecordStore 契约 --------------------------------------------------------------------
    def overview(self):
        return self._active().overview()

    def page(self, view='events', offset=0, limit=150, search='', hide_skipped=False,
             start=None, end=None):
        return self._active().page(view, offset, limit, search, hide_skipped, start, end)

    def event(self, seq):
        return self._active().event(seq)

    def iter_events(self, start=None, end=None):
        """流式展开事件（导出/诊断用；不属于 RecordStore 契约）。"""
        store = self._active()
        yield from store.events(start, end)

    def sources(self):
        store = self._active()
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
        return self._active().navigate(seq, direction, mode, pc, start, end)

    def issues(self, offset=0, limit=100):
        return self._active().issues(offset, limit)

    def _cached(self, cache, key, compute, limit=8):
        with self._cache_lock:
            if key in cache:
                cache.move_to_end(key)
                return cache[key]
        value = compute()
        with self._cache_lock:
            cache[key] = value
            while len(cache) > limit:
                cache.popitem(last=False)
        return value

    def function_timeline(self, start=None, end=None, search='', hide_skipped=False, bins=1600,
                          max_functions=6):
        store = self._active()
        key = (start, end, search or '', bool(hide_skipped), int(bins), int(max_functions))
        return self._cached(self._timelines, key,
                            lambda: store.function_timeline(start, end, search, hide_skipped,
                                                            bins, max_functions))

    def window(self, start=None, end=None, bins=1200):
        store = self._active()
        key = (start, end, int(bins))
        return self._cached(self._windows, key, lambda: store.window(start, end, bins))

    # 诊断用（不进入页面 JSON）
    def trace_store_stats(self):
        store = self._active()
        return dict(generation=store.generation, counts=store.counts, stats=store.stats())


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
