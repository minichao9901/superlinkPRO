"""P3 融合写出器测试：合成 native.bin（真实 ARM ELF 反汇编）+ 旧展开 oracle。

不依赖硬件与真实采集：受测输入用仓库内已跟踪的固件 ELF 现场合成
``trace-bin native v1`` 记录流（元素/包/状态），再用 ``decode_stream._expand_binary``
（旧展开参考实现）产出 oracle ``events.jsonl``，逐事件全字段对照融合写出结果。

覆盖：逐事件/分析/摘要等价、静态字典按唯一 PC 一份、展开段引用静态 ID、
executed 位图（内部谓词未知/末指令 not-taken）、周期折叠与 cycleSpan、字节核算、
坏 native.bin 拒绝、原子发布失败不留半成品、CRC/截断/未完成发布拒绝、开关矩阵。
"""
import io
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import decode_stream
import native_binary
import trace_store
import trace_store_compare
import trace_store_write
from decode_trace import (DEFAULT_CONFIG, Image, REG_ORDER, V3_REG_ORDER, decoder_config)
from flow_analysis import predicate_addresses

ETM_ROOT = Path(__file__).resolve().parents[2]
DECODER = ETM_ROOT / 'build-native/etm_decode.exe'
ELF_H743 = ETM_ROOT / 'firmware/stm32h743_cdc_trace/fw.elf'
ELF_F103 = ETM_ROOT / 'firmware/stm32f103_cdc_trace/fw-ze.elf'
RECORD_ETMV4 = ETM_ROOT / 'records/capture-4395612c313f47ce97af1e1c17e0a0d5'

V3_CONFIG = {'protocol': 'ETMv3', 'idr': 1091891777, 'ctrl': 2064, 'ccer': 100352,
             'traceid': 2}


# ---------------------------------------------------------------------------------------------
# 合成 trace-bin native v1 记录流
# ---------------------------------------------------------------------------------------------

def element_record(etype, offset, start=0, end=0, payload=0, last_executed=0,
                   cycle_count=0, flags=0, text='', timestamp=0, has_cc=False):
    encoded = text.encode('utf-8')
    flags8 = (0x01 if has_cc else 0) | (0x02 if timestamp else 0) | (0x08 if encoded else 0)
    body = native_binary.ELEMENT.pack(etype, 0, 0, int(last_executed), 0, flags8, flags,
                                      int(cycle_count), int(payload), 0, 0, int(offset),
                                      int(start), int(end), int(timestamp), len(encoded))
    return native_binary.RECORD_HEADER.pack(native_binary.RECORD_ELEMENT,
                                            5 + len(body) + len(encoded)) + body + encoded


def packet_record(offset, raw, type_code=0xF6):
    body = native_binary.PACKET.pack(type_code, 0, int(offset), len(raw))
    return native_binary.RECORD_HEADER.pack(native_binary.RECORD_PACKET,
                                            5 + len(body) + len(raw)) + body + raw


def status_record(processed, elements, packets, library='test-1.8.3', error_code=0,
                  last_error=''):
    library_bytes = library.encode('utf-8')
    error_bytes = last_error.encode('utf-8')
    body = native_binary.STATUS.pack(int(processed), int(error_code), len(library_bytes),
                                     len(error_bytes), int(elements), int(packets))
    return (native_binary.RECORD_HEADER.pack(native_binary.RECORD_STATUS,
                                             5 + len(body) + len(library_bytes) + len(error_bytes))
            + body + library_bytes + error_bytes)


def build_native(path, records, element_count, packet_count, protocol_code=1,
                 file_bytes=None):
    body = b''.join(records)
    header = native_binary.HEADER.pack(native_binary.MAGIC, native_binary.SCHEMA_VERSION,
                                       native_binary.HEADER_BYTES,
                                       native_binary.BYTE_ORDER_MARK, protocol_code,
                                       native_binary.FEATURE_RARE_ELEMENT_TEXT,
                                       element_count + packet_count, element_count,
                                       packet_count, len(body), 0)
    path = Path(path)
    path.write_bytes(header + body)
    if file_bytes is not None:
        path.write_bytes(path.read_bytes()[:file_bytes])
    return path


def instruction_ranges(elf, count=4, per_range=3):
    """从真实 ELF 的函数里取几段连续指令，构成原生范围元素。"""
    from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_MCLASS
    image = Image(elf)
    decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    ranges = []
    for start, size, _name in image.functions:
        if size < per_range * 4:
            continue
        instructions = list(decoder.disasm(image.read(start, size), start))
        if len(instructions) < per_range * count:
            continue
        for index in range(count):
            block = instructions[index * per_range:(index + 1) * per_range]
            ranges.append(block)
        break
    if not ranges:
        raise unittest.SkipTest('固件 ELF 里没有足够的函数指令用作合成范围')
    return image, ranges


