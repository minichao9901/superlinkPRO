/**
 * J-Scope 的**数据层**：类型化缓冲 + LOD（min/max 金字塔）+ 触发判定。
 *
 * 三条设计决定（都有理由）：
 *  1. **按类型分配**缓冲：`f32 → Float32Array`、`i32 → Int32Array`…… 不要一律用 Float32 存整数 ——
 *     超过 2^24 的整数会被四舍五入（靶子固件里那个 `0x10000000|t` 就是专门用来抓这个的）。
 *  2. **线性缓冲，满了就停**（`full = true` + 计数），不做环形覆盖：20 s 的采集容量是事先算出来的，
 *     线性缓冲让 LOD 金字塔可以"只往后追加"，不需要失效/重建。要连续滚动看，重开一次即可。
 *  3. **不存每个样本的时间戳**：只每隔 64 个样本存一个锚点（t_us），中间用实测速率插值 ——
 *     1 M 样本也只有 16 K 个锚点（几百 KB），时间轴精度远好于像素。
 *
 * LOD 金字塔：每通道额外维护 16 / 256 / 4096 三级的 min/max。
 * 画图时按"每像素列多少个样本"挑一级，**只有完全放大（每列 < 16 个样本）时才逐样本精确扫**。
 * 用 LOD 时列边界会向外扩到块边界（相邻列多含最多一个块的样本）—— 这是示波器软件的通行做法：
 * 包络只会"稍微胖一点"，但绝不会漏掉尖峰（漏尖峰比胖一点严重得多）。
 */
import { SCALARS } from '../elf/dwarf.js';

const CTOR = {
  u8: Uint8Array, i8: Int8Array, u16: Uint16Array, i16: Int16Array,
  u32: Uint32Array, i32: Int32Array, f32: Float32Array, f64: Float64Array,
};
export const ctorFor = scalar => CTOR[scalar] || Float32Array;

const LOD_FACTORS = [16, 256, 4096];

export class Channel {
  constructor({ name, scalar = 'f32', capacity, scale = 1, offset = 0 } = {}){
    this.name = name;
    this.scalar = SCALARS[scalar] ? scalar : 'f32';
    this.scale = scale;                 // 显示用：v * scale + offset
    this.offset = offset;
    this.capacity = capacity;
    const C = ctorFor(this.scalar);
    this.data = new C(capacity);
    this.levels = LOD_FACTORS.map(factor => ({
      factor,
      blocks: Math.ceil(capacity / factor),
      min: new C(Math.ceil(capacity / factor)),
      max: new C(Math.ceil(capacity / factor)),
    }));
    this.min = Infinity; this.max = -Infinity;   // 全局极值（自动量程用，省得每次扫）
  }

  push(i, v){
    this.data[i] = v;
    if (v < this.min) this.min = v;
    if (v > this.max) this.max = v;
    for (const L of this.levels){
      const b = (i / L.factor) | 0;
      if (i % L.factor === 0){ L.min[b] = v; L.max[b] = v; }
      else { if (v < L.min[b]) L.min[b] = v; if (v > L.max[b]) L.max[b] = v; }
    }
  }

  reset(){ this.min = Infinity; this.max = -Infinity; this.data.fill(0);
    for (const L of this.levels){ L.min.fill(0); L.max.fill(0); } }

  at(i){ return this.data[i]; }

  /** 显示值（应用 scale/offset）*/
  value(i){ return this.data[i] * this.scale + this.offset; }

  /** 精确 min/max（逐样本）—— 放大到"每列不到 16 个样本"时用 */
  exact(a, b){
    let mn = Infinity, mx = -Infinity;
    const d = this.data;
    for (let i = a; i < b; i++){ const v = d[i]; if (v < mn) mn = v; if (v > mx) mx = v; }
    return [mn, mx];
  }

  /**
   * 按列抽 min/max 包络。
   * @param {number} a 起始样本（含）
   * @param {number} b 结束样本（不含）
   * @param {number} cols 列数（= 画布像素宽）
   * @param {Float64Array} outMin/outMax 长度 ≥ cols 的复用缓冲
   * @returns {boolean} 是否用了 LOD（false = 逐样本精确）
   */
  columns(a, b, cols, outMin, outMax){
    const span = Math.max(1, b - a);
    const per = span / cols;
    let level = null;
    for (const L of this.levels) if (L.factor <= per) level = L;
    const d = this.data;
    if (!level){
      for (let c = 0; c < cols; c++){
        const s = a + Math.floor(c * per), e = Math.min(b, a + Math.floor((c + 1) * per));
        let mn = Infinity, mx = -Infinity;
        for (let i = s; i < e; i++){ const v = d[i]; if (v < mn) mn = v; if (v > mx) mx = v; }
        outMin[c] = mn; outMax[c] = mx;
      }
      return false;
    }
    const f = level.factor;
    for (let c = 0; c < cols; c++){
      const s = a + Math.floor(c * per), e = Math.min(b, a + Math.floor((c + 1) * per));
      if (e <= s){ outMin[c] = NaN; outMax[c] = NaN; continue; }
      let mn = Infinity, mx = -Infinity;
      // 向外扩到块边界（相邻列可能多含不到一个块的样本；包络只会稍胖，不会漏峰）
      const b0 = (s / f) | 0;
      const b1 = Math.min(level.blocks, Math.ceil(e / f));
      for (let blk = b0; blk < b1; blk++){
        const lmin = level.min[blk], lmax = level.max[blk];
        if (lmin < mn) mn = lmin;
        if (lmax > mx) mx = lmax;
      }
      outMin[c] = mn; outMax[c] = mx;
    }
    return true;
  }
}

