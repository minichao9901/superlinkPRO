#!/usr/bin/env python3
"""P3 融合写出器：`native.bin` → trace-store v1（一步落盘，跳过全部旧文本中转）。

与 P1 的 ``trace_store.write()``（从既有 ``events.jsonl`` 读取）不同，本模块**单遍消费**
P2 的原生二进制记录（``trace-bin native v1``，见 ``native_binary.py``），在内存里同时完成：

* 反汇编/静态代码字典：每个唯一 PC 只算一次、只存一份（机器码/反汇编/函数/文件/行/
  flow/谓词），并用 ELF 镜像字节与 DWARF 行表逐条交叉校验；
* 动态展开段：引用静态 ID，保存逻辑 ``seq`` 起点/长度、trace offset、末指令执行状态、
  区间周期与 ``cycleSpan``、内部谓词未知数——切段规则与 P1 参考写入器逐条相同；
* 调用状态检查点：与旧 ``flow_analysis.CallTracker`` 完全相同的状态机，按检查点间隔共享；
* 包块：offset/size/原始字节 + 故障分类（未同步/无效/溢出）；
* 字节核算（覆盖/重叠/未同步/尾部未覆盖）、稀疏/分层索引（AGG/NAV/检查点）与
  摘要/分析（``flow_analysis`` 口径），最后原子发布 manifest。

新路径**不读取也不生成** ``events.jsonl`` / ``packets.jsonl`` / ``elements.jsonl`` /
``browse-v3.sqlite``。语义 oracle 仍是旧文本展开器（``decode_stream._expand_records``）：

1. 展开规则复用 ``decode_stream._InstructionTable`` / ``_range_instructions``，谓词判定、
   单指令反汇编口径与旧路径完全同源；
2. 切段、调用栈、周期归因逻辑逐行对齐 ``trace_store.write()``（参考实现）；
3. 写入完成后既可用 ``TraceStore.events()`` 逐事件复现旧 ``events.jsonl``，
   也可直接读取摘要/窗口/导航。

开关与默认路径：本模块只提供显式入口；``decode_stream`` 里由 ``ETM_STORE_WRITE=1``
（默认关闭）启用融合写出，关闭时旧文本/JSONL 路径逐字节不变。

用法::

    python -X utf8 etm/tools/trace_store_write.py write <record_dir> <store_dir> \
        [--native-bin native.bin] [--etm etm.bin] [--elf firmware.elf] [--config native-config.txt] \
        [--work-dir DIR] [--decoder PATH] [--checkpoint-instructions N] [--write-decoded]
    python -X utf8 etm/tools/trace_store_write.py info <store_dir>

边界：不操作硬件、不改 `etm/native`、不提交 git。包说明文本（OpenCSD `ocsd_pkt_str()`）
在纯原生二进制输入下不可得，本写入器保存原始字节与故障分类并把
``meta.packetText.available`` 置 ``false``（P4 包视图的说明文本需重放或增量文本源）。
"""
from __future__ import annotations

import argparse
import collections
import gc
import hashlib
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import native_binary
import trace_store
import decode_stream as expander
import decode_trace as decoder_runtime
from decode_trace import Image, REG_ORDER, V3_REG_ORDER, decoder_config
from flow_analysis import CallTracker, predicate_addresses

__all__ = ['write', 'decode_to_native', 'manifest_config', 'STORE_DIR_NAME', 'NATIVE_FILE',
           'DECODED_FILE', 'TEXT_BASIS', 'peak_rss_bytes']

STORE_DIR_NAME = 'store'
NATIVE_FILE = 'native.bin'
DECODED_FILE = 'decoded.json'
ETM_FILE = 'etm.bin'
CONFIG_FILE = 'native-config.txt'
SOURCES_FILE = 'sources.json'
TEXT_BASIS = 'execution order; no per-instruction timestamp'
# 既有 decoded.json 里可原样带入新记录的**非派生元数据**白名单。
# 事件/元素/包本体与 summary/quality/analysis 一律从 native.bin 重算，绝不搬运。
METADATA_KEYS = ('physical', 'framing', 'capture', 'targetSnapshot', 'captureScope',
                 'captureEntry', 'captureStart', 'laFilter', 'spiValidation',
                 'samplingValidation', 'phaseValidation', 'recordError')

peak_rss_bytes = trace_store.peak_rss_bytes


# ---------------------------------------------------------------------------------------------
# 输入解析
# ---------------------------------------------------------------------------------------------

def manifest_config(path):
    """把 ``native-config.txt``（头部可选 ``ETMv3`` + 寄存器行 + 镜像映射行）解析成配置。

    文件由 `decode_stream._run_native` 生成：前 ``len(REG_ORDER)``/``len(V3_REG_ORDER)`` 行
    是十六进制寄存器值，之后是 ``<base> <image-N.bin 路径>`` 的镜像映射行（融合写出器
    从 ELF 重新生成镜像，不需要这些路径，但必须跳过而不是当成寄存器）。
    """
    path = Path(path)
    try:
        lines = [line.strip() for line in path.read_text(encoding='utf-8').splitlines()
                 if line.strip()]
    except OSError as error:
        raise trace_store.TraceStoreError('无法读取原生配置 %s：%s' % (path, error))
    protocol = 'ETMv3' if lines and lines[0] == 'ETMv3' else 'ETMv4'
    order = V3_REG_ORDER if protocol == 'ETMv3' else REG_ORDER
    entries = lines[1:] if protocol == 'ETMv3' else lines
    if len(entries) < len(order):
        raise trace_store.TraceStoreError(
            '原生配置寄存器数量不足：%s 中 %d 行，至少需要 %d（%s）'
            % (path.name, len(entries), len(order), protocol))
    raw = {}
    for key, value in zip(order, entries[:len(order)]):
        try:
            raw[key] = int(value, 16)
        except ValueError:
            raise trace_store.TraceStoreError('原生配置 %s 不是十六进制：%r' % (key, value))
    if protocol == 'ETMv3':
        raw['protocol'] = 'ETMv3'
    return decoder_config(raw)


def decode_to_native(etm_path, image, config, work_dir, decoder=None, timeout=900,
                     progress=None):
    """调用 ``etm_decode --binary`` 生成 ``native.bin``（等价于 ``decode_stream._run_native``）。

    返回 ``(native_path, info)``；``info`` 含命令、耗时与日志路径。失败抛
    ``TraceStoreError``，不留下半成品 ``native.bin``。
    """
    etm_path = Path(etm_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    if not etm_path.is_file():
        raise trace_store.TraceStoreError('缺少 ETM 原始流：%s' % etm_path)
    protocol = 'ETMv3' if str(config.get('protocol', 'ETMv4')) == 'ETMv3' else 'ETMv4'
    order = V3_REG_ORDER if protocol == 'ETMv3' else REG_ORDER
    manifest = ('ETMv3\n' if protocol == 'ETMv3' else '')
    manifest += '\n'.join(hex(int(config[key])) for key in order) + '\n'
    for index, (base, data) in enumerate(image.segments):
        target = work_dir / ('image-%d.bin' % index)
        if not target.is_file() or target.read_bytes() != data:
            target.write_bytes(data)
        manifest += '%s %s\n' % (hex(base), target.resolve())
    manifest_file = work_dir / CONFIG_FILE
    manifest_file.write_text(manifest, encoding='utf-8')

    native_path = work_dir / NATIVE_FILE
    if decoder is None:
        decoder = decoder_runtime.ROOT / 'build-native/etm_decode.exe'
    decoder = Path(decoder)
    if not decoder.is_file():
        raise trace_store.TraceStoreError(
            '缺少 OpenCSD 解码器：%s（请先运行 etm/build-decoder.ps1）' % decoder)
    command = [str(decoder), '--binary', str(native_path), str(etm_path), str(manifest_file)]
    if progress:
        progress('  融合写出：调用原生解码器生成 %s' % native_path.name)
    started = time.perf_counter()
    try:
        process = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 timeout=timeout)
    except subprocess.TimeoutExpired:
        raise trace_store.TraceStoreError('OpenCSD 解码超时（%d s）：%s' % (timeout, decoder))
    seconds = time.perf_counter() - started
    log_path = work_dir / 'native.log'
    log_path.write_bytes(process.stderr or b'')
    if process.returncode:
        native_path.unlink(missing_ok=True)
        raise trace_store.TraceStoreError(
            'OpenCSD 解码失败（退出码 %d）：%s'
            % (process.returncode,
               (process.stderr or b'').decode('utf-8', 'replace')[-3000:]))
    if not native_path.is_file() or native_path.stat().st_size <= native_binary.HEADER_BYTES:
        raise trace_store.TraceStoreError('原生解码没有产出有效的 native.bin：%s' % native_path)
    info = dict(seconds=round(seconds, 3), command=command, manifest=str(manifest_file),
                log=str(log_path), nativeBytes=native_path.stat().st_size)
    return native_path, info


