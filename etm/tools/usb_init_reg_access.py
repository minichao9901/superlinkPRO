"""Scan USB initialization register reads/writes and correlate them with ETM.

The ETM record does not contain data values, but every decoded instruction
event has `pc`/`file`/`line`/`mnemonic` and an execution state.  This tool:

1. parses `USB_OTG_*` accesses in the selected init functions, classifying
   left-hand-side assignments as writes (and `|=`/`&=`/`^=` as read+write),
   and right-hand-side/condition uses as reads;
2. adds the inlined `dwc2_get_hwparams()` reads from `usb_dwc2_param.h`;
3. streams the ETM record and attaches execution order, instruction count and
   load/store PCs to each source line.

Execution-state filter (review finding 2): only instructions whose
`executionState` is `executed` (or the older schema's `executed is True`)
count as **confirmed accesses**.  `not-taken` predicate failures and
`unknown` conditional results are counted separately as **candidate
accesses**; they never enter `seq`/`etm_instructions`/`etm_loads`/`etm_stores`.
Even for confirmed instructions this remains a per-source-line candidate
access list: the trace has no data values, so it cannot prove the concrete
read transaction or the value read.

Usage:

    python -X utf8 etm/tools/usb_init_reg_access.py \
        --source etm/firmware/stm32h743_cdc_trace/cherryusb/port/dwc2/usb_dc_dwc2.c \
        --header etm/firmware/stm32h743_cdc_trace/cherryusb/port/dwc2/usb_dwc2_param.h \
        --record etm/records/capture-4395612c313f47ce97af1e1c17e0a0d5 \
        --out docs/usb-init-register-access.csv
"""
import argparse
import collections
import csv
import json
import pathlib
import re

DEFAULT_FUNCTIONS = (
    'usb_dc_init', 'usb_dc_low_level_init', 'usb_dc_deinit',
    'dwc2_core_init', 'dwc2_reset', 'dwc2_set_mode',
    'dwc2_set_turnaroundtime', 'dwc2_set_txfifo',
    'dwc2_flush_txfifo', 'dwc2_flush_rxfifo',
    'dwc2_get_hwparams', 'dwc2_get_user_params',
)
FUNC_RE = re.compile(
    r'^\s*(?:static\s+)?(?:inline\s+)?'
    r'(?:int|void|uint\d+_t|int\d+_t|bool|size_t|struct\s+\w+\s*\*?|const\s+\w+\s*\*?)'
    r'\s+(\w+)\s*\([^;]*$')
OP_RE = re.compile(r'(?<![=!<>])(\|=|&=|\^=|=)(?!=)')
USB_RE = re.compile(
    r'USB_OTG_(?:GLB|DEV|INEP|OUTEP|FIFO|PCGCCTL)'
    r'(?:\((?:[^()]|\([^()]*\))*\))?\s*(?:->\s*\w+|\[[^\]]*\])?')
HEADER_READ_RE = re.compile(r'dwc2_readl\(reg_base\s*\+\s*(\w+)_OFFSET\)')
EXECUTED = 'executed'
NOT_TAKEN = 'not-taken'
UNKNOWN = 'unknown'
# 既有 CSV 列保持原位；新增列追加在末尾，供“确定访问 vs 候选覆盖”对照。
EXTRA_COLUMNS = ('etm_not_taken', 'etm_unknown', 'etm_instructions_total', 'candidate_seq')


def execution_state(event):
    """指令执行状态；旧版 decoded.json 只有 `executed` 布尔值时按它归一。"""
    state = event.get('executionState')
    if state in (EXECUTED, NOT_TAKEN, UNKNOWN):
        return state
    executed = event.get('executed')
    if executed is True:
        return EXECUTED
    if executed is False:
        return NOT_TAKEN
    return UNKNOWN


def scan_source(path, functions):
    """Yield one row per USB_OTG occurrence in selected functions."""
    rows = []
    function = None
    for number, line in enumerate(path.read_text(encoding='utf8', errors='replace').splitlines(), 1):
        match = FUNC_RE.match(line)
        if match:
            function = match.group(1)
        if function not in functions or 'USB_OTG_' not in line:
            continue
        op = OP_RE.search(line)
        for occurrence in USB_RE.finditer(line):
            if op is None:
                kind = 'read'
            elif occurrence.start() >= op.start():
                kind = 'read'
            elif op.group(1) in ('|=', '&=', '^='):
                kind = 'rw'
            else:
                kind = 'write'
            rows.append(dict(file=path.name, line=number, function=function,
                             kind=kind, register=occurrence.group(0).strip(),
                             source=line.strip()[:240]))
    return rows


def scan_header(path):
    """The inlined hwparams reader uses reg_base + *_OFFSET, not USB_OTG macros."""
    rows = []
    function = None
    for number, line in enumerate(path.read_text(encoding='utf8', errors='replace').splitlines(), 1):
        match = FUNC_RE.match(line)
        if match:
            function = match.group(1)
        if function != 'dwc2_get_hwparams':
            continue
        match = HEADER_READ_RE.search(line)
        if match:
            rows.append(dict(file=path.name, line=number, function=function, kind='read',
                             register='USB_OTG_GLB->' + match.group(1),
                             source=line.strip()[:240]))
    return rows


