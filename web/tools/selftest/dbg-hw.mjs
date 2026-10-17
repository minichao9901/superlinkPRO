/**
 * 「调试器」页的**真机冒烟**（CDP 驱动真页面 + 真探针 + 真目标板，只读为主）：
 *   node tools/selftest/dbg-hw.mjs          （等价：make test-dbg-hw）
 * 前置：page-prep（8899 服务 + 9333 浏览器），并且探针已在浏览器里授权过一次。
 *
 * 做的是**低风险**动作：连接 → 读 IDCODE/寄存器/内存 → 暂停 → 单步 → 继续 → 暂停 → 试一次断点，
 * **不写内存、不改寄存器、不烧录**。跑完会把探针状态收拾干净（清掉自己下的比较器 + 断开）。
 * 目标板随便什么都能跑（H7B0 / F103 都试过），只要 SWD 接上了。
 */
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');
const CDP = process.env.CDP || 'http://127.0.0.1:9333';
const APP = process.env.APP || 'http://127.0.0.1:8899/index.html';
const URL_ = APP + '?t=' + Date.now() + '#dbg';

setTimeout(() => { console.error('[WATCHDOG] 总超时'); process.exit(9); }, 240000);
const sleep = ms => new Promise(r => setTimeout(r, ms));
let pass = 0, fail = 0, skip = 0;
const ok = (cond, name, extra = '') => {
  if (cond){ pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name} ${extra}`); }
};
const info = (s) => console.log('  ·  ' + s);

async function ensureBrowser(){
  try { await fetch(CDP + '/json/version', { signal: AbortSignal.timeout(2500) }); return; } catch {}
  console.log('  （CDP 浏览器没在跑，自己拉一个…）');
  const ps = spawn('pwsh', ['-NoProfile', '-File', join(root, 'tools', 'selftest', 'launch-browser.ps1'), '-Port', '9333', '-Url', APP],
    { stdio: 'ignore', detached: true });
  ps.unref();
  for (let i = 0; i < 90; i++){
    await sleep(500);
    try { await fetch(CDP + '/json/version', { signal: AbortSignal.timeout(2000) }); return; } catch {}
  }
  throw new Error('等 CDP 浏览器超时');
}

await ensureBrowser();
const list = await (await fetch(CDP + '/json/list', { signal: AbortSignal.timeout(5000) })).json();
const page = list.find(t => t.type === 'page');
if (!page) throw new Error('CDP 里没有页面目标');
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('CDP 连不上')); });
let seq = 0; const pend = new Map();
ws.onmessage = e => {
  const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)){ const p = pend.get(m.id); pend.delete(m.id); m.error ? p.rej(new Error(m.error.message)) : p.res(m.result); }
};
const send = (method, params = {}, t = 30000) => new Promise((res, rej) => {
  const id = ++seq; pend.set(id, { res, rej });
  ws.send(JSON.stringify({ id, method, params }));
  setTimeout(() => { if (pend.delete(id)) rej(new Error(method + ' 超时')); }, t);
});
async function ev(expr){
  const r = await send('Runtime.evaluate', { expression: `(async()=>{ ${expr} })()`, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error('页面里报错：' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
  return r.result.value;
}
async function until(expr, ms = 6000, step = 150){
  const t0 = Date.now();
  for (;;){
    if (await ev(`return !!(${expr});`)) return true;
    if (Date.now() - t0 > ms) return false;
    await sleep(step);
  }
}

await send('Page.enable'); await send('Runtime.enable');
try { await send('Network.enable'); await send('Network.setCacheDisabled', { cacheDisabled: true }); } catch {}
await send('Page.navigate', { url: URL_ });
console.log('目标: ' + URL_);
for (let i = 0; i < 60; i++){
  await sleep(500);
  try { if (await ev('return !!window.__tools?.dbg;')) break; } catch {}
}
if (!await ev('return !!window.__tools?.dbg;')) throw new Error('页面没起来');

console.log('== 0. 探针授权与就绪 ==');
{
  const r = await ev(`
    const devs = await navigator.usb.getDevices();
    return devs.map(d => ({ vid: d.vendorId, pid: d.productId, name: d.productName }));`);
  info('已授权设备：' + JSON.stringify(r));
  if (!r.length){
    skip++;
    console.log('  SKIP  浏览器里还没有授权过任何 USB 设备 —— 先在页面上点一次「连接」并在弹框里选探针，再跑本脚本');
    console.log(`\n== 汇总：${pass} 通过 / ${fail} 失败 / ${skip} 跳过 ==`);
    ws.close();
    process.exit(0);
  }
}

console.log('== 1. 连接（WebUSB · 10 MHz，与页面默认一致）==');
{
  const r = await ev(`
    const d = window.__tools.dbg;
    d._stopWatch();          // 🚨 脚本自己独占 SWD：观察循环还在读的话会和这里的每次调用交错（实测 18% 读到废值）
    document.getElementById('d-backend').value = 'webusb';
    document.getElementById('d-backend').dispatchEvent(new Event('change'));
    document.getElementById('d-clock').value = '10000';        // 页面默认档：真机实测 PPB/内存都正常
    const okc = await d.connect();
    d._stopWatch();          // connect() 见目标在跑会重新开观察循环，这里再关一次
    /**
     * 🚨 目标可能是**运行中**的（上一次冒烟收尾会把它放跑/板子本来就在跑）——
     * 先停下来再读寄存器。不停的话 regList 是空的，下面两条会莫名其妙地红（实测踩过）。
     */
    const wasRunning = !d.session.halted;
    if (wasRunning) await d.session.halt();
    d.renderRegs();
    return { okc, wasRunning, sum: d.summary(), err: d.session.lastError || null };`);
  ok(r.okc === true && r.sum.connected, '网页连上了探针', JSON.stringify(r.sum));
  if (!r.sum.connected){
    const out = await ev('return document.getElementById("d-out").textContent.slice(-600);');
    console.log('  页面日志尾部：\n' + out);
    console.log(`\n== 汇总：${pass} 通过 / ${fail} 失败 ==`);
    ws.close();
    process.exit(1);
  }
  if (r.wasRunning) info('连上时目标在跑 —— 已先「暂停」再读寄存器（脚本自己保证幂等）');
  ok(r.sum.clockKhz === 10000, `实际用的是 10 MHz（${r.sum.clockKhz} kHz；若探针在 10 MHz 下读 PPB 回 0，session 会自动退回 1 MHz 并在页面日志里说明）`, String(r.sum.clockKhz));
  info(`后端 ${r.sum.backend} · SWD ${r.sum.clockKhz} kHz · 断点容量 ${r.sum.bpCap}（FPB rev${(await ev('return window.__tools.dbg.session.caps.rev;'))}）`);
  ok(r.sum.bpCap > 0, '读到了 FPB 比较器个数（>0）', String(r.sum.bpCap));
  info('IDCODE = 0x' + (await ev('return window.__tools.dbg.session.idcode.toString(16);')).toUpperCase());
  ok(r.sum.regs === 23, '读回 23 个寄存器', String(r.sum.regs));
  const pc = r.sum.pc >>> 0;
  ok(pc !== 0 && pc !== 0xffffffff, 'PC 是个像样的值', '0x' + pc.toString(16));
  info('PC = 0x' + pc.toString(16) + ' · 状态 ' + (r.sum.halted ? '已停止' : '运行中'));
}

console.log('== 2. 读内存（向量表 + PC 附近，只读）==');
{
  const r = await ev(`
    const d = window.__tools.dbg;
    const vt = await d.session.memRead(0x08000000, 8);
    const pc = d.session.pc;
    let near = null, nearErr = '';
    try { near = await d.session.memRead(pc & ~3, 16); } catch (e){ nearErr = e.message; }
    const hx = b => [...b].map(x => x.toString(16).padStart(2, '0')).join(' ');
    const u32 = (b,o) => (b[o] | (b[o+1]<<8) | (b[o+2]<<16) | (b[o+3]<<24)) >>> 0;
    return { vt: hx(vt), sp: u32(vt,0), reset: u32(vt,4), near: near ? hx(near) : null, nearErr };`);
  info('0x08000000: ' + r.vt + '  →  SP=0x' + r.sp.toString(16) + '  复位向量=0x' + r.reset.toString(16));
  ok(r.sp !== 0 && r.sp !== 0xffffffff, '向量表里的初始 SP 是个像样的值（真的读到目标 flash 了）', '0x' + r.sp.toString(16));
  ok(/^[0-9a-f ]+$/.test(r.vt), '读回的字节格式正常', r.vt);
  if (r.near) info('PC 附近: ' + r.near);
  else info('PC 附近读失败（可能 PC 落在不可读区）：' + r.nearErr);
}

console.log('== 3. 暂停 / 单步 / 继续（不动内存与寄存器）==');
{
  const r = await ev(`
    const d = window.__tools.dbg;
    await d.session.halt();
    const a = { h: d.session.halted, pc: d.session.pc };
    await d.session.step();
    const b = { pc: d.session.pc };
    await d.session.step();
    const c = { pc: d.session.pc };
    await d.session.refresh();
    return { a, b, c, h: d.session.halted };`);
  ok(r.a.h === true && r.h === true, '「暂停」把目标停住了');
  const stepped = r.b.pc !== r.a.pc && r.c.pc !== r.b.pc;
  if (stepped){
    ok(true, '连续两次单步 PC 都往前走了', `0x${r.a.pc.toString(16)} → 0x${r.b.pc.toString(16)} → 0x${r.c.pc.toString(16)}`);
  } else {
    /**
     * 🚨 单步不动**不一定是调试链坏了**：目标卡在 `b .`（自己跳自己，HardFault 死循环的典型写法）
     *    时，C_STEP 执行的就是那条分支，PC 当然不变（本机那块 BOOT0=1 的 F103ZE 就是这样）。
     *    这里读回 PC 处的机器码与 IPSR，把"目标的锅"和"我们的锅"分开。
     */
    const diag = await ev(`
      const d = window.__tools.dbg;
      const pc = d.session.pc & 0xfffffffe;
      let raw = null;
      try { raw = [...await d.session.probe.readMem(pc, 2)]; } catch {}
      const dhcsr = await d.session.probe._readWord(0xe000edf0);
      const dfsr = await d.session.probe._readWord(0xe000ed30);
      let cfsr = null;
      try { cfsr = await d.session.probe._readWord(0xe000ed28); } catch {}
      const xpsr = d.session.regList().find(x => x.name === 'XPSR')?.value | 0;
      const bits = v => '0x' + (v >>> 0).toString(16);
      return { pc, raw, isr: xpsr & 0x1ff, dhcsr: bits(dhcsr), dfsr: bits(dfsr), cfsr: cfsr == null ? null : bits(cfsr),
               selfBranch: !!raw && raw[0] === 0xfe && raw[1] === 0xe7 };`);
    const hex = diag.raw ? diag.raw.map(b => b.toString(16).padStart(2, '0')).join(' ') : '读不到';
    if (diag.selfBranch){
      skip++;
      console.log(`  SKIP  单步没有前进：目标 PC=0x${diag.pc.toString(16)} 处是 \`b .\`（${hex}，IPSR=${diag.isr} = ${diag.isr === 3 ? 'HardFault' : '异常 ' + diag.isr}）`);
      info(`DHCSR=${diag.dhcsr} · DFSR=${diag.dfsr} · CFSR=${diag.cfsr} —— 板子自己卡在死循环里，换个正常固件再测这一条`);
    } else {
      ok(false, '连续两次单步 PC 都往前走了', `0x${r.a.pc.toString(16)} → 0x${r.b.pc.toString(16)} → 0x${r.c.pc.toString(16)}`);
      info(`单步诊断：PC 处机器码=${hex} · IPSR=${diag.isr} · DHCSR=${diag.dhcsr} · DFSR=${diag.dfsr} · CFSR=${diag.cfsr}`);
    }
  }
  const cont = await ev(`
    const d = window.__tools.dbg;
    await d.session.cont();
    await new Promise(r => setTimeout(r, 400));
    const running = !d.session.halted;
    await d.session.halt();
    return { running, h: d.session.halted, pc: d.session.pc };`);
  ok(cont.running, '「继续」之后目标真的在跑（停住四五百毫秒后仍在跑 = SWD 没被我们自己打断）');
  ok(cont.h === true, '再「暂停」又停住了');
  info('暂停回来 PC = 0x' + cont.pc.toString(16));
}