export class SampleStore {
  /**
   * @param {Array<{name,addr,size,scalar}>} vars 变量表（顺序 = 帧内顺序）
   * @param {number} capacity 每个通道能存多少个样本
   */
  constructor(vars, capacity, opts = {}){
    this.vars = vars.map(v => ({ ...v }));
    this.capacity = capacity;
    this.channels = this.vars.map(v => new Channel({
      name: v.name, scalar: v.scalar || 'f32', capacity,
      scale: opts.scale?.[v.name] ?? 1, offset: opts.offset?.[v.name] ?? 0,
    }));
    this.tsEvery = 64;
    this.tsUs = new Float64Array(Math.ceil(capacity / this.tsEvery) + 2);
    this.tsN = 0;
    this.count = 0;
    this.full = false;
    this.overrun = 0;          // 满了之后又来了多少帧（= 丢掉的样本数）
    this.frames = 0;
    this.t0Us = null;
    this.tLastUs = null;
    this._firstT = null;
  }

  reset(){
    for (const ch of this.channels) ch.reset();
    this.count = 0; this.full = false; this.overrun = 0; this.frames = 0;
    this.tsN = 0; this.t0Us = null; this.tLastUs = null;
  }

  get width(){ return this.vars.length; }
  channel(i){ return this.channels[i]; }
  channelByName(n){ return this.channels.find(c => c.name === n) || null; }

  /** 存一帧（nums 顺序与 vars 一致；tUs 是这一帧的绝对时间戳，可缺省）*/
  pushFrame(nums, tUs){
    if (this.full){ this.overrun++; return false; }
    const i = this.count;
    for (let k = 0; k < this.channels.length; k++) this.channels[k].push(i, nums[k]);
    if (tUs != null){
      if (this.t0Us == null){ this.t0Us = tUs; this._firstT = tUs; }
      if (i % this.tsEvery === 0 && this.tsN < this.tsUs.length) this.tsUs[this.tsN++] = tUs;
      this.tLastUs = tUs;
    }
    this.count = i + 1;
    this.frames++;
    if (this.count >= this.capacity) this.full = true;
    return true;
  }

  /** Append a packed little-endian u32 packet without per-sample frame arrays.
   * Same typed values, LOD extrema, timestamp anchors and capacity semantics as pushFrame.
   * Returns false when this store does not have the matching single-u32 layout. */
  pushU32Packet(payload, n, t0, period){
    if (this.channels.length !== 1 || this.vars[0].size !== 4 || this.channels[0].scalar !== 'u32') return false;
    const offered = Math.min(n, Math.floor(payload.byteLength / 4));
    const accepted = Math.min(offered, this.capacity - this.count);
    const start = this.count, ch = this.channels[0];
    const dv = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
    for (let k = 0; k < accepted; k++) ch.push(start + k, dv.getUint32(k * 4, true));
    if (accepted && t0 != null){
      if (this.t0Us == null){ this.t0Us = t0; this._firstT = t0; }
      const firstAnchor = Math.ceil(start / this.tsEvery) * this.tsEvery;
      for (let i = firstAnchor; i < start + accepted && this.tsN < this.tsUs.length; i += this.tsEvery)
        this.tsUs[this.tsN++] = t0 + (i - start) * period;
      this.tLastUs = t0 + (accepted - 1) * period;
    }
    this.count += accepted; this.frames += accepted;
    this.overrun += offered - accepted;
    if (this.count >= this.capacity) this.full = true;
    return accepted;
  }

  /** 实测速率（Hz）：用首尾时间戳和样本数算（没有时间戳就返回 0）*/
  rate(){
    if (this.t0Us == null || this.tLastUs == null || this.count < 2) return 0;
    const dt = this.tLastUs - this.t0Us;
    return dt > 0 ? (this.count - 1) * 1e6 / dt : 0;
  }