def load_events(record):
    path = record / 'events.jsonl'
    if path.exists():
        with path.open(encoding='utf8') as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('kind') == 'instruction':
                    yield event
        return
    decoded = record / 'decoded.json'
    data = json.loads(decoded.read_text(encoding='utf8'))
    for event in data.get('events', []):
        if event.get('kind') == 'instruction':
            yield event


def correlate(rows, record):
    """按源码行聚合 ETM 指令：确定访问只计 executed，not-taken/unknown 单独计数。

    trace 不含数据值；即使指令 executed，也只能证明访问过该源码行的寄存器地址，
    不能证明具体读事务或读到的值。候选覆盖（not-taken/unknown）单独输出。
    """
    wanted = {(row['file'], row['line']) for row in rows}
    def empty():
        return dict(count=0, first=None, candidate_first=None, loads=[], stores=[],
                    not_taken=0, unknown=0, total=0, function='')
    info = collections.defaultdict(empty)
    for event in load_events(record):
        key = (pathlib.PurePosixPath(event.get('file', '')).name, event.get('line'))
        if key not in wanted:
            continue
        item = info[key]
        state = execution_state(event)
        item['total'] += 1
        if item['candidate_first'] is None:
            item['candidate_first'] = event.get('seq')
        item['function'] = event.get('function') or item['function']
        if state == NOT_TAKEN:
            item['not_taken'] += 1
            continue
        if state != EXECUTED:
            item['unknown'] += 1
            continue
        item['count'] += 1
        if item['first'] is None:
            item['first'] = event.get('seq')
        mnemonic = str(event.get('mnemonic', ''))
        if mnemonic.startswith('ldr'):
            item['loads'].append(event.get('pc'))
        if mnemonic.startswith('str'):
            item['stores'].append(event.get('pc'))
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=pathlib.Path)
    parser.add_argument('--header', required=True, type=pathlib.Path)
    parser.add_argument('--record', required=True, type=pathlib.Path)
    parser.add_argument('--out', required=True, type=pathlib.Path)
    parser.add_argument('--functions', default=','.join(DEFAULT_FUNCTIONS))
    args = parser.parse_args()
    functions = {name.strip() for name in args.functions.split(',') if name.strip()}
    rows = scan_source(args.source, functions) + scan_header(args.header)
    info = correlate(rows, args.record)
    for row in rows:
        item = info[(row['file'], row['line'])]
        row.update(seq=item['first'], etm_instructions=item['count'],
                   etm_loads=len(item['loads']), etm_stores=len(item['stores']),
                   load_pcs=' '.join(hex(pc) for pc in item['loads'][:8]),
                   store_pcs=' '.join(hex(pc) for pc in item['stores'][:8]),
                   etm_not_taken=item['not_taken'], etm_unknown=item['unknown'],
                   etm_instructions_total=item['total'], candidate_seq=item['candidate_first'])
    rows.sort(key=lambda row: (row['seq'] is None,
                               row['seq'] if row['seq'] is not None
                               else (row['candidate_seq'] if row['candidate_seq'] is not None else 0),
                               row['function'], row['line']))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('w', newline='', encoding='utf8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['seq', 'function', 'file', 'line', 'kind', 'register',
                         'etm_instructions', 'etm_loads', 'etm_stores',
                         'load_pcs', 'store_pcs', 'source', *EXTRA_COLUMNS])
        for row in rows:
            writer.writerow([row['seq'] if row['seq'] is not None else '',
                             row['function'], row['file'], row['line'], row['kind'],
                             row['register'], row['etm_instructions'],
                             row['etm_loads'], row['etm_stores'],
                             row['load_pcs'], row['store_pcs'], row['source'],
                             row['etm_not_taken'], row['etm_unknown'],
                             row['etm_instructions_total'],
                             row['candidate_seq'] if row['candidate_seq'] is not None else ''])
    executed = [row for row in rows if row['seq'] is not None]
    candidates = [row for row in rows if row['seq'] is None and row['etm_instructions_total']]
    kinds = collections.Counter(row['kind'] for row in rows)
    not_taken_total = sum(row['etm_not_taken'] for row in rows)
    unknown_total = sum(row['etm_unknown'] for row in rows)
    print(f'source rows: {len(rows)} (read={kinds["read"]}, write={kinds["write"]}, rw={kinds["rw"]})')
    print(f'confirmed executed accesses in {args.record.name}: {len(executed)}')
    print(f'not-taken matches: {not_taken_total}, unknown matches: {unknown_total}, '
          f'unconfirmed candidate rows: {len(candidates)}')
    print('note: 按源码行聚合的候选访问；executed 只证明执行过该行，trace 不含数据值，'
          '不能证明具体读事务/数据。')
    print(f'csv: {args.out}')
    for row in executed:
        print(f"  {row['seq']:>7} {row['function'][:24]:<24} {row['file']:<18} {row['line']:>4}"
              f" {row['kind']:<5} {row['register'][:48]:<48} instr={row['etm_instructions']:<3}"
              f" ld={row['etm_loads']} st={row['etm_stores']}"
              f" (not-taken={row['etm_not_taken']} unknown={row['etm_unknown']})")
    for row in candidates[:10]:
        print(f"  candidate seq~{row['candidate_seq'] if row['candidate_seq'] is not None else '-'}"
              f" {row['function'][:24]:<24} {row['file']:<18} {row['line']:>4}"
              f" {row['kind']:<5} not-taken={row['etm_not_taken']} unknown={row['etm_unknown']}"
              f" total={row['etm_instructions_total']}")


if __name__ == '__main__':
    main()
