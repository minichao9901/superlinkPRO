import test from 'node:test';
import assert from 'node:assert/strict';
import {
  MIN_SLICES,
  clampSeqRange,
  formatBytes,
  formatCount,
  formatDuration,
  formatInteger,
  formatSpan,
  normalizeSliceRecord,
  renderSliceTimeline,
  sliceIndexForRange,
  sliceLayout,
  sliceSeqRange,
  sliceTooltip,
} from '../../web/app/slices.js';

/** 时间轴布局：宽度按 (t1-t0) 占总时长比例。 */
test('布局按片时长占比分配宽度', () => {
  const layout = sliceLayout([
    {index: 0, t0: 0, t1: 0.2},
    {index: 1, t0: 0.2, t1: 0.4},
  ]);
  assert.equal(layout.length, 2);
  assert.equal(layout[0].left, 0);
  assert.equal(layout[0].width, 50);
  assert.equal(layout[1].left, 50);
  assert.equal(layout[1].width, 50);
});

test('时长不均时宽度按比例（0.1 s / 0.3 s = 25% / 75%）', () => {
  const layout = sliceLayout([
    {t0: 0, t1: 0.1},
    {t0: 0.1, t1: 0.4},
  ]);
  assert.ok(Math.abs(layout[0].width - 25) < 1e-9);
  assert.ok(Math.abs(layout[1].width - 75) < 1e-9);
  assert.ok(Math.abs(layout[1].left - 25) < 1e-9);
});

test('每段首尾相接、总宽 100%（浮点误差被最后一段吃掉）', () => {
  const layout = sliceLayout([
    {t0: 0, t1: 0.2},
    {t0: 0.2, t1: 0.4},
    {t0: 0.4, t1: 0.6},
    {t0: 0.6, t1: 0.8},
    {t0: 0.8, t1: 1.0},
  ]);
  for(let i = 1; i < layout.length; i++) {
    assert.equal(layout[i].left, layout[i - 1].left + layout[i - 1].width);
  }
  const end = layout[layout.length - 1].left + layout[layout.length - 1].width;
  assert.ok(Math.abs(end - 100) < 1e-9, `总宽应约等于 100%，实际 ${end}`);
});

test('总时长为 0 时退化成等宽，而不是除零', () => {
  const layout = sliceLayout([{t0: 0, t1: 0}, {t0: 0, t1: 0}]);
  assert.deepEqual(layout.map(item => item.width), [50, 50]);
  assert.ok(layout.every(item => item.fallback));
  const missing = sliceLayout([{}, {}]);
  assert.deepEqual(missing.map(item => item.width), [50, 50]);
});

test('t0 > t1 的异常片宽度为 0，正常片占满（不虚构时长、不反向）', () => {
  const layout = sliceLayout([
    {t0: 0.2, t1: 0.1},
    {t0: 0, t1: 0.2},
  ]);
  assert.equal(layout[0].width, 0);
  assert.equal(layout[1].width, 100);
  assert.equal(layout[1].left, 0);
});

test('空数组不产生布局', () => {
  assert.deepEqual(sliceLayout([]), []);
  assert.deepEqual(sliceLayout(null), []);
});

/** 标签：秒 / 毫秒 / 千分位。 */
test('分片时间标签：秒单位 0.0–0.2 s', () => {
  assert.equal(formatSpan(0, 0.2), '0.0–0.2 s');
  assert.equal(formatSpan(0.2, 0.4), '0.2–0.4 s');
  assert.equal(formatSpan(0.8, 1.0), '0.8–1.0 s');
  assert.equal(formatSpan(12, 12.2), '12.0–12.2 s');
});

test('分片时间标签：小于 10 ms 用毫秒，窄片保留两位小数', () => {
  assert.equal(formatSpan(0, 0.005), '0–5 ms');
  assert.equal(formatSpan(0, 0.0005), '0–0.5 ms');
  assert.equal(formatSpan(0, 0.05), '0.00–0.05 s');
});

test('分片时间标签：缺字段 / 反向输入给占位符，不抛异常', () => {
  assert.equal(formatSpan(null, 0.2), '—');
  assert.equal(formatSpan(undefined, undefined), '—');
  assert.equal(formatSpan('x', 1), '—');
  assert.equal(formatSpan(0.4, 0.2), '0.2–0.4 s');
});

test('紧凑计数：4.37M / 1.2k / 千分位精确值', () => {
  assert.equal(formatCount(4_370_000), '4.37M');
  assert.equal(formatCount(1_200), '1.2k');
  assert.equal(formatCount(999), '999');
  assert.equal(formatCount(0), '0');
  assert.equal(formatCount(1_000_000_000), '1G');
  assert.equal(formatCount(undefined), '—');
  assert.equal(formatInteger(3_900_000), '3,900,000');
  assert.equal(formatInteger(999), '999');
  assert.equal(formatInteger(-1234), '-1,234');
  assert.equal(formatInteger(null), '—');
});

test('字节与时长格式化', () => {
  assert.equal(formatBytes(512), '512 B');
  assert.equal(formatBytes(480 * 1024 * 1024), '480 MB');
  assert.equal(formatBytes(1.5 * 1024 ** 3), '1.5 GB');
  assert.equal(formatBytes(null), '—');
  assert.equal(formatDuration(0.2), '0.2 s');
  assert.equal(formatDuration(0.005), '5 ms');
});

