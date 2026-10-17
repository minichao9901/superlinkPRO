/**
 * SPI 桥的**定时采集执行器**（对应 `#i2c` 页的 `runner.js`，语义刻意保持一致）。
 *
 * 输入是 `frames-dsl.js` 的解析结果：
 *   · **没有周期**的帧 = 一次性：开跑先按顺序各发一遍；
 *   · **有周期**的帧 = 定时组（同一次 `loop`/`every` 块里的帧同属一组，共用周期与轮数）。
 *     真机通过 BPT1 一次上传，由 probe 定时器执行；假探针才由浏览器逐拍 sendFrames。
 *     每帧读回数据交给自己的 as 表达式解码。
 *
 * 三条口径：
 *   1. 真机错过节拍会计数并跳过，不补跑；结果队列满会停采。假探针保留只晚不丢的模拟。
 *      轮数（`loop … 3`）按**成功采样**算，一次失败的发送不吃掉用户要的那一轮。
 *   2. 真机周期和延时由 MCU 驱动，网页等待只控制结果读取；实测频率用 probe 时间戳。
 *      假探针周期仍使用浏览器定时器。
 *   3. **解码偏移是"这一帧自己的读数据"**：SPI 一次 xfer 通常就是一个寄存器/一次转换，
 *      把偏移定义在整组拼起来的大缓冲上会算不清（I2C 那边是"一次逻辑读"，语义不同）。
 */
import { applyAs } from '../core/expr.js';
import { waitMs } from '../core/pace.js';
import * as P from './protocol.js';
import { BUS, delayRecord, program } from '../core/bus-periodic.js';

export class SpiRunner {
  /**
   * @param {object} session SpiSession（一次性 sendFrames、周期 runPeriodic）
   * @param {{onEvent?:(e:object)=>void}} opts
   */
  constructor(session, { onEvent } = {}){
    this.session = session;
    this.onEvent = onEvent || null;
    this.parsed = null;
    this.running = false;
    this.stopping = false;
    this.timers = new Set();
    this.stat = { ticks: 0, late: 0, errors: 0, sent: 0, failed: 0, t0: 0, lastTick: 0 };
    this._generation = 0;
    this._activeGroups = new Map();
  }

  _emit(e){ try { this.onEvent?.(e); } catch { /* 视图自己出错不该拖垮采集 */ } }

  /** 解析结果里能不能跑（一次性帧也可以，跑一遍就结束）*/
  static runnable(parsed){
    return !!parsed && !parsed.errors?.length && parsed.items.length > 0;
  }

  /**
   * 开跑。
   * @param {object} parsed `parseFrames()` 的结果
   */
  async start(parsed){
    if (this.running) this.stop('重新开始');
    if (this._probeTask) await this._probeTask;
    if (!SpiRunner.runnable(parsed)){
      this._emit({ type: 'error', msg: parsed?.errors?.length ? '脚本有语法错，先修好再跑' : '没有可发的帧' });
      return false;
    }
    this.parsed = parsed;
    const generation = ++this._generation;
    this._activeGroups.clear();
    this.running = true;
    this.stopping = false;
    this.stat = { ticks: 0, late: 0, errors: 0, sent: 0, failed: 0, t0: performance.now(), lastTick: 0 };
    const once = parsed.items.filter(it => !it.period);
    const timed = parsed.items.filter(it => it.period);
    const groups = new Map();
    for (const it of timed){
      const g = it.group ?? 0;
      if (!groups.has(g)) groups.set(g, { group: g, period: it.period, count: it.count || 0, items: [] });
      const grp = groups.get(g);
      // 同一组里的周期取最小（正常解析器保证同块同周期；行尾 every 单独成组）
      grp.period = Math.min(grp.period, it.period);
      grp.items.push(it);
    }
    this._emit({ type: 'start', oneShots: once.length, groups: groups.size, items: parsed.items.length,
                 vars: parsed.stats?.vars || [] });
    let probeGroups = null;
    try {
      if (groups.size && this.session.runPeriodic && !this.session.usingMock)
        probeGroups = this._probeGroups([...groups.values()]);
    } catch (e){ this._emit({ type: 'error', msg: e.message }); this.stop('任务不支持'); return false; }
    // 一次性部分：先跑完再启动定时（"先认片子再采样"的顺序语义）
    if (once.length){
      const r = await this._send(once, null, generation);
      if (generation !== this._generation) return false;
      this._emit({ type: 'once', sent: r.sent, failed: r.failed, ms: r.ms });
    }
    if (generation !== this._generation || this.stopping) return false;
    if (!groups.size){
      this._emit({ type: 'stop', reason: '只有一次性帧，已跑完' });
      this.running = false;
      return true;
    }
    // 每组一个定时器；用"目标时刻 + 序号"算下一拍，避免漂移累积
    if (probeGroups){
      const p = this._runProbe(probeGroups, generation);
      this._probeTask = p;
      p.finally(() => { if (this._probeTask === p) this._probeTask = null; });
    } else for (const grp of groups.values()) this._startGroup(grp, generation);
    this._emit({ type: 'running', groups: groups.size });
    return true;
  }

