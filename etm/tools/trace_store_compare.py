"""trace-store v1 逐事件/逐接口对照器：旧记录 vs store 展开，差异必须为 0。

对照层次（对应方案 §9.2）：

1. **逻辑层**：完整展开 store，逐事件比较旧 ``events.jsonl`` / 内联 ``events`` 的每个字段；
   同时用 ``flow_analysis.CallTracker`` 重放展开流，与旧 ``decoded.json.analysis`` 对照。
2. **摘要层**：``overview``（summary/quality/analysis/summary 计数）、``issues``。
3. **接口层**：``page``（首/末/随机页、搜索、隐藏未成立）、``page(view=packets)``、``event``、
   ``navigate``（instruction/call/return/exception/issue/pc/source）、``window``、
   ``function_timeline`` 与旧 ``record_store.RecordStore`` 输出逐字段对照。

旧 ``RecordStore`` 的索引写入派生目录（镜像目录，硬链接原始文件），不改动原始证据目录。
允许的表示差异只有白名单里的规则；每条规则都要给出量化证明，否则判失败。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from trace_store import (TraceStore, TraceStoreError, INSTRUCTION_VARIANTS, EVENT_FIELDS,
                         peak_rss_bytes)

# 白名单：唯一的“允许表示差异”。逐项在报告里给出证明与计数。
WHITELIST = [
    dict(id='W1', area='overview',
         rule='store 在 overview 里新增 traceStore 摘要键（格式版本/代/展开段数）',
         proof='对照时排除该键；其余键逐项完全一致（见 overview.differences）'),
    dict(id='W2', area='analysis',
         rule='旧版解码器产出的记录缺少后加的分析字段时，只比较旧记录实际存在的键（逐层子集）',
         proof='缺失键清单在 analysis.omittedKeys 中逐项列出并计数，旧记录出现的键必须完全一致'),
    dict(id='W3', area='window/timeline',
         rule='对照固定使用 RecordStore 的 JSONL 回退路径（ETM_PYTHON_WINDOW=1），'
              '避免依赖可选原生 metrics 索引',
         proof='原生 metrics 与 JSONL 回放的等价性由 etm/tools/tests/test_record_store.py 保证；'
               '本对照器不声称覆盖原生 metrics 路径'),
]


class Comparison:
    """收集各层对照结果与差异样本。"""

    def __init__(self, name):
        self.name = name
        self.sections = {}
        self.failures = []

    def section(self, key):
        return self.sections.setdefault(key, dict(checks=0, differences=0, samples=[],
                                                  details={}))

    def note(self, key, check, difference, sample=None):
        section = self.section(key)
        section['checks'] += 1
        if difference:
            section['differences'] += 1
            if sample is not None and len(section['samples']) < 6:
                section['samples'].append(sample)
            if difference > 0:
                self.failures.append('%s: %s' % (key, sample if sample else '差异'))

    def equal(self, key, expected, actual, label):
        difference = expected != actual
        self.note(key, label, 1 if difference else 0,
                  None if not difference else dict(label=label,
                                                   expected=_clip(expected),
                                                   actual=_clip(actual)))
        return not difference

    def result(self):
        total = sum(section['checks'] for section in self.sections.values())
        differences = sum(section['differences'] for section in self.sections.values())
        return dict(name=self.name, checks=total, differences=differences,
                    sections={key: dict(value, samples=value['samples'])
                              for key, value in self.sections.items()},
                    failures=self.failures[:40], passed=differences == 0)


def _clip(value, limit=800):
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[:limit] + '…'


def _load_decoded(record_dir):
    with open(Path(record_dir) / 'decoded.json', 'rb') as handle:
        return json.loads(handle.read().decode('utf-8'))


def _old_events(record_dir, decoded):
    streamed = decoded.get('streamed') or {}
    if streamed.get('schema') in (2, 3):
        path = Path(record_dir) / 'events.jsonl'
        with path.open(encoding='utf-8') as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)
    else:
        yield from decoded['events']


def _apply_enrichment(timing, decoded):
    """复刻 flow_analysis.enrich() 的 coreHz/coveredSeconds 四行。

    旧内联记录走 ``decode_trace.decode`` → ``enrich(result, image)``，而 ``enrich`` 读
    ``result['targetSnapshot']``；解码路径从不设置该键（它由 server 从 session.json 附加），
    所以历史记录里的 ``coreHz`` 恒为 None、basis 也不会追加折算说明。这里按同一路径复原。
    """
    if 'coreHz' not in timing:
        return False
    timing['coreHz'] = None
    return True


def _subset_differences(expected, actual, path, out):
    """expected 是旧记录（可能缺新字段）：只要求旧记录出现的键完全一致。"""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            out.append(dict(path=path, expected=_clip(expected), actual=_clip(actual)))
            return
        for key, value in expected.items():
            if key not in actual:
                out.append(dict(path=path + '.' + key, expected=_clip(value), actual='<缺失>'))
                continue
            _subset_differences(value, actual[key], path + '.' + key, out)
        return
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            out.append(dict(path=path, expected=_clip(expected), actual=_clip(actual)))
            return
        for index, value in enumerate(expected):
            _subset_differences(value, actual[index], '%s[%d]' % (path, index), out)
        return
    if expected != actual:
        out.append(dict(path=path, expected=_clip(expected), actual=_clip(actual)))


def _missing_keys(expected, actual, path, out):
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key, value in expected.items():
            if key in actual:
                _missing_keys(value, actual[key], path + '.' + key, out)
        for key in actual:
            if key not in expected:
                out.append(path + '.' + key)
    elif isinstance(expected, list) and isinstance(actual, list):
        for index, (left, right) in enumerate(zip(expected, actual)):
            _missing_keys(left, right, '%s[%d]' % (path, index), out)


# ---------------------------------------------------------------------------------------------
# 旧 RecordStore 参考实现（镜像目录 + 硬链接，不改原始证据目录）
# ---------------------------------------------------------------------------------------------

REFERENCE_FILES = ('decoded.json', 'events.jsonl', 'packets.jsonl', 'browse-columns.tsv',
                   'sources.json', 'session.json', 'firmware.elf', 'etm.bin',
                   'browse-columns.tsv')


def prepare_reference(record_dir, reference_dir):
    """建立参考镜像：硬链接原文件（写索引时不改原始目录），返回目录。"""
    record_dir, reference_dir = Path(record_dir).resolve(), Path(reference_dir)
    reference_dir.mkdir(parents=True, exist_ok=True)
    for name in dict.fromkeys(REFERENCE_FILES):
        source = record_dir / name
        if not source.is_file():
            continue
        target = reference_dir / name
        if target.exists():
            continue
        try:
            os.link(source, target)
        except OSError:
            # 跨卷或不支持硬链接时退回复制（只复制 RecordStore 需要的文件）
            import shutil
            shutil.copy2(source, target)
    return reference_dir


def reference_loader(reference_dir):
    from stream_store import attach_streams

    def load():
        return attach_streams(_load_decoded(reference_dir), Path(reference_dir))
    return load


def open_reference_store(reference_dir, allow_build):
    from record_store import RecordStore
    store = RecordStore(Path(reference_dir), reference_loader(reference_dir))
    if not store.ready():
        if not allow_build:
            raise RuntimeError('参考 RecordStore 索引未就绪（%s）；加 --build-reference-index 允许构建'
                               % (Path(reference_dir) / 'browse-v3.sqlite'))
        started = time.time()
        store.ensure()
        print('  参考 RecordStore 索引构建完成（%.1f s）' % (time.time() - started), flush=True)
    return store


# ---------------------------------------------------------------------------------------------
# 逐层对照
# ---------------------------------------------------------------------------------------------

def compare_events(comparison, record_dir, decoded, store, limit_samples=6, progress=None):
    """完整逐事件对照；同时用 CallTracker 重放 store 展开流，供 analysis 对照。"""
    from flow_analysis import CallTracker
    tracker = CallTracker()
    section = comparison.section('events')
    count = 0
    field_counts = {}
    key_differences = 0
    iterator = store.events()
    for old in _old_events(record_dir, decoded):
        try:
            new = next(iterator)
        except StopIteration:
            section['details']['storeEvents'] = count
            comparison.note('events', 'count', 1,
                            dict(label='store 事件提前结束', at=count, old=old.get('seq')))
            return tracker
        count += 1
        if old != new:
            section['details']['firstDifferenceSeq'] = old.get('seq')
            if len(section['samples']) < limit_samples:
                keys = {}
                for key in set(old) | set(new):
                    if old.get(key, '<缺失>') != new.get(key, '<缺失>'):
                        keys[key] = [old.get(key, '<缺失>'), new.get(key, '<缺失>')]
                section['samples'].append(dict(seq=old.get('seq'), fields=keys))
            old_keys, new_keys = set(old), set(new)
            if old_keys != new_keys:
                key_differences += 1
                field_counts['<字段集合>'] = field_counts.get('<字段集合>', 0) + 1
            for key in old_keys & new_keys:
                if old[key] != new[key]:
                    field_counts[key] = field_counts.get(key, 0) + 1
        section['checks'] += 1
        if old != new:
            section['differences'] += 1
        if old.get('kind') != 'instruction':
            tracker.feed_event(old.get('type'), old.get('flags', 0), old.get('payload'),
                               old.get('end'))
        else:
            tracker.feed_instruction(old.get('function'), old.get('file', ''), old.get('line'),
                                    old.get('executed'), old.get('flow'), old['pc'], old['size'],
                                    int(old.get('cycleCount') or 0))
        if progress and count % 500000 == 0:
            progress('  逐事件对照：%d 条（差异 %d）' % (count, section['differences']))
    trailing = sum(1 for _ in iterator)
    section['details'].update(eventCount=count, fieldDifferences=field_counts,
                              keySetDifferences=key_differences, trailingStoreEvents=trailing)
    if trailing:
        comparison.note('events', 'count', 1, dict(label='store 事件多于旧记录', trailing=trailing))
    if section['differences']:
        comparison.failures.append('events: %d 条事件存在字段差异' % section['differences'])
    return tracker


def compare_analysis(comparison, decoded, tracker, store, rederive=True):
    old = decoded.get('analysis')
    if old is None:
        comparison.note('analysis', 'missing', 1, dict(label='旧记录没有 analysis'))
        return
    if rederive:
        actual = tracker.finish()
        section = comparison.section('analysis')
        if isinstance(old.get('timing'), dict) and isinstance(actual.get('timing'), dict) \
                and 'coreHz' in old['timing']:
            # 旧内联记录的 analysis 来自 enrich()；解码路径没有 targetSnapshot，coreHz 恒为 None
            old_core = old['timing'].get('coreHz')
            actual['timing']['coreHz'] = None
            section['details']['enrichedCoreHz'] = old_core
            if old_core is not None:
                section['details']['enrichmentNote'] = (
                    '旧记录在带 targetSnapshot 的路径上做过 enrich；本对照器只验证无快照路径')
        differences = []
        _subset_differences(old, actual, 'analysis', differences)
        omitted = []
        _missing_keys(old, actual, 'analysis', omitted)
        section['details'].update(comparedKeys=sorted(old), omittedKeys=omitted,
                                  differences=differences[:10], mode='subset')
        section['checks'] = 1
        section['differences'] = 1 if differences else 0
        if differences:
            section['samples'] = differences[:6]
            comparison.failures.append('analysis: 重放分析结果与旧记录不一致（%d 处）'
                                       % len(differences))
    # 旧记录里保存的分析摘要必须与 store 保存的摘要一致（生成即对账）
    comparison.equal('summary', old, (store.summary or {}).get('analysis'),
                     'analysis 摘要（store 保存 vs 旧 decoded.json）')


def compare_overview(comparison, decoded, store, reference_store):
    section = comparison.section('overview')
    overview = store.overview()
    comparison.store_overview = overview
    streamed = decoded.get('streamed') or {}
    expected = {key: value for key, value in decoded.items()
                if key not in ('events', 'elements', 'packets', 'sources')}
    expected['eventCount'] = (streamed.get('events') if streamed.get('schema') is not None
                              else len(decoded.get('events') or []))
    expected['packetCount'] = (streamed.get('packets') if streamed.get('schema') is not None
                               else len(decoded.get('packets') or []))
    expected['sourceFiles'] = list(decoded.get('sources') or {})
    if not expected['sourceFiles']:
        sources = Path(comparison.record_dir) / 'sources.json'
        if sources.is_file():
            expected['sourceFiles'] = list(json.loads(sources.read_text(encoding='utf-8')))
    # firstSeq 以旧事件的第一个指令 seq 为准（与 RecordStore 的口径一致）
    expected['firstSeq'] = 0
    for event in _old_events(comparison.record_dir, decoded):
        if event.get('kind') == 'instruction':
            expected['firstSeq'] = event['seq']
            break
    actual = dict(overview)
    removed = actual.pop('traceStore', None)
    section['details']['traceStore'] = removed
    if removed is not None:
        section['details']['whitelist'] = 'W1'
    _compare_dict(comparison, 'overview', expected, actual, 'overview')
    if reference_store is not None:
        reference = reference_store.overview()
        _compare_dict(comparison, 'overview', reference, actual, 'overview vs RecordStore')


def _compare_dict(comparison, key, expected, actual, label):
    if isinstance(expected, dict) and isinstance(actual, dict):
        for name in expected:
            if name not in actual:
                comparison.note(key, 1, 1, dict(label=label, path=name, expected=_clip(expected[name]),
                                                actual='<缺失>'))
            else:
                _compare_dict(comparison, key, expected[name], actual[name], label + '.' + name)
        for name in actual:
            if name not in expected:
                comparison.note(key, 1, 1, dict(label=label, path=name, actual=_clip(actual[name]),
                                                expected='<缺失>'))
        return
    comparison.equal(key, expected, actual, label)


def compare_record_store(comparison, store, reference_store, options):
    rng = random.Random(options.seed)
    total = int(store.counts.get('events') or 0)
    record_total = total
    limit = 150
    # ---- page ---------------------------------------------------------------------------
    offsets = [0, record_total - 1, 10 ** 9, record_total // 3, record_total // 2]
    offsets += [rng.randrange(0, max(1, record_total)) for _ in range(options.random_pages)]
    for offset in offsets:
        expected = reference_store.page('events', offset, limit)
        actual = store.page('events', offset, limit)
        comparison.equal('page', expected, actual, 'page(offset=%d)' % offset)
    # 搜索页：大数据集限制区间，保持两边同样范围（旧 page 的 total 也是区间内计数）
    span = min(200000, record_total)
    lo = rng.randrange(0, max(1, record_total - span)) if record_total > span else 0
    terms = options.search_terms or ['main', '0x0800']
    for term in terms:
        expected = reference_store.page('events', 0, limit, term, False, lo, lo + span)
        actual = store.page('events', 0, limit, term, False, lo, lo + span)
        comparison.equal('page', expected, actual, 'page(search=%r,range=%d..%d)'
                         % (term, lo, lo + span))
    # 隐藏未成立
    expected = reference_store.page('events', 0, limit, '', True, lo, lo + span)
    actual = store.page('events', 0, limit, '', True, lo, lo + span)
    comparison.equal('page', expected, actual, 'page(hideSkipped,range=%d..%d)' % (lo, lo + span))
    # 包页
    packet_total = int(store.counts.get('packets') or 0)
    for offset in (0, max(0, packet_total - 1), packet_total // 2, 10 ** 9):
        expected = reference_store.page('packets', offset, limit)
        actual = store.page('packets', offset, limit)
        comparison.equal('page', expected, actual, 'page(packets,offset=%d)' % offset)
    # ---- event --------------------------------------------------------------------------
    seqs = [0, 1, record_total - 1, record_total // 2, record_total // 3]
    seqs += [rng.randrange(0, max(1, record_total)) for _ in range(options.random_events)]
    for seq in seqs:
        expected = reference_store.event(seq)
        actual = store.event(seq)
        comparison.equal('event', expected, actual, 'event(%d)' % seq)
    # ---- navigate -----------------------------------------------------------------------
    modes = ('instruction', 'call', 'return', 'exception', 'issue')
    navigation_seqs = [0, record_total // 4, record_total // 2, record_total - 2]
    for mode in modes:
        for seq in navigation_seqs:
            for direction in (1, -1):
                expected = reference_store.navigate(seq, direction, mode)
                actual = store.navigate(seq, direction, mode)
                comparison.equal('navigate', expected, actual,
                                 'navigate(%s,%d,%d)' % (mode, seq, direction))
    # pc 定位：固定在小区间里，避免退化成整段扫描
    scope_end = min(record_total - 1, 50000)
    for pc in [entry.pc for entry in store.static[:3]]:
        for seq in (0, 100, scope_end // 2):
            for direction in (1, -1):
                expected = reference_store.navigate(seq, direction, 'instruction', pc,
                                                    0, scope_end)
                actual = store.navigate(seq, direction, 'instruction', pc, 0, scope_end)
                comparison.equal('navigate', expected, actual,
                                 'navigate(pc=0x%08x,%d,%d)' % (pc, seq, direction))
    # source 导航（区间限定）
    for seq in (0, scope_end // 2, scope_end - 10):
        expected = reference_store.navigate(seq, 1, 'source', None, 0, scope_end)
        actual = store.navigate(seq, 1, 'source', None, 0, scope_end)
        comparison.equal('navigate', expected, actual, 'navigate(source,%d)' % seq)
    # ---- window / function_timeline -----------------------------------------------------
    for lo, hi in options.window_ranges or default_windows(record_total):
        expected = reference_store.window(lo, hi, options.bins)
        actual = store.window(lo, hi, options.bins)
        comparison.equal('window', expected, actual, 'window(%d,%d)' % (lo, hi))
        expected = reference_store.function_timeline(lo, hi, '', False, 1600, 6)
        actual = store.function_timeline(lo, hi, '', False, 1600, 6)
        comparison.equal('timeline', expected, actual, 'function_timeline(%d,%d)' % (lo, hi))
    # ---- issues -------------------------------------------------------------------------
    expected = reference_store.issues(0, 500)['total']
    actual = store.issues(0, 500)['total']
    comparison.equal('issues', expected, actual, 'issues.total')
    expected = reference_store.issues(0, 200)['rows']
    actual = store.issues(0, 200)['rows']
    comparison.equal('issues', expected, actual, 'issues.rows(0,200)')


def default_windows(total):
    """默认窗口：全段（小记录）+ 一段 20 万条的局部窗口。"""
    windows = []
    if total <= 400000:
        windows.append((0, total - 1))
    else:
        windows.append((total // 3, min(total - 1, total // 3 + 200000)))
        windows.append((0, min(total - 1, 200000)))
    return windows


# ---------------------------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------------------------

def run(record_dir, store_dir, options):
    started = time.time()
    record_dir, store_dir = Path(record_dir).resolve(), Path(store_dir).resolve()
    decoded = _load_decoded(record_dir)
    comparison = Comparison(record_dir.name)
    comparison.record_dir = record_dir
    comparison.store_overview = None
    print('== %s' % record_dir.name, flush=True)
    print('  打开 store（校验 manifest + sha256 + 块 CRC）…', flush=True)
    store = TraceStore(store_dir, verify='full')
    try:
        print('  展开段 %d，检查点 %d，块 %d' % (store.counts.get('runs'),
                                                store.counts.get('checkpoints'),
                                                store.stats()['blocks']), flush=True)
        if not options.skip_events:
            tracker = compare_events(comparison, record_dir, decoded, store,
                                     progress=print if options.progress else None)
            print('  逐事件对照完成：%d 条，差异 %d'
                  % (comparison.section('events')['checks'],
                     comparison.section('events')['differences']), flush=True)
            compare_analysis(comparison, decoded, tracker, store,
                             rederive=not options.skip_analysis)
        reference_store = None
        if not options.no_reference:
            reference_dir = options.reference_dir or default_reference_dir(record_dir)
            print('  准备旧 RecordStore 参考（镜像 %s）…' % reference_dir, flush=True)
            prepare_reference(record_dir, reference_dir)
            os.environ['ETM_PYTHON_WINDOW'] = '1'      # W3：固定 JSONL 回退路径
            reference_store = open_reference_store(reference_dir,
                                                   options.build_reference_index)
        compare_overview(comparison, decoded, store, reference_store)
        if reference_store is not None:
            print('  逐接口对照（page/event/navigate/window/timeline/issues）…', flush=True)
            compare_record_store(comparison, store, reference_store, options)
        report = comparison.result()
        report.update(recordDir=str(record_dir), storeDir=str(store_dir),
                      generation=store.generation, schemaVersion=store.meta.get('schemaVersion'),
                      storeBytes=sum(entry['bytes'] for entry in store.manifest['files']),
                      storeSha256=[entry['sha256'] for entry in store.manifest['files']],
                      counts=store.counts, stats=store.stats(),
                      whitelist=WHITELIST,
                      elapsedSeconds=round(time.time() - started, 3),
                      peakRssBytes=peak_rss_bytes())
        return report
    finally:
        store.close()


def default_reference_dir(record_dir):
    base = Path(os.environ.get('TRACE_STORE_REFERENCE_ROOT')
                or (Path(os.environ.get('TEMP', '.')) / 'trace-store-reference'))
    return base / Path(record_dir).name


def _main(argv=None):
    parser = argparse.ArgumentParser(
        prog='trace_store_compare.py',
        description='trace-store v1 对照器：完整逐事件 + overview/page/event/navigate/window/'
                    'timeline/issues 与旧记录和旧 RecordStore 对照，差异必须为 0。')
    parser.add_argument('record_dir', help='旧解码记录目录')
    parser.add_argument('store_dir', help='trace-store 目录')
    parser.add_argument('--no-reference', action='store_true', help='不与旧 RecordStore 对照')
    parser.add_argument('--build-reference-index', action='store_true',
                        help='允许构建参考 RecordStore 的 SQLite 索引（写镜像目录，不改原记录）')
    parser.add_argument('--reference-dir', type=Path, default=None, help='参考镜像目录')
    parser.add_argument('--skip-events', action='store_true', help='跳过完整逐事件对照')
    parser.add_argument('--skip-analysis', action='store_true', help='跳过分析重放对照')
    parser.add_argument('--random-pages', type=int, default=8, help='随机页数量')
    parser.add_argument('--random-events', type=int, default=8, help='随机 event 数量')
    parser.add_argument('--search-terms', nargs='*', default=None, help='搜索页关键词')
    parser.add_argument('--window-ranges', type=parse_range, nargs='*', default=None,
                        help='窗口区间，形如 1000:200000')
    parser.add_argument('--bins', type=int, default=1200, help='窗口分桶数')
    parser.add_argument('--seed', type=int, default=20261019, help='随机种子')
    parser.add_argument('--json-out', type=Path, default=None, help='把完整报告写成 JSON')
    parser.add_argument('--progress', action='store_true', help='打印进度')
    args = parser.parse_args(argv)
    try:
        report = run(args.record_dir, args.store_dir, args)
    except (TraceStoreError, RuntimeError) as error:
        print('对照失败：%s' % error, file=sys.stderr)
        return 2
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text + '\n', encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('name', 'checks', 'differences', 'passed',
                                                   'elapsedSeconds', 'peakRssBytes')},
                     ensure_ascii=False, indent=2))
    for key, section in report['sections'].items():
        print('  %-10s 校验 %7d 项，差异 %d' % (key, section['checks'], section['differences']))
    if not report['passed']:
        print('差异样本：')
        for key, section in report['sections'].items():
            for sample in section['samples'][:3]:
                print('  [%s] %s' % (key, _clip(sample, 1200)))
        return 1
    print('结果：全部差异为 0，白名单 %d 条（见报告）' % len(WHITELIST))
    return 0


def parse_range(text):
    left, _, right = text.partition(':')
    return int(left), int(right)


if __name__ == '__main__':
    sys.exit(_main())