def _known_addresses(image):
    """与 ``decode_stream._expand_records`` 相同口径的“已知函数内指令地址”集合。"""
    from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_MCLASS
    known = set()
    probe = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    for start, size, _ in image.functions:
        known.update(item.address for item in probe.disasm(image.read(start, size), start))
    return known


def _load_json(path):
    return trace_store._load_json_strict(path)


# ---------------------------------------------------------------------------------------------
# 展开段组装（与 trace_store.write 的单遍状态机同序）
# ---------------------------------------------------------------------------------------------

class _Assembler:
    """把原生范围/稀有事件流组装成 SEQUENCE 记录，并同时维护调用栈与统计。

    与 ``trace_store.write()`` 的差别只在“事件从哪来”：本类直接消费原生记录，
    因此不需要先写 ``events.jsonl``；切段、检查点、周期归因、导航与问题列表的规则
    与该参考写入器逐行一致（见 ``etm/docs/trace-store-v1.md`` §5.2/§6）。
    """

    def __init__(self, sink, checkpoint_instructions, table, image, predicates,
                 verify_image=True, max_events=None, progress=None, progress_every=500000):
        self.sink = sink
        self.table = table
        self.image = image
        self.predicates = predicates
        self.verify_image = verify_image
        self.max_events = max_events
        self.progress = progress
        self.progress_every = progress_every
        self.strings = trace_store._Strings()
        self.sequence = trace_store._SequenceBuffer(sink, checkpoint_instructions)
        self.tracker = CallTracker()
        self.static_by_pc = {}
        self.static_entries = []
        self.range_cache = {}
        self.span_cache = {}
        self.nav = {mode: [] for mode in trace_store.NAV_NAMES}
        self.issues = []
        self.issue_run = None
        self.expected_seq = 0
        self.instruction_count = 0
        self.pending = None
        self.run_ids = []
        self.run_start = None
        self.run_offset = None
        self.run_predicated = 0
        self.pending_stats = [0, 0, 0, 0, 0]
        self.totals = [0, 0, 0, 0, 0]
        self.state = dict(events=0, instructions=0, rareEvents=0, runs=0, executed=0,
                          unknown=0, notTaken=0, firstInstructionOffset=None,
                          minOffset=None, maxOffset=None, firstInstructionSeq=None)
        self.type_counts = {}
        self.exceptions = 0
        self.gaps = 0
        self.mapped = 0
        self.first_instruction_pc = None
        self.predicate_fallback = 0
        self.range_fallbacks = 0
        self.range_count = 0
        # 起点检查点：seq=0 处的调用状态（空栈）必须显式保存（与参考写入器一致）。
        self.sequence.emit_checkpoint(0, self.tracker, self.strings)

    # -- 检查点/切段 --------------------------------------------------------------------------
    def checkpoint(self, seq):
        sequence = self.sequence
        if sequence.since_checkpoint >= sequence.checkpoint_instructions \
                or sequence.size() >= trace_store.TARGET_BLOCK_BYTES:
            if self.run_ids:
                self.flush_pending_run()
            if sequence.size() >= trace_store.TARGET_BLOCK_BYTES:
                self._sync_stats()
                sequence.rotate()
            sequence.emit_checkpoint(seq, self.tracker, self.strings)

    def _sync_stats(self):
        """把批量累加的展开段统计并入序列块统计（参考 emit_run 的逐项口径）。"""
        pending = self.pending_stats
        if not any(pending):
            return
        stats = self.sequence.stats
        stats['instructions'] += pending[0]
        stats['executed'] += pending[1]
        stats['unknown'] += pending[2]
        stats['notTaken'] += pending[3]
        stats['cycles'] += pending[4]
        self.pending_stats = [0, 0, 0, 0, 0]

    def _emit_run(self, start_seq, count, identifiers, tail_executed, offset, cycles, span,
                  internal_unknown):
        """写出展开段：与 ``_SequenceBuffer.emit_run`` 逐字节相同的快速编码。

        常见的取值（段长/尾状态/周期/静态 ID 增量）走单字节快路径，避免每条记录
        8~10 次 Python 函数调用；记录长度 >=128 或块接近上限时回退参考实现。
        """
        sequence = self.sequence
        buffer = sequence.buffer
        start = len(buffer)
        if len(buffer) > trace_store.MAX_BLOCK_BYTES - 8192:
            sequence.emit_run(start_seq, count, identifiers, tail_executed, offset, cycles, span,
                              internal_unknown)
            return
        buffer.append(trace_store.REC_RUN)
        buffer.append(0)                                  # 变长长度占位（绝大多数 <128）
        value = start_seq
        while value >= 0x80:
            buffer.append((value & 0x7F) | 0x80)
            value >>= 7
        buffer.append(value)
        value = count
        while value >= 0x80:
            buffer.append((value & 0x7F) | 0x80)
            value >>= 7
        buffer.append(value)
        buffer.append(trace_store.TAIL_CODES[tail_executed])
        value = offset
        while value >= 0x80:
            buffer.append((value & 0x7F) | 0x80)
            value >>= 7
        buffer.append(value)
        value = cycles
        while value >= 0x80:
            buffer.append((value & 0x7F) | 0x80)
            value >>= 7
        buffer.append(value)
        value = span
        while value >= 0x80:
            buffer.append((value & 0x7F) | 0x80)
            value >>= 7
        buffer.append(value)
        value = internal_unknown
        while value >= 0x80:
            buffer.append((value & 0x7F) | 0x80)
            value >>= 7
        buffer.append(value)
        previous = identifiers[0]
        value = previous
        while value >= 0x80:
            buffer.append((value & 0x7F) | 0x80)
            value >>= 7
        buffer.append(value)
        for identifier in identifiers[1:]:
            delta = identifier - previous
            previous = identifier
            if 0 <= delta < 64:
                buffer.append(delta << 1)
            elif delta >= 64:
                value = delta << 1
                while value >= 0x80:
                    buffer.append((value & 0x7F) | 0x80)
                    value >>= 7
                buffer.append(value)
            else:
                trace_store._wzv(buffer, delta)
        length = len(buffer) - start - 2
        if length < 0x80:
            buffer[start + 1] = length
        else:
            del buffer[start:]
            sequence.emit_run(start_seq, count, identifiers, tail_executed, offset, cycles, span,
                              internal_unknown)
            return
        sequence.record_count += 1
        sequence.note_seq(start_seq, start_seq + count - 1)
        pending = self.pending_stats
        internal = count - 1
        pending[0] += count
        pending[1] += (internal - internal_unknown) + (tail_executed is True)
        pending[2] += internal_unknown + (tail_executed is None)
        pending[3] += tail_executed is False
        pending[4] += cycles

    def flush_run(self, tail_executed, cycles, span):
        identifiers = self.run_ids
        if not identifiers:
            return
        internal_unknown = self.run_predicated
        self._emit_run(self.run_start, len(identifiers), identifiers, tail_executed,
                       self.run_offset, cycles, span, internal_unknown)
        totals = self.totals
        totals[0] += 1
        totals[1] += len(identifiers)
        totals[2] += (len(identifiers) - 1 - internal_unknown) + (tail_executed is True)
        totals[3] += internal_unknown + (tail_executed is None)
        totals[4] += (tail_executed is False)
        self.run_ids = []
        self.run_start = None
        self.run_offset = None
        self.run_predicated = 0

    def flush_pending_run(self):
        """把挂起的展开段按末指令真实状态落盘：末指令可能是谓词未知的内部指令。"""
        if not self.run_ids:
            return
        last = self.static_entries[self.run_ids[-1]]
        self.flush_run(None if last.predicated else True, 0, 0)

    # -- 问题列表（谓词未知段/边界事件；与参考写入器同口径）--------------------------------
    def _close_issue_run(self):
        run = self.issue_run
        if run is not None:
            self.issues.append(run)
            self.issue_run = None

    def _note_unknown(self, seq, offset):
        run = self.issue_run
        if run is not None and seq == run['endSeq'] + 1:
            run['endSeq'] = seq
            run['records'] += 1
            return
        self._close_issue_run()
        self.issue_run = dict(category='predicate', startSeq=seq, endSeq=seq, records=1,
                              offset=offset, reason=trace_store.IT_REASON)

    def _note_executed(self):
        if self.issue_run is not None:
            self._close_issue_run()

    # -- 指令/事件 ----------------------------------------------------------------------------
    def _apply_instruction(self, seq, offset, entry, executed, cycles, span, internal=False):
        if seq != self.expected_seq:
            raise trace_store.TraceStoreError('seq 不连续：期望 %d，实际 %d（本实现要求逻辑 '
                                              'seq 从 0 连续）' % (self.expected_seq, seq))
        self.expected_seq += 1
        self.state['events'] += 1
        self.instruction_count += 1
        if self.max_events and self.instruction_count > self.max_events:
            raise ValueError('已超过 --max-events=%d；流式解码本身没有上限。' % self.max_events)
        internal_ok = (executed == (None if entry.predicated else True)) \
            and cycles == 0 and span == 0
        if self.run_ids and (offset != self.run_offset
                             or len(self.run_ids) >= trace_store.MAX_RUN_INSTRUCTIONS):
            self.flush_pending_run()
        if not self.run_ids:
            self.run_start = seq
            self.run_offset = offset
        self.run_ids.append(entry.identifier)
        if internal and entry.predicated:
            self.run_predicated += 1
        if not internal_ok:
            self.flush_run(executed, cycles, span)
        self.tracker.feed_instruction(entry.function, entry.file, entry.line, executed, entry.flow,
                                      entry.pc, entry.size, cycles)
        if entry.flow == 'call' and executed is True:
            self.nav[trace_store.NAV_CALL].append(seq)
        if entry.flow == 'return' and executed is True:
            self.nav[trace_store.NAV_RETURN].append(seq)
        if executed is None:
            self.nav[trace_store.NAV_ISSUE].append(seq)
            self._note_unknown(seq, offset)
        else:
            self._note_executed()
        self.sequence.since_checkpoint += 1
        if self.state['firstInstructionSeq'] is None:
            self.state['firstInstructionSeq'] = seq
            self.state['firstInstructionOffset'] = offset
            self.first_instruction_pc = entry.pc
        if self.state['minOffset'] is None or offset < self.state['minOffset']:
            self.state['minOffset'] = offset
        if self.state['maxOffset'] is None or offset > self.state['maxOffset']:
            self.state['maxOffset'] = offset
        if entry.line:
            self.mapped += 1
        if self.progress is not None and self.progress_every \
                and self.instruction_count % self.progress_every == 0:
            self.progress('  融合写出：已展开 %d 条指令（seq=%d）'
                          % (self.instruction_count, self.expected_seq))

    def hold_tail(self, entry, offset, last_executed, cycles, payload):
        """区间末指令先挂起：紧随其后的周期元素（type 12）要回填到它身上。"""
        seq = self.expected_seq
        self.checkpoint(seq)
        executed = None if entry.predicated else bool(last_executed)
        cycles = int(cycles)
        self.pending = [seq, offset, entry, executed, cycles,
                        int(payload) if cycles else 0, int(payload)]

    def fold_cycles(self, cycles):
        """`OCSD_GEN_TRC_ELEM_CYCLE_COUNT` 折叠到前一个范围末指令（只折叠一次）。"""
        pending = self.pending
        if pending is None:
            return
        if cycles:
            pending[4] += int(cycles)
            if not pending[5]:
                pending[5] = pending[6]
        self.flush_pending_tail()

    def flush_pending_tail(self):
        pending = self.pending
        if pending is None:
            return
        self.pending = None
        seq, offset, entry, executed, cycles, span, _payload = pending
        if executed is True and cycles == 0:
            self._apply_tail_fast(seq, offset, entry)
        else:
            self._apply_instruction(seq, offset, entry, executed, cycles, span)

    def _apply_tail_fast(self, seq, offset, entry):
        """常见末指令（executed=True、无周期、非谓词）的快路径，语义同 _apply_instruction。"""
        if seq != self.expected_seq:
            raise trace_store.TraceStoreError('seq 不连续：期望 %d，实际 %d（本实现要求逻辑 '
                                              'seq 从 0 连续）' % (self.expected_seq, seq))
        self.expected_seq += 1
        self.state['events'] += 1
        self.instruction_count += 1
        if self.max_events and self.instruction_count > self.max_events:
            raise ValueError('已超过 --max-events=%d；流式解码本身没有上限。' % self.max_events)
        run_ids = self.run_ids
        if run_ids and (offset != self.run_offset
                        or len(run_ids) >= trace_store.MAX_RUN_INSTRUCTIONS):
            self.flush_pending_run()
            run_ids = self.run_ids
        if not run_ids:
            self.run_start = seq
            self.run_offset = offset
        run_ids.append(entry.identifier)
        tracker = self.tracker
        tracker.feed_instruction(entry.function, entry.file, entry.line, True, entry.flow,
                                 entry.pc, entry.size, 0)
        flow = entry.flow
        if flow == 'call':
            self.nav[trace_store.NAV_CALL].append(seq)
        elif flow == 'return':
            self.nav[trace_store.NAV_RETURN].append(seq)
        if self.issue_run is not None:
            self._close_issue_run()
        self.sequence.since_checkpoint += 1
        state = self.state
        if state['firstInstructionSeq'] is None:
            state['firstInstructionSeq'] = seq
            state['firstInstructionOffset'] = offset
            self.first_instruction_pc = entry.pc
        if state['minOffset'] is None or offset < state['minOffset']:
            state['minOffset'] = offset
        if state['maxOffset'] is None or offset > state['maxOffset']:
            state['maxOffset'] = offset
        if entry.line:
            self.mapped += 1
        if self.progress is not None and self.progress_every \
                and self.instruction_count % self.progress_every == 0:
            self.progress('  融合写出：已展开 %d 条指令（seq=%d）'
                          % (self.instruction_count, self.expected_seq))

    def add_event(self, etype, offset, payload, start, end, flags, text):
        sequence = self.sequence
        # 参考写入器在每条事件**之前**判定检查点：先看 since/块大小，再 flush 上一段，
        # 最后按 flush 后的块大小决定是否 rotate。这里必须用 flush 前的条件判定，
        # 否则“flush 上一段刚好把块顶过 1 MiB”时旋转点会提前一条记录。
        due = (sequence.since_checkpoint >= sequence.checkpoint_instructions
               or sequence.size() >= trace_store.TARGET_BLOCK_BYTES)
        self.flush_pending_tail()
        if self.run_ids:                        # 稀有事件前必须结束当前展开段（seq 递增）
            self.flush_pending_run()
        seq = self.expected_seq
        if due:
            if sequence.size() >= trace_store.TARGET_BLOCK_BYTES:
                sequence.rotate()
            sequence.emit_checkpoint(seq, self.tracker, self.strings)
        self.expected_seq += 1
        self.state['events'] += 1
        self.type_counts[etype] = self.type_counts.get(etype, 0) + 1
        if etype == 9:
            self.exceptions += 1
            self.sequence.stats['exceptions'] += 1
        if etype in trace_store.GAP_TYPES or (etype == 2 and payload):
            self.gaps += 1
        if etype in trace_store.BOUNDARY_TYPES:
            self.sequence.stats['boundaries'] += 1
            self.nav[trace_store.NAV_ISSUE].append(seq)
        if etype in (9, 10):
            self.nav[trace_store.NAV_EXCEPTION].append(seq)
        self.nav[trace_store.NAV_EVENT].append(seq)
        self.sequence.emit_event(seq, etype, offset, payload, start, end, flags,
                                 self.strings.id_of(text))
        self._note_executed()
        if etype in trace_store.BOUNDARY_TYPES:
            self.issues.append(dict(category='boundary', startSeq=seq, endSeq=seq,
                                    offset=offset, reason=text, records=1))
        self.state['rareEvents'] += 1
        self.tracker.feed_event(etype, flags, payload, end)

    def add_range(self, offset, start, end, payload, cycles, last_executed):
        self.flush_pending_tail()
        plan = self.range_plan(start, end, payload)
        if plan['internal']:
            self._feed_internals(offset, plan)
        self.hold_tail(plan['entries'][-1], offset, last_executed, cycles, payload)
        self.range_count += 1

    def _feed_internals(self, offset, plan):
        """区间内部指令：顺序与参考写入器逐事件一致。

        参考写入器的每条事件顺序是「先做检查点判定，再因 offset 变化 flush 上一段」；
        这里对第一条内部指令复刻该顺序（检查点判定 → flush 上一段 → 批量追加），
        之后到下一个检查点之前的内部指令一次性追加，字节结果与逐条处理相同。
        """
        sequence = self.sequence
        count = plan['internal']
        # 参考写入器在处理第一条内部指令**之前**做检查点判定（此时上一段仍挂起）
        self.checkpoint(self.expected_seq)
        run_ids = self.run_ids
        if run_ids and offset != self.run_offset:
            # 检查点判定之后，第一条指令的 offset 变化触发上一段落盘
            self.flush_pending_run()
            run_ids = self.run_ids
        if (count > sequence.checkpoint_instructions - sequence.since_checkpoint
                or sequence.size() >= trace_store.TARGET_BLOCK_BYTES
                or (run_ids and len(run_ids) + count
                    > trace_store.MAX_RUN_INSTRUCTIONS)):
            entries = plan['entries']
            for position in range(count):
                if position:
                    self.checkpoint(self.expected_seq)
                entry = entries[position]
                self._apply_instruction(self.expected_seq, offset, entry,
                                        None if entry.predicated else True, 0, 0, internal=True)
            return
        self._feed_internal_fast(offset, plan)

    def _feed_internal_fast(self, offset, plan):
        """范围内部指令的批量路径：一次性追加展开段 ID、计数与导航/问题列表。

        调用方已保证「范围内不会落检查点、不会旋转序列块、展开段不会超过 4096 条」，
        并已按参考写入器顺序完成检查点判定与 offset 变化 flush。tracker 的分组与旧
        展开器 ``feed_linear_many`` 完全相同，语义不变。
        """
        sequence = self.sequence
        count = plan['internal']
        run_ids = self.run_ids
        seq = self.expected_seq
        if not run_ids:
            self.run_start = seq
            self.run_offset = offset
        run_ids.extend(plan['internal_ids'])
        self.run_predicated += plan['internal_predicated']
        self.expected_seq = seq + count
        self.state['events'] += count
        self.instruction_count += count
        if self.max_events and self.instruction_count > self.max_events:
            raise ValueError('已超过 --max-events=%d；流式解码本身没有上限。' % self.max_events)
        self.mapped += plan['mapped']
        sequence.since_checkpoint += count
        if self.state['firstInstructionSeq'] is None:
            self.state['firstInstructionSeq'] = seq
            self.state['firstInstructionOffset'] = offset
            self.first_instruction_pc = plan['entries'][0].pc
        low = self.state['minOffset']
        if low is None or offset < low:
            self.state['minOffset'] = offset
        high = self.state['maxOffset']
        if high is None or offset > high:
            self.state['maxOffset'] = offset
        nav = self.nav
        if plan['call_positions']:
            target = nav[trace_store.NAV_CALL]
            target.extend(seq + position for position in plan['call_positions'])
        if plan['return_positions']:
            target = nav[trace_store.NAV_RETURN]
            target.extend(seq + position for position in plan['return_positions'])
        issue_positions = plan['issue_positions']
        if issue_positions or self.issue_run is not None:
            if self.issue_run is not None and 0 not in issue_positions:
                self._note_executed()
            for position in issue_positions:
                entry_seq = seq + position
                self._note_unknown(entry_seq, offset)
                nav[trace_store.NAV_ISSUE].append(entry_seq)
        entries = plan['entries']
        tracker = self.tracker
        for group in plan['groups']:
            if group[1] is None:
                entry = entries[group[0]]
                tracker.feed_instruction(entry.function, entry.file, entry.line, group[2],
                                         entry.flow, entry.pc, entry.size, 0)
            else:
                first = entries[group[0]]
                last = entries[group[1]]
                tracker.feed_linear_many(first.function, first.file, first.line, group[2],
                                         first.pc, first.size, last.pc, last.size, group[3])
        if self.progress is not None and self.progress_every:
            before = self.instruction_count - count
            if self.instruction_count // self.progress_every \
                    != before // self.progress_every:
                self.progress('  融合写出：已展开 %d 条指令（seq=%d）'
                              % (self.instruction_count, self.expected_seq))

    # -- 反汇编/静态字典 ----------------------------------------------------------------------
    def _static_entry(self, pc, instruction):
        """每个唯一 PC 只建一份；同一 PC 的静态字段取**首次出现**的反汇编上下文。

        与旧展开器完全一致：``decode_stream._InstructionTable.info()`` 按 PC 缓存，
        因此 IT 块内指令的 ``strlt/movhs`` 等助记符只在首次出现时确定（同一 PC 的
        后续出现即使落在不同范围起始上下文，也复用首次结果）。
        """
        entry = self.static_by_pc.get(pc)
        if entry is not None:
            return entry
        info = self.table.info(instruction)
        size, hexed, mnemonic, operands, flow, conditional, function, source, line, forced = info[:10]
        raw = bytes.fromhex(hexed)
        if len(raw) != size or len(raw) != instruction.size:
            raise trace_store.TraceStoreError(
                '指令长度与机器码不符：pc=0x%08x size=%d len=%d' % (pc, size, len(raw)))
        if self.verify_image:
            expected = self.image.read(pc, len(raw)).hex()
            if expected != hexed:
                raise trace_store.TraceStoreError(
                    '机器码与 ELF 不一致：pc=0x%08x 反汇编=%s 镜像=%s' % (pc, hexed, expected))
            location = self.image.location(pc)
            if location != (function, source, line):
                raise trace_store.TraceStoreError(
                    '源码位置与 ELF 不一致：pc=0x%08x 反汇编=%r 镜像=%r'
                    % (pc, (function, source, line), location))
        if flow not in trace_store.FLOW_CODES:
            raise trace_store.TraceStoreError('未知 flow：%r（pc=0x%08x）' % (flow, pc))
        predicated = bool(forced)
        if predicated and pc not in self.predicates:
            # 旧展开器的“镜像外 PC”回退扫描判定了 IT 谓词；参考写入器用
            # predicate_addresses 严格判定会拒绝这种记录，这里如实记录并保留语义。
            self.predicate_fallback += 1
        entry = trace_store._StaticEntry(pc, size, raw, hexed, mnemonic, operands, function,
                                         source, line, flow, conditional, predicated,
                                         len(self.static_entries))
        self.static_by_pc[pc] = entry
        self.static_entries.append(entry)
        return entry

    def range_plan(self, start, end, payload):
        """按旧展开器的**逐范围线性反汇编**取指令（保留 IT 块上下文）并预计算分组。

        计划里包含：静态条目元组、静态 ID、内部指令数、源码映射数、谓词未知位置、
        调用/返回位置，以及与旧展开器 ``groups`` 完全相同的 tracker 分组。
        """
        key = (start, end)
        plan = self.range_cache.get(key)
        if plan is not None:
            return plan
        selected = expander._range_instructions(start, end, self.table.decoder, self.image,
                                                self.span_cache)
        if len(selected) != payload or not selected \
                or selected[-1].address + selected[-1].size != end:
            raise trace_store.TraceStoreError(
                '机器码范围核对失败：0x%08x..0x%08x；OpenCSD=%d，反汇编=%d。'
                % (start, end, payload, len(selected)))
        entries = tuple(self._static_entry(instruction.address, instruction)
                        for instruction in selected)
        last = len(entries) - 1
        ids = tuple(entry.identifier for entry in entries)
        mapped = 0
        issue_positions = []
        call_positions = []
        return_positions = []
        for position in range(last):
            entry = entries[position]
            if entry.line:
                mapped += 1
            if entry.predicated:
                issue_positions.append(position)
            elif entry.flow == 'call':
                call_positions.append(position)
            elif entry.flow == 'return':
                return_positions.append(position)
        groups = []
        begin = 0
        while begin < last:
            entry = entries[begin]
            stop = begin + 1
            if entry.flow == 'linear':
                key_fields = (entry.function, entry.file, entry.line, entry.predicated)
                while stop < last:
                    candidate = entries[stop]
                    if candidate.flow != 'linear' or (
                            candidate.function, candidate.file, candidate.line,
                            candidate.predicated) != key_fields:
                        break
                    stop += 1
            executed = None if entry.predicated else True
            if stop - begin == 1:
                groups.append((begin, None, executed))
            else:
                groups.append((begin, stop - 1, executed, stop - begin))
            begin = stop
        plan = dict(entries=entries, ids=ids, internal=last,
                    internal_ids=ids[:last], mapped=mapped,
                    issue_positions=issue_positions, call_positions=call_positions,
                    return_positions=return_positions, groups=groups,
                    internal_predicated=len(issue_positions))
        if len(self.range_cache) >= expander.CACHE_MAX:
            self.range_cache.clear()
        self.range_cache[key] = plan
        return plan

    def finish(self):
        self.flush_pending_tail()
        if self.run_ids:
            self.flush_pending_run()
        if self.issue_run:
            self.issues.append(self.issue_run)
            self.issue_run = None
        self._sync_stats()
        totals = self.totals
        self.state['runs'] = totals[0]
        self.state['instructions'] = totals[1]
        self.state['executed'] = totals[2]
        self.state['unknown'] = totals[3]
        self.state['notTaken'] = totals[4]
        if self.expected_seq != self.state['events']:
            raise trace_store.TraceStoreError('内部错误：事件计数与 seq 不一致')
        self.sequence.finish()


