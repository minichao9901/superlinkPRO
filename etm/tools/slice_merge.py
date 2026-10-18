"""长窗（一次采集 + 自动分窗）记录的合并工具。

`server.export_slices()` 逐片导出/解码后，各片是**独立解码**的：
  · `seq` 从 1 起；
  · `cycles` 从 0 起；
  · events.jsonl 里没有片号。
合并 = 按片序追加到记录级 events.jsonl，同时把 `seq` 递增、`cycles` 累加、写上 `slice` 字段，
这样序列/火焰/函数/热点等既有视图不改一行就能读"整段"。

纯逻辑、无 IO 依赖硬件，单测见 `tools/test_slices.py`。
"""
import json
import pathlib


def merge_slice_events(dest, slice_dir, index, seq_off=0, cyc_off=0):
    """把一片的 events.jsonl 合并进 dest；返回该片之后的 (seq_off, cyc_off)。"""
    local_seq = local_cyc = 0
    mode = 'w' if index == 0 else 'a'
    src = pathlib.Path(slice_dir) / 'events.jsonl'
    with pathlib.Path(dest).open(mode, encoding='utf8') as out, src.open(encoding='utf8') as fh:
        for line in fh:
            if not line.startswith('{'):
                continue
            e = json.loads(line)
            s = int(e.get('seq') or 0)
            local_seq = max(local_seq, s)
            e['seq'] = s + seq_off
            if 'cycles' in e:
                c = int(e.get('cycles') or 0)
                local_cyc = max(local_cyc, c)
                e['cycles'] = c + cyc_off
            e['slice'] = index
            out.write(json.dumps(e, ensure_ascii=False) + '\n')
    # 注意：seq 本身就是"第几条"，所以下一片的偏移是 seq_off + local_seq（**不要 +1**，
    # 加了会跳号：3 条事件的片之后偏移变 4，下片第一条 1 会变成 5 而不是 4）。
    return seq_off + local_seq, cyc_off + local_cyc


def aggregate_slices(base, slices, step, la_filter=None):
    """把逐片结果合成记录级 decoded.json（合并 summary/quality + slices[]）。"""
    out = dict(base or {})
    summary = dict(out.get('summary') or {})
    summary.update(
        instructions=sum(int(s.get('instructions') or 0) for s in slices),
        packets=sum(int(s.get('packets') or 0) for s in slices),
        totalCycles=sum(int(s.get('cycles') or 0) for s in slices),
    )
    quality = dict(out.get('quality') or {})
    quality['packetIssues'] = sum(int(s.get('packetIssues') or 0) for s in slices)
    out.update(summary=summary, quality=quality, slices=slices, sliceSeconds=step,
               captureScope='sliced-window', laFilter=la_filter)
    return out
