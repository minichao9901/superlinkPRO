"""Correlate USB register write sites in source with an ETM record.

The ETM record does not contain load/store addresses or values, but each
decoded instruction event carries `file`/`line`/`pc`/`mnemonic`.  This tool:

1. parses `USB_OTG_*` assignments on the left-hand side of an assignment
   (direct USB register writes) from a C source file;
2. streams the record's `events.jsonl` and counts instruction events per
   source line (plus store PCs whose mnemonic starts with `str`);
3. writes a CSV with every static write site and whether it executed.

Usage (from the repository root):

    python -X utf8 etm/tools/usb_write_sites.py \
        --source etm/firmware/stm32h743_cdc_trace/cherryusb/port/dwc2/usb_dc_dwc2.c \
        --record C:/Users/Administrator/Downloads/etm-bridge-kit/data/records/capture-... \
        --out docs/usb-register-write-sites.csv

This is intentionally source-level: it finds the *program's* write sites even
when the call chain is deep.  It does not recover the written values.
"""
import argparse
import collections
import csv
import json
import pathlib
import re

FUNC_RE = re.compile(
    r'^\s*(?:static\s+)?(?:inline\s+)?'
    r'(?:int|void|uint\d+_t|int\d+_t|bool|size_t|struct\s+\w+\s*\*?|const\s+\w+\s*\*?)'
    r'\s+(\w+)\s*\([^;]*$')
OP_RE = re.compile(r'(?<![=!<>])(\|=|&=|\^=|=)(?!=)')
USB_RE = re.compile(r'USB_OTG_(?:GLB|DEV|INEP|OUTEP|FIFO|PCGCCTL)')
REG_RE = re.compile(r'(USB_OTG_(?:GLB|DEV|INEP|OUTEP|FIFO|PCGCCTL)'
                    r'(?:\([^)]*\))?\s*(?:->\s*\w+|\[[^\]]*\])?)')


def parse_source(path):
    function = None
    sites = []
    for number, line in enumerate(path.read_text(encoding='utf8', errors='replace').splitlines(), 1):
        match = FUNC_RE.match(line)
        if match:
            function = match.group(1)
        if 'USB_OTG_' not in line:
            continue
        op = OP_RE.search(line)
        if not op:
            continue
        left = line[:op.start()]
        if not USB_RE.search(left):
            continue
        reg = REG_RE.search(left)
        sites.append(dict(line=number, function=function or '?', op=op.group(1),
                          register=(reg.group(1) if reg else left.strip())[:120],
                          source=line.strip()[:240]))
    return sites


def scan_record(record, basename):
    """Return {line: {count, first_seq, function, stores[]}} for one source file."""
    info = collections.defaultdict(lambda: dict(count=0, first=None, function='', stores=[]))
    path = record / 'events.jsonl'
    with path.open(encoding='utf8') as stream:
        for line in stream:
            if basename not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('kind') != 'instruction':
                continue
            line_no = event.get('line')
            item = info[line_no]
            item['count'] += 1
            if item['first'] is None:
                item['first'] = event.get('seq')
            item['function'] = event.get('function') or item['function']
            if str(event.get('mnemonic', '')).startswith('str'):
                item['stores'].append(event.get('pc'))
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=pathlib.Path)
    parser.add_argument('--record', required=True, type=pathlib.Path)
    parser.add_argument('--out', required=True, type=pathlib.Path)
    parser.add_argument('--basename', default=None,
                        help='source basename as recorded in events.jsonl (default: source name)')
    args = parser.parse_args()
    basename = args.basename or args.source.name
    sites = parse_source(args.source)
    info = scan_record(args.record, basename)
    executed = [s for s in sites if info[s['line']]['count']]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('w', newline='', encoding='utf8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['line', 'function', 'op', 'register', 'executed',
                         'etm_instructions', 'etm_stores', 'first_seq', 'store_pcs', 'source'])
        for site in sorted(sites, key=lambda s: (s['function'], s['line'])):
            item = info[site['line']]
            writer.writerow([site['line'], site['function'], site['op'], site['register'],
                             bool(item['count']), item['count'], len(item['stores']),
                             item['first'], ' '.join(hex(pc) for pc in item['stores'][:8]),
                             site['source']])
    print(f'static sites: {len(sites)}')
    print(f'executed in {args.record.name}: {len(executed)}')
    print(f'csv: {args.out}')
    for site in sorted(executed, key=lambda s: info[s['line']]['first'] if info[s['line']]['first'] is not None else 0):
        item = info[site['line']]
        print(f"  seq {item['first']:>7}  line {site['line']:>5}  {item['function']:<28}"
              f" {site['op']:2} {site['register']:<55} instr={item['count']:<3} stores={len(item['stores'])}")


if __name__ == '__main__':
    main()
