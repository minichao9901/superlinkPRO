/**
 * 假探针：**不需要硬件**就能把整条链路（配置 → 采样 → 数据包 → 缓冲 → 绘图/触发/导出）跑起来。
 *
 * 它同时扮演两个角色，接口与真的那两套完全同形：
 *   · HID 控制面：`xfer(cmd, data)` —— 与 `app/hid/probe.js` 的 `AkaLinkHid.xfer` 同签名，
 *     返回 63 字节 payload 形状的回包（`[1]=cmd`、`[2]=rc`、`[3..]=12 个状态字`）。
 *   · 数据面：`poll(nowUs)` 产出 512 B 包 —— 页面把它当成"从 0x83 收到的字节"即可。
 *
 * 波形是**按类型+通道号确定性生成**的（和靶子固件一样"契约已知"）：
 * 所以自测可以逐点断言"画出来的值应当是什么"，而不是"看着像正弦"。
 */
import { HID_CMD, ACT, KIND, TYPES, SCOPE_FLAG, buildDef, buildData, buildStat, buildEvt, packSamples,
         samplesPerPacket, START_PENDING, VERSION, VERSION_TICKS, ticksForUs, usForTicks } from './protocol.js';

/** 类型 → 波形（i = 样本序号，k = 通道序号）。返回"原始整数值"（f32/f64 返浮点，其余按类型含义）*/
export function waveformFor(scalar, i, k = 0){
  switch (scalar){
    case 'f32': return Math.sin(i * (0.02 + 0.01 * k));            // 慢正弦 ±1
    case 'f64': return 1 + i * 1e-4;                               // 慢斜坡（验 8 字节载荷）
    case 'i32': return i - 500000;                                 // 有负值、会越过 2^24（验精度）
    case 'u32': return (0x10000000 + i) >>> 0;                     // 高位非零
    case 'u16': return i % 1000;
    case 'i16': return ((i % 10) < 5) ? 1000 : -1000;              // 1 kHz 方波（采样率自选）
    case 'u8':  return i & 0xff;
    case 'i8':  return (((i * 3) & 0xff) > 127 ? ((i * 3) & 0xff) - 256 : ((i * 3) & 0xff));
    default: return i;
  }
}

export class MockScopeProbe {
  /**
   * @param {{periodUs?:number, rateHz?:number, swdMhz?:number, dropEvery?:number,
   *          stallAfter?:number, startDelayPolls?:number, failStart?:number}} opts
   *   dropEvery   每 N 个样本丢 1 个（用来测"丢样本必须被数出来"）
   *   stallAfter  产出 N 个样本后卡住（测"探针跑着但没数据"的提示）
   *   startDelayPolls 启动要排队几次（复现真固件 -100 = 排队中）
   */
  constructor(opts = {}){
    this.name = 'MockScope';
    this.vars = [];
    this.periodUs = opts.periodUs ?? 100;          // 10 kHz
    this.periodUsActual = this.periodUs;
    this.version = Number.isInteger(this.periodUs) ? VERSION : VERSION_TICKS;
    this.slowdown = opts.slowdown ?? 1;            // 探针跟不上请求周期时的倍数（见 poll()）
    this.swdMhz = opts.swdMhz ?? 45;
    this.dropEvery = opts.dropEvery ?? 0;
    this.stallAfter = opts.stallAfter ?? 0;
    this.startDelayPolls = opts.startDelayPolls ?? 1;
    this.failStart = opts.failStart ?? null;       // 直接给一个负数 rc（测错误文案）

    this.running = false;
    this.produced = 0;      // 目标"应该"产出的样本数（含被丢的）
    this.delivered = 0;     // 实际发出的样本数
    this.dropped = 0;
    this.pkts = 0;
    this.usbErr = 0; this.swdErr = 0;
    this.seq = 0;
    this.bitmap = new Uint8Array(0);               // 采样进行中的序号位图（用于固定间隔丢样本）
    this.connects = 0;
    this.hidLog = [];
    this._pendingStart = 0;
    this._t0 = 0;             // 启动时刻（µs，外部注入的时间基）
    this._n = 0;              // 已经产出到第几个样本
    this._lastPollUs = null;
    this._discarding = false;
    this.riscv = !!opts.riscv;      // 生效后端（假探针也能装成 RISC-V，供页面自测用）
    this.nspans = 1;
    this._sentDef = false;
    this._statEvery = 64;
    this._packetAccum = [];   // 累计到整包才发（模拟固件的组包）
  }

