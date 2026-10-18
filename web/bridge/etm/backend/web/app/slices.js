/**
 * 长窗（一次采集 + 自动分窗）记录的**分片时间轴**：纯逻辑 + 可选 DOM 渲染。
 *
 * 数据契约（见 `docs/long-window-slicing.md`）：
 *   session.json / decoded.json 的 `slices[]`，每片
 *   `{index, t0, t1, instructions, packets, cycles, packetIssues, csvBytes, seqStart, seqEnd}`。
 *   `seqStart`/`seqEnd` 是该片事件在记录级 `events.jsonl` 里的**闭区间**，各片首尾相接、
 *   整体覆盖 `0..eventCount-1`（空片为 `seqEnd = seqStart - 1`）。
 *
 * 为什么单独一个模块：布局百分比、标签格式化、范围裁剪都是纯函数，**不碰浏览器全局**，
 * 这样 Node 的 `node:test` 能直接 import 断言（`etm/tools/tests/slices-timeline.test.mjs`）。
 * DOM 渲染放在文件末尾的 `renderSliceTimeline()` 里，只在真正调用时用 `container.ownerDocument`。
 */

/** 记录里"分片"要能被前端使用的最小条件：至少 2 片，且每片都有整数 seqStart/seqEnd。 */
export const MIN_SLICES = 2;

