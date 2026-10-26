"""Exact execution-order / weighted call flame SVG, shared by Web and CLI.

No timeline buckets, depth truncation, source dependency or per-instruction time
inference. Uniform geometry is expanded just enough to fit every full label.
"""
import colorsys
from collections import Counter
import html
import math

MAX_FRAMES = 200000

def make_frames(events):
    """Only merge consecutive equal *prefixes*, keeping every instruction path.

    A changed parent invalidates all descendants even if their function names
    happen to match. Events break continuity. No averaging, LCP bucketing or
    made-up ancestors are used.
    """
    frames, active, previous_path = [], [], ()
    previous_seq = None
    counts = Counter()
    max_depth = 0
    for event in events:
        seq = int(event['seq'])
        if event['kind'] != 'instruction':
            active, previous_path, previous_seq = [], (), seq
            counts['events'] += 1
            continue
        path = tuple(event.get('callPath') or ())
        if not path:
            raise ValueError('Instruction lacks an exact callPath at #' + str(seq))
        if '多段调用（放大查看）' in path:
            raise ValueError('Binned paths must never be exported as exact paths')
        common = 0
        if previous_seq is not None and previous_seq + 1 == seq:
            for a, b in zip(previous_path, path):
                if a != b:
                    break
                common += 1
        active = active[:common]
        for depth, name in enumerate(path):
            if depth < common:
                frame = frames[active[depth]]
                frame['end'] = seq
                frame['instructions'] += 1
            else:
                frame = {'name': name, 'depth': depth, 'start': seq, 'end': seq,
                         'instructions': 1, 'parent': active[-1] if active else None}
                active.append(len(frames))
                frames.append(frame)
                if len(frames) > MAX_FRAMES:
                    raise ValueError('调用段超过200000个，请缩小执行序号区间后导出；不会省略调用深度')
        # Prove frame coverage matches each original instruction's complete path.
        assert tuple(frames[i]['name'] for i in active) == path
        counts['instructions'] += 1
        counts['unknown'] += event.get('executionState') == 'unknown'
        counts['notTaken'] += event.get('executionState') == 'not-taken'
        counts['fn:' + event.get('function', '')] += 1
        max_depth = max(max_depth, len(path))
        previous_path, previous_seq = path, seq
    for frame in frames:
        assert frame['instructions'] == frame['end'] - frame['start'] + 1
        if frame['parent'] is not None:
            parent = frames[frame['parent']]
            assert parent['start'] <= frame['start'] <= frame['end'] <= parent['end']
            assert parent['depth'] + 1 == frame['depth']
    return frames, counts, max_depth


def font_measurer():
    # Fixed advance + SVG textLength makes fitting independent of installed fonts.
    import unicodedata
    return lambda name: sum(14 if unicodedata.east_asian_width(c) in ('W','F') else 8 for c in str(name))


def frame_color(name, depth):
    if '未知' in name or '未证实' in name:
        return '#c4ccd8'
    value = 0
    for c in name:
        value = (31 * value + ord(c)) & 0xffffffff
    if value >= 2**31:
        value -= 2**32
    rgb = colorsys.hls_to_rgb((25 + abs(value) % 40) / 360, (58 + depth % 3 * 5) / 100, .78)
    return '#%02x%02x%02x' % tuple(round(c * 255) for c in rgb)


def svg_document(title, start, end, frames, depth, measure):
    if not frames:
        raise ValueError('所选区间没有带调用栈的指令记录')
    # A single common scale preserves identical X coordinates for parent/child.
    # Solve the tightest label constraint, instead of widening each rectangle.
    for f in frames:
        f['textWidth'] = float(measure(f['name']))
    critical = max(frames, key=lambda f: (f['textWidth'] + 16) / (f['end'] - f['start'] + 1))
    scale = (critical['textWidth'] + 16) / (critical['end'] - critical['start'] + 1)
    scale = math.ceil(scale * 1000) / 1000
    width = (end - start + 1) * scale + 32
    height = 72 + depth * 30
    esc = html.escape
    text = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.3f}" height="{height}" viewBox="0 0 {width:.3f} {height}">',
            f'<title>{esc(title)} — 按执行顺序，完整调用深度</title>',
            '<rect width="100%" height="100%" fill="#0d1117"/>',
            '<g font-family="Consolas,Microsoft YaHei,monospace" font-size="14">',
            f'<text x="16" y="22" fill="#d0d7de">{esc(title)} · #{start}～#{end} · {depth} 层（含未知祖先标记） · 横轴为执行序号</text>']
    for i, f in enumerate(frames):
        x = 16 + (f['start'] - start) * scale
        w = (f['end'] - f['start'] + 1) * scale - 2
        y = 40 + f['depth'] * 30
        assert f['textWidth'] + 12 <= w + 1e-6
        text.extend([f'<g data-frame="{i}" data-depth="{f["depth"]}" data-start="{f["start"]}" data-end="{f["end"]}">',
            f'<title>{esc(f["name"])} · #{f["start"]}～#{f["end"]} · {f["instructions"]} 条指令记录</title>',
            f'<rect x="{x:.3f}" y="{y}" width="{w:.3f}" height="26" rx="2" fill="{frame_color(f["name"],f["depth"])}"/>',
            f'<text x="{x+6:.3f}" y="{y+18}" fill="#403329" textLength="{f["textWidth"]:.3f}" lengthAdjust="spacingAndGlyphs">{esc(f["name"])}</text></g>'])
    text.extend([f'<text x="16" y="{height-8}" fill="#9aa4af" font-size="12">{len(frames)} 个连续调用段 · 无分桶、无截断、未推断逐指令耗时</text>', '</g></svg>'])
    return ''.join(text), {'width': width, 'height': height, 'pixelsPerSeq': scale,
        'tightestFrame': {key: critical[key] for key in ('name','start','end','textWidth')}}


