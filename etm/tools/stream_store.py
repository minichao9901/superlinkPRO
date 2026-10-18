"""Replay JSONL without materializing a trace. File names are fixed, never user paths."""
import json
from pathlib import Path


class JsonLines:
    def __init__(self, path, count):
        self.path, self.count = Path(path), int(count) if count is not None else None

    def __len__(self):
        if self.count is None:raise TypeError("JSONL count not provided")
        return self.count

    def __iter__(self):
        count=0
        with self.path.open(encoding='utf-8') as stream:
            for line in stream:
                if line.strip():
                    count+=1
                    yield json.loads(line)
        if self.count is not None and count!=self.count:
            raise ValueError(f"JSONL记录数不一致：{self.path.name}，预期{self.count}，实际{count}；请重新解码")


# 记录级 `streamed.schema` 的语义：
#   2 = 单窗流式记录：events/packets/elements 三个 JSONL 都在记录根目录；
#   3 = 长窗（一次采集 + 自动分窗）记录：只有**合并后**的 events.jsonl 在根目录，
#       原始 packets/elements 留在 slices/NN/ 里（各片字节偏移互不相通，不能直接拼）。
STREAM_SCHEMAS = (2, 3)


def attach_streams(result, folder):
    meta = result.get('streamed', {})
    if meta.get('schema') not in STREAM_SCHEMAS:
        return result
    folder = Path(folder)
    sliced = meta.get('schema') == 3
    for key in ('events',) if sliced else ('events', 'packets', 'elements'):
        result[key] = JsonLines(folder / (key + '.jsonl'), meta[key])
    if sliced:
        # 保底：其它读取器仍按列表访问 packets/elements（整段视图只用事件流）。
        result.setdefault('packets', [])
        result.setdefault('elements', [])
    result['sources'] = json.loads((folder / 'sources.json').read_text(encoding='utf-8'))
    return result
