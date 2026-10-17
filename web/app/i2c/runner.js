/**
 * 命令**执行器** —— 把解析出来的命令项跑成"一次一段 + 定时若干段"。纯调度，不碰 DOM。
 *
 * ── 执行模型（这是本页与 SPI 桥页最大的不同，先看这段再看代码）──────────────
 * 命令项分两类：
 *   · **一次性**（`period = 0`）：按书写顺序跑一遍，跑完就完。
 *   · **定时**（`period > 0`）：这就是"while(1)"。每一条定时命令属于一个**任务**，
 *     任务里的命令**按顺序整段执行**，然后等一个周期再从头来。
 *
 * 🚨 为什么必须"整段"而不是"每行各自定时"：ADS1115 的采样是
 *      `写配置（启动转换）→ delay 10ms → 读结果` 三步，分开各按 100 ms 跑，
 *      读到的就会是上一次甚至上上次的值。同理 MPU6050 的"一次读 14 B"、
 *      EEPROM 的"页写 → 等 tWR → 回读对账"都是**不可拆的序列**。
 *      所以 `group`（由 `loop…end` / `every` 产生）是任务的边界，不是装饰。
 *
 * ── 两条纪律 ─────────────────────────────────────────────────────────
 *   1. **等待用对原语**（见 core/pace.js）：轮询间隔用 `waitMs`（不受后台限速影响），
 *      真机循环周期与 delay 由 probe 定时器执行，网页只取结果；假探针用浏览器模拟。
 *      真机迟拍计数并跳过，满队列停采，不能把网页到包时间当作采样时间。
 *   2. **停止要立刻响应**：`stop()` 会打断等待中的周期，并把在飞的那一笔等完
 *      （浏览器取消不了已经发出的 USB 传输，硬断只会留下半截状态）。
 *   3. **稳态不刷日志**：一轮 50 ms × 几行的循环 = 每秒几十行，日志环（600 条）十几秒就被
 *      冲干净，真正有用的历史反而没了。所以每个定时任务只在前 `LOUD_TICKS` 拍写完整日志，
 *      之后转静默（**结果列与实时值面板继续更新**，那才是稳态该看的地方）；错误仍然报，
 *      但每个任务最多再补 `ERR_LOGS_AFTER` 条，免得器件一直 NACK 时把日志刷爆。
 */

import { KIND, describeItem } from './dsl.js';
import { applyAs } from './expr.js';
import { hexBytes, errText, RD_MAX, actXfer } from './protocol.js';
import { BUS, delayRecord, program } from '../core/bus-periodic.js';

/** 定时循环里"前几拍"照常写日志，之后转入静默 —— 见 runner 顶部的说明 */
const LOUD_TICKS = 2;
/** 静默之后，每个任务最多再补这么多条错误日志（否则器件一直 NACK 会把日志刷爆） */
const ERR_LOGS_AFTER = 5;

/** 一份可中断的"停止令牌" */
function makeSignal(){
  const s = { stopped: false, cbs: new Set() };
  s.onAbort = cb => { s.cbs.add(cb); return () => s.cbs.delete(cb); };
  s.abort = () => { s.stopped = true; for (const cb of [...s.cbs]) { try { cb(); } catch { /* 忽略 */ } } };
  return s;
}

/**
 * 把命令项分组 → `{once, tasks}`。**纯函数**，Node 自测直接打。
 *
 * 规则：
 *   · `period = 0` → `once`（按原顺序）
 *   · `period > 0` 且 `group` 相同且**相邻** → 同一个任务（这就是 `loop…end` 的块）
 *   · `period > 0` 且没有 group（行尾 `every 100ms` 的单行）→ 自己一个任务
 */
export function buildTasks(items){
  const once = [];
  const tasks = [];
  let cur = null;
  const close = () => { if (cur){ tasks.push(cur); cur = null; } };
  for (const it of items){
    if (!it.period){ close(); once.push(it); continue; }
    const gid = it.group || 0;
    if (gid && cur && cur.group === gid){ cur.items.push(it); continue; }
    close();
    cur = { group: gid, period: it.period, count: it.count || 0, items: [it], key: `t${tasks.length + 1}` };
  }
  close();
  return { once, tasks };
}

export class ScriptRunner {
  /**
   * @param {import('./session.js').I2cSession} session
   * @param {{onEvent?:(e:object)=>void}} opts
   */
  constructor(session, { onEvent } = {}){
    this.session = session;
    this.onEvent = onEvent || (() => {});
    this.signal = null;
    this.running = false;
    this.stats = null;
  }

