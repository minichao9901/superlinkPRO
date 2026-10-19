"""trace-store v1 参考读写器的往返与查询兼容测试（不依赖网络/硬件/真实采集）。"""
import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import trace_store
from trace_store import TraceStore, TraceStoreError, write


# ---------------------------------------------------------------------------------------------
# 合成记录：覆盖调用/返回、嵌套异常、谓词未知、周期折叠、边界事件、gap 与缺省字段变体
# ---------------------------------------------------------------------------------------------

def _ins(pc, code, mnemonic, operands, function, file, line, flow, conditional, executed=True):
    return dict(pc=pc, bytes=code, mnemonic=mnemonic, operands=operands, function=function,
                file=file, line=line, flow=flow, conditional=conditional, executed=executed)


RANGES = [
    dict(offset=10, cycles=0, last=False, ins=[
        _ins(0x100, '1b78', 'ldrb', 'r3, [r3]', 'main', 'main.c', 10, 'linear', False),
        _ins(0x102, '002b', 'cmp', 'r3, #0', 'main', 'main.c', 10, 'linear', False),
        _ins(0x104, '00d0', 'beq', '#0x8000108', 'main', 'main.c', 11, 'branch', True,
             executed=False),
    ]),
    dict(offset=12, cycles=0, last=True, ins=[
        _ins(0x106, '00f0', 'bl', '#0x8000200', 'main', 'main.c', 12, 'call', False),
    ]),
    dict(offset=14, cycles=0, last=True, folded=5, ins=[
        _ins(0x200, '0368', 'ldr', 'r3, [r0]', 'leaf', 'leaf.c', 1, 'linear', False),
        _ins(0x202, '7047', 'bx', 'lr', 'leaf', 'leaf.c', 2, 'return', False),
    ]),
    dict(offset=20, cycles=3, last=True, ins=[
        _ins(0x108, '0133', 'adds', 'r3, #1', 'main', 'main.c', 13, 'linear', False),
        _ins(0x10a, '0bd1', 'bne', '#0x8000100', 'main', 'main.c', 13, 'branch', True),
    ]),
    dict(offset=24, cycles=0, last=True, ins=[
        _ins(0x10c, '4ff0', 'it', 'eq', 'main', 'main.c', 14, 'linear', False),
        _ins(0x10e, '01bf', 'nop', '', 'main', 'main.c', 14, 'linear', False, executed=None),
    ]),
    dict(offset=30, cycles=0, last=True, ins=[
        _ins(0x110, '10bd', 'pop', '{r4, pc}', 'main', 'main.c', 15, 'return', False),
    ]),
    dict(offset=40, cycles=0, last=False, ins=[
        _ins(0x300, '70b5', 'push', '{r4, r5, r6, lr}', 'irq', 'irq.c', 3, 'linear', False),
        _ins(0x302, '0120', 'movs', 'r0, #1', 'irq', 'irq.c', 4, 'linear', False),
        _ins(0x304, '00bd', 'pop', '{pc}', 'irq', 'irq.c', 5, 'return', False, executed=False),
    ]),
    dict(offset=50, cycles=7, last=True, ins=[
        _ins(0x112, '00bf', 'nop', '', 'main', 'main.c', 16, 'linear', False),
    ]),
]