def build_synthetic(elf, folder, protocol='ETMv4', with_cycles=True):
    """构造 native.bin（元素+包+状态）与一份 oracle 目录（旧展开 events.jsonl）。"""
    image, ranges = instruction_ranges(elf)
    elements = 0
    packet_count = 0
    offset = 100
    chunks = []
    # 顺序刻意安排：边界事件 → 范围0 → 范围1（带周期）→ 周期元素（折叠）→ 异常 → 返回
    # → 范围2 → 范围3 → EO_TRACE，覆盖 pending-tail 与周期折叠的交界。
    chunks.append(element_record(2, 90, payload=0, text='OCSD_GEN_TRC_ELEM_TRACE_ON'))
    elements += 1
    seq_offsets = []
    for index, block in enumerate(ranges):
        start = block[0].address
        end = block[-1].address + block[-1].size
        cycles = 7 if (with_cycles and index == 1) else 0
        chunks.append(element_record(5, offset, start, end, payload=len(block),
                                     last_executed=1 if index % 2 == 0 else 0,
                                     cycle_count=cycles, has_cc=bool(cycles)))
        elements += 1
        seq_offsets.append(offset)
        if cycles:
            chunks.append(element_record(12, offset, cycle_count=5))
            elements += 1
            exception_end = block[0].address
            chunks.append(element_record(
                9, offset + 1, end=exception_end, payload=15, flags=64,
                text='OCSD_GEN_TRC_ELEM_EXCEPTION(pref ret addr:0x%x; '
                     'excep num (0x0f) )' % exception_end))
            elements += 1
            chunks.append(element_record(10, offset + 2,
                                         text='OCSD_GEN_TRC_ELEM_EXCEPTION_RET()'))
            elements += 1
        offset += 10
    chunks.append(element_record(3, 500, payload=7, text='OCSD_GEN_TRC_ELEM_EO_TRACE'))
    elements += 1

    records = []
    packet_specs = [(0, b'\x04\x00\x00\x00', 0x01), (4, b'\x04\x00\x00\x01', 0xF6),
                    (8, b'\x02\x00\x00\x02', 0xF8), (12, b'\x02\x00\x00\x03', 0x105),
                    (16, b'\x04\x00\x00\x04', 0x200), (20, b'\x04\x00\x00\x05', 0x201)]
    for packet_offset, raw, type_code in packet_specs:
        records.append(packet_record(packet_offset, raw, type_code))
        packet_count += 1
    records.extend(chunks)
    processed = 24
    records.append(status_record(processed, elements, packet_count))
    native = build_native(folder / 'native.bin', records, elements, packet_count,
                          protocol_code=2 if protocol == 'ETMv3' else 1)

    image_dir = folder / 'oracle'
    image_dir.mkdir(exist_ok=True)
    config = V3_CONFIG if protocol == 'ETMv3' else decoder_config(None)
    oracle = decode_stream._expand_binary(image_dir, image, native, protocol, config,
                                          input_bytes=64)
    return image, native, image_dir, oracle, config


def compare_store_with_oracle(case, store_dir, oracle_dir, oracle_decoded):
    store = trace_store.open_store(store_dir)
    case.addCleanup(store.close)
    try:
        comparison = trace_store_compare.Comparison('fused-vs-oracle')
        comparison.record_dir = str(oracle_dir)
        tracker = trace_store_compare.compare_events(comparison, oracle_dir, oracle_decoded,
                                                     store, progress=None)
        trace_store_compare.compare_analysis(comparison, oracle_decoded, tracker, store)
        trace_store_compare.compare_overview(comparison, oracle_decoded, store, None)
        report = comparison.result()
        case.assertEqual(report['sections']['events']['differences'], 0,
                         json.dumps(report['sections']['events']['details'], ensure_ascii=False))
        case.assertEqual(report['sections']['analysis']['differences'], 0)
        # overview 唯一允许的表示差异：事件源改由 store.bin 提供（白名单 W-P3-1）。
        samples = report['sections']['overview']['samples']
        labels = {item['label'] for item in samples}
        case.assertLessEqual(labels, {'overview.streamed.eventsFile',
                                      'overview.streamed.nativeJsonl'})
        return store, report
    except Exception:
        store.close()
        raise


class SyntheticCase(unittest.TestCase):
    protocol = 'ETMv4'
    elf = ELF_H743

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.record = self.root / '合成 记录'
        self.record.mkdir()
        elf = self.elf
        if not elf.is_file():
            self.skipTest('缺少固件 ELF：' + str(elf))
        self.image, self.native, self.oracle_dir, self.oracle, self.config = build_synthetic(
            elf, self.record, protocol=self.protocol)
        self.store_dir = self.record / 'store'

    def tearDown(self):
        import gc
        gc.collect()
        self.tmp.cleanup()

    def write_store(self, **options):
        return trace_store_write.write(
            self.record, self.store_dir, native_bin=self.native, elf=str(self.image.path),
            config=self.config, input_bytes=64,
            checkpoint_instructions=options.pop('checkpoint_instructions', 4096),
            **options)