/** 旧记录 / 非长窗记录的降级：整条时间轴隐藏。 */
test('没有 slices / 只有 1 片 / 缺 seqStart 或 seqEnd → 不渲染', () => {
  assert.equal(normalizeSliceRecord({}), null);
  assert.equal(normalizeSliceRecord({slices: []}), null);
  assert.equal(normalizeSliceRecord({slices: [{seqStart: 0, seqEnd: 3}]}), null);
  const two = {slices: [{t0: 0, t1: 0.2, seqStart: 0, seqEnd: 2},
                        {t0: 0.2, t1: 0.4, seqStart: 3, seqEnd: 4}]};
  assert.equal(normalizeSliceRecord(two).count, MIN_SLICES);
  // 旧记录：旧版本导出没有 seq 区间 → 整条隐藏（而不是显示一段点不动的条）
  assert.equal(normalizeSliceRecord({slices: [{t0: 0, t1: 0.2}, {t0: 0.2, t1: 0.4}]}), null);
  assert.equal(normalizeSliceRecord({slices: [{t0: 0, t1: 0.2, seqStart: 0}, {
    t0: 0.2, t1: 0.4, seqStart: 1, seqEnd: 2}]}), null);
  // 非整数（字符串）同样视为缺失，避免把 "3" 当成 seq 写进 fromSeq
  assert.equal(normalizeSliceRecord({slices: [{t0: 0, t1: 0.2, seqStart: '0', seqEnd: 2},
                                              {t0: 0.2, t1: 0.4, seqStart: 3, seqEnd: 4}]}), null);
});

test('合法记录被规范化：序号/计数缺省 + 总时长', () => {
  const model = normalizeSliceRecord({
    slices: [
      {t0: 0, t1: 0.2, seqStart: 0, seqEnd: 2, instructions: 4_370_000,
       packets: 10, cycles: 20, packetIssues: 1, csvBytes: 1024},
      {t0: 0.2, t1: 0.4, seqStart: 3, seqEnd: 4},
    ],
  });
  assert.equal(model.count, 2);
  assert.equal(model.slices[0].index, 0);
  assert.equal(model.slices[1].index, 1);
  assert.equal(model.slices[1].instructions, 0);
  assert.ok(Math.abs(model.duration - 0.4) < 1e-9);
  assert.equal(model.layout[0].width, 50);
  assert.match(sliceTooltip(model.slices[0], model.count), /第 1\/2 片/);
  assert.match(sliceTooltip(model.slices[0], model.count), /4,370,000/);
  assert.match(sliceTooltip(model.slices[0], model.count), /CSV 1 KB/);
});

/** 范围裁剪。 */
test('范围裁剪：负起点、越界终点、反序、记录外起点', () => {
  assert.deepEqual(clampSeqRange({start: 3, end: 5}, 10), {start: 3, end: 5});
  assert.deepEqual(clampSeqRange({start: -2, end: 4}, 10), {start: 0, end: 4});
  assert.deepEqual(clampSeqRange({start: 3, end: 99}, 10), {start: 3, end: 9});
  assert.equal(clampSeqRange({start: 20, end: 30}, 10), null);
  assert.equal(clampSeqRange({start: 5, end: 3}, 10), null);
  assert.equal(clampSeqRange(null, 10), null);
  assert.equal(clampSeqRange({start: 'a', end: 3}, 10), null);
  // 记录总事件数未知时不裁剪上界（仍保证起点非负）
  assert.deepEqual(clampSeqRange({start: 0, end: 500}, null), {start: 0, end: 500});
});

test('某片的浏览范围：缺失 / 空片 → null；越界终点的截断', () => {
  assert.equal(sliceSeqRange(null, 10), null);
  assert.equal(sliceSeqRange({seqStart: 0, seqEnd: 3}, 10).end, 3);
  assert.equal(sliceSeqRange({seqStart: 0, seqEnd: -1}, 10), null);   // 空片
  assert.equal(sliceSeqRange({seqStart: 0}, 10), null);               // 缺 seqEnd
  assert.deepEqual(sliceSeqRange({seqStart: 2, seqEnd: 4}, 3), {start: 2, end: 2});
  assert.equal(sliceSeqRange({seqStart: 40, seqEnd: 50}, 10), null);
});

test('按范围反查片号：精确匹配才算选中，手动缩放不误判', () => {
  const slices = [
    {seqStart: 0, seqEnd: 2},
    {seqStart: 3, seqEnd: 4},
  ];
  assert.equal(sliceIndexForRange(slices, {start: 0, end: 2}, 5), 0);
  assert.equal(sliceIndexForRange(slices, {start: 3, end: 4}, 5), 1);
  assert.equal(sliceIndexForRange(slices, {start: 1, end: 2}, 5), -1);
  assert.equal(sliceIndexForRange(slices, null, 5), -1);
  assert.equal(sliceIndexForRange([], {start: 0, end: 1}, 5), -1);
});

test('不需要 DOM：模块在纯 Node 下可 import，纯函数不读浏览器全局', () => {
  assert.equal(typeof document, 'undefined');
  assert.equal(typeof renderSliceTimeline, 'function');
  assert.equal(typeof globalThis.window, 'undefined');
});