def build_events(cycles=True):
    """按 RANGES 生成旧格式事件（含 callPath/cycles），返回 (events, elements, summary, quality)。"""
    events = []
    elements = []

    def event(etype, payload=0, start=0, end=0, flags=0, text=''):
        return dict(kind='event', type=etype, offset=0, text=text or 'EVENT_%d' % etype,
                    payload=payload, start=start, end=end, flags=flags)
    # 起始边界事件
    events.append(dict(event(1, payload=1, text='OCSD_GEN_TRC_ELEM_NO_SYNC'), seq=0))
    events[-1]['offset'] = 0
    elements.append(dict(kind='element', type=1, offset=0, payload=1, start=0, end=0, flags=0,
                         cycleCount=0, lastExecuted=0, text='NO_SYNC'))
    events.append(dict(event(2, text='OCSD_GEN_TRC_ELEM_TRACE_ON'), seq=1))
    events[-1]['offset'] = 2
    elements.append(dict(kind='element', type=2, offset=2, payload=0, start=0, end=0, flags=0,
                         cycleCount=0, lastExecuted=0, text='TRACE_ON'))
    for index, spec in enumerate(RANGES):
        if index == 6:
            events.append(dict(event(9, payload=15, end=0x300, flags=64,
                                     text='OCSD_GEN_TRC_ELEM_EXCEPTION'), seq=len(events)))
            events[-1]['offset'] = 38
            elements.append(dict(kind='element', type=9, offset=38, payload=15, start=0,
                                 end=0x300, flags=64, cycleCount=0, lastExecuted=0,
                                 text='EXCEPTION'))
        first = True
        effective = spec['cycles'] + spec.get('folded', 0)
        for item in spec['ins']:
            position = len(events)
            ev = dict(kind='instruction', seq=position, offset=spec['offset'], pc=item['pc'],
                      size=len(bytes.fromhex(item['bytes'])), bytes=item['bytes'],
                      mnemonic=item['mnemonic'], operands=item['operands'],
                      executed=item['executed'], function=item['function'], file=item['file'],
                      line=item['line'], flow=item['flow'], conditional=item['conditional'])
            if cycles:
                tail = item is spec['ins'][-1]
                ev.update(cycleCount=(effective if tail else 0),
                          cycleSpan=(len(spec['ins']) if tail and effective else 0))
            events.append(ev)
            first = False
        last = spec['ins'][-1]
        elements.append(dict(kind='element', type=5, offset=spec['offset'],
                             start=spec['ins'][0]['pc'],
                             end=last['pc'] + len(bytes.fromhex(last['bytes'])),
                             payload=len(spec['ins']), lastExecuted=int(spec['last']),
                             cycleCount=spec['cycles'], flags=0, start_raw=0,
                             text='RANGE'))
        if spec.get('folded'):
            elements.append(dict(kind='element', type=12, offset=spec['offset'],
                                 payload=0, start=0, end=0, flags=0,
                                 cycleCount=spec['folded'], lastExecuted=0, text='CYCLE_COUNT'))
    if True:
        events.append(dict(event(10, text='OCSD_GEN_TRC_ELEM_EXCEPTION_RET'), seq=len(events)))
        events[-1]['offset'] = 45
        elements.append(dict(kind='element', type=10, offset=45, payload=0, start=0, end=0,
                             flags=0, cycleCount=0, lastExecuted=0, text='EXCEPTION_RET'))
    events.append(dict(event(3, payload=7, text='OCSD_GEN_TRC_ELEM_EO_TRACE'), seq=len(events)))
    events[-1]['offset'] = 60
    elements.append(dict(kind='element', type=3, offset=60, payload=7, start=0, end=0, flags=0,
                         cycleCount=0, lastExecuted=0, text='EO_TRACE'))

    # 执行状态与谓词未知口径与 decode_stream 一致
    from flow_analysis import analyze_calls
    for element in events:
        if element['kind'] == 'instruction':
            executed = element['executed']
            element['executionState'] = ('executed' if executed is True else
                                         'not-taken' if executed is False else 'unknown')
            element['executionReason'] = (trace_store.IT_REASON if executed is None else '')
            if cycles:
                element['cycles'] = element.get('cycleCount', 0)
            element['callPath'] = []
    analyze_calls(events)
    if not cycles:
        # 旧版记录没有 cycleCount/cycleSpan，也没有 analyze_calls 后加的 cycles
        for item in events:
            item.pop('cycles', None)
    instructions = [item for item in events if item['kind'] == 'instruction']
    summary = dict(
        packets=0, ranges=sum(1 for item in elements if item['type'] == 5),
        instructions=len(instructions),
        executed=sum(1 for item in instructions if item['executed'] is True),
        sourceMapped=sum(1 for item in instructions if item['line']),
        exceptions=sum(1 for item in elements if item['type'] == 9),
        gaps=1, packetIssues=0,
        unknownExecution=sum(1 for item in instructions if item['executed'] is None))
    quality = dict(inputBytes=64, processedBytes=64, unprocessedBytes=0, accountedBytes=64,
                   overlapBytes=0, unaccountedBytes=0, unsynchronizedBytes=0,
                   unsynchronizedRecords=0, validPackets=0, packetIssues=0, overflowPackets=0,
                   decoderLastError=0,
                   unknownExecution=summary['unknownExecution'],
                   firstPathOffset=instructions[0]['offset'],
                   timing=dict(cycleElements=sum(1 for item in elements if item['type'] == 12),
                               timeFlameAvailable=False),
                   boundaryPresent=True)
    analysis = analyze_calls(copy.deepcopy(events))
    return events, elements, summary, quality, analysis


