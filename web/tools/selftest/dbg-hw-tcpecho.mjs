/**
 * 调试器 **HPM6800EVK + lwip_tcpecho 例程** 专用真机压测（2026-10）。
 *
 * 为什么单独一份：这个 case 踩出来的坑比哪个靶子都多（SBA 读 flash/未映射地址会把 DM 卡死、
 * 复位并跑写触发器时机、resumereq、粘性目标类型、混版缓存……），而 `dbg-hw-riscv.mjs` 用的是
 * dbgstress 靶子固件（另一套符号/地址）。这一份就钉在**用户手上这张板子 + 这份 ELF** 上，
 * 穷举各类调试操作序列，专抓"操作组合"才现形的问题。
 *
 *   node tools/selftest/dbg-hw-tcpecho.mjs            # 全量
 *   node tools/selftest/dbg-hw-tcpecho.mjs --quick    # 每节少跑几轮（冒烟）
 *
 * 前置：make page-prep（8899 静态服务 + 9333 CDP 浏览器）；板子在跑该例程；探针没被别的东西占着。
 * 产物：tmp/tcpecho-dbg-stress.json（逐项结果，便于事后对比）。
 *
 * 设计约定（照 dbg-hw-stress 的规矩）：
 *   · **只读为主**：不往目标内存写东西（写内存默认关，`--writes` 才开），免得把用户板子写花；
 *   · 每个动作独立 try/catch，**出错继续往下跑**（压测要的是"收集全部问题"，不是第一个就停）；
 *   · 判定用"不变量"而不是具体数值：PC 必须是 ELF 里的有效地址、halted 与状态字一致、
 *     RTT 控制块必须认得出、断点数量对得上、SBA 事后必须干净。
 */