# ---------------------------------------------------------------------------------------------
# 问题列表（包字节账/解码器/物理边界；口径同 trace_store._packet_issues）
# ---------------------------------------------------------------------------------------------

def _packet_issues(quality, status, input_size, packet_end, faults, streamed_schema=2):
    issues = list(faults)
    if streamed_schema != 3 and input_size and input_size > packet_end:
        issues.append(dict(category='gap', startSeq=None, endSeq=None, records=0,
                           offset=packet_end, bytes=input_size - packet_end,
                           reason='ETM 输入尾部未被包记录覆盖'))
    if status.get('lastErrorCode'):
        issues.append(dict(category='decoder', startSeq=None, endSeq=None, records=0,
                           offset=status.get('processed'),
                           reason='解码器错误：' + str(status.get('lastError') or '未知错误')))
    for key, label in (('initialPhysicalBytes', 'TPIU 同步前物理字节'),
                       ('trailingFrameBytes', '不完整的 TPIU 尾帧'),
                       ('unknownSourceBytes', '未知源 ID 字节')):
        if quality.get(key):
            issues.append(dict(category='physical', startSeq=None, endSeq=None, records=0,
                               bytes=quality[key], reason=label))
    return issues


# ---------------------------------------------------------------------------------------------
# 融合写出
# ---------------------------------------------------------------------------------------------