  /** 停（幂等）：清定时器、取消在飞请求的等待、广播 stop */
  stop(reason = '用户停止'){
    this._generation++;
    if (!this.running && !this.timers.size) return;
    this.stopping = true;
    for (const t of this.timers) clearTimeout(t);
    this.timers.clear();
    this._activeGroups.clear();
    this.running = false;
    this._emit({ type: 'stop', reason, stat: { ...this.stat } });
  }

  _probeGroups(groups){
    return groups.map(g => {
      const records = g.items.map(it => {
        if (it.type === P.T.DELAY) return { ...delayRecord(new DataView(it.payload.buffer, it.payload.byteOffset, it.payload.byteLength).getUint32(0, true)), item: it };
        if (![P.T.XFER, P.T.CS, P.T.GPIO, P.T.PING, P.T.AUX_IN].includes(it.type))
          throw Error('probe 周期组支持 XFER/CS/GPIO/PING/AUX_IN/延时；初始化步骤请放到一次性部分');
        if (it.type === P.T.XFER && new DataView(it.payload.buffer, it.payload.byteOffset, it.payload.byteLength).getUint16(6, true) > 54)
          throw Error('probe 周期任务每帧最多读取 54 B');
        return { kind: BUS.SPI, data: P.frame(it.type, it.payload, { flags: it.flags }), item: it };
      });
      program(records);
      return { ...g, records, lastResult: records.findLastIndex(r => r.kind !== BUS.DELAY), values: [], failed: false };
    });
  }
  async _runProbe(groups, generation){
    try {
      this.session.log('i', '周期由 probe 定时器驱动；网页只读取结果', 'bus');
      await this.session.runPeriodic(groups, {
        shouldStop: () => this.stopping || generation !== this._generation,
        onResult: (r, g) => {
          if (generation !== this._generation || this.stopping) return;
          const it = g.records[r.step].item;
          if (g.cycle !== r.cycle){ g.cycle = r.cycle; g.values = []; g.failed = false; }
          this.stat.sent++;
          if (r.err){
            g.failed = true; this.stat.errors++;
            this._emit({ type: 'error', group: g.group, msg: `${it.label || '帧'}：${P.ST_TEXT[r.err] || r.err}` });
          } else if (it.as?.length){
            const a = applyAs({ ok: true, fields: it.as, hexOnly: false }, r.data);
            g.values.push(...a.values);
            if (a.warn) this._emit({ type: 'warn', group: g.group, msg: a.warn });
          }
          if (r.step === g.lastResult){
            if (g.values.length) this._emit({ type: 'values', group: g.group, values: g.values, timeMs: r.timeMs });
            if (!g.failed){
              this.stat.ticks++; this.stat.lastTick = performance.now();
              this._emit({ type: 'tick', group: g.group, n: r.cycle, stat: { ...this.stat }, timeMs: r.timeMs, timing: 'probe' });
            }
            this.stat.late += Math.max(0, r.skipped - (g.skipped || 0)); g.skipped = r.skipped;
          }
        },
      });
      if (generation === this._generation) this.stop('probe 定时任务结束');
    } catch (e){
      if (generation === this._generation){ this._emit({ type: 'error', msg: e.message }); this.stop('probe 采集失败'); }
    }
  }