console.log('== 4. 硬件断点（下在 PC 自己身上，跨过一次再删掉）==');
{
  const r = await ev(`
    const d = window.__tools.dbg;
    await d.session.halt();
    const pc = d.session.pc & ~1;
    const r1 = await d.session.bpAdd(pc);
    const list = d.session.bpList();
    const ctrl = await d.session.probe._readWord(0xe0002000);
    const comp0 = await d.session.probe._readWord(0xe0002008);
    await d.session.bpDel(pc);
    const comp0After = await d.session.probe._readWord(0xe0002008);
    return { pc, bps: list.length, ctrl, comp0, comp0After, nAfter: d.session.bpList().length };`);
  info(`PC=0x${r.pc.toString(16)} · FP_CTRL=0x${(r.ctrl >>> 0).toString(16)} · FP_COMP0 写前/写后=0x${(r.comp0 >>> 0).toString(16)}/0x${(r.comp0After >>> 0).toString(16)}`);
  ok(r.bps === 1, '断点加进去了');
  ok((r.comp0 >>> 0) === (((r.pc & 0x1ffffffc) | (1 << 30) | 1) >>> 0) || (r.comp0 & 1) === 1, '比较器里真的写进了这个地址（bit0=ENABLE）', '0x' + (r.comp0 >>> 0).toString(16));
  ok(r.nAfter === 0 && (r.comp0After >>> 0) === 0, '删掉之后比较器被清 0（不会给下一个调试器留雷）');
  const ctrlAfter = await ev('return await window.__tools.dbg.session.probe._readWord(0xe0002000);');
  ok(((ctrlAfter >>> 0) & 1) === 0, '没有断点时 FPB 被关掉（FP_CTRL.ENABLE=0）', '0x' + (ctrlAfter >>> 0).toString(16));
}

