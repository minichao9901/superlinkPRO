"""离线重建长窗（自动分窗）记录的合并产物 —— **不重新解码、不碰 LA**。

用途：R01 之前产出的记录里，`events.jsonl` 的 `seq` 在片边界撞号、`cycles` 被加了偏移、
`decoded.json` 没有 `streamed.schema=3` 且 `summary.totalCycles` 是错的。
各片原始产物（`slices/NN/`）仍是好的，所以只要按新规则**重放一次合并**即可修好：

    python -X utf8 etm/tools/refinalize_slices.py etm/records/capture-xxxxxxxx

安全：先写 `events.jsonl.rebuild`，成功后原子替换；中途失败保留原文件。
大记录（1 s ≈ 5.7 GB）重放约 1~2 分钟（只做 JSON 解析 + 重写，没有解码）。
"""
import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from slice_merge import merge_slice_events, aggregate_slices  # noqa: E402


def rebuild(record):
    record = pathlib.Path(record)
    meta = json.loads((record / 'session.json').read_text(encoding='utf8'))
    step = float(meta.get('sliceSeconds') or 0.2)
    seconds = float(meta.get('seconds') or 0)
    folders = sorted(p for p in (record / 'slices').glob('[0-9][0-9]') if p.is_dir())
    if not folders:
        raise SystemExit(f'{record} 没有 slices/NN，不能重建')
    slices, parts, seq_off = [], [], 0
    temporary = record / 'events.jsonl.rebuild'
    try:
        for index, folder in enumerate(folders):
            decoded = folder / 'decoded.json'
            part = json.loads(decoded.read_text(encoding='utf8')) if decoded.is_file() else {}
            # 与 server.export_slices() 同一契约：seqStart/seqEnd 是本片在记录级 events.jsonl
            # 里的闭区间（合并前 seq_off 起，合并后 seq_off - 1 止），空片为 seqStart-1。
            seq_start = seq_off
            seq_off = merge_slice_events(temporary, folder, index, seq_off)
            timing = (part.get('analysis') or {}).get('timing') or {}
            summary = part.get('summary') or {}
            quality = part.get('quality') or {}
            csv = folder / 'capture.csv'
            slices.append(dict(index=index, t0=index * step, t1=min(seconds, (index + 1) * step),
                               csvBytes=csv.stat().st_size if csv.is_file() else 0,
                               instructions=int(summary.get('instructions') or 0),
                               packets=int(summary.get('packets') or 0),
                               cycles=int(timing.get('totalCycles') or 0),
                               packetIssues=int(quality.get('packetIssues') or 0),
                               seqStart=seq_start, seqEnd=seq_off - 1))
            parts.append(part)
            print(f'  片 {index:02d}: {slices[-1]["instructions"]:,} 指令 · '
                  f'{slices[-1]["cycles"]:,} 周期', flush=True)
        base = {}
        first = folders[0] / 'decoded.json'
        if first.is_file():
            base = json.loads(first.read_text(encoding='utf8'))
        out = aggregate_slices(base, slices, step, meta.get('laFilter'), parts)
        out.pop('capture', None)          # 多片记录没有单一 capture.csv
        out['streamed'] = dict(schema=3, events=seq_off, slices=len(slices),
                               packets=sum(int(s.get('packets') or 0) for s in slices))
        out['sliceSeconds'] = step
        # 原子替换：Windows 上先删目标再改名（没有别的进程在读时会成功）
        os.replace(temporary, record / 'events.jsonl')
        (record / 'slices.json').write_text(
            json.dumps(dict(sliceSeconds=step, seconds=seconds, slices=slices), ensure_ascii=False),
            encoding='utf8')
        (record / 'decoded.json').write_text(json.dumps(out, ensure_ascii=False), encoding='utf8')
        meta.update(slices=slices, sliceSeconds=step, longWindow=True, decodeStatus='done',
                    summary=out['summary'], captureScope='sliced-window', eventCount=seq_off)
        meta.pop('error', None)
        meta.pop('errorStage', None)
        (record / 'session.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf8')
        # 旧索引作废（下次浏览会按新签名重建）
        (record / 'browse-v3.sqlite').unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)
    print(f'完成：{len(slices)} 片 · {out["summary"]["instructions"]:,} 指令 · '
          f'{out["summary"]["totalCycles"]:,} 周期 · {seq_off:,} 条事件')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('record')
    rebuild(parser.parse_args().record)