  // ---------------------------------------------------------- HID 控制面
  async xfer(cmd, data, timeout = 3000){
    void timeout;
    this.hidLog.push({ cmd, data: data ? Array.from(data) : null });
    if (cmd !== HID_CMD) return this._res(cmd, -1);
    const action = data ? data[0] : ACT.STATUS;
    switch (action){
      case ACT.CONFIG:
      case ACT.CONFIG_TICKS: {
        const dv = new DataView(data.buffer, data.byteOffset, data.byteLength);
        const n = data[6];
        const vars = [];
        let o = 7;
        for (let i = 0; i < n; i++){
          vars.push({ addr: dv.getUint32(o, true), size: data[o + 4], type: data[o + 5] });
          o += 6;
        }
        this.configure({ periodUs: data[0] === ACT.CONFIG_TICKS ? usForTicks(dv.getUint32(1, true)) : dv.getUint32(1, true), flags: data[5], vars });
        this.version = data[0] === ACT.CONFIG_TICKS ? VERSION_TICKS : VERSION;
        return this._res(cmd, 0);
      }
      case ACT.START:
        this.start();
        return this._res(cmd, this._startRc());
      case ACT.STOP:
        this.stop();
        return this._res(cmd, 0);
      case ACT.TRIGGER:
        this.trigger = { channel: data[1], mode: data[2],
                         level: new DataView(data.buffer).getFloat32(3, true) };
        return this._res(cmd, 0);
      case ACT.BENCH: {
        const iters = new DataView(data.buffer, data.byteOffset).getUint32(1, true);
        // 标定结果：假装每个样本花 periodUs/2（够页面显示用）
        const ticks = Math.round(iters * this.periodUs * 24 / 2);
        this._bench = { iters, ticks };
        return this._res(cmd, 0);
      }
      case ACT.BENCH_RESULT: {
        const b = this._bench || { iters: 0, ticks: 0 };
        const r = this._res(cmd, 0);
        const dv = new DataView(r.buffer);
        dv.setUint32(3, b.ticks, true); dv.setUint32(7, b.iters, true); dv.setInt32(11, 0, true);
        return r;
      }
      case ACT.STATUS:
      default:
        return this._res(cmd, this._startRc());
    }
  }

  /** 12 个状态字的回包（形状与真固件一致）*/
  _res(cmd, rc){
    const p = new Uint8Array(63);
    p[0] = 51; p[1] = cmd; p[2] = rc & 0xff;
    const dv = new DataView(p.buffer);
    const running = this.running ? 1 : 0;
    // 状态字 0：bit0 运行中 / bit1 **生效后端是 RISC-V** / bit8-15 span 数 / bit16 SWD 已就绪 / bit24-31 变量数
    // （RISC-V 模式下探针不上报 SWD 时钟，bit16 也置 0 —— 与真固件一致）
    dv.setUint32(3, running | 4 | 8 | (this.riscv ? 2 : 0) | (this.nspans << 8) |
                    ((this.riscv ? 0 : 1) << 16) | (this.vars.length << 24), true);
    dv.setUint32(7, this.riscv ? 0 : Math.round(this.swdMhz * 1e6), true);
    dv.setUint32(11, this.produced >>> 0, true);
    dv.setUint32(15, this.dropped >>> 0, true);
    dv.setUint32(19, (this.pkts & 0xffff) | (this.usbErr << 16), true);
    dv.setUint32(23, (this.swdErr & 0xffff), true);
    dv.setUint32(27, this.seq >>> 0, true);
    dv.setUint32(31, (0 & 0xffff) | (0 << 16), true);
    dv.setUint32(35, this._planHash(), true);
    dv.setUint32(39, (ACT.STATUS & 0xff) | (0 << 8), true);
    dv.setInt32(43, rc, true);
    dv.setUint32(47, ((this.version === VERSION_TICKS ? ticksForUs(this.periodUs) : this.periodUs) & 0xffff)
      | (this.version === VERSION_TICKS ? (1 << 17) : 0), true);
    return p;
  }