  _startGroup(grp, generation){
    const period = Math.max(1, grp.period | 0);
    let t0 = 0;           // 第一拍建立时基（别把"启动延迟"算成迟拍）
    let n = 0;            // 尝试的拍数
    let done = 0;         // **成功采样**的拍数 —— 轮数按它算
    this._activeGroups.set(grp.group, grp);
    const loop = async () => {
      if (generation !== this._generation || this.stopping || !this.running) return;
      if (!t0) t0 = performance.now();
      n++;
      const target = t0 + (n - 1) * period;
      const late = performance.now() - target;
      /**
       * 周期比"一次发送"还短时，这一拍会**迟**（但照跑）—— 与 `#i2c` 的 runner 同一口径：
       * **只晚不丢**。丢拍等于悄悄少一个采样点，比"晚一点"坏得多；迟了多少如实记在 `stat.late`
       * 与日志里（"发送才是节拍器"的意思是别排队堆积，不是丢数据）。
       */
      if (late > period){
        this.stat.late++;
        this._emit({ type: 'late', group: grp.group, late: Math.round(late) });
      }
      const r = await this._send(grp.items, grp, generation);
      if (generation !== this._generation) return;
      if (r.ok){
        done++;
        this.stat.ticks++;
        this.stat.lastTick = performance.now();
        this._emit({ type: 'tick', group: grp.group, n: done, stat: { ...this.stat } });
      }
      if (this.stopping || !this.running) return;
      // 轮数按**成功采样**算（done）：一次失败的发送不该吃掉用户要的那一轮
      if (grp.count && done >= grp.count){
        this._emit({ type: 'groupDone', group: grp.group, n: done });
        this._activeGroups?.delete(grp.group);
        // 各组可能周期不同、轮数不同：只有**所有**组都跑完了才整体停
        if (!this._activeGroups || this._activeGroups.size === 0) this.stop(`定时组跑完 ${done} 轮`);
        return;
      }
      const nextTarget = t0 + n * period;
      const wait = Math.max(0, nextTarget - performance.now());
      const t = setTimeout(() => { this.timers.delete(t); loop(); }, wait);
      this.timers.add(t);
    };
    // 立刻打第一拍（不等一个周期）—— 用户点了「开始」就该马上看到数
    const t = setTimeout(() => { this.timers.delete(t); loop(); }, 0);
    this.timers.add(t);
  }

  /** 发一组帧 → 解码 → 广播；返回 {ok, sent, ms}（`ok` = 这一拍算不算一次成功采样） */
  async _send(items, grp, generation){
    const t0 = performance.now();
    let rsps = [];
    let okSend = true;
    try {
      // 在飞请求：读帧都带 RSP；这里给一个与帧数相称的超时（默认 1.5 s 对慢器件偏紧）
      const r = await this.session.sendFrames(items.map(it => ({ ...it })), {
        quiet: true, tag: 'bus', timeoutMs: 2500, shouldStop: () => this.stopping || generation !== this._generation,
      });
      if (generation !== this._generation) return { ok: false, cancelled: true };
      rsps = r.rsps || [];
      this.stat.sent += r.sent || 0;
      this.stat.failed += r.failed || 0;
      if (r.failed) okSend = false;
    } catch (e){
      if (generation !== this._generation) return { ok: false, cancelled: true };
      this.stat.errors++;
      this._emit({ type: 'error', group: grp?.group ?? null, msg: `发送失败：${e?.message || e}` });
      return { ok: false, sent: 0, ms: performance.now() - t0 };
    }
    const values = [];
    for (let i = 0; i < items.length; i++){
      const it = items[i];
      if (!it.as?.length) continue;
      const rsp = rsps[i];
      if (!rsp || rsp.error || rsp.status !== P.ST.OK){
        this.stat.errors++;
        okSend = false;
        this._emit({ type: 'error', group: grp?.group ?? null,
                     msg: `${it.label || '帧'} 没有有效应答（${rsp?.error?.message || 'status=' + rsp?.status}）` });
        continue;
      }
      const data = rsp.data instanceof Uint8Array ? rsp.data : new Uint8Array(rsp.data || 0);
      try {
        const r = applyAs({ ok: true, fields: it.as, hexOnly: false }, data);
        for (const v of r.values) values.push(v);
        if (r.warn) this._emit({ type: 'warn', group: grp?.group ?? null, msg: r.warn });
      } catch (e){
        this.stat.errors++;
        okSend = false;
        this._emit({ type: 'error', group: grp?.group ?? null, msg: `解码失败：${e?.message || e}` });
      }
    }
    if (values.length) this._emit({ type: 'values', group: grp?.group ?? null, values });
    return { ok: okSend, sent: items.length, ms: performance.now() - t0 };
  }
}

/** 给页面用：解析结果的一句话摘要（帧数 / 一次性 / 定时组 / 变量）*/
export function describeRun(parsed){
  if (!parsed) return '还没解析';
  const s = parsed.stats || {};
  const parts = [`${s.frames ?? 0} 条帧`];
  if (s.oneShots) parts.push(`${s.oneShots} 条一次性`);
  if (s.timed) parts.push(`${s.timed} 条定时（${s.groups ?? 0} 组）`);
  if (s.vars?.length) parts.push(`变量 ${s.vars.length} 个：${s.vars.slice(0, 6).join(' ')}${s.vars.length > 6 ? ' …' : ''}`);
  return parts.join(' · ');
}

export { waitMs };