def weighted_document(analysis, title, weight='records', root_path=''):
    """Keep the same weight ratios as the Web; enlarge one common X scale."""
    if weight not in ('records', 'cycles'):
        raise ValueError('权重必须为 records 或 cycles')
    if weight == 'cycles' and not (analysis.get('timing') or {}).get('timeFlameAvailable'):
        raise ValueError('所选区间没有可用的周期计数，不能导出周期权重图')
    root = analysis.get('flame')
    try:
        for part in str(root_path).split('.') if root_path else ():
            if not part.isdigit():
                raise ValueError()
            root = root['children'][int(part)]
    except (KeyError, TypeError, IndexError, ValueError):
        raise ValueError('聚合图放大节点路径无效') from None
    key = 'valueCycles' if weight == 'cycles' else 'value'
    value = lambda node: float(node.get(key) or 0)
    if not root or value(root) <= 0:
        raise ValueError('当前权重下没有可绘制调用段')
    frames = []
    def visit(node, x, fraction, depth):
        frames.append(dict(name=str(node['name']), x=x, fraction=fraction,
                           depth=depth, value=value(node)))
        if len(frames) > MAX_FRAMES:
            raise ValueError('调用节点超过200000个，请缩小区间后导出')
        children = sorted((c for c in node.get('children', []) if value(c) > 0),
                          key=value, reverse=True)
        if sum(value(c) for c in children) > value(node) + max(1e-8, value(node)*1e-10):
            raise ValueError('聚合调用树的子节点权重超过父节点')
        for child in children:
            part = fraction * value(child) / value(node)
            visit(child, x, part, depth+1)
            x += part
    visit(root, 0., 1., 0)
    measure = font_measurer()
    width = math.ceil(max((measure(f['name'])+16)/f['fraction'] for f in frames)*1000)/1000
    depth = max(f['depth'] for f in frames)+1
    height = 72+depth*30
    esc = html.escape
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width+32:.3f}" height="{height}" viewBox="0 0 {width+32:.3f} {height}">',
             f'<title>{esc(title)}</title><rect width="100%" height="100%" fill="#0d1117"/>',
             '<g font-family="Consolas,Microsoft YaHei,monospace" font-size="14">',
             f'<text x="16" y="22" fill="#d0d7de">{esc(title)} · 按调用权重（{"周期" if weight=="cycles" else "路径指令记录数"}）</text>']
    for i, f in enumerate(frames):
        x, y, w = 16+f['x']*width, 40+f['depth']*30, f['fraction']*width-2
        name, tw = esc(f['name']), measure(f['name'])
        parts.extend([f'<g data-frame="{i}" data-depth="{f["depth"]}"><title>{name} · {f["value"]:g}</title>',
                      f'<rect x="{x:.3f}" y="{y}" width="{w:.3f}" height="26" rx="2" fill="{frame_color(f["name"],f["depth"])}"/>',
                      f'<text x="{x+6:.3f}" y="{y+18}" fill="#403329" textLength="{tw:.3f}" lengthAdjust="spacingAndGlyphs">{name}</text></g>'])
    parts.extend([f'<text x="16" y="{height-8}" fill="#9aa4af" font-size="12">{len(frames)} 个调用节点 · 完整函数名 · 不含源码路径</text>', '</g></svg>'])
    return ''.join(parts), dict(width=width+32, height=height, frames=len(frames), depth=depth, weight=weight)


def export_svg(view, output, start=None, end=None, mode='timeline', weight='records', root_path=''):
    """Read exact events, never the lossy window.timeline display buckets."""
    if mode not in ('timeline', 'aggregate'):
        raise ValueError('火焰图模式必须为 timeline 或 aggregate')
    if (start is None) != (end is None):
        raise ValueError('必须同时指定区间起点和终点')
    info = view.overview()
    total = int(info.get('eventCount') or 0)
    low, high = (0, total-1) if start is None else (int(start), int(end))
    if not 0 <= low <= high < total:
        raise ValueError('火焰图导出区间超出记录范围')
    title = f'ETM 火焰图 · #{low}～#{high}'
    if mode == 'aggregate':
        analysis = info['analysis'] if start is None else view.window(low, high)['analysis']
        svg, details = weighted_document(analysis, title, weight, root_path)
    else:
        if root_path or weight != 'records':
            raise ValueError('执行顺序图横轴为执行序号，不接受聚合节点路径或周期权重')
        if hasattr(view, 'iter_events'):
            events = view.iter_events(low, high)
        else:
            def pages():
                offset = 0
                while True:
                    page = view.page('events', offset, 500, '', False, low, high)
                    yield from page['rows']
                    offset += len(page['rows'])
                    if not page['rows'] or offset >= page['total']:
                        break
            events = pages()
        try:
            frames, counts, depth = make_frames(events)
        finally:
            if hasattr(events, 'close'):
                events.close()
        svg, details = svg_document(title, low, high, frames, depth, font_measurer())
        details.update(frames=len(frames), depth=depth, instructionPathsChecked=counts['instructions'])
    output.write_text(svg, encoding='utf8')
    return dict(mode=mode, fromSeq=low, toSeq=high, **details)