def write(record_dir, store_dir, *, native_bin=None, work_dir=None, etm=None, elf=None,
          config=None, sources=None, input_bytes=None, metadata=None, framing=None,
          physical=None, capture=None, verify_image=True,
          checkpoint_instructions=trace_store.DEFAULT_CHECKPOINT_INSTRUCTIONS,
          hash_inputs=True, write_decoded=False, decoder=None, max_events=None,
          progress=None, progress_every=500000, image=None, keep_native=False,
          pause_gc=True):
    """把记录目录（或 etm.bin/elf/config）一步写成 trace-store v1。

    输入优先级：``native_bin`` 参数 > ``record_dir/native.bin`` > 用 ``etm_decode --binary``
    现场生成（记录目录需有 ``etm.bin``）。``metadata`` 可传既有 ``decoded.json``（只用于
    非派生元数据，如 ``physical/framing/capture/targetSnapshot/captureScope/laFilter``；
    ``summary/quality/analysis`` 一律由本函数从原生记录重算）。

    返回 ``(写入摘要, decoded 字典)``；失败抛 ``trace_store.TraceStoreError`` 且不发布
    manifest（半成品 generation 会被删除），``write_decoded=True`` 时 ``decoded.json``
    也只在发布前成功写出。

    ``pause_gc``（默认 True）：写出期间暂停自动 GC。本函数的对象图没有引用环，
    全部由引用计数回收；暂停代际扫描可省去大量无收益工作（实测 steady 约 −8 s），
    结束或异常后恢复调用前的 GC 状态。
    """
    paused = bool(pause_gc) and gc.isenabled()
    if not paused:
        return _write_fused(
            record_dir, store_dir, native_bin=native_bin, work_dir=work_dir, etm=etm,
            elf=elf, config=config, sources=sources, input_bytes=input_bytes,
            metadata=metadata, framing=framing, physical=physical, capture=capture,
            verify_image=verify_image, checkpoint_instructions=checkpoint_instructions,
            hash_inputs=hash_inputs, write_decoded=write_decoded, decoder=decoder,
            max_events=max_events, progress=progress, progress_every=progress_every,
            image=image, keep_native=keep_native)
    gc.disable()
    try:
        return _write_fused(
            record_dir, store_dir, native_bin=native_bin, work_dir=work_dir, etm=etm,
            elf=elf, config=config, sources=sources, input_bytes=input_bytes,
            metadata=metadata, framing=framing, physical=physical, capture=capture,
            verify_image=verify_image, checkpoint_instructions=checkpoint_instructions,
            hash_inputs=hash_inputs, write_decoded=write_decoded, decoder=decoder,
            max_events=max_events, progress=progress, progress_every=progress_every,
            image=image, keep_native=keep_native)
    finally:
        gc.enable()