def build_packets(count=20):
    packets = []
    offset = 0
    for index in range(count):
        raw = bytes([0x04 + (index % 3)])
        packets.append(dict(kind='packet', offset=offset, size=len(raw), hex=raw.hex(),
                            text='I_ATOM_F1 : Atom format 1.; %d' % index))
        offset += len(raw)
    return packets


def build_fixture(folder, *, cycles=True, jsonl=True, with_packets=True):
    """在 folder 里构造一个既有记录目录（streamed schema 2 或 decoded.json 内联）。"""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    events, elements, summary, quality, analysis = build_events(cycles=cycles)
    packets = build_packets() if with_packets else []
    summary['packets'] = len(packets)
    sources = {'main.c': 'line1\nline2\n', 'leaf.c': 'leaf\n', 'irq.c': 'irq\n'}
    decoded = dict(version=3, protocol='ETMv4',
                   config=dict(idr0=0x080006E1, idr1=0x4100F401, idr2=4, idr8=1, idr9=0,
                               idr10=0, idr11=0, idr12=1, idr13=0, config=1, traceid=2),
                   inputBytes=64, elf=dict(name='firmware.elf', sha256='0' * 64),
                   summary=summary, quality=quality, analysis=analysis,
                   status=dict(kind='status', processed=64, library='1.8.3', lastErrorCode=0,
                               lastError=''),
                   timeBasis='execution order; no per-instruction timestamp')
    if jsonl:
        decoded['streamed'] = dict(eventsFile='events.jsonl', events=len(events),
                                   elements=len(elements), packets=len(packets),
                                   nativeJsonl='native.jsonl', schema=2)
        (folder / 'events.jsonl').write_text(
            ''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in events),
            encoding='utf-8')
        (folder / 'elements.jsonl').write_text(
            ''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in elements),
            encoding='utf-8')
        (folder / 'packets.jsonl').write_text(
            ''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in packets),
            encoding='utf-8')
        (folder / 'sources.json').write_text(json.dumps(sources, ensure_ascii=False),
                                             encoding='utf-8')
    else:
        decoded['events'] = events
        decoded['elements'] = elements
        decoded['packets'] = packets
        decoded['sources'] = sources
    (folder / 'decoded.json').write_text(json.dumps(decoded, ensure_ascii=False),
                                         encoding='utf-8')
    (folder / 'etm.bin').write_bytes(b'\x00' * 64)
    (folder / 'session.json').write_text(json.dumps({'name': 'fixture', 'seconds': 0.01}),
                                         encoding='utf-8')
    return folder, events, packets, sources


def old_events(record_dir):
    folder = Path(record_dir)
    decoded = json.loads((folder / 'decoded.json').read_text(encoding='utf-8'))
    if (decoded.get('streamed') or {}).get('schema') is not None:
        with (folder / 'events.jsonl').open(encoding='utf-8') as handle:
            return [json.loads(line) for line in handle if line.strip()]
    return decoded['events']