class EngineSelectionTests(SyntheticCase):
    """t17：引擎选路（默认 python / native 明确失败 / 显式回退 / manifest 元数据）。"""

    def test_default_engine_is_python_with_build_costs(self):
        result, _ = self.write_store()
        self.assertEqual(result['engine']['name'], 'python')
        self.assertIsNone(result['engine']['fallback'])
        self.assertIn('phases', result['engine']['buildCosts'])
        manifest = json.loads((self.store_dir / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual((manifest.get('engine') or {}).get('name'), 'python')
        processing = (manifest.get('bindings') or {}).get('processing') or {}
        self.assertEqual(processing.get('engine'), 'python')
        self.assertIn('buildCosts', processing)

    def test_native_engine_fails_explicitly_without_build_capability(self):
        with self.assertRaises(trace_store_write.NativeStorebuildUnavailable) as caught:
            self.write_store(engine='native')
        self.assertIn('build', str(caught.exception))
        self.assertFalse((self.store_dir / 'manifest.json').is_file(),
                         'native 失败不得发布半成品 manifest')

    def test_native_engine_fallback_records_reason(self):
        result, _ = self.write_store(engine='native', engine_fallback=True)
        self.assertEqual(result['engine']['name'], 'python')
        self.assertEqual(result['engine']['fallback'], 'native-unavailable')
        self.assertTrue(result['engine']['reason'])
        manifest = json.loads((self.store_dir / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual((manifest.get('engine') or {}).get('fallback'),
                         'native-unavailable')
        store = trace_store.TraceStore(self.store_dir, verify='sizes')
        try:
            window = store.window()
            self.assertIsInstance(window, dict)
            self.assertTrue(window)
            self.assertEqual(store.counts['instructions'], result['instructions'])
        finally:
            store.close()

    def test_engine_env_switch_and_fallback(self):
        with patch.dict(os.environ, {'ETM_STORE_ENGINE': 'native'}, clear=False):
            os.environ.pop('ETM_STORE_ENGINE_FALLBACK', None)
            with self.assertRaises(trace_store_write.NativeStorebuildUnavailable):
                self.write_store()
        with patch.dict(os.environ, {'ETM_STORE_ENGINE': 'native',
                                     'ETM_STORE_ENGINE_FALLBACK': '1'}, clear=False):
            result, _ = self.write_store()
        self.assertEqual(result['engine']['fallback'], 'native-unavailable')

    def test_unknown_engine_rejected(self):
        with self.assertRaises(trace_store.TraceStoreError) as caught:
            self.write_store(engine='gpu')
        self.assertIn('未知引擎', str(caught.exception))


class EquivalenceTests(SyntheticCase):
    def test_events_analysis_summary_match_oracle(self):
        result, decoded = self.write_store()
        self.assertTrue(result['ok'])
        store, report = compare_store_with_oracle(self, self.store_dir, self.oracle_dir,
                                                  self.oracle)
        counts = store.counts
        self.assertGreater(counts['instructions'], 0)
        self.assertEqual(counts['events'], self.oracle['streamed']['events'])
        self.assertEqual(counts['ranges'], self.oracle['streamed']['elements']
                         - (counts['rareEvents'] + 1))     # 非范围元素：事件 + 1 个周期元素
        self.assertEqual(store.meta['hasCycleFields'], True)
        self.assertEqual(store.meta['fieldPresence']['instruction'],
                         list(trace_store.INSTRUCTION_VARIANTS[0]))
        self.assertEqual(decoded['summary']['instructions'], counts['instructions'])
        self.assertEqual(decoded['summary']['executed'],
                         self.oracle['summary']['executed'])
        self.assertEqual(decoded['analysis'], self.oracle['analysis'])
        self.assertTrue(store.stats()['aggregateMatches'])

    def test_static_dictionary_is_per_pc_and_runs_reference_ids(self):
        self.write_store()
        store = trace_store.open_store(self.store_dir)
        try:
            unique = set()
            for event in store.events():
                if event['kind'] == 'instruction':
                    unique.add(event['pc'])
            self.assertEqual(len(store.static), len(unique))
            self.assertEqual(len({entry.pc for entry in store.static}), len(store.static))
            self.assertEqual(store.meta['counts']['staticPcs'], len(unique))
            # 展开段数 ≤ 原生范围数，且每条指令都能从静态字典还原
            self.assertLessEqual(store.counts['runs'], store.counts['ranges'])
            self.assertGreater(store.counts['runs'], 0)
            # 包块保存原始字节；说明文本在原生二进制下不可用（meta 显式声明）
            packets = store.page('packets', 0, 100)
            self.assertEqual(packets['total'], store.counts['packets'])
            for row in packets['rows']:
                self.assertEqual(len(bytes.fromhex(row['hex'])), row['size'])
                self.assertEqual(row['text'], '')
            self.assertFalse(store.meta['packetText']['available'])
        finally:
            store.close()

    def test_cycle_attribution_and_executed_bitmap(self):
        self.write_store()
        store = trace_store.open_store(self.store_dir)
        try:
            tails = {}
            for event in store.events():
                if event['kind'] != 'instruction':
                    continue
                if event['cycleCount']:
                    tails.setdefault(event['offset'], []).append(event)
            # 合成输入：第二个范围有 7 个周期，紧随一个 type 12 的 5 个周期 → 折叠为 12
            folded = [item for values in tails.values() for item in values]
            self.assertEqual(len(folded), 1, '应当只有一个周期末指令')
            tail = folded[0]
            self.assertEqual(tail['cycleCount'], 12)
            self.assertEqual(tail['cycleSpan'], 3)
            self.assertEqual(tail['cycles'], 12)
            # 该范围 lastExecuted=0（不是所有末指令都执行）
            self.assertIs(tail['executed'], False)
            # not-taken 末指令（lastExecuted=0）必须显式保留
            not_taken = [event for event in store.events()
                         if event['kind'] == 'instruction' and event['executed'] is False]
            self.assertGreaterEqual(len(not_taken), 1)
            self.assertTrue(all(item['executionState'] == 'not-taken' for item in not_taken))
        finally:
            store.close()

    def test_byte_accounting_and_issues(self):
        self.write_store()
        store = trace_store.open_store(self.store_dir)
        try:
            quality = store.overview()['quality']
            self.assertEqual(quality['processedBytes'], 24)
            self.assertEqual(quality['accountedBytes'], 24)
            self.assertEqual(quality['unsynchronizedBytes'], 4)      # I_NOT_SYNC 包
            self.assertEqual(quality['unsynchronizedRecords'], 1)
            self.assertEqual(quality['packetIssues'], 2)             # OVERFLOW + INCOMPLETE_EOT
            self.assertEqual(quality['overflowPackets'], 1)
            self.assertEqual(quality['validPackets'], 6 - 1 - 2)
            self.assertEqual(quality['unknownExecution'],
                             self.oracle['quality']['unknownExecution'])
            categories = [item['category'] for item in store.issues(0, 200)['rows']]
            self.assertIn('packet', categories)
            self.assertIn('gap', categories)                          # inputBytes 64 > 覆盖 24
        finally:
            store.close()


class PredicateUnknownTests(unittest.TestCase):
    """IT 保护指令作为**区间内部**指令时必须 executed=None（不能全部标 executed）。"""

    def test_internal_predicated_instruction_is_unknown(self):
        if not ELF_H743.is_file():
            self.skipTest('缺少固件 ELF')
        image = Image(ELF_H743)
        predicates = predicate_addresses(image)
        from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_MCLASS
        decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
        target = None
        for start, size, _name in image.functions:
            instructions = list(decoder.disasm(image.read(start, size), start))
            for index in range(len(instructions) - 2):
                if instructions[index].address in predicates:
                    target = instructions[index:index + 3]
                    break
            if target:
                break
        if not target:
            self.skipTest('固件 ELF 里找不到 IT 保护指令')
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            start = target[0].address
            end = target[-1].address + target[-1].size
            records = [element_record(5, 10, start, end, payload=len(target), last_executed=1),
                       status_record(10, 1, 0)]
            native = build_native(root / 'native.bin', records, 1, 0)
            oracle_dir = root / 'oracle'
            oracle_dir.mkdir()
            config = decoder_config(None)
            oracle = decode_stream._expand_binary(oracle_dir, image, native, 'ETMv4', config,
                                                  input_bytes=16)
            store_dir = root / 'store'
            trace_store_write.write(root, store_dir, native_bin=native, elf=str(image.path),
                                    config=config, input_bytes=16)
            store, report = compare_store_with_oracle(self, store_dir, oracle_dir, oracle)
            try:
                unknown = [event for event in store.events()
                           if event['kind'] == 'instruction' and event['executed'] is None]
                self.assertGreaterEqual(len(unknown), 1)
                self.assertEqual(len(unknown), store.counts['unknownExecution'])
                self.assertTrue(all(item['executionReason'] for item in unknown))
                self.assertEqual(store.counts['executed'],
                                 len(target) - len(unknown))
            finally:
                store.close()


class CheckpointFlushPredicatedTailTests(unittest.TestCase):
    """检查点 flush 把“谓词未知的内部指令”当段末时，internal_unknown 不能多算它。

    复现 t6 在 100× 合成 1 s 数据上发现的缺陷：`_feed_internals` 批量追加内部指令后，
    `hold_tail` 的 `checkpoint()` 若恰好到期，会调用 `flush_pending_run()`，把最后一条
    内部指令当作展开段 tail；此时写侧若仍按“所有内部指令”累加 internal_unknown，
    读侧按 ``identifiers[:-1]`` 校验就会报“展开段内部谓词未知计数不符”。

    合成输入：85 个普通 3 指令范围把 ``since_checkpoint`` 推到 255（同一 offset，展开段
    连续），再跟一个 ``IT + 受保护指令 + 尾指令`` 范围；检查点在第二条内部指令前到期，
    该指令（谓词未知）成为被 flush 的段末。修复前 ``store.events()`` 抛错，修复后全量
    展开通过。
    """

    @staticmethod
    def _windows(image):
        from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_MCLASS
        decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
        predicates = predicate_addresses(image)     # 返回“受谓词控制”的指令地址
        plain = None
        guarded = None
        for start, size, _name in image.functions:
            instructions = list(decoder.disasm(image.read(start, size), start))
            for index in range(1, len(instructions) - 2):
                window = instructions[index - 1:index + 2]
                if (guarded is None and instructions[index].address in predicates
                        and instructions[index - 1].mnemonic.startswith('it')):
                    guarded = window
                if (plain is None
                        and not any(item.address in predicates or item.mnemonic.startswith('it')
                                    for item in window)):
                    plain = window
                if plain and guarded:
                    return image, plain, guarded
        return image, plain, guarded

    def test_checkpoint_flush_with_predicated_last_internal(self):
        if not ELF_H743.is_file():
            self.skipTest('缺少固件 ELF')
        image, plain, guarded = self._windows(Image(ELF_H743))
        if plain is None or guarded is None:
            self.skipTest('固件 ELF 里找不到所需指令窗口')

        def range_record(window, offset):
            return element_record(5, offset, window[0].address,
                                  window[-1].address + window[-1].size,
                                  payload=len(window), last_executed=1)

        head = [range_record(plain, 10) for _ in range(84)]
        head.append(range_record(plain[:2], 10))
        final = range_record(guarded, 10)
        total = 84 * 3 + 2 + 3
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            records = head + [final, status_record(10, len(head) + 1, 0)]
            native = build_native(root / 'native.bin', records, len(head) + 1, 0)
            config = decoder_config(None)
            store_dir = root / 'store'
            result, _decoded = trace_store_write.write(
                root, store_dir, native_bin=native, elf=str(image.path), config=config,
                input_bytes=16, checkpoint_instructions=256)
            self.assertTrue(result['ok'])
            store = trace_store.open_store(store_dir)
            self.addCleanup(store.close)
            # 修复前：这里抛 TraceStoreError('展开段内部谓词未知计数不符')
            events = list(store.events())
            instructions = [event for event in events if event['kind'] == 'instruction']
            self.assertEqual(len(instructions), total)
            self.assertEqual(sum(1 for item in instructions if item['executed'] is None), 1)
            unknown = [item['seq'] for item in instructions if item['executed'] is None]
            self.assertEqual(unknown, [84 * 3 + 2 + 1])   # IT 保护的第 2 条内部指令
            self.assertIs(instructions[-1]['executed'], True)
            self.assertEqual(store.counts['unknownExecution'], 1)
            self.assertGreaterEqual(len(store.checkpoints), 2)
            self.assertTrue(store.stats()['aggregateMatches'])


class RobustnessTests(SyntheticCase):
    def test_missing_elf_is_rejected(self):
        with self.assertRaisesRegex(trace_store.TraceStoreError, '缺少固件 ELF'):
            trace_store_write.write(self.record, self.store_dir, native_bin=self.native,
                                    elf=str(self.root / 'missing.elf'), config=self.config)

    def test_corrupt_native_headers_are_rejected_and_nothing_published(self):
        payload = bytearray(self.native.read_bytes())
        cases = {
            'magic': (0, b'BADMAGIC'),                    # magic 不符
            'version': (8, struct.pack('<H', 2)),         # 版本不符
            'byorder': (12, struct.pack('<I', 0x04030201)),   # 大端标记
            'counts': (32, struct.pack('<Q', 99)),        # 元素计数不符
        }
        for name, (position, replacement) in cases.items():
            broken = bytearray(payload)
            broken[position:position + len(replacement)] = replacement
            target = self.root / ('坏-%s.bin' % name)
            target.write_bytes(bytes(broken))
            store_dir = self.root / ('store-%s' % name)
            with self.assertRaises(native_binary.NativeBinaryError):
                trace_store_write.write(self.record, store_dir, native_bin=target,
                                        elf=str(self.image.path), config=self.config)
            self.assertFalse((store_dir / trace_store.MANIFEST_NAME).exists(), name)
            generations = store_dir / 'generations'
            self.assertFalse(generations.exists() and any(generations.iterdir()), name)

    def test_truncated_native_is_rejected(self):
        truncated = self.root / 'truncated.bin'
        truncated.write_bytes(self.native.read_bytes()[:-7])
        with self.assertRaisesRegex(native_binary.NativeBinaryError, '截断|bodyBytes'):
            trace_store_write.write(self.record, self.root / 'store-trunc',
                                    native_bin=truncated, elf=str(self.image.path),
                                    config=self.config)

    def test_failed_publish_leaves_no_partial_generation(self):
        real_publish = trace_store._publish_manifest

        def boom(store_dir, manifest):
            raise trace_store.TraceStoreError('模拟发布失败')

        with patch.object(trace_store, '_publish_manifest', boom):
            with self.assertRaisesRegex(trace_store.TraceStoreError, '模拟发布失败'):
                self.write_store()
        self.assertFalse((self.store_dir / trace_store.MANIFEST_NAME).exists())
        generations = self.store_dir / 'generations'
        self.assertFalse(generations.exists() and any(generations.iterdir()))
        # 上一次成功的 store 仍然可读（先写一次成功，再模拟失败覆盖）
        self.write_store()
        store = trace_store.open_store(self.store_dir)
        store.close()
        with patch.object(trace_store, '_publish_manifest', boom):
            with self.assertRaises(trace_store.TraceStoreError):
                self.write_store()
        store = trace_store.open_store(self.store_dir)
        store.close()
        self.assertTrue(callable(real_publish))

    def test_crc_truncation_and_incomplete_publish_are_rejected(self):
        self.write_store()
        store = trace_store.open_store(self.store_dir)
        generation = store.generation
        sequence_index = store.container.of_type(trace_store.BLK_SEQUENCE)[0]
        block = store.container.blocks[sequence_index]
        store.close()
        store_bin = self.store_dir / 'generations' / generation / 'store.bin'
        original = store_bin.read_bytes()

        # 等长改写序列块中间一个字节 → 访问该块时报校验和错误
        broken = bytearray(original)
        broken[block.offset + max(1, block.length // 2)] ^= 0xFF
        store_bin.write_bytes(bytes(broken))
        store = trace_store.open_store(self.store_dir, verify='none')
        with self.assertRaisesRegex(trace_store.TraceStoreError, '校验和'):
            list(store.events())
        store.close()
        store_bin.write_bytes(original)

        # 截断 → 打开失败（文件长度与头部声明不符）
        store_bin.write_bytes(original[:-16])
        with self.assertRaises(trace_store.TraceStoreError):
            trace_store.open_store(self.store_dir)

        # 未完成发布（manifest 删除）→ 明确拒绝
        store_bin.write_bytes(original)
        manifest = self.store_dir / trace_store.MANIFEST_NAME
        saved = manifest.read_bytes()
        manifest.unlink()
        with self.assertRaisesRegex(trace_store.TraceStoreError, '未完成的 generation|缺少'):
            trace_store.open_store(self.store_dir)
        manifest.write_bytes(saved)
        # 未完成状态（state=building）→ 拒绝
        building = json.loads(saved)
        building['state'] = 'building'
        manifest.write_text(json.dumps(building), encoding='utf-8')
        with self.assertRaisesRegex(trace_store.TraceStoreError, '未完成发布'):
            trace_store.open_store(self.store_dir)

    def test_max_events_aborts_without_publish(self):
        with self.assertRaisesRegex(ValueError, 'max-events'):
            trace_store_write.write(self.record, self.store_dir, native_bin=self.native,
                                    elf=str(self.image.path), config=self.config,
                                    input_bytes=64, max_events=2)
        self.assertFalse((self.store_dir / trace_store.MANIFEST_NAME).exists())

    def test_manifest_config_parses_registers_and_skips_image_lines(self):
        lines = [hex(DEFAULT_CONFIG[key]) for key in REG_ORDER]
        lines += ['0x8000000 %s' % (self.record / 'image-0.bin'),
                  '0x20000000 C:\\path with space\\image-1.bin']
        path = self.root / 'native-config.txt'
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        config = trace_store_write.manifest_config(path)
        self.assertEqual(config['idr1'], DEFAULT_CONFIG['idr1'])
        self.assertEqual(config['traceid'], DEFAULT_CONFIG['traceid'])
        self.assertNotIn('protocol', config)

        v3_lines = ['ETMv3'] + [hex(V3_CONFIG[key]) for key in ('idr', 'ctrl', 'ccer', 'traceid')]
        v3_lines.append('0x8000000 image-0.bin')
        v3_path = self.root / 'v3-config.txt'
        v3_path.write_text('\n'.join(v3_lines) + '\n', encoding='utf-8')
        config3 = trace_store_write.manifest_config(v3_path)
        self.assertEqual(config3['protocol'], 'ETMv3')
        self.assertEqual(config3['idr'], V3_CONFIG['idr'])

        short = self.root / 'short-config.txt'
        short.write_text('\n'.join(lines[:2]) + '\n', encoding='utf-8')
        with self.assertRaisesRegex(trace_store.TraceStoreError, '寄存器数量不足'):
            trace_store_write.manifest_config(short)


class Etmv3Tests(SyntheticCase):
    protocol = 'ETMv3'
    elf = ELF_F103

    def test_etmv3_binary_record_writes_20_field_store(self):
        self.write_store()
        store, report = compare_store_with_oracle(self, self.store_dir, self.oracle_dir,
                                                  self.oracle)
        self.assertEqual(store.overview()['protocol'], 'ETMv3')
        self.assertEqual(store.meta['hasCycleFields'], True)


class NoHashInputsTests(SyntheticCase):
    """`--no-hash-inputs` 必须在读取/哈希**之前**跳过输入文件（P3 审计 F1）。"""

    def record_input_files(self):
        """记录目录里的输入文件集合（排除 store 输出目录）。"""
        files = set()
        for path in self.record.rglob('*'):
            if not path.is_file():
                continue
            if path == self.store_dir or self.store_dir in path.parents:
                continue
            files.add(str(path))
        return files

    def test_no_hash_inputs_skips_input_file_hashing(self):
        real_sha = trace_store._sha256_file
        hashed = []

        def spy(path, *args, **kwargs):
            hashed.append(str(path))
            return real_sha(path, *args, **kwargs)

        with patch.object(trace_store, '_binding_files',
                          side_effect=AssertionError(
                              '--no-hash-inputs 仍调用了 _binding_files（会读取并哈希输入文件）')):
            with patch.object(trace_store, '_sha256_file', spy):
                result, decoded = self.write_store(hash_inputs=False)
        self.assertTrue(result['ok'])
        self.assertIsInstance(decoded, dict)
        touched = [path for path in hashed if path in self.record_input_files()]
        self.assertEqual(touched, [], '--no-hash-inputs 仍哈希了输入文件：%r' % touched)
        manifest = json.loads((self.store_dir / trace_store.MANIFEST_NAME)
                              .read_text(encoding='utf-8'))
        bindings = manifest['bindings']
        self.assertEqual(bindings['inputHash']['skipped'], True)
        self.assertEqual(bindings['inputHash']['computed'], False)
        self.assertGreater(len(bindings['inputs']), 0)
        for name, item in bindings['inputs'].items():
            self.assertIsNone(item['sha256'], name)
            self.assertGreater(item['bytes'], 0, name)
        self.assertIsNone(bindings['record']['nativeBinSha256'])
        self.assertIsNone(bindings['record']['decodedSha256'])
        store = trace_store.open_store(self.store_dir)
        self.addCleanup(store.close)
        self.assertEqual(store.meta['verify']['inputHash']['skipped'], True)
        self.assertIsNone(store.meta['sourceSha256']['nativeBin'])
        self.assertIsNone(store.meta['sourceSha256']['decoded'])
        # store 仍可发布/打开/查询
        self.assertGreater(store.counts['instructions'], 0)
        self.assertEqual(len(store.page('events', 0, 5)['rows']), 5)
        self.assertEqual(store.event(0)['event']['seq'], 0)
        self.assertTrue(any('--no-hash-inputs' in item for item in result['warnings']))

    def test_default_still_hashes_inputs(self):
        calls = []
        real_bindings = trace_store._binding_files

        def spy(record_dir):
            calls.append(str(record_dir))
            return real_bindings(record_dir)

        with patch.object(trace_store, '_binding_files', spy):
            result, decoded = self.write_store()
        self.assertTrue(result['ok'])
        self.assertEqual(len(calls), 1)
        self.assertFalse(any('--no-hash-inputs' in item for item in result['warnings']))
        manifest = json.loads((self.store_dir / trace_store.MANIFEST_NAME)
                              .read_text(encoding='utf-8'))
        bindings = manifest['bindings']
        self.assertNotIn('inputHash', bindings)
        self.assertTrue(any(item['sha256'] for item in bindings['inputs'].values()),
                        '默认路径必须计算输入哈希')
        self.assertIsNotNone(bindings['record']['nativeBinSha256'])
        store = trace_store.open_store(self.store_dir)
        self.addCleanup(store.close)
        self.assertNotIn('inputHash', store.meta['verify'])
        self.assertIsNotNone(store.meta['sourceSha256']['nativeBin'])

    def test_no_hash_inputs_content_matches_default(self):
        """跳过哈希只影响绑定元数据，不改变事件数据块。

        META/SUMMARY 两块含每次运行都会变的 `generation`/`created` 时间戳（且 META 在
        --no-hash-inputs 下多一个 `verify.inputHash` 标记），因此只比较其余内容块。
        """
        def block_table(store_dir):
            store = trace_store.open_store(store_dir)
            try:
                return [(block.block_type, block.length, block.count, block.flags,
                         block.crc32) for block in store.container.blocks
                        if block.block_type not in (trace_store.BLK_META,
                                                    trace_store.BLK_SUMMARY)]
            finally:
                store.close()

        self.write_store(hash_inputs=False)
        no_hash = block_table(self.store_dir)
        self.write_store()
        default = block_table(self.store_dir)
        self.assertEqual(no_hash, default)
        self.assertTrue(no_hash)


class SwitchTests(unittest.TestCase):
    def test_store_write_switch_matrix(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('ETM_STORE_WRITE', None)
            os.environ.pop('ETM_NATIVE_FORMAT', None)
            self.assertFalse(decode_stream.store_write_enabled())
            self.assertEqual(decode_stream.native_format(), 'text')
            os.environ['ETM_STORE_WRITE'] = '0'
            self.assertFalse(decode_stream.store_write_enabled())
            self.assertEqual(decode_stream.native_format(), 'text')
            os.environ['ETM_STORE_WRITE'] = '1'
            self.assertTrue(decode_stream.store_write_enabled())
            self.assertEqual(decode_stream.native_format(), 'binary')
            os.environ['ETM_NATIVE_FORMAT'] = 'binary'
            self.assertEqual(decode_stream.native_format(), 'binary')
            os.environ['ETM_NATIVE_FORMAT'] = 'text'
            with self.assertRaisesRegex(ValueError, 'ETM_STORE_WRITE=1'):
                decode_stream.native_format()
            os.environ.pop('ETM_NATIVE_FORMAT', None)
            os.environ['ETM_STORE_WRITE'] = 'maybe'
            with self.assertRaisesRegex(ValueError, 'ETM_STORE_WRITE'):
                decode_stream.store_write_enabled()
            os.environ.pop('ETM_STORE_WRITE', None)


class IntegrationTests(unittest.TestCase):
    """端到端：`decode_stream` 开关（真实 10 ms 记录；缺解码器/记录则跳过）。"""

    @classmethod
    def setUpClass(cls):
        if not DECODER.is_file():
            raise unittest.SkipTest('缺少 build-native/etm_decode.exe，请先运行 build-decoder.ps1')
        if not (RECORD_ETMV4 / 'etm.bin').is_file():
            raise unittest.SkipTest('缺少 10 ms 记录样本')

    def config(self):
        return json.loads((RECORD_ETMV4 / 'decoded.json').read_text(encoding='utf-8'))['config']

    def run_pipeline(self, workdir, environment):
        with patch.dict(os.environ, environment, clear=False):
            for key in ('ETM_STORE_WRITE', 'ETM_NATIVE_FORMAT'):
                if key not in environment:
                    os.environ.pop(key, None)
            return decode_stream.decode_stream(
                (RECORD_ETMV4 / 'etm.bin').read_bytes(),
                str(RECORD_ETMV4 / 'firmware.elf'), workdir, self.config(), None)

    def test_fused_switch_writes_store_without_text_artifacts(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            # 注意：etm_decode 在 Windows 上打不开非 ASCII 路径（既有行为），这里用 ASCII 目录；
            # 含空格/非 ASCII 路径的 store 写入/读取由合成用例覆盖。
            workdir = Path(directory) / 'fused'
            result = self.run_pipeline(workdir, {'ETM_STORE_WRITE': '1'})
            for name in ('events.jsonl', 'packets.jsonl', 'elements.jsonl', 'native.jsonl',
                         'browse-columns.tsv', 'browse-v3.sqlite'):
                self.assertFalse((workdir / name).exists(), name)
            self.assertTrue((workdir / 'native.bin').is_file())
            self.assertTrue((workdir / 'store' / trace_store.MANIFEST_NAME).is_file())
            decoded = json.loads((workdir / 'decoded.json').read_text(encoding='utf-8'))
            self.assertEqual(decoded['traceStore']['format'], 'trace-store v1')
            self.assertEqual(decoded['traceStore']['source'], 'native.bin')
            self.assertEqual(decoded['streamed']['eventsFile'], 'store.bin')
            store = trace_store.open_store(workdir / 'store')
            try:
                self.assertEqual(store.counts['events'], result['streamed']['events'])
                self.assertEqual(store.counts['instructions'], result['summary']['instructions'])
                self.assertEqual(store.last(), result['streamed']['events'] - 1)
                first = next(store.events())
                self.assertEqual(first['seq'], 0)
            finally:
                store.close()

    def test_default_path_artifacts_unchanged(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            workdir = Path(directory) / 'default'
            result = self.run_pipeline(workdir, {})
            for name in ('events.jsonl', 'packets.jsonl', 'elements.jsonl',
                         'native.jsonl', 'browse-columns.tsv'):
                self.assertTrue((workdir / name).is_file(), name)
            self.assertFalse((workdir / 'store').exists())
            decoded = json.loads((workdir / 'decoded.json').read_text(encoding='utf-8'))
            self.assertNotIn('traceStore', decoded)
            self.assertEqual(decoded['streamed']['eventsFile'], 'events.jsonl')
            self.assertGreater(result['summary']['instructions'], 0)


if __name__ == '__main__':
    unittest.main()