def _write_fused(record_dir, store_dir, *, native_bin=None, work_dir=None, etm=None, elf=None,
                 config=None, sources=None, input_bytes=None, metadata=None, framing=None,
                 physical=None, capture=None, verify_image=True,
                 checkpoint_instructions=trace_store.DEFAULT_CHECKPOINT_INSTRUCTIONS,
                 hash_inputs=True, write_decoded=False, decoder=None, max_events=None,
                 progress=None, progress_every=500000, image=None, keep_native=False):
    """``write`` 的内部实现（暂停 GC 的包装见上）。"""
    started = time.perf_counter()
    record_dir = Path(record_dir).resolve()
    store_dir = Path(store_dir).resolve()
    if not record_dir.is_dir():
        raise trace_store.TraceStoreError('记录目录不存在：%s' % record_dir)
    phases = collections.OrderedDict()
    warnings = []

    def note(message):
        if progress is not None:
            progress(message)

    def mark(name, since):
        phases[name] = round(time.perf_counter() - since, 3)
        return time.perf_counter()

    # ---- 1) 输入解析 ----------------------------------------------------------------------
    since = time.perf_counter()
    elf_path = Path(elf).resolve() if elf else record_dir / 'firmware.elf'
    if not elf_path.is_file():
        raise trace_store.TraceStoreError(
            '缺少固件 ELF：%s（融合写出需要镜像做反汇编与机器码/源码位置交叉校验）' % elf_path)
    if metadata is None and (record_dir / DECODED_FILE).is_file():
        metadata = _load_json(record_dir / DECODED_FILE)
        if not isinstance(metadata, dict):
            raise trace_store.TraceStoreError('decoded.json 不是 JSON 对象')
    raw_config = config if isinstance(config, dict) else None
    if raw_config is None and isinstance(metadata, dict):
        raw_config = metadata.get('config')
    config_path = record_dir / CONFIG_FILE
    if raw_config is None and config_path.is_file():
        raw_config = manifest_config(config_path)
    config = decoder_config(raw_config)
    if sources is None and (record_dir / SOURCES_FILE).is_file():
        values = _load_json(record_dir / SOURCES_FILE)
        sources = values if isinstance(values, dict) else None
    framing_value = framing if framing is not None else (
        metadata.get('framing') if isinstance(metadata, dict) else None)
    etm_path = Path(etm).resolve() if etm else record_dir / ETM_FILE
    native_path = Path(native_bin).resolve() if native_bin else record_dir / NATIVE_FILE
    since = mark('inputs', since)

    # ---- 2) ELF/镜像 ----------------------------------------------------------------------
    if image is None:
        image = Image(elf_path, sources)
    since = mark('loadElf', since)

    # ---- 3) 解码（仅当没有现成 native.bin） ------------------------------------------------
    decode_info = None
    temporary_work = False
    if not native_path.is_file():
        if not etm_path.is_file():
            raise trace_store.TraceStoreError(
                '缺少 native.bin 且没有 etm.bin：至少需要一个输入（记录目录 %s）' % record_dir)
        if work_dir is None:
            work_dir = Path(tempfile.mkdtemp(prefix='etm-p3-native-'))
            temporary_work = True
        else:
            work_dir = Path(work_dir).resolve()
            work_dir.mkdir(parents=True, exist_ok=True)
        native_path, decode_info = decode_to_native(etm_path, image, config, work_dir,
                                                    decoder=decoder, progress=progress)
    since = mark('decode', since)

    # ---- 4) 打开原生记录、校验协议 ---------------------------------------------------------
    native = native_binary.NativeFile(native_path)
    protocol = native.protocol
    configured = 'ETMv3' if str(config.get('protocol', 'ETMv4')) == 'ETMv3' else 'ETMv4'
    if configured != protocol:
        raise trace_store.TraceStoreError(
            '原生二进制协议与配置不符：%s != %s' % (protocol, configured))
    if input_bytes is None:
        input_bytes = etm_path.stat().st_size if etm_path.is_file() else native.meta['fileBytes']
    native_sha256 = trace_store._sha256_file(native_path) if hash_inputs else None
    since = mark('openNative', since)

    # ---- 5) 静态字典前置（与 decode_stream._expand_records 同源）-------------------------
    predicates = predicate_addresses(image)
    known = _known_addresses(image)
    table = expander._InstructionTable(image, predicates, known)
    since = mark('staticPrep', since)

    # ---- 6) store 骨架（独立 generation，最后原子发布）------------------------------------
    store_dir.mkdir(parents=True, exist_ok=True)
    if store_dir.is_file():
        raise trace_store.TraceStoreError('store 目录被同名文件占用：%s' % store_dir)
    (store_dir / 'generations').mkdir(exist_ok=True)
    generation_tag = secrets.randbits(63) or 1
    generation = 'g-%s-%s' % (time.strftime('%Y%m%dT%H%M%S'), secrets.token_hex(4))
    generation_dir = store_dir / 'generations' / generation
    try:
        generation_dir.mkdir()
    except FileExistsError:
        raise trace_store.TraceStoreError('generation 目录已存在：%s' % generation)
    temp_payload = generation_dir / (trace_store.STORE_FILE + '.payload.tmp')
    sink = trace_store._BlockSink(temp_payload)
    try:
        assembler = _Assembler(sink, checkpoint_instructions, table, image, predicates,
                               verify_image=verify_image, max_events=max_events,
                               progress=progress, progress_every=progress_every)
        counters = dict(covered=0, overlap=0, unknown=0, unsync_records=0, invalid=0,
                        overflow=0, packet_count=0, unknown_execution=0,
                        first_path_offset=None, cycle_elements=0)
        packet_count = 0
        packet_buffer = bytearray()
        packets_in_block = 0
        previous_packet_offset = None
        previous_packet = -1
        packet_end = 0
        packet_faults = []
        elements_n = 0
        status = None

        for record in native.records(with_payload=True):
            kind = record[0]
            if kind == native_binary.RECORD_PACKET:
                _, offset, size, type_code, raw = record
                if offset < previous_packet:
                    raise trace_store.TraceStoreError('包偏移倒序，拒绝伪造字节对账')
                previous_packet = offset
                stop = offset + size
                if offset > packet_end:
                    packet_faults.append(dict(
                        category='gap', startSeq=None, endSeq=None, records=0, offset=packet_end,
                        bytes=offset - packet_end,
                        reason='ETM 包字节覆盖缺口；不能据此补造指令'))
                if offset < packet_end:
                    counters['overlap'] += max(0, min(packet_end, stop) - offset)
                    packet_faults.append(dict(
                        category='overlap', startSeq=None, endSeq=None, records=0, offset=offset,
                        bytes=max(0, min(stop, packet_end) - offset),
                        reason='ETM 包字节覆盖重叠'))
                counters['covered'] += max(0, stop - max(offset, packet_end))
                packet_end = max(packet_end, stop)
                flags = native_binary.classify_packet(type_code, native.protocol)
                if flags & native_binary.PACKET_UNSYNC:
                    counters['unknown'] += size
                    counters['unsync_records'] += 1
                if flags & native_binary.PACKET_INVALID:
                    counters['invalid'] += 1
                    if flags & native_binary.PACKET_OVERFLOW:
                        counters['overflow'] += 1
                    packet_faults.append(dict(
                        category='packet', startSeq=None, endSeq=None, records=0, offset=offset,
                        bytes=size,
                        reason='包问题：' + native_binary.packet_name(type_code, native.protocol)))
                counters['packet_count'] += 1
                # 包块编码：offsetDelta + size + 原始字节 + 说明文本（原生二进制无文本，见 meta）
                trace_store._wv(packet_buffer, offset - (previous_packet_offset or 0))
                trace_store._wv(packet_buffer, size)
                trace_store._wpair(packet_buffer, raw)
                trace_store._wpair(packet_buffer, b'')
                previous_packet_offset = offset
                packet_count += 1
                packets_in_block += 1
                if len(packet_buffer) >= trace_store.TARGET_BLOCK_BYTES:
                    sink.add(trace_store.BLK_PACKETS, bytes(packet_buffer), packets_in_block)
                    packet_buffer = bytearray()
                    packets_in_block = 0
                continue
            if kind == native_binary.RECORD_STATUS:
                status = record[1]
                continue
            # ---- 元素 ----
            (_, etype, offset, _elem_id, start, end, _isa, flags, cycle_count, _has_cc,
             _timestamp, _has_ts, last_executed, _last_cond, _last_type, _last_subtype,
             payload, text) = record
            elements_n += 1
            if etype == 12:
                counters['cycle_elements'] += 1
                assembler.fold_cycles(cycle_count)
                continue
            if etype == 5:
                assembler.add_range(offset, start, end, payload, cycle_count, last_executed)
                continue
            if etype in (1, 2, 3, 6, 7, 8, 9, 10):
                assembler.add_event(etype, offset, payload, start, end, flags, text)
                continue
            # 其余元素类型不进入事件流/范围：与旧展开器一致，只把挂起的末指令 flush。
            assembler.flush_pending_tail()

        if packet_buffer:
            sink.add(trace_store.BLK_PACKETS, bytes(packet_buffer), packets_in_block)
        if status is None:
            raise trace_store.TraceStoreError('原生二进制没有状态记录（解码不完整）')
        native_file_bytes = native.meta['fileBytes']
        native = None                       # 释放 54 MB 级原生负载，装配阶段不再需要
        assembler.finish()
        if not assembler.state['instructions']:
            raise trace_store.TraceStoreError(
                '没有恢复到可信指令范围；原始采集和包记录已保留。'
                '请检查 CPU 是否继续、ETM 同步、Trace ID 与匹配 ELF。')
        since = mark('stream', since)

        # ---- 7) 摘要/分析/质量（与 decode_stream._expand_records 同口径）------------------
        processed = int(status.get('processed') or 0)
        if input_bytes is None:                            # pragma: no cover - 上游已兜底
            input_bytes = processed
        counters['unknown_execution'] = assembler.state['unknown']
        counters['first_path_offset'] = assembler.state['firstInstructionOffset']
        quality = expander.quality_from_counters(status, input_bytes, counters, framing_value)
        analysis = assembler.tracker.finish()
        summary = dict(packets=packet_count, ranges=assembler.range_count,
                       instructions=assembler.state['instructions'], executed=0,
                       sourceMapped=assembler.mapped, exceptions=assembler.exceptions,
                       gaps=assembler.gaps, packetIssues=counters['invalid'])
        summary['executed'] = analysis['timing']['executedInstructions']
        summary['unknownExecution'] = quality['unknownExecution']
        summary['packetIssues'] = quality['packetIssues']
        summary['functions'] = {row['function']: row['knownExecuted']
                                for row in analysis['functions'] if row['knownExecuted']}
        sources_data = {name: image.source(name) for name in sorted(table.files)}
        streamed = dict(eventsFile=trace_store.STORE_FILE, events=assembler.state['events'],
                        elements=elements_n, packets=packet_count, nativeJsonl=NATIVE_FILE,
                        schema=2)
        decoded = dict(version=3, protocol=protocol, config=config, inputBytes=input_bytes,
                       elf=dict(name=Path(image.path).name,
                                sha256=hashlib.sha256(image.bytes).hexdigest()),
                       summary=summary, status=status, timeBasis=TEXT_BASIS,
                       streamed=streamed, quality=quality, analysis=analysis)
        # 既有记录的非派生元数据原样带入（physical/framing/capture/targetSnapshot/...）。
        if isinstance(metadata, dict):
            for key in METADATA_KEYS:
                if key in metadata and key not in decoded:
                    decoded[key] = metadata[key]
        if physical is not None:
            decoded['physical'] = physical
        if framing_value is not None:
            decoded['framing'] = framing_value
        if capture is not None:
            decoded['capture'] = capture
        # session.json 里 server 层会合并进 decoded 的字段（与 server.result 同义）。
        session_path = record_dir / 'session.json'
        session = _load_json(session_path) if session_path.is_file() else {}
        if isinstance(session, dict):
            for key in ('targetSnapshot', 'captureScope', 'laFilter'):
                if key not in decoded and key in session:
                    decoded[key] = session.get(key)
            capture_entry = session.get('captureEntry')
            if capture_entry is not None:
                decoded['captureStart'] = dict(
                    selectedPC=capture_entry,
                    firstRecoveredPC=assembler.first_instruction_pc,
                    entryRecovered=capture_entry in assembler.static_by_pc,
                    firstMatchesEntry=assembler.first_instruction_pc == capture_entry)
        decoded['traceStore'] = dict(
            format=trace_store.FULL_FORMAT_NAME, schemaVersion=trace_store.SCHEMA_VERSION,
            store=STORE_DIR_NAME, source=NATIVE_FILE, generation=generation,
            counts=dict(events=assembler.state['events'], instructions=summary['instructions'],
                        runs=assembler.state['runs'], staticPcs=len(assembler.static_entries),
                        packets=packet_count))
        if isinstance(metadata, dict):
            _compare_declared(metadata, summary, quality, warnings)
        since = mark('summarize', since)

        # ---- 8) 静态字典/字符串表落块（静态条目按首次出现顺序、每个 PC 一份）-------------
        static_buffer = bytearray()
        static_count = 0
        for entry in assembler.static_entries:
            trace_store._wv(static_buffer, entry.pc)
            static_buffer.append(entry.size)
            flags = 0
            if entry.predicated:
                flags |= 0x01
            if entry.conditional:
                flags |= 0x02
            if entry.function is not None:
                flags |= 0x04
            if entry.file is not None:
                flags |= 0x08
            if entry.line is not None:
                flags |= 0x10
            static_buffer.append(flags)
            static_buffer.append(trace_store.FLOW_CODES[entry.flow])
            static_buffer += entry.bytes
            trace_store._wv(static_buffer, assembler.strings.id_of(entry.mnemonic))
            trace_store._wv(static_buffer, assembler.strings.id_of(entry.operands))
            if entry.function is not None:
                trace_store._wv(static_buffer, assembler.strings.id_of(entry.function))
            if entry.file is not None:
                trace_store._wv(static_buffer, assembler.strings.id_of(entry.file))
            if entry.line is not None:
                trace_store._wv(static_buffer, entry.line)
            static_count += 1
            if len(static_buffer) >= trace_store.TARGET_BLOCK_BYTES:
                sink.add(trace_store.BLK_STATIC, bytes(static_buffer), static_count)
                static_buffer = bytearray()
                static_count = 0
        if static_buffer:
            sink.add(trace_store.BLK_STATIC, bytes(static_buffer), static_count)

        string_buffer = bytearray()
        string_count = 0
        for value in assembler.strings.values:
            encoded = value.encode('utf-8')
            if len(encoded) > trace_store.MAX_STRING_BYTES:
                raise trace_store.TraceStoreError('字符串过长：%d 字节' % len(encoded))
            trace_store._wpair(string_buffer, encoded)
            string_count += 1
            if len(string_buffer) >= trace_store.TARGET_BLOCK_BYTES:
                sink.add(trace_store.BLK_STRINGS, bytes(string_buffer), string_count)
                string_buffer = bytearray()
                string_count = 0
        if string_buffer:
            sink.add(trace_store.BLK_STRINGS, bytes(string_buffer), string_count)

        # ---- 9) 导航/检查点索引/块级聚合 ----------------------------------------------------
        for mode, values in assembler.nav.items():
            if not values:
                continue
            if any(a >= b for a, b in zip(values, values[1:])):
                raise trace_store.TraceStoreError('导航索引未严格递增（mode=%d）' % mode)
            for position in range(0, len(values), 65536):
                part = values[position:position + 65536]
                payload_buffer = bytearray([mode])
                trace_store._wv(payload_buffer, len(part))
                trace_store._wv(payload_buffer, part[0])
                previous = part[0]
                for value in part[1:]:
                    trace_store._wv(payload_buffer, value - previous)
                    previous = value
                sink.add(trace_store.BLK_NAV, bytes(payload_buffer), len(part))

        checkpoint_buffer = bytearray()
        previous_checkpoint_seq = 0
        for seq, block_index, record_offset in assembler.sequence.checkpoints:
            trace_store._wv(checkpoint_buffer, seq - previous_checkpoint_seq)
            trace_store._wv(checkpoint_buffer, block_index)
            trace_store._wv(checkpoint_buffer, record_offset)
            previous_checkpoint_seq = seq
        sink.add(trace_store.BLK_CHECKPOINTS, bytes(checkpoint_buffer),
                 len(assembler.sequence.checkpoints))

        agg_buffer = bytearray()
        for aggregate in assembler.sequence.agg:
            for key in trace_store.AGG_KEYS:
                trace_store._wv(agg_buffer, int(aggregate.get(key, 0)))
        sink.add(trace_store.BLK_AGG, bytes(agg_buffer), len(assembler.sequence.agg))
        since = mark('blocks', since)

        # ---- 10) JSON 摘要块（meta/summary/sources/issues） --------------------------------
        issues = list(assembler.issues)
        issues.extend(_packet_issues(quality, status, input_bytes, packet_end,
                                     packet_faults, streamed_schema=2))
        declared = metadata.get('summary') if isinstance(metadata, dict) else None
        declared_ranges = declared.get('ranges') if isinstance(declared, dict) else None
        # decoded.json 与旧路径同格式（默认分隔符），供 decode_capture_stream 等上层原样重写。
        decoded_text = json.dumps(decoded, ensure_ascii=False)
        decoded_bytes = decoded_text.encode('utf-8')
        if write_decoded:
            decoded_sha256 = hashlib.sha256(decoded_bytes).hexdigest()
        else:
            existing = record_dir / DECODED_FILE
            decoded_sha256 = trace_store._sha256_file(existing) if existing.is_file() else None
        meta = dict(
            format=trace_store.FORMAT_NAME, formatName=trace_store.FULL_FORMAT_NAME,
            schemaVersion=trace_store.SCHEMA_VERSION, byteOrder='little',
            record=dict(name=record_dir.name, eventsSource=NATIVE_FILE,
                        streamedSchema=2, decodedVersion=3, protocol=protocol,
                        nativeFormat='trace-bin native v1',
                        nativeFile=Path(native_path).name,
                        nativeBytes=native_file_bytes),
            counts=dict(events=assembler.state['events'],
                        instructions=assembler.state['instructions'],
                        ranges=assembler.range_count,
                        declaredRanges=declared_ranges,
                        runs=assembler.state['runs'],
                        rareEvents=assembler.state['rareEvents'],
                        staticPcs=len(assembler.static_entries),
                        strings=len(assembler.strings.values), packets=packet_count,
                        sequenceBlocks=len(assembler.sequence.agg),
                        checkpoints=len(assembler.sequence.checkpoints),
                        executed=assembler.state['executed'],
                        unknownExecution=assembler.state['unknown'],
                        notTaken=assembler.state['notTaken'],
                        exceptions=assembler.exceptions, gaps=assembler.gaps),
            eventTypes={str(key): value for key, value in sorted(assembler.type_counts.items())},
            seq=dict(first=0, last=assembler.expected_seq - 1, contiguous=True,
                     count=assembler.state['events'],
                     firstInstruction=assembler.state['firstInstructionSeq'],
                     offsetRange=[assembler.state['minOffset'], assembler.state['maxOffset']]),
            fieldPresence=dict(instruction=list(trace_store.INSTRUCTION_VARIANTS[0]),
                               event=list(trace_store.EVENT_FIELDS)),
            hasCycleFields=True,
            verify=dict(elements=True, elementsVerified=assembler.range_count, image=image is not None,
                        staticPcs=len(assembler.static_entries),
                        machineCodeCrossCheck=image is not None,
                        predicateFallback=assembler.predicate_fallback,
                        rangeFallbacks=assembler.range_fallbacks),
            packetText=dict(available=False, reason=(
                '原生二进制只保存包类型码与原始字节；OpenCSD 说明文本需按同版本配置重放'
                '（trace-store v1 §11.1-5：原生包文本不存储）。故障分类与字节核算不依赖该文本。')),
            layout=dict(headerBytes=trace_store.HEADER_SIZE,
                        indexEntryBytes=trace_store.INDEX_ENTRY_SIZE,
                        targetBlockBytes=trace_store.TARGET_BLOCK_BYTES,
                        maxBlockBytes=trace_store.MAX_BLOCK_BYTES,
                        runInstructionLimit=trace_store.MAX_RUN_INSTRUCTIONS,
                        checkpointInstructions=assembler.sequence.checkpoint_instructions),
            created=dict(unix=time.time(), iso=time.strftime('%Y-%m-%dT%H:%M:%S%z')),
            sourceSha256=dict(decoded=decoded_sha256, nativeBin=native_sha256),
            issues=len(issues),
        )
        stored_summary = {key: value for key, value in decoded.items()
                          if key not in ('events', 'elements', 'packets', 'sources')}
        stored_summary['eventCount'] = assembler.state['events']
        stored_summary['packetCount'] = packet_count
        stored_summary['sourceFiles'] = list(sources_data)
        stored_summary['firstSeq'] = assembler.state['firstInstructionSeq']
        stored_summary['traceStore'] = dict(
            format=trace_store.FULL_FORMAT_NAME, generation=generation,
            runs=assembler.state['runs'], instructions=assembler.state['instructions'])
        sink.add_compressed_json(trace_store.BLK_META, meta)
        sink.add_compressed_json(trace_store.BLK_SUMMARY, stored_summary)
        sink.add_compressed_json(trace_store.BLK_SOURCES, sources_data)
        sink.add_compressed_json(trace_store.BLK_ISSUES, issues)

        sink.handle.flush()
        info = sink.assemble(generation_dir / trace_store.STORE_FILE, generation_tag)
        sink.close()
        try:
            temp_payload.unlink()
        except OSError:
            pass
        since = mark('assemble', since)

        # ---- 11) decoded.json/sources.json（在发布 manifest 之前写；manifest 是唯一发布点）--
        if write_decoded:
            (record_dir / DECODED_FILE).write_text(decoded_text, encoding='utf-8')
            (record_dir / SOURCES_FILE).write_text(
                json.dumps(sources_data, ensure_ascii=False), encoding='utf-8')
        since = mark('decoded', since)

        # ---- 12) manifest（原子发布） -----------------------------------------------------
        inputs = _bindings(record_dir, native_path)
        if not hash_inputs:
            inputs = {name: dict(bytes=value['bytes'], sha256=None)
                      for name, value in inputs.items()}
        bindings = dict(inputs=inputs)
        bindings['record'] = dict(
            eventsSource=NATIVE_FILE, streamedSchema=2,
            decodedSha256=meta['sourceSha256']['decoded'],
            eventsSha256=None, eventsBytes=None,
            nativeBinSha256=inputs.get(NATIVE_FILE, {}).get('sha256'),
            nativeBinBytes=inputs.get(NATIVE_FILE, {}).get('bytes'))
        bindings['config'] = config
        bindings['processing'] = dict(
            traceStore=trace_store.SCHEMA_VERSION, processor='trace_store_write.py fused writer',
            decodedVersion=3, decoderLibrary=(status or {}).get('library'), protocol=protocol,
            nativeFormat='trace-bin native v1', source=NATIVE_FILE)
        manifest = dict(
            format=trace_store.FORMAT_NAME, formatName=trace_store.FULL_FORMAT_NAME,
            schemaVersion=trace_store.SCHEMA_VERSION, byteOrder='little', state='complete',
            generation=generation, generationTag='0x%016x' % generation_tag,
            createdAt=time.strftime('%Y-%m-%dT%H:%M:%S%z'), createdUnix=time.time(),
            files=[dict(name=trace_store.STORE_FILE, bytes=info['bytes'],
                        sha256=info['sha256'], blocks=info['blocks'],
                        payloadBytes=info['payloadBytes'])],
            counts=meta['counts'], fieldPresence=meta['fieldPresence'], bindings=bindings,
            checksums=dict(algorithm='sha256', block='crc32', verifiedAtOpen=['size', 'sha256']),
            notes=['manifest 是唯一发布点：失败/中止的 generation 目录不会被本 manifest 引用',
                   '旧 generation 保留不删，读取只认 manifest 指向的一代',
                   '本代由 P3 融合写出器从 native.bin 直接生成，未生成 events/packets/elements '
                   'JSONL 或 SQLite；包说明文本不可用（见 meta.packetText）'],
        )
        trace_store._publish_manifest(store_dir, manifest)
        since = mark('publish', since)

        elapsed = time.perf_counter() - started
        kept_native = not temporary_work or keep_native
        result = dict(ok=True, storeDir=str(store_dir), generation=generation,
                      storeBytes=info['bytes'], storeSha256=info['sha256'],
                      elapsedSeconds=round(elapsed, 3), phases=phases,
                      counts=meta['counts'], inputBytes=input_bytes,
                      nativePath=str(native_path) if kept_native else None,
                      nativeGenerated=decode_info is not None,
                      decodeInfo=decode_info,
                      peakRssBytes=trace_store.peak_rss_bytes(),
                      elementsVerified=assembler.range_count,
                      staticPcs=len(assembler.static_entries),
                      runs=assembler.state['runs'], events=assembler.state['events'],
                      instructions=assembler.state['instructions'],
                      packetText=False, decodedWritten=bool(write_decoded),
                      decodedPath=str(record_dir / DECODED_FILE) if write_decoded else None,
                      warnings=warnings)
        note('  完成：%s（%.1f s，%.1f MiB，%d 条事件 / %d 条指令）'
             % (store_dir, elapsed, info['bytes'] / 1048576, assembler.state['events'],
                assembler.state['instructions']))
        return result, decoded
    except Exception:
        sink.close()
        trace_store._remove_tree(generation_dir)
        raise
    finally:
        if temporary_work and not keep_native:
            shutil.rmtree(work_dir, ignore_errors=True)