  _planHash(){
    let h = 0x811c9dc5;
    for (const v of this.vars){
      for (const b of [v.addr & 0xff, (v.addr >>> 8) & 0xff, (v.addr >>> 16) & 0xff, (v.addr >>> 24) & 0xff, v.size, v.type]){
        h ^= b; h = Math.imul(h, 0x01000193) >>> 0;
      }
    }
    return h >>> 0;
  }

  // ---------------------------------------------------------- 采样
  configure({ periodUs = this.periodUs, flags = 0, vars = this.vars } = {}){
    if (vars.length > 8) throw new Error('假探针：变量最多 8 个');
    // 线上只有 type 码，没有名字 —— 这里按类型表补回 scalar（组包时要用它决定怎么写位型）
    // 🚨 **按地址排序**：真固件就是这么打包的（帧内顺序 = 地址顺序）。假探针必须一样，
    //    否则"勾选顺序 ≠ 地址顺序"这类整体错位在自测里根本暴露不出来（真机上已经踩过一次）。
    this.vars = vars.map(v => ({ ...v, scalar: v.scalar || TYPES[v.type]?.name || 'u32' }))
                    .sort((a, b) => a.addr - b.addr);
    this.periodUs = Math.max(2, usForTicks(ticksForUs(periodUs)));
    this.version = Number.isInteger(this.periodUs) ? VERSION : VERSION_TICKS;
    this.periodUsActual = this.periodUs;
    // flags：bit0 = 允许 60 MHz，bit1 = 丢弃模式，bit5 = 采样时暂停 CDC 桥，bit6 = 强制 RISC-V
    // 🚨 老写法 `!!(flags & 1)` 把"允许 60 MHz"当成了丢弃模式（位搞错了）—— 顺手对齐协议。
    this._discarding = !!(flags & SCOPE_FLAG.DISCARD);
    if (flags & SCOPE_FLAG.RISCV) this.riscv = true;    // 强制 RISC-V：粘住（真固件里全局目标类型也是粘的）
    this._sentDef = false;
  }

  start(){
    if (this.failStart != null){ this._startRcValue = this.failStart; this.running = false; return; }
    this.running = true;
    this.produced = 0; this.delivered = 0; this.dropped = 0; this.pkts = 0;
    this.seq = 0; this._n = 0; this._packetAccum = [];
    this._pendingStart = this.startDelayPolls;     // 先 "排队" 几次（复现 -100）
    this._startRcValue = START_PENDING;
    this._lastPollUs = null;
  }

  stop(){ this.running = false; }

  _startRc(){
    if (this._pendingStart > 0){
      this._pendingStart--;
      if (this._pendingStart === 0) this._startRcValue = 0;
      return START_PENDING;                        // -100 = 排队中（真固件的哨兵值）
    }
    return this._startRcValue ?? 0;
  }

