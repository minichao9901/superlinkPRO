"""执行流派生分析：明确未知状态，不把函数切换当成调用或把计数当耗时。"""
import json
from collections import Counter, defaultdict
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_MCLASS
from capstone.arm import ARM_INS_IT, ARM_INS_BL, ARM_INS_BLX, ARM_CC_AL, ARM_CC_INVALID


def predicate_addresses(image):
    """从完整函数的机器码找到 IT 保护的指令；不推测条件是否成立。"""
    decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    decoder.detail = True
    addresses = set()
    for address, size, _ in image.functions:
        remaining = 0
        for ins in decoder.disasm(image.read(address, size), address):
            if ins.id == ARM_INS_IT:
                remaining = len(ins.mnemonic) - 1
            elif remaining:
                addresses.add(ins.address)
                remaining -= 1
    return addresses


def possible_predicates(image, events):
    addresses = predicate_addresses(image)
    known = set()
    decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    decoder.detail = True
    for start, size, _ in image.functions:
        known.update(i.address for i in decoder.disasm(image.read(start,size),start))
    # A stripped image or undecodable function can begin a trace inside IT.
    # Mark plausible guarded slots unknown instead of assuming unconditional.
    for pc in {e['pc'] for e in events if e['kind']=='instruction'}-known:
        for start in range(max(0,pc-18),pc,2):
            decoded=list(decoder.disasm(image.read(start,pc-start+4),start))
            if decoded and decoded[0].id==ARM_INS_IT:
                count=len(decoded[0].mnemonic)-1
                if any(i.address==pc for i in decoded[1:count+1]):
                    addresses.add(pc)
                    break
    return addresses


def instruction_info(ins):
    mnemonic, operands = ins.mnemonic, ins.op_str
    if ins.id in (ARM_INS_BL, ARM_INS_BLX):
        flow = 'call'
    elif mnemonic.startswith('bx') or ((mnemonic.startswith('pop') or mnemonic.startswith('ldm')) and 'pc' in operands):
        flow = 'return' if 'lr' in operands or 'pc' in operands else 'branch'
    elif 1 in ins.groups or any(ins.reg_name(r)=='pc' for r in ins.regs_access()[1]):
        flow = 'branch'
    else:
        flow = 'linear'
    return dict(flow=flow, conditional=ins.cc not in (ARM_CC_AL, ARM_CC_INVALID))


def mark_execution(events, predicates=None):
    """给历史记录升级状态。None 表示当前 trace 没有证实谓词结果。"""
    predicates = predicates or set()
    decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    decoder.detail = True
    cache = {}
    for e in events:
        if e['kind'] != 'instruction':
            continue
        key = (e['pc'], e['bytes'])
        if key not in cache:
            ins = next(decoder.disasm(bytes.fromhex(e['bytes']), e['pc']), None)
            cache[key] = instruction_info(ins) if ins else dict(flow='unknown', conditional=True)
        e.update(cache[key])
        unknown = e.get('executed') is None or (e['pc'] in predicates and e['flow'] == 'linear')
        if unknown:
            e['executed'] = None
            e['executionState'] = 'unknown'
            e['executionReason'] = 'IT 非分支条件指令：当前 trace 未记录条件结果'
        else:
            e['executionState'] = 'executed' if e['executed'] else 'not-taken'
            e['executionReason'] = ''


def byte_accounting(result):
    size = result.get('status', {}).get('processed', 0)
    input_size=result.get('inputBytes',size)
    packets = result['packets']
    # Native packets are monotonic. Legacy in-memory records may be unordered.
    ordered = sorted(packets, key=lambda p: p['offset']) if isinstance(packets, list) else packets
    end = covered = overlap = unknown = unsync_records = invalid_count = overflow_count = packet_count = 0
    previous = -1
    for packet in ordered:
        start, stop = packet['offset'], packet['offset'] + packet['size']
        if start < previous:
            raise ValueError('包偏移倒序，拒绝伪造字节对账')
        previous = start
        if start < end:
            overlap += max(0, min(end, stop) - start)
        covered += max(0, stop - max(start, end))
        end = max(end, stop)
        text = packet.get('text', '')
        packet_count += 1
        if text.startswith(('I_NOT_SYNC', 'NOTSYNC')):
            unknown += packet['size']
            unsync_records += 1
        if any(t in text.upper() for t in ('BAD_', 'RESERVED', 'OVERFLOW', 'INCOMPLETE_EOT')):
            invalid_count += 1
            overflow_count += 'OVERFLOW' in text.upper()
    unknown_execution = sum(e.get('executionState') == 'unknown' for e in result['events'])
    framing = result.get('framing', {})
    return dict(inputBytes=input_size,unprocessedBytes=max(0,input_size-size),processedBytes=size, accountedBytes=covered, overlapBytes=overlap,
                unaccountedBytes=max(0, input_size-covered), unsynchronizedBytes=unknown,
                unsynchronizedRecords=unsync_records, validPackets=packet_count-unsync_records-invalid_count,
                packetIssues=invalid_count, overflowPackets=overflow_count,
                decoderLastError=result.get('status', {}).get('lastErrorCode', None),
                unknownExecution=unknown_execution, initialPhysicalBytes=framing.get('initial_skipped_bytes'),
                unknownSourceBytes=framing.get('unknown_bytes'), trailingFrameBytes=framing.get('trailing_frame_bytes'),
                trailingSyncBytes=framing.get('trailing_sync_bytes',0),
                firstPathOffset=next((e['offset'] for e in result['events'] if e['kind']=='instruction'), None),
                boundaryPresent=bool(unknown or framing.get('initial_skipped_bytes') or framing.get('trailing_frame_bytes') or framing.get('trailing_sync_bytes')),
                timing=dict(cycleElements=sum(e['type']==12 for e in result['elements']), timeFlameAvailable=False),
                basis='字节核算不等于采集无损证明；源码映射率不等于程序覆盖率')