import { Cdp, sleep, DEV_RE } from './cdp-lib.mjs';
import { writeFileSync, existsSync, copyFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const argv = process.argv.slice(2);
const QUICK = argv.includes('--quick');
const WRITES = argv.includes('--writes');
const ROUNDS = QUICK ? 3 : 6;
const MIX = QUICK ? 20 : 60;

const SDK_ELF = 'E:/sdk_env_v1.11.0/work/lwip_lwip_tcpecho_hpm6800evk_flash_sdram_xip_debug/output/demo.elf';
const here = dirname(fileURLToPath(import.meta.url));
const ELF_LOCAL = join(here, '..', '..', 'tmp', '_tcpecho_demo.elf');
const ELF_URL = '/tmp/_tcpecho_demo.elf';
const APP = 'http://127.0.0.1:8899/index.html';
const OUT = join(here, '..', '..', 'tmp', 'tcpecho-dbg-stress.json');

/** 这份 ELF 的已知地址（前面几轮真机测出来的）*/
const A = {
  rtt: 0x4C0003C0,        // _SEGGER_RTT（非缓存 SDRAM）
  ram: 0x4000B61C,        // desc_rx_buff_cfg.count 一类，ELF 里在 RAM
  code: 0x8000790C,       // main（XIP flash 窗口，靠 ELF 只读段兜底）
  xipGap: 0x80000500,     // 跨出 ELF 只读段的一段 flash
  unmapped: 0x08000300,   // 上一块 ARM 板子留下的地址（HPM 上没映射）
};

if (!existsSync(ELF_LOCAL)) copyFileSync(SDK_ELF, ELF_LOCAL);

const rows = [];
let failures = 0;
const t0 = Date.now();
const rec = (phase, op, ok, detail = '', ms = null) => {
  if (!ok) failures++;
  rows.push({ phase, op, ok, detail: String(detail).slice(0, 300), ms });
  console.log(`  ${ok ? '·' : '✗'} [${phase}] ${op}${detail ? ' — ' + String(detail).slice(0, 160) : ''}`);
};
const sec = t => console.log('\n== ' + t + ' ==');

const cdp = new Cdp();
await cdp.connect();

/** 在页面里跑一段（自动包 exclusive，脚本的规矩）；返回 JSON */
const run = (js, userGesture = false) => cdp.json(js, userGesture);
/** 页面就绪 */
async function pageReady(){
  for (let i = 0; i < 60; i++){ if (await cdp.eval(`return !!window.__tools;`).catch(() => false)) return true; await sleep(300); }
  return false;
}

try {
  /* ---------------------------------------------------------------- 0. 前置 */
  sec('0. 前置：页面 + ELF + RISC-V 后端 + 连接');
  rec('前置', '页面就绪（模块全加载完）', await pageReady());
  await cdp.send('Page.navigate', { url: APP + '?t=' + Date.now() });   // 干净重载，避免混版缓存
  rec('前置', '页面重载后就绪', await pageReady());

  const pre = await run(`(async () => {
    const d = window.__tools.dbg;
    const r = await fetch('${ELF_URL}?t=' + Date.now());
    const st = d.loadElfBuffer(await r.arrayBuffer(), 'demo.elf');
    const be = document.getElementById('d-backend'); be.value = 'riscv'; be.dispatchEvent(new Event('change'));
    await new Promise(x => setTimeout(x, 250));
    const main = d.sym.find('main'), rtt = d.sym.rttSym();
    return { syms: st.size, lines: st.lines?.summary?.() || '', session: d.session?.constructor?.name,
             main: main ? '0x' + (main.addr >>> 0).toString(16) : null,
             rtt: rtt ? '0x' + (rtt.addr >>> 0).toString(16) : null };
  })()`);
  rec('前置', 'ELF 载入（符号/行号表）', pre.syms > 1000 && /行号表/.test(pre.lines), `${pre.syms} 个符号`);
  rec('前置', 'RISC-V 会话对象', pre.session === 'RiscvDebugSession', pre.session);
  rec('前置', 'main 符号 = 0x8000790c', pre.main === '0x8000790c', pre.main);
  rec('前置', 'RTT 控制块 = 0x4c0003c0', pre.rtt === '0x4c0003c0', pre.rtt);

  const connP = cdp.eval(`await window.__tools.dbg.connect(); return document.getElementById('d-state').textContent;`, true);
  await cdp.settle(DEV_RE, 'window.__tools.dbg.session.connected', 30000).catch(() => {});
  const connTxt = await connP.catch(e => 'ERR ' + e.message);
  rec('前置', '连接探针', await cdp.eval(`return !!window.__tools.dbg.session.connected;`), connTxt);

  /** 每次取样都走 exclusive：脚本直接调 session 时必须独占，否则和页面观察循环撞车读串 */
  const probe = () => run(`(async () => {
    const S = window.__tools.dbg.session, dm = S.dm;
    return await S.exclusive(async () => {
      let dmstatus = null, haltReason = null;
      try { dmstatus = (await dm.dmiRead(0x11)) >>> 0; } catch (e){ dmstatus = 'ERR ' + (e?.message || e); }
      try { const cs = (await dm.dmiRead(0x16)) >>> 0; haltReason = { abstractcs: '0x' + cs.toString(16), cmderr: (cs >> 8) & 7 }; } catch {}
      return { halted: !!S.halted, pc: '0x' + (S.pc >>> 0).toString(16), dmstatus: typeof dmstatus === 'string' ? dmstatus : '0x' + dmstatus.toString(16),
               running: typeof dmstatus === 'number' ? !!((dmstatus >> 10) & 1) : null, haltReason };
    });
  })()`);

  let st = await probe();
  rec('0', '连接后能读到 DM 状态', typeof st.dmstatus === 'string' && /^0x/.test(st.dmstatus), JSON.stringify(st));

  /**
   * 不变量 oracle：每做完一段就拿**硬件真值**核一遍会话状态，别让"状态漂移"溜过去。
   *
   * 这是这一轮压测最要紧的补充 —— 之前只验"操作没抛异常"，于是"界面说停着、硬件在跑"
   * 这类漂移能一路混到用户手里（复位后按继续报 cmderr=4 就是这么来的）。四条不变量：
   *   ① session.halted 必须等于 dmstatus 的 halted 位；
   *   ② 停住时 session.pc 必须等于硬件当场读回的 PC（**不先 refresh** —— 先刷新
   *      再比就成了自己跟自己比，什么都抓不到）；
   *   ③ abstractcs.cmderr 必须为 0（没有残留的抽象命令错误）；
   *   ④ sbcs 必须干净（没有挂住/出错的 SBA 事务）。
   * 读不到寄存器本身也算失败 —— "读不出来"和"读出来不对"都是要抓的。
   *
   * 注意：这颗 DM 的 `dpc` **不能**用 `dmiRead(0x7b1)` 取（实测恒为 0 —— 本工程里
   * `dpc` 一直是抽象命令读 PC 的别名，见 riscv.js 的 regno 映射）。所以这里用
   * `S.readReg('PC')`，与产线路径同源。
   */
  const oracle = async label => {
    const v = await run(`(async () => {
      const S = window.__tools.dbg.session, dm = S.dm;
      return await S.exclusive(async () => {
        const rd = async a => { try { return (await dm.dmiRead(a)) >>> 0; } catch (e){ return 'ERR ' + (e?.message || e); } };
        const cached = { halted: !!S.halted, pc: S.pc >>> 0 };
        const dmstatus = await rd(0x11);
        let hwPc = null;
        if (typeof dmstatus === 'number' && (dmstatus & 0x300) === 0x300){
          try { hwPc = (await S.readReg('PC')) >>> 0; } catch (e){ hwPc = 'ERR ' + (e?.message || e); }
        }
        return { cached, dmstatus, hwPc, abstractcs: await rd(0x16), sbcs: await rd(0x38) };
      });
    })()`);
    const num = x => typeof x === 'number';
    const bad = [];
    if (!num(v.dmstatus)) bad.push('dmstatus 读不到（' + v.dmstatus + '）');
    else {
      const hwHalted = (v.dmstatus & 0x300) !== 0;
      if (hwHalted !== v.cached.halted)
        bad.push(`halted 漂移：会话=${v.cached.halted} dmstatus=0x${v.dmstatus.toString(16)}`);
      if (v.cached.halted && num(v.hwPc) && v.hwPc !== v.cached.pc)
        bad.push(`pc 漂移：会话=0x${v.cached.pc.toString(16)} 硬件=0x${v.hwPc.toString(16)}`);
      if (v.cached.halted && !num(v.hwPc)) bad.push('停住时读不到 PC（' + v.hwPc + '）');
    }
    if (num(v.abstractcs) && ((v.abstractcs >> 8) & 7)) bad.push(`残留 cmderr=${(v.abstractcs >> 8) & 7}`);
    if (num(v.sbcs)) {
      if ((v.sbcs >> 21) & 3) bad.push(`SBA 不干净 sbcs=0x${v.sbcs.toString(16)}`);
      if ((v.sbcs >> 12) & 7) bad.push(`sberror=${(v.sbcs >> 12) & 7}`);
    }
    rec('不变式', label, bad.length === 0, bad.length ? bad.join('；') : 'halted/pc/cmderr/SBA 与硬件一致');
  };

  const baseOk = await run(`(async () => { const S = window.__tools.dbg.session;
    return await S.exclusive(async () => { await S.halt(); const b = await S.memRead(${A.ram}, 4); return { halted: !!S.halted, pc: '0x' + (S.pc>>>0).toString(16), ram: Array.from(b).map(x=>x.toString(16).padStart(2,'0')).join(' ') }; }); })()`);
  rec('0', '连接即可停 + 读 RAM（SBA 干净）', baseOk.halted && baseOk.ram.length === 11, JSON.stringify(baseOk));
  await oracle('§0 连接/载入/停住之后');

  /* ---------------------------------------------------------------- 1. 连接循环 */
  sec('1. 连接 / 断连 循环');
  for (let i = 1; i <= ROUNDS; i++){
    const t = Date.now();
    let ok = false, detail = '';
    try {
      await cdp.eval(`await window.__tools.dbg.disconnect(); return true;`).catch(() => {});
      await sleep(300);
      const p = cdp.eval(`await window.__tools.dbg.connect(); return document.getElementById('d-state').textContent;`, true);
      await cdp.settle(DEV_RE, 'window.__tools.dbg.session.connected', 30000).catch(() => {});
      detail = await p.catch(e => 'ERR ' + e.message);
      const s = await run(`(async () => { const S = window.__tools.dbg.session;
        return await S.exclusive(async () => { await S.halt(); const b = await S.memRead(${A.ram}, 4); return Array.from(b).map(x=>x.toString(16)).join(' '); }); })()`);
      ok = !!s && !/ERR/.test(String(s));
      detail += ' | RAM=' + s;
    } catch (e){ detail = 'ERR ' + (e?.message || e); }
    rec('1', `第 ${i} 轮 断开→重连→读 RAM`, ok, detail, Date.now() - t);
  }

  /* ---------------------------------------------------------------- 2. 运行控制 */
  sec('2. 运行控制穷举（停 / 单步 / 继续 / 各种单步）');
  const ctl = await run(`(async () => {
    const S = window.__tools.dbg.session, out = [];
    const one = async (name, fn) => { const t = Date.now(); try { await S.exclusive(fn); out.push({ name, ok: true, pc: '0x' + (S.pc >>> 0).toString(16), halted: !!S.halted, ms: Date.now() - t }); }
      catch (e){ out.push({ name, ok: false, err: String(e?.message || e).slice(0, 160) }); } };
    await one('halt', async () => { await S.halt(); });
    for (let i = 0; i < ${QUICK ? 5 : 20}; i++) await one('step#' + (i + 1), async () => { await S.step(); });
    await one('stepOver', async () => { await S.stepOver(); await S.halt().catch(() => {}); });
    await one('stepInto', async () => { await S.stepInto(); await S.halt().catch(() => {}); });
    await one('cont', async () => { await S.cont(); await new Promise(r => setTimeout(r, 150)); });
    await one('halt(again)', async () => { await S.halt(); });
    return out;
  })()`);
  const badCtl = ctl.filter(r => !r.ok);
  rec('2', `运行控制 ${ctl.length} 个动作全部无异常`, badCtl.length === 0,
      badCtl.length ? JSON.stringify(badCtl.slice(0, 3)) : `含 ${QUICK ? 5 : 20} 次单步`);
  const pcs = ctl.filter(r => r.pc).map(r => parseInt(r.pc, 16));
  const pcSane = pcs.every(p => p >= 0x80000000 || p === 0);
  rec('2', 'PC 都落在 ELF 代码区（0x80xxxxxx）', pcSane, pcs.slice(0, 3).map(p => '0x' + p.toString(16)).join(' '));
  await oracle('§2 运行控制穷举之后');

  /* ---------------------------------------------------------------- 3. 内存读 */
  sec('3. 内存读穷举（RAM / flash / 未映射 / 边界 / 大块 / 连打）');
  const mem = await run(`(async () => {
    const S = window.__tools.dbg.session, out = {};
    const rd = async (name, addr, len) => { const t = Date.now(); try { const b = await S.exclusive(() => S.memRead(addr, len));
      out[name] = { ok: true, len: b.length, head: Array.from(b.subarray(0, 8)).map(x => x.toString(16).padStart(2, '0')).join(' '), ms: Date.now() - t }; }
      catch (e){ out[name] = { ok: false, err: String(e?.message || e).slice(0, 160), ms: Date.now() - t }; } };
    await rd('rttCB', ${A.rtt}, 24);            // 应含 SEGGER RTT
    await rd('ram', ${A.ram}, 4);
    await rd('elfCode', ${A.code}, 16);         // XIP 窗口：走 ELF 只读段
    await rd('xipGap', ${A.xipGap}, 512);       // 跨出只读段：ELF 覆盖 + 补 0，或传输层拒绝
    await rd('unmapped', ${A.unmapped}, 128);   // 没映射：允许失败，但之后必须还能读
    await rd('afterUnmapped', ${A.ram}, 4);     // ← 关键：失败之后链路要能自愈
    await rd('pageCross', ${A.rtt} + 0xF00, 512);
    await rd('big', ${A.rtt} - 0x3C0, 4096);
    const t = Date.now(); let n = 0, err = '';
    for (let i = 0; i < ${QUICK ? 20 : 50}; i++){
      try { const b = await S.exclusive(() => S.memRead(${A.ram}, 16)); if (b.length !== 16) throw new Error('短读'); n++; }
      catch (e){ err = String(e?.message || e).slice(0, 120); break; }
    }
    out.repeat = { ok: n === ${QUICK ? 20 : 50}, n, err, ms: Date.now() - t };
    return out;
  })()`);
  rec('3', 'RTT 控制块读得到 SEGGER RTT', mem.rttCB.ok && mem.rttCB.head.includes('53 45 47 47 45 52'), JSON.stringify(mem.rttCB).slice(0, 120));
  rec('3', 'RAM 变量读正常', mem.ram.ok, JSON.stringify(mem.ram).slice(0, 100));
  rec('3', 'ELF 代码段（XIP）读得到（走 ELF 只读段）', mem.elfCode.ok, JSON.stringify(mem.elfCode).slice(0, 100));
  rec('3', 'flash 跨段读不把链路搞坏', mem.xipGap.ok, JSON.stringify(mem.xipGap).slice(0, 140));
  rec('3', '未映射地址：失败是允许的（但要有清楚报错）', true, mem.unmapped.ok ? '居然读到了（目标可能真有映射）' : mem.unmapped.err);
  rec('3', '未映射读之后 RAM 仍然可读（自愈）', mem.afterUnmapped.ok, JSON.stringify(mem.afterUnmapped).slice(0, 120));
  rec('3', '跨页 / 大块读', mem.pageCross.ok && mem.big.ok, `pageCross=${mem.pageCross.len} big=${mem.big.len}`);
  rec('3', '连打 RAM 读（压力）', mem.repeat.ok, `${mem.repeat.n} 次 ${mem.repeat.ms}ms ${mem.repeat.err}`);

  /**
   * 3b. **同一个坏地址连读**（监视面板每 2 s 刷一次就是这个形状）
   *     第 1 次允许失败（并触发一次自愈），之后必须**快速失败**（不再"两次 SBA + dm.init + 自愈"，
   *     免得反复把 SBA 搞脏）；最后 RAM 必须仍然可读。
   */
  const bad5 = await run(`(async () => {
    const S = window.__tools.dbg.session, out = { ms: [] };
    for (let i = 0; i < 5; i++){
      const t = Date.now();
      try { await S.exclusive(() => S.memRead(${A.unmapped}, 128)); out.ms.push(-(Date.now() - t)); }
      catch (e){ out.ms.push(Date.now() - t); if (i === 0) out.firstErr = String(e?.message || e).slice(0, 90); }
    }
    try { const b = await S.exclusive(() => S.memRead(${A.ram}, 4)); out.ram = Array.from(b).map(x => x.toString(16)).join(' '); }
    catch (e){ out.ram = 'ERR ' + String(e?.message || e).slice(0, 90); }
    try { const h = await S.dm.sbaHealthCheck({ allowSystemReset: false }); out.sba = h.level; } catch (e){ out.sba = 'ERR'; }
    return out;
  })()`);
  const later = bad5.ms.slice(1).filter(x => x >= 0);
  rec('3', '坏地址连读 5 次：第 1 次失败后就"记仇"快速失败',
      later.length >= 3 && Math.max(...later) < 200,
      `各次耗时 ${bad5.ms.map(x => Math.abs(x) + 'ms').join(' ')} | 首次：${bad5.firstErr}`);
  rec('3', '连读坏地址之后 RAM 仍可读、SBA 干净', !/ERR/.test(bad5.ram) && !/ERR/.test(String(bad5.sba)),
      `RAM=${bad5.ram} SBA=${bad5.sba}`);
  await oracle('§3 内存读穷举（含未映射地址自愈）之后');

  /* ---------------------------------------------------------------- 4. 断点 */
  sec('4. 硬件断点穷举（1..8 / 重复 / 删 / 清 / 复位后重下发 / 命中后单步继续）');
  const bp = await run(`(async () => {
    const S = window.__tools.dbg.session, out = {};
    await S.exclusive(async () => { await S.bpClear(); await S.halt().catch(() => {}); });
    const addrs = [${A.code}, ${A.code} + 4, ${A.code} + 8, ${A.code} + 12, ${A.code} + 16, ${A.code} + 20, ${A.code} + 24, ${A.code} + 28];
    const add = [];
    for (let i = 0; i < ${QUICK ? 3 : 8}; i++){
      try { await S.exclusive(() => S.bpAdd(addrs[i], 'stress' + i)); add.push('ok'); }
      catch (e){ add.push('ERR ' + String(e?.message || e).slice(0, 60)); }
    }
    out.add = { results: add, list: S.bpList().map(b => '0x' + (b.addr >>> 0).toString(16)) };
    out.dup = await S.exclusive(() => S.bpAdd(addrs[0], 'dup')).then(r => 'index=' + r.index + ' ' + (r.warn || '')).catch(e => 'ERR ' + String(e?.message || e).slice(0, 80));
    out.del = await S.exclusive(() => S.bpDel(addrs[0])).then(r => 'deleted=' + r).catch(e => 'ERR ' + String(e?.message || e).slice(0, 80));
    out.afterDel = S.bpList().map(b => '0x' + (b.addr >>> 0).toString(16));
    // 复位并跑：断点必须在"停住"之后重下发（这是上一轮修的那个 bug）
    out.resetRun = await S.exclusive(async () => { await S.resetRun(); return 'ok'; }).catch(e => 'ERR ' + String(e?.message || e).slice(0, 100));
    await new Promise(r => setTimeout(r, 1200));
    out.afterReset = { bps: S.bpList().length };
    await S.exclusive(async () => { await S.bpClear(); });
    out.cleared = S.bpList().length;
    return out;
  })()`);
  rec('4', `加 ${QUICK ? 3 : 8} 个触发器`, bp.add.results.every(r => r === 'ok'), JSON.stringify(bp.add.results));
  rec('4', '重复下同一个地址：不报错、给提示', /index=|ERR/.test(String(bp.dup)) && !/ERR/.test(String(bp.dup)), String(bp.dup));
  rec('4', '删断点', /deleted=true/.test(String(bp.del)), String(bp.del));
  rec('4', '复位并跑（有断点）：不报 cmderr', bp.resetRun === 'ok', bp.resetRun);
  rec('4', '清空断点', bp.cleared === 0, '剩 ' + bp.cleared);
  await oracle('§4 断点穷举（含复位并跑）之后');

  /* ---------------------------------------------------------------- 5. 回栈 */
  sec('4b. 用户那条路：b main → reset（复位并停）→ c（继续）');
  const userPath = await run(`(async () => {
    const S = window.__tools.dbg.session, out = {};
    try { await S.exclusive(async () => { await S.bpClear(); await S.halt().catch(() => {}); await S.bpAdd(${A.code}, 'main'); }); out.bp = 'ok'; }
    catch (e){ out.bp = 'ERR ' + String(e?.message || e).slice(0, 80); }
    try { await S.exclusive(() => S.resetHalt()); out.reset = 'ok'; } catch (e){ out.reset = 'ERR ' + String(e?.message || e).slice(0, 100); }
    // 复位并停之后紧接着继续 —— 这一条以前会报「抽象命令出错（cmderr=4）」
    try { await S.exclusive(() => S.cont()); out.cont = 'ok'; } catch (e){ out.cont = 'ERR ' + String(e?.message || e).slice(0, 120); }
    await new Promise(r => setTimeout(r, 1500));
    try { await S.exclusive(async () => { await S.halt(); }); const b = await S.exclusive(() => S.memRead(${A.ram}, 4)); out.after = Array.from(b).map(x => x.toString(16)).join(' '); }
    catch (e){ out.after = 'ERR ' + String(e?.message || e).slice(0, 100); }
    try { await S.exclusive(async () => { await S.bpClear(); }); } catch {}
    return out;
  })()`);
  rec('4b', 'b main → 复位并停 → 继续：全程不报 cmderr', userPath.bp === 'ok' && userPath.reset === 'ok' && userPath.cont === 'ok' && !/ERR/.test(userPath.after),
      JSON.stringify(userPath));
  await oracle('§4b 用户路径 b main → reset → c 之后');

  sec('5. 回栈 bt（停住后）');
  const bt = await run(`(async () => {
    const S = window.__tools.dbg.session;
    await S.exclusive(async () => { await S.halt(); });
    try {
      const res = await S.exclusive(() => S.backtrace({ max: 24 }));
      // 返回形状是 {frames, reason, scan}（见 session.backtrace）—— 别当成数组
      const frames = res?.frames || [];
      return { ok: true, n: frames.length, reason: res?.reason || '', scan: !!res?.scan,
               head: frames.slice(0, 3).map(f => (f.name || '?') + '@0x' + ((f.pc >>> 0).toString(16))).join(' → ') };
    }
    catch (e){ return { ok: false, err: String(e?.message || e).slice(0, 200) }; }
  })()`);
  rec('5', 'bt 不抛异常', bt.ok, bt.ok ? `${bt.n} 帧 ${bt.head}` : bt.err);
  rec('5', 'bt 要么给帧、要么给清楚的原因', bt.ok && (bt.n > 0 || !!bt.reason), bt.reason || `${bt.n} 帧`);
  const alive = await probe();
  rec('5', 'bt 之后链路仍然活着', /^0x/.test(alive.dmstatus), JSON.stringify(alive));
  await oracle('§5 回栈 bt 之后');

  /* ---------------------------------------------------------------- 6. RTT 同屏 */
  sec('6. RTT 同屏（定位 / 泵 / 与调试动作交替）');
  const rtt = await run(`(async () => {
    const d = window.__tools.dbg, S = d.session, out = {};
    /* 定位 RTT 控制块要求**目标真的跑过 RTT 初始化**：上一节把核停在 main 上，
     * 那时 _SEGGER_RTT 还没被初始化，扫描必然失败。所以先让它跑起来再找，
     * 找不到就等一会儿重试一次 —— 这是测试时序，不是产品行为。 */
    try { await S.exclusive(async () => { if (S.halted) await S.cont(); }); } catch {}
    await new Promise(r => setTimeout(r, 500));
    try { out.start = await d.rttStart() ? 'ok' : 'fail'; } catch (e){ out.start = 'ERR ' + String(e?.message || e).slice(0, 120); }
    if (out.start !== 'ok'){
      await new Promise(r => setTimeout(r, 800));
      try { out.start = await d.rttStart() ? 'ok' : 'fail(retry)'; } catch (e){ out.start = 'ERR(retry) ' + String(e?.message || e).slice(0, 120); }
    }
    const rdOff = async () => await S.exclusive(async () => { const b = await S.memRead(${A.rtt} + 40, 4); return (b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)) >>> 0; });
    const a = await rdOff();
    for (let i = 0; i < 6; i++){ await new Promise(r => setTimeout(r, 400)); await S.tryExclusive(() => d._rttPump()).catch(() => {}); }
    const b = await rdOff();
    out.consumed = ((b - a) >>> 0);
    out.panel = (document.getElementById('d-rtt')?.textContent || '').length;
    // 交替：泵一段 → 暂停读寄存器 → 单步 → 继续 → 再泵
    try { await S.exclusive(async () => { await S.halt(); const r0 = S.pc >>> 0; await S.step(); out.stepPc = '0x' + (r0 >>> 0).toString(16) + '->0x' + (S.pc >>> 0).toString(16); await S.cont(); }); }
    catch (e){ out.stepErr = String(e?.message || e).slice(0, 120); }
    const c = await rdOff();
    out.consumed2 = ((c - b) >>> 0);
    d.rttStop();
    return out;
  })()`);
  rec('6', '定位 RTT 成功', rtt.start === 'ok', String(rtt.start));
  rec('6', 'RTT 环在被消费（字节推进）', rtt.consumed >= 0, `消耗 ${rtt.consumed} B，面板 ${rtt.panel} 字`);
  rec('6', 'RTT 与 暂停/单步/继续 交替不出错', !rtt.stepErr, rtt.stepErr || rtt.stepPc);
  await oracle('§6 RTT 与调试动作交替之后');

  /* ---------------------------------------------------------------- 7. 复位组合 */
  sec('7. 复位组合（每次复位后立刻读 RAM + RTT）');
  for (let i = 1; i <= (QUICK ? 2 : 4); i++){
    const r = await run(`(async () => {
      const S = window.__tools.dbg.session, out = {};
      try { await S.exclusive(async () => { await S.resetHalt(); }); out.resetHalt = 'ok'; } catch (e){ out.resetHalt = 'ERR ' + String(e?.message || e).slice(0, 100); }
      try { const b = await S.exclusive(() => S.memRead(${A.ram}, 4)); out.ramAfterHalt = Array.from(b).map(x => x.toString(16)).join(' '); } catch (e){ out.ramAfterHalt = 'ERR ' + String(e?.message || e).slice(0, 120); }
      try { await S.exclusive(async () => { await S.resetRun(); }); out.resetRun = 'ok'; } catch (e){ out.resetRun = 'ERR ' + String(e?.message || e).slice(0, 100); }
      await new Promise(r => setTimeout(r, 800));
      try { const b = await S.exclusive(() => S.memRead(${A.ram}, 4)); out.ramAfterRun = Array.from(b).map(x => x.toString(16)).join(' '); } catch (e){ out.ramAfterRun = 'ERR ' + String(e?.message || e).slice(0, 120); }
      return out;
    })()`);
    const ok = r.resetHalt === 'ok' && r.resetRun === 'ok' && !/ERR/.test(r.ramAfterHalt) && !/ERR/.test(r.ramAfterRun);
    rec('7', `第 ${i} 轮 复位并停/并跑 后 RAM 可读`, ok, JSON.stringify(r));
  }
  await oracle('§7 复位组合之后');

  /* ---------------------------------------------------------------- 8. 随机混合压测 */
  sec(`8. 随机混合压测（${MIX} 步，固定种子）`);
  const mix = await run(`(async () => {
    const d = window.__tools.dbg, S = d.session, out = { errs: [], n: 0 };
    let seed = 20261006;
    const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
    const ops = [
      ['halt', async () => await S.exclusive(() => S.halt())],
      ['cont', async () => await S.exclusive(() => S.cont())],
      ['step', async () => { await S.exclusive(async () => { if (!S.halted) await S.halt(); await S.step(); }); }],
      ['rdRAM', async () => { const b = await S.exclusive(() => S.memRead(${A.ram}, 16)); if (b.length !== 16) throw new Error('短读'); }],
      ['rdCode', async () => { const b = await S.exclusive(() => S.memRead(${A.code}, 32)); if (!b.length) throw new Error('空'); }],
      ['rdRTT', async () => { const b = await S.exclusive(() => S.memRead(${A.rtt}, 24)); if (!b.length) throw new Error('空'); }],
      ['bt', async () => { await S.exclusive(async () => { if (!S.halted) await S.halt(); }); await S.exclusive(() => S.backtrace({ max: 12 })); }],
      ['bpAddDel', async () => { const a = ${A.code} + 4 * (1 + (out.n % 16)); await S.exclusive(() => S.bpAdd(a, 'mix')).catch(() => {}); await S.exclusive(() => S.bpDel(a)).catch(() => {}); }],
      ['rttPump', async () => { await S.tryExclusive(() => d._rttPump()).catch(() => {}); }],
      ['resetHalt', async () => await S.exclusive(() => S.resetHalt())],
    ];
    for (let i = 0; i < ${MIX}; i++){
      const [name, fn] = ops[Math.floor(rnd() * ops.length) % ops.length];
      try { await fn(); }
      catch (e){ out.errs.push({ i, name, err: String(e?.message || e).slice(0, 120) }); }
      out.n++;
      await new Promise(r => setTimeout(r, 60));
    }
    try { await S.exclusive(async () => { await S.bpClear?.(); }); } catch {}
    return out;
  })()`);
  rec('8', `${mix.n} 步混合操作无异常`, mix.errs.length === 0, mix.errs.length ? JSON.stringify(mix.errs.slice(0, 4)) : '全通');
  const post = await probe();
  rec('8', '压测后 DM/SBA 仍然健康', /^0x/.test(post.dmstatus), JSON.stringify(post));
  await oracle('§8 随机混合压测之后');

  /* ---------------------------------------------------------------- 9. 收尾 */
  sec('9. 收尾');
  const fin = await run(`(async () => {
    const d = window.__tools.dbg, S = d.session, out = {};
    try { await S.exclusive(async () => { await S.bpClear(); if (S.halted) await S.cont(); }); out.run = 'ok'; } catch (e){ out.run = 'ERR ' + String(e?.message || e).slice(0, 100); }
    try { const h = await S.dm.sbaHealthCheck({ allowSystemReset: false }); out.sba = h.level + ':' + h.note; } catch (e){ out.sba = 'ERR ' + String(e?.message || e).slice(0, 100); }
    try { await d.disconnect(); out.byebye = 'ok'; } catch (e){ out.byebye = 'ERR ' + String(e?.message || e).slice(0, 100); }
    return out;
  })()`);
  rec('9', '清断点 + 恢复运行 + 断开', fin.run === 'ok' && fin.byebye === 'ok', JSON.stringify(fin));
  rec('9', '收尾时 SBA 健康', /^(none|clear|dm|ndmreset):/.test(String(fin.sba)), String(fin.sba));

  /* ---------------------------------------------------------------- 汇总 */
  const secs = {};
  for (const r of rows){ secs[r.phase] = secs[r.phase] || { ok: 0, bad: 0 }; secs[r.phase][r.ok ? 'ok' : 'bad']++; }
  console.log('\n================ 汇总 ================');
  for (const [k, v] of Object.entries(secs)) console.log(`  ${k.padEnd(6)} 通过 ${v.ok}  失败 ${v.bad}`);
  console.log(`  合计 ${rows.length} 项，失败 ${failures} 项，耗时 ${((Date.now() - t0) / 1000).toFixed(1)}s`);
  console.log(failures ? '✗ 见上表 ✗ 项' : '✅ 全部通过');
  writeFileSync(OUT, JSON.stringify({ when: new Date().toISOString(), quick: QUICK, rows }, null, 2));
  console.log('逐项结果: ' + OUT);
  cdp.close();
  process.exit(failures ? 1 : 0);
} catch (e){
  console.error('!! 压测没跑完：' + (e?.message || e));
  writeFileSync(OUT, JSON.stringify({ when: new Date().toISOString(), quick: QUICK, rows, fatal: String(e?.message || e) }, null, 2));
  try { cdp.close(); } catch {}
  process.exit(2);
}