const isInteger = value => typeof value === 'number' && Number.isInteger(value);
const toNumber = value => {
  if(value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
};

/** 去掉小数末尾多余的 0（"1.20" → "1.2"、"1.00" → "1"；"1000" 不动）。 */
function trimFixed(value, digits){
  const text = value.toFixed(digits);
  return text.includes('.') ? text.replace(/0+$/, '').replace(/\.$/, '') : text;
}

/** 紧凑计数：`4_370_000` → `4.37M`、`1_200` → `1.2k`；用于时间轴段上的标签。 */
export function formatCount(value){
  const number = toNumber(value);
  if(number === null) return '—';
  const size = Math.abs(number);
  if(size >= 1e9) return trimFixed(number / 1e9, 2) + 'G';
  if(size >= 1e6) return trimFixed(number / 1e6, 2) + 'M';
  if(size >= 1e3) return trimFixed(number / 1e3, 2) + 'k';
  return String(Math.round(number));
}

/** 带千分位的整数：`3900000` → `3,900,000`；用于悬停说明里的精确值。 */
export function formatInteger(value){
  const number = toNumber(value);
  if(number === null) return '—';
  return String(Math.round(number)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

/** 字节数：`504_000_000` → `480.6 MB`；用于 CSV 大小。 */
export function formatBytes(value){
  const number = toNumber(value);
  if(number === null || number < 0) return '—';
  if(number < 1024) return Math.round(number) + ' B';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let size = number / 1024, unit = 0;
  while(size >= 1024 && unit < units.length - 1){ size /= 1024; unit++; }
  return trimFixed(size, size < 10 ? 2 : 1) + ' ' + units[unit];
}

/**
 * 分片时间标签：同一段用同一个单位，避免"0.0 s–200 ms"这种混排。
 * `0, 0.2` → `0.0–0.2 s`；`0, 0.005` → `0–5 ms`（小于 10 ms 用毫秒）。
 * 精度跟着片宽走：0.2 s 的片给 0.1 s 分辨率，0.05 s 的片给 0.01 s，不会把窄片标成同一个值。
 */
export function formatSpan(t0, t1){
  const start = toNumber(t0), end = toNumber(t1);
  if(start === null || end === null) return '—';
  const low = Math.min(start, end), high = Math.max(start, end), width = high - low;
  if(high < 0.01) return `${trimFixed(low * 1000, 2)}–${trimFixed(high * 1000, 2)} ms`;
  const digits = high < 10 ? (width >= 0.1 ? 1 : 2) : (width < 1 ? 1 : 0);
  return `${low.toFixed(digits)}–${high.toFixed(digits)} s`;
}

/** 单值时长（摘要用）：`0.2` → `0.2 s`、`0.005` → `5 ms`。 */
export function formatDuration(seconds){
  const number = toNumber(seconds);
  if(number === null || number < 0) return '—';
  if(number < 0.01) return trimFixed(number * 1000, 2) + ' ms';
  return number.toFixed(number < 10 ? 1 : 0) + ' s';
}

/** 一片的时长；t0/t1 缺失或 t1 < t0（异常数据）都按 0 处理，不让时间轴反向。 */
function sliceDuration(slice){
  const t0 = toNumber(slice?.t0), t1 = toNumber(slice?.t1);
  if(t0 === null || t1 === null) return 0;
  return Math.max(0, t1 - t0);
}

/**
 * 布局：每片 `{left, width}` 百分比，宽度按 `(t1-t0)` 占总时长比例。
 *   · 总时长为 0（或全部片缺失 t0/t1）→ 退化成等宽，保证仍能点；
 *   · 单片 t0 > t1 / 缺失 → 该片宽度 0（数据异常，不虚构时长）；
 *   · 最后一段吃掉浮点误差，避免拼接处出现 1e-14% 的缝。
 */
export function sliceLayout(slices){
  const list = Array.isArray(slices) ? slices : [];
  const count = list.length;
  if(!count) return [];
  const durations = list.map(sliceDuration);
  const total = durations.reduce((sum, value) => sum + value, 0);
  if(!(total > 0)){
    return list.map((slice, index) => ({
      index, left: index * 100 / count, width: 100 / count,
      t0: toNumber(slice?.t0), t1: toNumber(slice?.t1), fallback: true,
    }));
  }
  const items = [];
  let cursor = 0;
  list.forEach((slice, index) => {
    const width = durations[index] / total * 100;
    const last = index === count - 1;
    items.push({
      index, left: cursor, width: last ? Math.max(0, 100 - cursor) : width,
      t0: toNumber(slice?.t0), t1: toNumber(slice?.t1), fallback: false,
    });
    cursor += width;
  });
  return items;
}

/**
 * 记录 → 时间轴模型；不能渲染时返回 `null`（调用方直接隐藏整条时间轴，不占位、不报错）。
 * 隐藏条件：没有 `slices`、只有 1 片、任一片缺整数 `seqStart`/`seqEnd`（旧记录/旧版本产物）。
 */
export function normalizeSliceRecord(record){
  const list = record?.slices;
  if(!Array.isArray(list) || list.length < MIN_SLICES) return null;
  const slices = [];
  for(let index = 0; index < list.length; index++){
    const raw = list[index];
    if(!raw || typeof raw !== 'object') return null;
    if(!isInteger(raw.seqStart) || !isInteger(raw.seqEnd)) return null;
    slices.push({
      index: isInteger(raw.index) ? raw.index : index,
      t0: toNumber(raw.t0),
      t1: toNumber(raw.t1),
      seqStart: raw.seqStart,
      seqEnd: raw.seqEnd,
      instructions: toNumber(raw.instructions) ?? 0,
      packets: toNumber(raw.packets) ?? 0,
      cycles: toNumber(raw.cycles) ?? 0,
      packetIssues: toNumber(raw.packetIssues) ?? 0,
      csvBytes: toNumber(raw.csvBytes) ?? null,
    });
  }
  const duration = slices.reduce((sum, slice) => sum + sliceDuration(slice), 0);
  return {slices, layout: sliceLayout(slices), count: slices.length, duration};
}

/**
 * 把 seq 区间裁到记录范围内：负起点抬到 0、终点超过 `eventCount-1` 截断；
 * 起点整段都在记录之外、或区间反序（异常）→ `null`（不生成非法浏览范围）。
 */
export function clampSeqRange(range, eventCount){
  if(!range) return null;
  const rawStart = toNumber(range.start), rawEnd = toNumber(range.end);
  if(rawStart === null || rawEnd === null) return null;
  let start = Math.trunc(rawStart), end = Math.trunc(rawEnd);
  if(start > end) return null;
  start = Math.max(0, start);
  end = Math.max(start, end);
  const total = toNumber(eventCount);
  if(total !== null && total > 0){
    const last = Math.floor(total) - 1;
    if(start > last) return null;
    end = Math.min(end, last);
  }
  return {start, end};
}

/** 某片的浏览范围；空片（seqEnd < seqStart）或缺字段返回 `null`（该段不可点）。 */
export function sliceSeqRange(slice, eventCount){
  if(!slice) return null;
  if(!isInteger(slice.seqStart) || !isInteger(slice.seqEnd)) return null;
  if(slice.seqEnd < slice.seqStart) return null;
  return clampSeqRange({start: slice.seqStart, end: slice.seqEnd}, eventCount);
}

/** 当前浏览范围恰好等于哪一片；找不到返回 -1（用户手动缩放/没选片）。 */
export function sliceIndexForRange(slices, range, eventCount){
  if(!Array.isArray(slices) || !range) return -1;
  const wanted = clampSeqRange(range, eventCount);
  if(!wanted) return -1;
  return slices.findIndex(slice => {
    const own = sliceSeqRange(slice, eventCount);
    return !!own && own.start === wanted.start && own.end === wanted.end;
  });
}

/** 悬停说明：片号 + 时长 + seq 区间 + 指令/包/周期/包问题 + CSV 大小。 */
export function sliceTooltip(slice, count){
  if(!slice) return '';
  return [
    `第 ${slice.index + 1}/${count} 片`,
    `时间 ${formatSpan(slice.t0, slice.t1)}`,
    `事件 #${formatInteger(slice.seqStart)}–#${formatInteger(slice.seqEnd)}`,
    `指令 ${formatInteger(slice.instructions)}`,
    `包 ${formatInteger(slice.packets)}`,
    `周期 ${formatInteger(slice.cycles)}`,
    `包问题 ${formatInteger(slice.packetIssues)}`,
    `CSV ${formatBytes(slice.csvBytes)}`,
  ].join('\n');
}

/**
 * 渲染水平分片时间轴（DOM）。`model` 来自 `normalizeSliceRecord()`；
 * `current` 是当前选中的片序号（-1 = 未选）；`onSelect(index)` 由调用方决定怎么过滤，
 * `onSelect(-1)` 表示"全部"。样式只用页面现有 CSS 变量/类（见 style.css + main-tool-theme.css）。
 *
 * 纯计算都在上面的导出函数里，这里只做 createElement，不做任何数值推导。
 */
export function renderSliceTimeline(container, model, options = {}){
  if(!container) return;
  if(!model || !model.slices?.length){
    container.hidden = true;
    container.replaceChildren();
    return;
  }
  const doc = container.ownerDocument;
  const current = Number.isInteger(options.current) ? options.current : -1;
  const onSelect = typeof options.onSelect === 'function' ? options.onSelect : null;

  const head = doc.createElement('div');
  head.className = 'slice-timeline-head';
  const title = doc.createElement('strong');
  title.textContent = '长窗分片';
  const state = doc.createElement('span');
  state.className = 'slice-timeline-state';
  state.textContent = current >= 0
    ? `当前：第 ${current + 1}/${model.count} 片`
    : `共 ${model.count} 片 · ${formatDuration(model.duration)}`;
  const all = doc.createElement('button');
  all.type = 'button';
  all.className = 'slice-timeline-all';
  all.textContent = '全部';
  all.title = '显示整条记录（清除分片过滤）';
  all.disabled = current < 0;
  all.onclick = () => { if(onSelect) onSelect(-1); };
  head.append(title, state, all);

  const track = doc.createElement('div');
  track.className = 'slice-timeline-track';
  track.setAttribute('role', 'group');
  track.setAttribute('aria-label', '长窗分片时间轴（宽度按片时长）');

  model.layout.forEach((item, position) => {
    const slice = model.slices[position];
    const selected = position === current;
    const segment = doc.createElement('button');
    segment.type = 'button';
    segment.className = 'slice-segment' + (selected ? ' selected' : '');
    segment.style.left = item.left + '%';
    segment.style.width = item.width + '%';
    segment.dataset.slice = String(slice.index);
    segment.setAttribute('aria-pressed', String(selected));
    segment.title = sliceTooltip(slice, model.count);
    segment.disabled = sliceSeqRange(slice, options.eventCount) === null;
    const span = doc.createElement('span');
    span.className = 'slice-segment-span';
    span.textContent = formatSpan(slice.t0, slice.t1);
    const count = doc.createElement('span');
    count.className = 'slice-segment-count';
    count.textContent = formatCount(slice.instructions);
    segment.append(span, count);
    segment.onclick = () => { if(onSelect) onSelect(position); };
    track.append(segment);
  });

  container.replaceChildren(head, track);
  container.hidden = false;
}