class CallTracker:
    """调用栈/周期归因的单遍累加器。

    `analyze_calls()`（历史整段路径）与 `decode_stream` 的单遍展开路径共用本类，
    因此两条路径的统计口径、字典插入顺序与最终排序完全一致。
    """

    def __init__(self):
        self.stats, self.hotspots, self.edges = {}, {}, Counter()
        self.root = dict(name='已恢复执行区间', self=0, value=0, selfCycles=0, valueCycles=0, children={})
        self.stack, self.contexts, self.previous = [], [], None
        self.expected_resume = None
        self.unknown_restarts = 0
        self.path = []
        self.path_json = '[]'
        self._path_nodes = None
        self._path_stats = None

    def statistics(self, fn):
        value = self.stats.get(fn)
        if value is None:
            value = dict(function=fn, instructions=0, knownExecuted=0,
                         unknownExecution=0, notTaken=0, calls=0,
                         closedCalls=0, tailTransfers=0, interruptEntries=0,
                         selfCycles=0, totalCycles=0, cycleRanges=0)
            self.stats[fn] = value
        return value

    @staticmethod
    def frame(fn, return_pc=None, origin=None):
        return dict(function=fn, returnPC=return_pc, origin=origin)

    def _sync(self):
        """调用栈变化后刷新路径列表与预序列化 JSON（展开时只在栈变化时执行）。"""
        self.path = [f['function'] for f in self.stack]
        self.path_json = json.dumps(self.path, ensure_ascii=False)
        # Resolve tree nodes lazily on the next instruction. An interrupt with
        # no following instruction must not create an empty branch in reports.
        self._path_nodes = None
        self._path_stats = None

    def feed_event(self, etype, flags=0, payload=0, end=None):
        """非指令事件：边界/异常/返回，按历史规则重置或压栈。"""
        if etype in (1, 2, 6, 7, 8):
            self.stack, self.contexts, self.previous = [], [], None
            self.expected_resume = None
            self.unknown_restarts += 1
        elif etype == 9:
            if flags & (1 << 12):
                # Tail chaining has no ordinary nested-call stack contract.
                self.stack, self.contexts, self.previous = [], [], None
                self.unknown_restarts += 1
            self.contexts.append((self.stack, self.previous, end if flags & (1 << 6) else None))
            self.stack = [self.frame(f'中断 {payload}')]
            self.previous = None
        elif etype == 10:
            if self.contexts:
                self.stack, self.previous, self.expected_resume = self.contexts.pop()
            else:
                self.stack, self.previous = [], None
                self.unknown_restarts += 1
        else:
            return
        self._sync()

    def feed_instruction(self, function, file, line, executed, flow, pc, size, cycles):
        """一条指令：维护调用栈、周期归因与火焰图；返回计入该事件的周期数。"""
        fn = function or '(未知函数)'
        s = self.statistics(fn)
        expected_resume = self.expected_resume
        if expected_resume is not None:
            if pc != (expected_resume & ~1):
                self.stack = []
                self.previous = None
                self.unknown_restarts += 1
            self.expected_resume = None
        stack = self.stack
        if not stack:
            stack = self.stack = [self.frame('采集前调用栈未知'), self.frame(fn)]
            self._sync()
        if stack[-1]['function'].startswith('中断 '):
            stack.append(self.frame(fn))
            s['interruptEntries'] += 1
            self._sync()
        previous = self.previous
        if previous and previous[3] is True:
            old_fn = previous[4] or '(未知函数)'
            if previous[2] == 'call':
                stack.append(self.frame(fn, previous[0] + previous[1], fn))
                s['calls'] += 1
                self.edges[(old_fn, fn, 'call')] += 1
                self._sync()
            elif previous[2] in ('return', 'branch'):
                match = None
                for index in range(len(stack) - 1, -1, -1):
                    if stack[index]['returnPC'] == pc:
                        match = index
                        break
                if match is not None:
                    for closing in stack[match:]:
                        if closing['origin']:
                            self.statistics(closing['origin'])['closedCalls'] += 1
                    self.stack = stack[:match]
                    self._sync()
                elif old_fn != fn and previous[2] == 'branch':
                    s['tailTransfers'] += 1
                    self.edges[(old_fn, fn, 'tail-transfer')] += 1
                    stack[-1]['function'] = fn
                    self._sync()
                elif old_fn != fn:
                    self.stack = [self.frame('采集前调用栈未知'), self.frame(fn)]
                    self.unknown_restarts += 1
                    self._sync()
        stack = self.stack
        if not stack or stack[-1]['function'] != fn:
            self.stack = [self.frame('调用关系未证实'), self.frame(fn)]
            self.unknown_restarts += 1
            self._sync()
        stack = self.stack
        if len(stack) > 128:
            raise ValueError('调用层级超过 128，拒绝生成可能失真的调用图。')
        s['instructions'] += 1
        s['knownExecuted'] += executed is True
        s['unknownExecution'] += executed is None
        s['notTaken'] += executed is False
        path = self.path
        # ---- 周期计数归因（ETMv4 cycle count，参考 Ozone 的"每函数耗时/热点"）----
        # cycleCount 只落在"区间最后一条指令"上（见 decode_trace/decode_stream），
        # 所以这里累加到该条指令所属函数 = 函数的**自身周期**；整条调用路径累加 = **包含周期**。
        cc = cycles
        if cc:
            s['selfCycles'] += cc
            s['cycleRanges'] += 1
            if self._path_stats is None:
                self._path_stats = [self.statistics(name) for name in path]
            for ancestor in self._path_stats:
                ancestor['totalCycles'] += cc
        node = self.root
        node['value'] += 1
        node['valueCycles'] += cc
        if self._path_nodes is None:
            nodes = []
            for name in path:
                child = node['children'].get(name)
                if child is None:
                    child = dict(name=name, self=0, value=0, selfCycles=0, valueCycles=0, children={})
                    node['children'][name] = child
                node = child
                nodes.append(node)
            self._path_nodes = nodes
        for node in self._path_nodes:
            node['value'] += 1
            node['valueCycles'] += cc
        node = self._path_nodes[-1] if self._path_nodes else self.root
        node['self'] += 1
        node['selfCycles'] += cc
        key = (fn, file, line)
        hot = self.hotspots.get(key)
        if hot is None:
            hot = dict(function=fn, file=file, line=line, records=0, unknown=0, cycles=0, pc=pc)
            self.hotspots[key] = hot
        hot['records'] += 1
        hot['cycles'] += cc
        hot['unknown'] += executed is None
        self.previous = (pc, size, flow, executed, function)
        return cc

    def feed_linear_many(self,function,file,line,executed,first_pc,first_size,last_pc,last_size,count):
        """Same source/state, consecutive linear instructions with zero cycles.

        Only the first instruction can change the call context. The decoder
        guards each group; this preserves the per-instruction replay contract.
        """
        self.feed_instruction(function,file,line,executed,'linear',first_pc,first_size,0)
        extra=count-1
        if extra:
            fn=function or '(未知函数)';stat=self.stats[fn]
            stat['instructions']+=extra
            stat['knownExecuted']+=extra*(executed is True)
            stat['unknownExecution']+=extra*(executed is None)
            stat['notTaken']+=extra*(executed is False)
            self.root['value']+=extra
            for node in self._path_nodes:node['value']+=extra
            self._path_nodes[-1]['self']+=extra
            hot=self.hotspots[(fn,file,line)];hot['records']+=extra;hot['unknown']+=extra*(executed is None)
            self.previous=(last_pc,last_size,'linear',executed,function)

    def finish(self):
        stats, hotspots, edges = self.stats, self.hotspots, self.edges
        root, unknown_restarts = self.root, self.unknown_restarts
        total_calls = sum(s['calls'] for s in stats.values())
        total_cycles = sum(s['selfCycles'] for s in stats.values())
        executed_instructions = sum(s['knownExecuted'] for s in stats.values())
        for s in stats.values():
            s['openCalls'] = max(0, s['calls']-s['closedCalls'])
            s['callPercent'] = 100*s['calls']/total_calls if total_calls else 0
            s['cyclePercent'] = (100*s['selfCycles']/total_cycles) if total_cycles else 0
            s['cpi'] = (s['selfCycles']/s['instructions']) if (s['instructions'] and s['selfCycles']) else None
        cycle_functions = sorted((s for s in stats.values() if s['selfCycles']),
                                 key=lambda s: s['selfCycles'], reverse=True)
        timing = dict(totalCycles=total_cycles,
                      cycleRanges=sum(s['cycleRanges'] for s in stats.values()),
                      executedInstructions=executed_instructions,
                      instructions=sum(s['instructions'] for s in stats.values()),
                      cpi=(total_cycles/executed_instructions) if (executed_instructions and total_cycles) else None,
                      functions=[dict(function=s['function'], selfCycles=s['selfCycles'],
                                      cyclePercent=s['cyclePercent'], instructions=s['instructions'],
                                      cpi=s['cpi'], calls=s['calls']) for s in cycle_functions],
                      timeFlameAvailable=bool(total_cycles),
                      basis=('周期计数来自 ETMv4 cycle-count 元素（区间末指令记录，可求和）；'
                             '区间若跨越函数边界会让归因偏移，属极端情况'
                             if total_cycles else
                             '本记录没有周期计数数据（目标未启用，或 ETMv3/F103 不支持），时间轴不可用'))
        def serialize(node):
            return {**node, 'children':[serialize(child) for child in node['children'].values()]}
        return dict(functions=sorted(stats.values(), key=lambda s:s['instructions'], reverse=True),
                    hotspots=sorted(hotspots.values(),key=lambda h:(h['cycles'],h['records']),reverse=True),
                    flame=serialize(root), observedCalls=total_calls, unknownStackRestarts=unknown_restarts,
                    edges=[dict(caller=a, callee=b, kind=k, count=n) for (a,b,k),n in edges.items()],
                    timing=timing,
                    weight=('路径指令记录数（含未成立/未知状态）；周期列才是耗时，指令数不是' if total_cycles
                            else '路径指令记录数（含未成立/未知状态），不是耗时'), callsPerSecond=None,
                    rateReason=('缺少覆盖可信执行区间的时间基准，暂不计算调用/秒' if not total_cycles
                                else '可用周期计数折算时间（coreHz 已知），但调用/秒仍取决于覆盖完整度'))


