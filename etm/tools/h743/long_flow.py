"""长采集分片流式驱动：一次连续采集 → 按时间窗逐片导出/解码 → 合并成一份事件流。

为什么这样切：LA 缓冲可以放 1 s，但"一次导出 500 ms"会产出 GB 级 CSV，
而 trace_port.extract() 必须把整份 CSV 的跳变表读进内存。按窗导出后每片只有几十 MB，
内存恒定；解码侧本身就是流式（events.jsonl），所以整条链路对总时长的内存是 O(1)，
只有总耗时线性。

产物（<record>/merged/）：
  events.jsonl      所有窗事件按全局 seq 连续排列（带 window 字段）
  sources.json      各窗源码合并
  decoded.json      仅摘要（streamed.schema=2）+ 每窗明细
  etm-simplified.c  流式导出的简化 C

用法：
  python tools/h743/long_flow.py --record <rec> --start 0 --end 0.5 --window 0.05 --filter-ns 4
前置：LA 缓冲里要有一次连续采集（capture 时给一个小 export-span 即可，缓冲会保留到下次采集）。
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(r'E:\web-etm-trace')
sys.path.insert(0, str(ROOT / 'tools'))

from decode_stream import decode_capture_stream, export_simplified_stream   # noqa: E402

LA = ROOT / 'tools/la/kingst_la.py'


def export_window(out_csv, start, end):
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, '-u', '-X', 'utf8', str(LA), 'export',
               '--out', str(out_csv), '--channels', '0', '1', '2', '3', '4',
               '--time-span', str(start), str(end), '--no-kvdat']
    result = subprocess.run(command, capture_output=True, text=True,
                            encoding='utf-8', errors='replace', cwd=str(ROOT))
    if result.returncode:
        raise RuntimeError('LA 导出失败：' + (result.stderr or result.stdout)[-400:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--record', required=True)
    ap.add_argument('--start', type=float, default=0.0)
    ap.add_argument('--end', type=float, required=True)
    ap.add_argument('--window', type=float, default=0.05)
    ap.add_argument('--filter-ns', type=float, default=4.0)
    ap.add_argument('--fraction', type=float, default=0.25)
    ap.add_argument('--keep-csv', action='store_true')
    args = ap.parse_args()

    record = Path(args.record).resolve()
    meta = json.loads((record / 'meta.json').read_text(encoding='utf-8'))
    config = {k: int(v) for k, v in meta['etm'].items()}
    elf = record / 'firmware.elf'
    merged = record / 'merged'
    merged.mkdir(exist_ok=True)
    events_out = merged / 'events.jsonl'
    events_out.write_text('', encoding='utf-8')

    sources, counts = {}, {}
    total = dict(packets=0, ranges=0, instructions=0, executed=0, sourceMapped=0,
                 exceptions=0, gaps=0, packetIssues=0, unknownExecution=0)
    windows, seq_base = [], 0
    t, index = args.start, 0

    while t < args.end - 1e-9:
        stop = min(t + args.window, args.end)
        tag = f'w{index:02d}-{t:g}-{stop:g}'.replace('.', 'p')
        work = merged / tag
        csv_path = merged / f'{tag}.csv'
        print(f'[{index}] {t:.3f}~{stop:.3f}s 导出中…', flush=True)
        export_window(csv_path, t, stop)
        result = decode_capture_stream(csv_path, elf, work, config=config,
                                       fraction=args.fraction, glitch_ns=args.filter_ns)
        summary = result['summary']
        for key in ('packets', 'ranges', 'instructions', 'executed', 'sourceMapped',
                    'exceptions', 'gaps', 'packetIssues', 'unknownExecution'):
            total[key] += summary.get(key, 0)
        for name, value in summary.get('functions', {}).items():
            counts[name] = counts.get(name, 0) + value
        sources.update(json.loads((work / 'sources.json').read_text(encoding='utf-8')))
        moved = 0
        with (work / 'events.jsonl').open(encoding='utf-8') as src, \
                events_out.open('a', encoding='utf-8') as dst:
            for line in src:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                event['seq'] = seq_base + moved
                event['window'] = index
                dst.write(json.dumps(event, ensure_ascii=False) + '\n')
                moved += 1
        windows.append(dict(index=index, start=t, stop=stop, dir=str(work),
                            instructions=summary['instructions'],
                            packetIssues=summary['packetIssues'],
                            gaps=summary['gaps'], events=moved,
                            framing=result.get('framing'),
                            filter=(result.get('physical') or {}).get('software_filter')))
        print(f'    instr={summary["instructions"]} issues={summary["packetIssues"]} '
              f'gaps={summary["gaps"]} events={moved}', flush=True)
        seq_base += moved
        if not args.keep_csv:
            csv_path.unlink(missing_ok=True)
        t, index = stop, index + 1

    total['functions'] = counts
    (merged / 'sources.json').write_text(json.dumps(sources, ensure_ascii=False),
                                         encoding='utf-8')
    doc = dict(version=1, protocol='ETMv4',
               streamed=dict(schema=2, eventsFile='events.jsonl', events=seq_base,
                             windows=len(windows)),
               summary=total, windows=windows,
               basis='一次连续采集按时间窗导出并逐窗解码后合并；不是单次解码',
               filter=dict(software_ns=args.filter_ns, fraction=args.fraction))
    (merged / 'decoded.json').write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                                         encoding='utf-8')
    rows = export_simplified_stream(merged, merged / 'etm-simplified.c')
    print(f'\n合并完成：{len(windows)} 窗 / {seq_base} 事件 / 简化 C {rows} 行')
    print(f'  指令合计 {total["instructions"]}，包问题合计 {total["packetIssues"]}')
    print(f'  产物：{merged}')


if __name__ == '__main__':
    main()