console.log('== 5. 新功能（符号列表 / RTT 地址 / 监视 / 源码行）真机走一遍 ==');
{
  // 板上跑的正是 tools/target-firmware/stm32f103_rtt_speed 那份固件，用它的 ELF 当符号源
  const sym = await ev(`
    const d = window.__tools.dbg;
    const res = await fetch('/tools/fixtures/dwarf/stm32f103_rtt_speed.elf');
    const st = d.loadElfBuffer(await res.arrayBuffer(), 'stm32f103_rtt_speed.elf');
    await new Promise(r => setTimeout(r, 300));
    const rows = [...document.querySelectorAll('#d-sym-list .symrow')];
    return { n: st ? st.size : 0, vars: st ? st.varCount : 0, rows: rows.length,
             hasRtt: rows.some(x => x.dataset.name === '_SEGGER_RTT'),
             lines: st?.lines ? st.lines.size : 0,
             rttSym: document.getElementById('d-rtt-sym').textContent };`);
  ok(sym.n > 50 && sym.rows > 5, `符号列表在真机页面上渲染出来（${sym.n} 个符号 · 列表 ${sym.rows} 行）`, JSON.stringify(sym));
  ok(sym.hasRtt, '符号列表里有 _SEGGER_RTT');
  ok(sym.lines > 50, `行号表解析出 ${sym.lines} 条记录`, String(sym.lines));
  ok(/_SEGGER_RTT = 0x2000000C/i.test(sym.rttSym), '侧栏 RTT 那格直接显示了 _SEGGER_RTT 的地址', sym.rttSym);

  const watch = await ev(`
    const d = window.__tools.dbg;
    d.clearWatch();
    d.addWatch('_SEGGER_RTT');
    await new Promise(r => setTimeout(r, 500));
    return { n: d.watch.items.length, err: d.watch.items[0]?.error || null,
             val: d.watch.items[0]?.value?.text || null, addr: d.watch.items[0]?.addr,
             dom: document.getElementById('d-watch-list').textContent.trim().slice(0, 90) };`);
  ok(watch.n === 1 && watch.addr === 0x2000000c, '监视项加在真目标的 RTT 控制块地址上', JSON.stringify(watch));
  ok(watch.val != null && !watch.err, `停止时从真目标读回了值：${String(watch.val).slice(0, 40)}`, JSON.stringify(watch));

  const src = await ev(`
    const d = window.__tools.dbg;
    await d.afterStop();
    await new Promise(r => setTimeout(r, 200));
    const pc = d.session.pc >>> 0;
    const at = d.sym.at(pc & 0xfffffffe);
    return { pc, at: at ? { file: at.file, line: at.line } : null,
             pos: document.getElementById('d-src-pos').textContent,
             cur: document.querySelector('#d-src .srcrow.cur')?.dataset.line || null };`);
  if (src.at){
    ok(new RegExp(`:${src.at.line}\\b`).test(src.pos) && String(src.cur) === String(src.at.line),
      `停下来时显示当前源码行（${src.at.file.split('/').pop()}:${src.at.line}，PC=0x${src.pc.toString(16)}）`, JSON.stringify(src));
  } else {
    info(`PC=0x${src.pc.toString(16)} 不在有行号信息的代码里 —— 源码行这一条本机跳过了（可能是启动代码/库函数）`);
  }
  await ev('window.__tools.dbg.clearWatch(); return 1;');
}

console.log('== 6. 收尾 ==');
{
  await ev(`
    const d = window.__tools.dbg;
    await d.session.bpClear();
    await d.session.cont();          // 让目标回到运行状态（别把板子停在 halt 上）
    await new Promise(r => setTimeout(r, 200));
    await d.disconnect();
    return 1;`);
  const r = await ev('return { sum: window.__tools.dbg.summary(), errs: window.__tools.errors };');
  ok(r.sum.connected === false, '已断开探针');
  ok(Array.isArray(r.errs) && r.errs.length === 0, '整轮跑完页面没有未捕获错误', JSON.stringify(r.errs));
}

console.log(`\n== 汇总：${pass} 通过 / ${fail} 失败 / ${skip} 跳过 ==`);
try { ws.close(); } catch {}
process.exit(fail ? 1 : 0);