def analyze_calls(events):
    """整段事件列表的调用栈/周期归因（历史路径；流式展开直接用 CallTracker）。"""
    tracker = CallTracker()
    for e in events:
        if e['kind'] != 'instruction':
            tracker.feed_event(e['type'], e.get('flags', 0), e.get('payload'), e.get('end'))
            continue
        cycles = tracker.feed_instruction(e.get('function'), e.get('file', ''), e.get('line'),
                                          e.get('executed'), e.get('flow'), e['pc'], e['size'],
                                          int(e.get('cycleCount') or 0))
        e['callPath'] = list(tracker.path)
        e['cycles'] = cycles
    return tracker.finish()


def enrich(result, image=None):
    by_element = {(e['type'],e['offset']):e for e in result['elements']}
    for event in result['events']:
        if event['kind']=='event':
            element=by_element.get((event['type'],event['offset']))
            if element:
                for key in ('start','end','flags'):
                    event[key]=element.get(key,0)
    if image is not None:
        mark_execution(result['events'], possible_predicates(image,result['events']))
    elif not all('executionState' in e for e in result['events'] if e['kind']=='instruction'):
        mark_execution(result['events'])
    result['quality'] = byte_accounting(result)
    result['analysis'] = analyze_calls(result['events'])
    # 周期计数 → 时间：coreHz 来自采集时的目标快照（时钟未回读时留空，页面就不显示 µs）
    timing = result['analysis']['timing']
    target = result.get('targetSnapshot') or {}
    core_hz = (target.get('clocks') or {}).get('coreHz')
    timing['coreHz'] = core_hz if isinstance(core_hz, (int, float)) and core_hz > 0 else None
    if timing['coreHz'] and timing['totalCycles']:
        timing['coveredSeconds'] = timing['totalCycles'] / timing['coreHz']
        timing['basis'] += f"；按 coreHz={timing['coreHz']} 折算时间"
    result['summary']['executed'] = sum(e['kind']=='instruction' and e.get('executed') is True for e in result['events'])
    result['summary']['unknownExecution'] = result['quality']['unknownExecution']
    result['summary']['packetIssues'] = result['quality']['packetIssues']
    result['summary']['functions'] = dict(Counter(e.get('function') or '(未知函数)' for e in result['events'] if e['kind']=='instruction' and e.get('executed') is True))
    result['version'] = 3
    return result