  _emit(e){ try { this.onEvent(e); } catch (err){ console.warn('[i2c] runner 事件回调出错', err); } }

  /** 可中断的等待（定时器语义：至少等这么久）*/
  _sleep(ms){
    if (!(ms > 0)) return Promise.resolve();
    return new Promise(resolve => {
      let done = false;
      const off = this.signal?.onAbort?.(() => finish());
      const timer = setTimeout(finish, ms);
      function finish(){
        if (done) return;
        done = true;
        clearTimeout(timer);
        off?.();
        resolve();
      }
    });
  }

  stop(){
    if (!this.running) return false;
    this._emit({ type: 'stopping' });
    this.signal?.abort();
    return true;
  }

  /**
   * 跑一批命令项。**返回的 promise 在"一次性部分跑完 + 所有定时任务结束"之后才 resolve**
   * （不调用 `stop()` 且任务次数为 0 时，定时任务会一直跑 —— 那是"while(1)"的本意）。
   */
  async run(items, { label = '脚本' } = {}){
    if (this.running) throw new Error('已经有一段在跑了，先点「停止」');
    const { once, tasks } = buildTasks(items);
    if (!once.length && !tasks.length) throw new Error('没有可执行的命令');
    this.signal = makeSignal();
    this.running = true;
    this.stats = { label, startedAt: Date.now(), once: 0, ticks: 0, errors: 0, samples: 0, t0: performance.now() };
    this.session.setBusy(true);
    this._emit({ type: 'start', once: once.length, tasks: tasks.length, label });
    try {
      const probeGroups = tasks.length && this.session.runPeriodic && !this.session.usingMock
        ? this._probeGroups(tasks) : null;
      if (once.length){
        this._emit({ type: 'phase', phase: 'once', total: once.length });
        const r = await this._runSequence(once, null);
        this.stats.once = once.length;
        if (r.aborted) return this._finish('stopped');
      }
      if (tasks.length){
        this._emit({ type: 'phase', phase: 'timed', tasks: tasks.length });
        if (probeGroups) await this._runProbeTasks(probeGroups);
        else {
          this.session.log?.('i', '假探针周期由浏览器模拟');
          await Promise.all(tasks.map(t => this._runTask(t)));
        }
      }
      return this._finish('done');
    } catch (e){
      this._emit({ type: 'error', error: String(e?.message || e) });
      return this._finish('error');
    } finally {
      this.running = false;
      if (!this.session._periodic?.client.stopError) this.session.setBusy(false);
    }
  }

  _finish(why){
    const s = this.stats || {};
    const ms = performance.now() - (s.t0 || performance.now());
    this._emit({ type: 'end', why, ms, stats: { ...s, ms } });
    if (!this.session._periodic?.client.stopError) this.session.setBusy(false);
    this.running = false;
    return { why, ms, stats: this.stats };
  }

  /** 整段一条一条跑；`task` 非空时用它记账（`iter` = 这是循环的第几拍，从 1 起） */
  async _runSequence(items, task, iter = 1){
    for (const it of items){
      if (this.signal.stopped) return { aborted: true };
      await this._runOne(it, task, iter);
    }
    return { aborted: false };
  }

  /** 一个定时任务：按**绝对时间表**排，跑得慢就顺延（不累积漂移） */
  async _runTask(task){
    const t0 = performance.now();
    let n = 0;
    this._emit({ type: 'task-start', task });
    while (!this.signal.stopped){
      if (task.count && n >= task.count) break;
      const target = t0 + n * task.period;
      const wait = target - performance.now();
      if (wait > 0) await this._sleep(wait);
      if (this.signal.stopped) break;
      const iterStart = performance.now();
      const r = await this._runSequence(task.items, task, n + 1);
      n++;
      this.stats.ticks++;
      // 实测周期：跑得比设定还慢（I2C 慢 / 页面被限速）时如实报出来
      this._emit({
        type: 'tick', task, n,
        actualMs: n > 1 ? (performance.now() - t0) / n : performance.now() - iterStart,
        busyMs: performance.now() - iterStart,
        late: performance.now() - iterStart > task.period,
      });
      if (r.aborted) break;
    }
    this._emit({ type: 'task-end', task, ticks: n });
  }