class FixtureCase(unittest.TestCase):
    """公共夹具：一个记录目录 + 一个已写好的 store。"""

    cycles = True
    jsonl = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.record = self.root / '记录 目录'          # 含空格与非 ASCII，验证路径处理
        build_fixture(self.record, cycles=self.cycles, jsonl=self.jsonl)
        self.store_dir = self.root / 'store'
        self.result = write(self.record, self.store_dir, verify_image=False)
        self.events = old_events(self.record)

    def tearDown(self):
        import gc
        gc.collect()
        self.tmp.cleanup()

    def test_full_expansion_is_field_identical(self):
        with TraceStore(self.store_dir) as store:
            expanded = list(store.events())
        self.assertEqual(len(expanded), len(self.events))
        for index, (expected, actual) in enumerate(zip(self.events, expanded)):
            self.assertEqual(expected, actual, 'seq=%d 展开结果不同' % index)

    def test_first_last_and_seq_position(self):
        with TraceStore(self.store_dir) as store:
            self.assertEqual(store.first(), 0)
            self.assertEqual(store.last(), len(self.events) - 1)
            self.assertEqual(store.bounds(), [0, len(self.events) - 1])
            for seq in (0, 1, len(self.events) // 2, len(self.events) - 1):
                position = store.seq_position(seq)
                self.assertEqual(position['validRange'], [0, len(self.events) - 1])
                self.assertLessEqual(position['replayInstructions'],
                                     store.meta['layout']['checkpointInstructions'])
                self.assertLessEqual(position['checkpointSeq'], seq)
            with self.assertRaises(TraceStoreError):
                store.event(len(self.events))

    def test_page_matches_old_slices(self):
        with TraceStore(self.store_dir) as store:
            total = len(self.events)
            for offset in (0, 1, 7, total - 1, total, 10 ** 9):
                page = store.page(offset=offset, limit=5)
                expected_offset = min(offset, max(0, ((total - 1) // 5) * 5))
                self.assertEqual(page['total'], total)
                self.assertEqual(page['offset'], expected_offset)
                self.assertEqual(page['rows'], self.events[expected_offset:expected_offset + 5])
            page = store.page(start=3, end=9, limit=100)
            expected = [item for item in self.events if 3 <= item['seq'] <= 9]
            self.assertEqual(page['rows'], expected)
            self.assertEqual(page['total'], len(expected))

    @staticmethod
    def old_search_text(event):
        """旧 sidecar 的搜索列规则（decode_stream._InstructionTable.info / 事件分支）。"""
        if event['kind'] == 'instruction':
            text = ' '.join((event.get('function') or '', event.get('file') or '',
                             event['mnemonic'], event['operands'], ''))
            text = ('%s 0x%08x %d' % (text, event['pc'], event['pc'])).lower()
            return text.replace('\t', ' ').replace('\n', ' ')
        return ('    ' + event.get('text', '')).lower().replace('\t', ' ').replace('\n', ' ')

    def test_search_and_hide_skipped_match_old_semantics(self):
        with TraceStore(self.store_dir) as store:
            expected = [item for item in self.events
                        if 'main.c' in self.old_search_text(item)]
            page = store.page(search='main.c', limit=500)
            self.assertEqual(page['rows'], expected)
            self.assertEqual(page['total'], len(expected))
            skipped = [item for item in self.events
                       if item['kind'] != 'instruction'
                       or item.get('executionState') != 'not-taken']
            page = store.page(hide_skipped=True, limit=500)
            self.assertEqual(page['rows'], skipped)

    def test_packet_page_matches_old_records(self):
        if (self.record / 'packets.jsonl').is_file():
            packets = [json.loads(line) for line in
                       (self.record / 'packets.jsonl').read_text(encoding='utf-8').splitlines()
                       if line.strip()]
        else:
            decoded = json.loads((self.record / 'decoded.json').read_text(encoding='utf-8'))
            packets = list(decoded.get('packets') or [])
        with TraceStore(self.store_dir) as store:
            page = store.page('packets', 0, 5)
            self.assertEqual(page['total'], len(packets))
            self.assertEqual(page['rows'], packets[:5])
            page = store.page('packets', 10 ** 9, 5)
            self.assertEqual(page['offset'], max(0, ((len(packets) - 1) // 5) * 5))

    def test_event_equals_full_expansion(self):
        with TraceStore(self.store_dir) as store:
            for seq in (0, 2, 3, 5, len(self.events) // 2, len(self.events) - 1):
                actual = store.event(seq)['event']
                self.assertEqual(actual, self.events[seq])
                self.assertEqual(store.event(seq)['callPath'],
                                 self.events[seq].get('callPath', []))

    def test_overview_keeps_summary_quality_analysis(self):
        decoded = json.loads((self.record / 'decoded.json').read_text(encoding='utf-8'))
        with TraceStore(self.store_dir) as store:
            overview = store.overview()
        self.assertEqual(overview['summary'], decoded['summary'])
        self.assertEqual(overview['quality'], decoded['quality'])
        self.assertEqual(overview['analysis'], decoded['analysis'])
        first_instruction = next(item['seq'] for item in self.events
                                 if item['kind'] == 'instruction')
        self.assertEqual(overview['eventCount'], len(self.events))
        self.assertEqual(overview['firstSeq'], first_instruction)
        self.assertIn('traceStore', overview)                 # 白名单 W1：新增摘要键

    def test_analysis_replay_matches_old_analysis(self):
        from flow_analysis import CallTracker
        decoded = json.loads((self.record / 'decoded.json').read_text(encoding='utf-8'))
        tracker = CallTracker()
        with TraceStore(self.store_dir) as store:
            for item in store.events():
                if item['kind'] != 'instruction':
                    tracker.feed_event(item['type'], item.get('flags', 0), item.get('payload'),
                                       item.get('end'))
                else:
                    tracker.feed_instruction(item.get('function'), item.get('file', ''),
                                             item.get('line'), item.get('executed'),
                                             item.get('flow'), item['pc'], item['size'],
                                             int(item.get('cycleCount') or 0))
        self.assertEqual(tracker.finish(), decoded['analysis'])

    def test_window_and_timeline_match_old_record_store(self):
        from record_store import RecordStore
        import os
        from stream_store import attach_streams

        def loader():
            return attach_streams(
                json.loads((self.record / 'decoded.json').read_text(encoding='utf-8')),
                self.record)

        reference = RecordStore(self.record, loader)
        reference.ensure()
        os.environ['ETM_PYTHON_WINDOW'] = '1'
        try:
            total = len(self.events)
            for lo, hi in ((0, total - 1), (0, min(total - 1, 12)), (5, 5)):
                with TraceStore(self.store_dir) as store:
                    self.assertEqual(store.window(lo, hi), reference.window(lo, hi),
                                     'window(%d,%d)' % (lo, hi))
                    self.assertEqual(store.function_timeline(lo, hi),
                                     reference.function_timeline(lo, hi),
                                     'function_timeline(%d,%d)' % (lo, hi))
            with TraceStore(self.store_dir) as store:
                self.assertEqual(store.issues(0, 500), reference.issues(0, 500))
                for mode in ('instruction', 'call', 'return', 'exception', 'issue', 'source'):
                    for seq in (0, 3, total // 2):
                        for direction in (1, -1):
                            self.assertEqual(store.navigate(seq, direction, mode),
                                             reference.navigate(seq, direction, mode),
                                             'navigate(%s,%d,%d)' % (mode, seq, direction))
                for pc in (0x100, 0x200, 0x300):
                    for seq in (0, 5):
                        self.assertEqual(store.navigate(seq, 1, 'instruction', pc),
                                         reference.navigate(seq, 1, 'instruction', pc),
                                         'navigate(pc=0x%x,%d)' % (pc, seq))
                self.assertEqual(store.page(offset=0, limit=150),
                                 reference.page(offset=0, limit=150))
                self.assertEqual(store.page(offset=total - 1),
                                 reference.page(offset=total - 1))
        finally:
            os.environ.pop('ETM_PYTHON_WINDOW', None)


class StreamedCyclesTests(FixtureCase):
    cycles = True
    jsonl = True


class InlineNoCyclesTests(FixtureCase):
    """旧版内联记录：没有 cycleCount/cycleSpan/cycles 字段，store 不得补造它们。"""

    cycles = False
    jsonl = False

    def test_field_presence_is_preserved(self):
        with TraceStore(self.store_dir) as store:
            self.assertEqual(store.meta['fieldPresence']['instruction'],
                             list(trace_store.INSTRUCTION_FIELDS) +
                             ['flow', 'conditional', 'executionState', 'executionReason',
                              'callPath'])
            self.assertFalse(store.has_cycle_fields)
            item = next(store.events())
        self.assertNotIn('cycleCount', item)
        self.assertNotIn('cycles', item)


class StoreShapeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_static_dictionary_is_per_pc_and_body_has_no_event_json(self):
        record, events, _, _ = build_fixture(self.root / 'record')
        store_dir = self.root / 'store'
        result = write(record, store_dir, verify_image=False)
        unique = {item['pc'] for item in events if item['kind'] == 'instruction'}
        self.assertEqual(result['counts']['staticPcs'], len(unique))
        self.assertEqual(result['counts']['instructions'],
                         sum(1 for item in events if item['kind'] == 'instruction'))
        self.assertLess(result['storeBytes'],
                        (record / 'events.jsonl').stat().st_size)
        with TraceStore(store_dir) as store:
            self.assertEqual(len(store.static), len(unique))
            # 静态字典每条只出现一次；重复 PC 不再增加条目
            self.assertEqual(len({entry.pc for entry in store.static}), len(store.static))
            stats = store.stats()
            self.assertTrue(stats['aggregateMatches'])
            self.assertEqual(stats['instructions'], result['counts']['instructions'])
        # 存储里不保存逐指令 JSON 行：manifest 里只有 store.bin，且没有 events 副本
        generation = store_dir / 'generations' / result['generation']
        names = sorted(path.name for path in generation.iterdir())
        self.assertEqual(names, ['store.bin'])
        manifest = json.loads((store_dir / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['state'], 'complete')
        self.assertEqual(manifest['schemaVersion'], trace_store.SCHEMA_VERSION)
        self.assertIn('inputs', manifest['bindings'])
        self.assertEqual(manifest['bindings']['record']['eventsSource'], 'events.jsonl')

    def test_inline_and_jsonl_sources_produce_the_same_events(self):
        jsonl_record, events, _, _ = build_fixture(self.root / 'jsonl', jsonl=True)
        inline_record, _, _, _ = build_fixture(self.root / 'inline', jsonl=False)
        write(jsonl_record, self.root / 'store-a', verify_image=False)
        write(inline_record, self.root / 'store-b', verify_image=False)
        with TraceStore(self.root / 'store-a') as left, TraceStore(self.root / 'store-b') as right:
            self.assertEqual(list(left.events()), list(right.events()))
            self.assertEqual(left.overview()['eventCount'], right.overview()['eventCount'])
        with TraceStore(self.root / 'store-a') as store:
            self.assertEqual(list(store.events()), events)

    def test_rewrite_publishes_new_generation_and_keeps_old_one(self):
        record, events, _, _ = build_fixture(self.root / 'record')
        store_dir = self.root / 'store'
        first = write(record, store_dir, verify_image=False)
        second = write(record, store_dir, verify_image=False)
        self.assertNotEqual(first['generation'], second['generation'])
        manifest = json.loads((store_dir / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['generation'], second['generation'])
        generations = sorted(path.name for path in (store_dir / 'generations').iterdir())
        self.assertEqual(len(generations), 2)
        with TraceStore(store_dir) as store:
            self.assertEqual(store.generation, second['generation'])
            self.assertEqual(len(list(store.events())), len(events))

    def test_aborted_generation_is_not_published(self):
        record, events, _, _ = build_fixture(self.root / 'record')
        store_dir = self.root / 'store'
        first = write(record, store_dir, verify_image=False)
        original = trace_store._publish_manifest

        def broken(*args, **kwargs):
            raise trace_store.TraceStoreError('模拟发布失败')

        trace_store._publish_manifest = broken
        try:
            with self.assertRaises(trace_store.TraceStoreError):
                write(record, store_dir, verify_image=False)
        finally:
            trace_store._publish_manifest = original
        manifest = json.loads((store_dir / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['generation'], first['generation'])
        with TraceStore(store_dir) as store:
            self.assertEqual(len(list(store.events())), len(events))
        # 失败的新 generation 已被清除，不会留下“看起来完整”的目录
        self.assertEqual(sorted(path.name for path in (store_dir / 'generations').iterdir()),
                         [first['generation']])

    def test_write_rejects_inconsistent_summary(self):
        record, events, _, _ = build_fixture(self.root / 'record')
        decoded = json.loads((record / 'decoded.json').read_text(encoding='utf-8'))
        decoded['summary']['instructions'] += 1
        (record / 'decoded.json').write_text(json.dumps(decoded, ensure_ascii=False),
                                            encoding='utf-8')
        with self.assertRaises(TraceStoreError):
            write(record, self.root / 'store', verify_image=False)
        self.assertFalse((self.root / 'store' / 'manifest.json').exists())

    def test_write_rejects_missing_elements_tiling(self):
        record, events, _, _ = build_fixture(self.root / 'record')
        elements = [json.loads(line) for line in
                    (record / 'elements.jsonl').read_text(encoding='utf-8').splitlines()
                    if line.strip()]
        elements = [item for item in elements if item['type'] != 5][:2]
        (record / 'elements.jsonl').write_text(
            ''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in elements),
            encoding='utf-8')
        with self.assertRaises(TraceStoreError):
            write(record, self.root / 'store', verify_image=False)

    def test_store_requires_manifest(self):
        record, _, _, _ = build_fixture(self.root / 'record')
        store_dir = self.root / 'store'
        result = write(record, store_dir, verify_image=False)
        (store_dir / 'manifest.json').unlink()
        with self.assertRaises(TraceStoreError):
            TraceStore(store_dir)
        # 未发布的 generation 仍在盘上但不可读
        self.assertTrue((store_dir / 'generations' / result['generation']).is_dir())


if __name__ == '__main__':
    unittest.main()
