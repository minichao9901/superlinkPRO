"""Scan USB initialization register reads/writes and correlate them with ETM.

The ETM record does not contain data values, but every decoded instruction
event has `pc`/`file`/`line`/`mnemonic`.  This tool:

1. parses `USB_OTG_*` accesses in the selected init functions, classifying
   left-hand-side assignments as writes (and `|=`/`&=`/`^=` as read+write),
   and right-hand-side/condition uses as reads;
2. adds the inlined `dwc2_get_hwparams()` reads from `usb_dwc2_param.h`;
3. streams the ETM record and attaches execution order, instruction count and
   load/store PCs to each source line.

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
    wanted = {(row['file'], row['line']) for row in rows}
    info = collections.defaultdict(lambda: dict(count=0, first=None, loads=[], stores=[], function=''))
    for event in load_events(record):
        key = (pathlib.PurePosixPath(event.get('file', '')).name, event.get('line'))
        if key not in wanted:
            continue
        item = info[key]
        item['count'] += 1
        if item['first'] is None:
            item['first'] = event.get('seq')
        item['function'] = event.get('function') or item['function']
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
                   store_pcs=' '.join(hex(pc) for pc in item['stores'][:8]))
    rows.sort(key=lambda row: (row['seq'] is None, row['seq'] if row['seq'] is not None else 0,
                               row['function'], row['line']))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('w', newline='', encoding='utf8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['seq', 'function', 'file', 'line', 'kind', 'register',
                         'etm_instructions', 'etm_loads', 'etm_stores',
                         'load_pcs', 'store_pcs', 'source'])
        for row in rows:
            writer.writerow([row['seq'] if row['seq'] is not None else '',
                             row['function'], row['file'], row['line'], row['kind'],
                             row['register'], row['etm_instructions'],
                             row['etm_loads'], row['etm_stores'],
                             row['load_pcs'], row['store_pcs'], row['source']])
    executed = [row for row in rows if row['seq'] is not None]
    kinds = collections.Counter(row['kind'] for row in rows)
    print(f'source rows: {len(rows)} (read={kinds["read"]}, write={kinds["write"]}, rw={kinds["rw"]})')
    print(f'executed in {args.record.name}: {len(executed)}')
    print(f'csv: {args.out}')
    for row in executed:
        print(f"  {row['seq']:>7} {row['function'][:24]:<24} {row['file']:<18} {row['line']:>4}"
              f" {row['kind']:<5} {row['register'][:48]:<48} instr={row['etm_instructions']:<3}"
              f" ld={row['etm_loads']} st={row['etm_stores']}")


if __name__ == '__main__':
    main()