  _probeGroups(tasks){
    return tasks.map(task => {
      const records = task.items.map(it => {
        if (it.kind === KIND.DELAY) return { ...delayRecord(Math.round(it.ms * 1000)), item: it };
        if (it.kind !== KIND.XFER || it.rd > RD_MAX)
          throw Error('probe 周期任务支持读写和延时；扫描及单条超过 54 B 的读请放到一次性部分');
        return { kind: BUS.I2C, data: actXfer({ dev: it.dev, addr: it.addr, wr: it.wr, rd: it.rd }).subarray(1), item: it };
      });
      program(records); // Validate the entire plan before any one-shot write.
      return { ...task, task, records, lastResult: records.findLastIndex(r => r.kind !== BUS.DELAY) };
    });
  }
  async _runProbeTasks(groups){
    this.session.log('i', '周期由 probe 定时器驱动；网页只读取结果');
    for (const g of groups) this._emit({ type: 'task-start', task: g.task });
    await this.session.runPeriodic(groups, {
      shouldStop: () => this.signal.stopped,
      onResult: (r, g) => {
        if (this.signal.stopped) return;
        const it = g.records[r.step].item;
        this._publish(it, g.task, r.cycle, { err: r.err, data: r.data, ms: 0, timeMs: r.timeMs, skipped: r.skipped });
        if (r.step === g.lastResult){
          this.stats.ticks++;
          const elapsed = g.lastTime == null ? null : ((r.timeMs - g.lastTime) >>> 0);
          this._emit({ type: 'tick', task: g.task, n: r.cycle,
            actualMs: elapsed ?? g.period, busyMs: 0, late: r.skipped > (g.skipped || 0),
            timeMs: r.timeMs, skipped: r.skipped, timing: 'probe' });
          g.lastTime = r.timeMs; g.skipped = r.skipped;
        }
      },
    });
    for (const g of groups) this._emit({ type: 'task-end', task: g.task });
  }

  /** 跑一条命令，并把结果发出去 */
  async _runOne(it, task, iter = 1){
    if (it.kind === KIND.DELAY){
      this._emit({ type: 'item', item: it, task, phase: 'begin' });
      await this._sleep(it.ms);
      this._emit({ type: 'item', item: it, task, phase: 'done', ms: it.ms });
      return;
    }
    if (it.kind === KIND.SCAN){
      this._emit({ type: 'item', item: it, task, phase: 'begin' });
      try {
        const { addrs, ms } = await this.session.scan();
        this._emit({ type: 'item', item: it, task, phase: 'done', ms, addrs });
      } catch (e){
        this.stats.errors++;
        this._emit({ type: 'item', item: it, task, phase: 'error', error: String(e?.message || e) });
      }
      return;
    }
    // XFER：定时任务从第 LOUD_TICKS+1 拍起转静默（成功不再逐笔写日志）
    const loud = !task || iter <= LOUD_TICKS;
    this._emit({ type: 'item', item: it, task, phase: 'begin' });
    // 读 >54 B 走 readLong()：分片、拼接、错误定位都在会话层做完，中间几笔不露出来
    const r = it.rd > RD_MAX
      ? await this.session.readLong(
          { dev: it.dev, addr: it.addr, rd: it.rd, chunk: it.chunk },
          { label: it.label || describeItem(it), quiet: !loud })
      : await this.session.transaction(
          { dev: it.dev, addr: it.addr, wr: it.wr, rd: it.rd },
          { label: it.label || describeItem(it), quiet: !loud });
    this._publish(it, task, iter, r);
  }
  _publish(it, task, iter, r){
    const loud = !task || iter <= LOUD_TICKS;
    let values = [];
    let warn = null;
    if (r.err === 0 && it.as?.fields?.length && r.data.length){
      const a = applyAs(it.as, r.data);
      values = a.values; warn = a.warn;
      if (values.length) this.stats.samples++;
    }
    if (r.err !== 0){
      this.stats.errors++;
      // 静默期里错误仍然要说话，但要限量（否则器件一直 NACK 会把日志刷爆）
      if (!loud && (task._errLogged = (task._errLogged || 0) + 1) <= ERR_LOGS_AFTER){
        this.session.log('e', `${it.label || describeItem(it)} → ${errText(r.err)}` +
          (task._errLogged === ERR_LOGS_AFTER ? `（此后同类错误不再刷屏，共失败 ${this.stats.errors} 笔）` : ''));
      }
    }
    this._emit({
      type: 'item', item: it, task, phase: 'done',
      err: r.err, data: r.data, ms: r.ms, values, warn,
      chunks: r.chunks || 1, failNote: r.failNote || '',
      timeMs: r.timeMs, skipped: r.skipped,
      hex: hexBytes(r.data),
    });
  }
}