def _bindings(record_dir, native_path):
    """manifest.binding.inputs：记录目录全部输入 + 实际使用的 native.bin 指纹。"""
    result = trace_store._binding_files(record_dir)
    native_path = Path(native_path)
    if native_path.is_file():
        recorded = Path(record_dir) / NATIVE_FILE
        if recorded.resolve() != native_path.resolve() or NATIVE_FILE not in result:
            stat = native_path.stat()
            result[NATIVE_FILE] = dict(bytes=stat.st_size,
                                       sha256=trace_store._sha256_file(native_path))
    return result


def _compare_declared(metadata, summary, quality, warnings):
    """与既有 decoded.json 的派生字段对账（不阻断；差异写 warnings，供证据核对）。"""
    declared = metadata.get('summary') if isinstance(metadata, dict) else None
    if isinstance(declared, dict):
        for key in ('packets', 'ranges', 'instructions', 'executed', 'exceptions', 'gaps',
                    'packetIssues'):
            if key in declared and declared[key] != summary.get(key):
                warnings.append('summary.%s：既有记录 %r，本次重算 %r'
                                % (key, declared[key], summary.get(key)))
    declared_quality = metadata.get('quality') if isinstance(metadata, dict) else None
    if isinstance(declared_quality, dict):
        for key in ('processedBytes', 'unknownExecution', 'packetIssues',
                    'unsynchronizedBytes', 'accountedBytes'):
            if key in declared_quality and declared_quality[key] != quality.get(key):
                warnings.append('quality.%s：既有记录 %r，本次重算 %r'
                                % (key, declared_quality[key], quality.get(key)))