  /** 模拟时间推进：按 periodUs 产出样本，返回 512 B 包数组。
   *  `slowdown` = 探针**实际**做不到请求的周期时要乘的倍数（真机常态：周期是下限，不是承诺）。
   *  例：周期填 5 µs 想要 200 kHz，探针每样本要 11 µs ⇒ slowdown = 2.2，实得 90.9 kHz ——
   *  时间轴照实推进，于是"名义速率 × 时长"开出来的缓冲会被提前/滞后填满（用户正是被这个坑到）。*/
  poll(nowUs){
    if (!this.running) return [];
    if (this._lastPollUs == null) this._lastPollUs = nowUs;
    if (this.stallAfter && this.produced >= this.stallAfter) return [];   // 卡住（不发也不涨）
    const perEff = this.periodUs * (this.slowdown || 1);
    const want = Math.floor((nowUs - this._lastPollUs) / perEff);
    if (want <= 0) return [];
    this._lastPollUs += want * perEff;
    const out = [];
    if (!this._sentDef){ out.push(buildDef({ seq: this.seq++, swdHz: Math.round(this.swdMhz * 1e6),
                                             periodUs: this.periodUs,
                                             flags: (this._discarding ? SCOPE_FLAG.DISCARD : 0) | (this.riscv ? SCOPE_FLAG.RISCV : 0),
                                             vars: this.vars, version: this.version }));
      this._sentDef = true; this.pkts++; }
    for (let i = 0; i < want; i++) this._emit(out, perEff);
    return out;
  }

  _emit(out, perEff = this.periodUs){
    if (this.stallAfter && this.produced >= this.stallAfter) return;
    const dropped = this.dropEvery && (this.produced % this.dropEvery === 0) && this.produced > 0;
    this.produced++;
    if (dropped){ this.dropped++; return; }
    const nums = this.vars.map((v, k) => this.valueAt(k, this._n));
    const spp = Math.max(1, samplesPerPacket(this.frameBytes()));
    if (this._packetAccum.length === 0) this._pktT0 = this._n * perEff;  // 包内**第一个**样本的时刻
    this._packetAccum.push(nums);
    this._n++;
    if (this._packetAccum.length >= spp){
      const frameBytes = this.frameBytes();
      const payload = new Uint8Array(spp * frameBytes);
      for (let j = 0; j < this._packetAccum.length; j++) packSamples(this.vars, this._packetAccum[j], payload.subarray(j * frameBytes));
      out.push(buildData({ seq: this.seq++, tUs: this._pktT0, n: this._packetAccum.length, payload, version: this.version }));
      this.delivered += this._packetAccum.length;
      this._packetAccum = [];
      this.pkts++;
      if (this.pkts % this._statEvery === 0){
        out.push(buildStat({ seq: this.seq++, tUs: this._pktT0, produced: this.produced, dropped: this.dropped,
                             pkts: this.pkts, usbErr: this.usbErr, swdErr: this.swdErr,
                             periodUs: this.periodUsActual, swdMhz: this.swdMhz, discarding: this._discarding, version: this.version }));
        this.pkts++;
      }
    }
  }

  frameBytes(){ return this.vars.reduce((s, v) => s + v.size, 0); }

  /** 把攒着没发满的那半包发出去（真固件在停止/一批结束时也会发短包）。
   *  不调用它的话，最后不足一包的样本会留在 `_packetAccum` 里 —— 于是
   *  "产出 = 送达 + 丢弃"这条账就对不上了（自测里就是这么发现的）。*/
  flush(out = []){
    if (!this._packetAccum.length) return out;
    const frameBytes = this.frameBytes();
    const payload = new Uint8Array(this._packetAccum.length * frameBytes);
    for (let j = 0; j < this._packetAccum.length; j++){
      packSamples(this.vars, this._packetAccum[j], payload.subarray(j * frameBytes));
    }
    out.push(buildData({ seq: this.seq++, tUs: this._pktT0 ?? 0,
                         n: this._packetAccum.length, payload, version: this.version }));
    this.delivered += this._packetAccum.length;
    this.pkts++;
    this._packetAccum = [];
    return out;
  }

  /** 第 k 通道第 i 个样本的**应有值**（自测的参照物）*/
  valueAt(k, i){
    const scalar = this.vars[k]?.scalar || 'f32';
    return waveformFor(scalar, i, k);
  }
}

export function buildEvtFor(seq, tUs, code, a = 0, b = 0){ return buildEvt({ seq, tUs, code, a, b }); }
export { KIND };