  /** 第 i 个样本的绝对时间（µs）。
   *  锚点每 64 个样本一个（真时间戳），段内用**相邻两个锚点的实测间隔**插值：
   *  🚨 这里踩过坑：早先用全局平均速率插值，那个速率是按"首尾时间戳 ÷ 样本数"算的，
   *  一旦中途卡顿/丢包（真机实测出现过 40 ms 的长间隔），整段数据都被摊薄 ——
   *  段内局部读数会偏（64 样本 × 0.45 µs ≈ 29 µs；量 1 kHz 方波一个周期就是 3% 误差）。
   *  用相邻锚点插值，误差只跟"这一小段"的真实抖动有关。 */
  timeAt(i){
    const n = this.tsN;
    if (n === 0) return i;                                     // 没有时间戳：退化成"样本序号"
    const k = Math.min(n - 1, Math.max(0, Math.floor(i / this.tsEvery)));
    const idx0 = k * this.tsEvery;
    const base = this.tsUs[k];
    if (k + 1 < n){                                            // 还有下一个锚点 → 用它量这一段的间隔
      const dtSeg = this.tsUs[k + 1] - base;
      if (dtSeg > 0) return base + (i - idx0) * dtSeg / this.tsEvery;
    } else if (this.tLastUs != null && this.count - 1 > idx0){  // 最后一段（不满 64 个）
      const dtTail = this.tLastUs - base;                       // 最后一个锚点 → 最后一个样本
      if (dtTail > 0) return base + (i - idx0) * dtTail / (this.count - 1 - idx0);
    }
    const dt = this.rate();
    return dt > 0 ? base + (i - idx0) * 1e6 / dt : base + (i - idx0);
  }

  tSpan(){ return this.count ? (this.timeAt(this.count - 1) - this.timeAt(0)) : 0; }

  /** 显存占用（含 LOD）*/
  bytes(){
    let n = 0;
    for (const ch of this.channels){
      const bpe = ch.data.BYTES_PER_ELEMENT;
      n += ch.capacity * bpe;
      for (const L of ch.levels) n += L.blocks * bpe * 2;
    }
    n += this.tsUs.length * 8;
    return n;
  }

  /** 导出一列数据（CSV 用；返回 Float64Array 视图会复制，注意按需分块）*/
  copyChannel(i, a = 0, b = this.count){
    const ch = this.channels[i];
    const out = new Float64Array(Math.max(0, b - a));
    for (let k = 0; k < out.length; k++) out[k] = ch.value(a + k);
    return out;
  }
}

// ---------------------------------------------------------------- 触发
export const TRIG = { NONE: 0, ABOVE: 1, BELOW: 2, RISING: 3, FALLING: 4, CHANGE: 5 };
export const TRIG_NAME = { 0: '不触发', 1: '大于', 2: '小于', 3: '上升沿', 4: '下降沿', 5: '变化' };

/** 判定单个样本是否命中（prev 是同一通道上一个样本，可为 null）*/
export function trigHit(mode, prev, cur, level){
  switch (mode){
    case TRIG.ABOVE: return cur > level;
    case TRIG.BELOW: return cur < level;
    case TRIG.RISING: return prev != null && prev <= level && cur > level;
    case TRIG.FALLING: return prev != null && prev >= level && cur < level;
    case TRIG.CHANGE: return prev != null && prev !== cur;
    default: return false;
  }
}

/** 触发配置 + 状态机（实时用；离线重触发见 findTrigger）*/
export class Trigger {
  constructor(){ this.reset(); }
  reset(){
    this.mode = TRIG.NONE; this.channel = 0; this.level = 0;
    this.pre = 0; this.post = 0; this.single = true;
    this.fired = false; this.hitIndex = -1;
    this.hits = 0; this._prev = null;
  }
  configure({ mode, channel, level, pre, post, single }){
    if (mode !== undefined) this.mode = mode;
    if (channel !== undefined) this.channel = channel;
    if (level !== undefined) this.level = level;
    if (pre !== undefined) this.pre = pre;
    if (post !== undefined) this.post = post;
    if (single !== undefined) this.single = single;
    this._prev = null;
  }
  /** 喂一帧；命中返回 true（单次模式下命中后不再重复报）*/
  feed(nums, index){
    const cur = nums[this.channel];
    const hit = this.mode !== TRIG.NONE && trigHit(this.mode, this._prev, cur, this.level);
    this._prev = cur;
    if (!hit) return false;
    if (this.single && this.fired) return false;
    this.fired = true; this.hitIndex = index; this.hits++;
    return true;
  }
  /** 命中点对应的显示窗口（预触发可能不足，会截断到 0）*/
  window(total){ return windowFor(this.hitIndex, this.pre, this.post, total); }
}

export function windowFor(hitIndex, pre, post, total){
  if (hitIndex < 0) return { start: 0, end: total, short: false };
  const start = Math.max(0, hitIndex - pre);
  const end = Math.min(total, hitIndex + post + 1);
  return { start, end, short: start === 0 && pre > hitIndex };
}

/**
 * **离线重触发**：在已经采到的缓冲里找下一个命中点（不用重采！）。
 * 这是主机侧触发白送的好处：20 s 的数据都在内存里，改个阈值立刻重新定位。
 * @returns {number} 命中样本索引，-1 = 没找到
 */
export function findTrigger(store, { channel = 0, mode = TRIG.NONE, level = 0 }, from = 0, to = store.count){
  if (mode === TRIG.NONE) return -1;
  const ch = store.channel(channel);
  if (!ch) return -1;
  const a = Math.max(0, from), b = Math.min(to, store.count);
  let prev = a > 0 ? ch.value(a - 1) : null;
  for (let i = a; i < b; i++){
    const cur = ch.value(i);
    if (trigHit(mode, prev, cur, level)) return i;
    prev = cur;
  }
  return -1;
}