# ---------------------------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------------------------

def _main(argv=None):
    parser = argparse.ArgumentParser(
        prog='trace_store_write.py',
        description='P3 融合写出器：从 native.bin（无则调用 etm_decode --binary）一步生成 '
                    'trace-store v1，不生成 events/packets/elements JSONL 或 SQLite。')
    sub = parser.add_subparsers(dest='command', required=True)

    writer = sub.add_parser('write', help='从记录目录/etm.bin 生成 trace-store')
    writer.add_argument('record_dir', help='记录目录（etm.bin、firmware.elf、sources.json 等）')
    writer.add_argument('store_dir', help='输出目录（新 generation，成功后原子发布 manifest）')
    writer.add_argument('--native-bin', default=None, help='已存在的 native.bin（省去现场解码）')
    writer.add_argument('--etm', default=None, help='覆盖记录目录里的 etm.bin')
    writer.add_argument('--elf', default=None, help='覆盖记录目录里的 firmware.elf')
    writer.add_argument('--config', default=None, help='覆盖 native-config.txt（十六进制寄存器清单）')
    writer.add_argument('--work-dir', default=None, help='生成 native.bin 的工作目录')
    writer.add_argument('--decoder', default=None, help='etm_decode 可执行文件路径')
    writer.add_argument('--checkpoint-instructions', type=int,
                        default=trace_store.DEFAULT_CHECKPOINT_INSTRUCTIONS,
                        help='检查点最大指令间隔（默认 4096）')
    writer.add_argument('--no-image', action='store_true', help='跳过 ELF 机器码/源码位置交叉校验')
    writer.add_argument('--no-hash-inputs', action='store_true', help='不哈希输入文件（只记尺寸）')
    writer.add_argument('--write-decoded', action='store_true',
                        help='在记录目录写 decoded.json/sources.json（默认不覆盖既有记录）')
    writer.add_argument('--max-events', type=int, default=None, help='安全上限（超过即失败）')
    writer.add_argument('--quiet', action='store_true', help='不打印进度')
    writer.add_argument('--json', action='store_true', help='以 JSON 输出写入摘要')

    info = sub.add_parser('info', help='打印存储清单与计数')
    info.add_argument('store_dir')
    info.add_argument('--json', action='store_true', help='以 JSON 输出')

    args = parser.parse_args(argv)
    try:
        if args.command == 'write':
            def progress(message):
                if not args.quiet:
                    print(message, flush=True)

            config = manifest_config(args.config) if args.config else None
            result, _decoded = write(
                args.record_dir, args.store_dir, native_bin=args.native_bin,
                work_dir=args.work_dir, etm=args.etm, elf=args.elf, config=config,
                verify_image=not args.no_image, hash_inputs=not args.no_hash_inputs,
                write_decoded=args.write_decoded,
                checkpoint_instructions=args.checkpoint_instructions,
                max_events=args.max_events, decoder=args.decoder,
                progress=None if args.quiet else progress)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print('store：%s（%.1f MiB，%d 个序列块，generation=%s）'
                      % (result['storeDir'], result['storeBytes'] / 1048576,
                         result['counts']['sequenceBlocks'], result['generation']))
                print('事件 %d / 指令 %d / 展开段 %d / 静态 PC %d / 包 %d'
                      % (result['counts']['events'], result['counts']['instructions'],
                         result['counts']['runs'], result['counts']['staticPcs'],
                         result['counts']['packets']))
                print('耗时 %.2f s（%s），峰值 RSS %.1f MiB，元素校验 %d 个原生范围'
                      % (result['elapsedSeconds'],
                         '，'.join('%s %.2fs' % item for item in result['phases'].items()),
                         result['peakRssBytes'] / 1048576, result['elementsVerified']))
                for warning in result['warnings']:
                    print('  警告：' + warning)
        elif args.command == 'info':
            data = trace_store.describe_store(args.store_dir)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                meta = data['meta']
                print('格式 %s（schema %d，%s）generation=%s'
                      % (meta['formatName'], meta['schemaVersion'], meta['byteOrder'],
                         data['generation']))
                print('记录 %s；事件源 %s；原生格式 %r'
                      % (meta['record']['name'], meta['record']['eventsSource'],
                         meta['record'].get('nativeFormat')))
                for key, value in meta['counts'].items():
                    print('  %-16s %s' % (key, value))
                print('  %-16s %s' % ('seqRange', data['stats']['seqRange']))
                print('  %-16s %s' % ('blocks', data['stats']['blocks']))
                print('  %-16s %s' % ('aggregateOK', data['stats']['aggregateMatches']))
        return 0
    except trace_store.TraceStoreError as error:
        print('trace-store 错误：%s' % error, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(_main())
