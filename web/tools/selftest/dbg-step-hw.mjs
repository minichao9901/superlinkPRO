/**
 * 调试器真机验收：「停止 / 单步 / 断点」时**源码区的显示与同步**到底对不对。
 *   make dbg-step-hw                        # = node tools/selftest/dbg-step-hw.mjs
 *   make dbg-step-hw ARGS="--steps=10"      # 单步步数（默认 12）
 *   make dbg-step-hw ARGS="--no-wake"       # 不动目标状态，直接在当前位置测
 *
 * 背景（2026-10-02 实测）：本机 STM32F103ZE 卡在 HardFault 的 `b .`（PC=0x80007b8、IPSR=3、
 * CFSR=IBUSERR、VTOR=0 —— BOOT0=1 那块板的经典状态）。停在 `b .` 上单步**PC 本来就不会变**
 * （一条指令就是跳到自己），所以脚本先按"BOOT0=1 配方"把固件正常跑起来，再测同步：
 *   AIRCR 软复位（清异常态）→ 手工搬 VTOR=0x08000000 → SP/PC 取 0x08000000 处的向量 → 清 PRIMASK → 运行。
 *
 * 断言口径（都是"用户看得见的东西"）：
 *   · DOM 高亮行 == sym.at(PC).line（模型与视图一致）
 *   · PC 落在 at(PC) 给出的 [addr,end) 里；"行 → addrOfLine → at()"回环还是这一行
 *   · 单步后 PC 变、行号跟着变、高亮始终在可视区
 *   · 断点：先热身挑一个**真会执行到**的地址（别取 cur.line+1 —— 那可能是"收到串口字符才走"的分支）
 *
 * 默认固件 `tools/target-firmware/stm32f103/build/fw.elf`（本机板子上烧的就是它，前 4 KB 逐字节相同）；
 * 换板子用 `--elf=<路径>`，源码文件列表按需改 SRC_FILES。
 */
const CDP = process.env.CDP || 'http://127.0.0.1:9333';
const APP = process.env.APP || 'http://127.0.0.1:8899/index.html';
const argv = process.argv.slice(2);
const arg = (n, d) => { const m = argv.find(a => a.startsWith(`--${n}=`)); return m ? m.split('=')[1] : d; };
const FW = arg('elf', 'tools/target-firmware/stm32f103/build/fw.elf');
const FW_URL = '/' + FW.replace(/\\/g, '/');
const SRC_DIR = FW.replace(/\/build\/.*$/, '/') ;                       // tools/target-firmware/stm32f103/
const SRC_FILES = ['src/main.c', 'src/startup.c', 'src/stm32f103_regs.h', 'segger_rtt/SEGGER_RTT.c', 'segger_rtt/SEGGER_RTT.h', 'segger_rtt/SEGGER_RTT_Conf.h'];
const NSTEP = Number(arg('steps', '12'));
const NO_WAKE = argv.includes('--no-wake');
const sleep = ms => new Promise(r => setTimeout(r, ms));
setTimeout(() => { console.error('[WATCHDOG] 超时'); process.exit(9); }, 300000);
let pass = 0, fail = 0;
const ok = (c, name, extra = '') => { if (c){ pass++; console.log(`  PASS  ${name}`); } else { fail++; console.log(`  FAIL  ${name} ${extra}`); } };
const step = s => console.log(`\n== ${s} ==`);
const hx = v => '0x' + (v >>> 0).toString(16);

const list = await (await fetch(CDP + '/json/list')).json();
const page = list.find(t => t.type === 'page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('CDP ws')); });
let seq = 0; const pend = new Map();
ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pend.has(m.id)){ const p = pend.get(m.id); pend.delete(m.id); m.error ? p.rej(new Error(m.error.message)) : p.res(m.result); } };
const send = (method, params = {}, t = 60000) => new Promise((res, rej) => { const id = ++seq; pend.set(id, { res, rej }); ws.send(JSON.stringify({ id, method, params })); setTimeout(() => { if (pend.delete(id)) rej(new Error(method + ' 超时')); }, t); });
const ev = async expr => {
  const r = await send('Runtime.evaluate', { expression: `(async()=>{ ${expr} })()`, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error('页面异常：' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
  return r.result.value;
};
await send('Runtime.enable'); await send('Page.enable');
await send('Page.navigate', { url: APP + '?t=' + Date.now() + '#dbg' });
for (let i = 0; i < 60; i++){ await sleep(400); if (await ev('return !!window.__tools?.dbg;').catch(() => false)) break; }

await ev(`
  /* 视图状态的**唯一口径**：DOM + 模型一起取，才能发现"不同步" */
  window.__dbgProbe = async () => {
    const d = window.__tools.dbg, s = d.session, box = document.getElementById('d-src');
    const cur = box?.querySelector('.srcrow.cur');
    const rect = box?.getBoundingClientRect(), cr = cur?.getBoundingClientRect();
    const pc = s.pc >>> 0;
    const at = d.sym?.at ? d.sym.at(pc & 0xfffffffe) : null;
    const lines = d.sym?.lines;
    const curLine = cur ? +cur.dataset.line : null;
    const file = d.srcCur?.file || box?.dataset.file || null;
    const backAddr = (lines && file && curLine != null) ? lines.addrOfLine(file, curLine) : null;
    /* 行区间：行表里没有 lineSpan()，用 at(addr) 的 [addr, end)。
       ⚠️ **一行可能有多个不相邻的地址区间**（编译器会把一条语句拆成好几段），
       所以这里只用它做"回环对账"：addrOfLine(行) → at(地址) 应当还是那一行；
       "PC 在不在这一行里"由 at(PC).line 直接回答（那才是权威口径）。 */
    let span = null, roundTrip = null;
    if (backAddr != null){
      const row = lines.at(backAddr & 0xfffffffe);
      if (row) span = [row.addr >>> 0, row.end >>> 0];
      roundTrip = row ? row.line === curLine : false;
    }
    const rows = [...(box?.querySelectorAll('.srcrow') || [])];
    return {
      pc, halted: s.halted, stateText: document.getElementById('d-state')?.textContent || '',
      pcStrip: document.getElementById('d-pc')?.textContent || '',
      model: at ? { file: String(at.file).split(/[\\\\/]/).pop(), line: at.line, addr: at.addr >>> 0, end: at.end >>> 0 } : null,
      dom: { curLine, file: file ? String(file).split(/[\\\\/]/).pop() : null, rows: rows.length,
             firstLine: rows[0]?.dataset.line ?? null, lastLine: rows[rows.length - 1]?.dataset.line ?? null,
             curText: (cur?.querySelector('.srctx')?.textContent || '').trim().slice(0, 70) },
      backAddr: backAddr ?? null, span, roundTrip,
      visible: !!(rect && cr && cr.top >= rect.top - 2 && cr.bottom <= rect.bottom + 2),
      srcReady: d.src?.ready, srcFiles: d.src?.count,
    };
  };
  return 1;`);

console.log(`调试器真机实测　固件=${FW}　单步 ${NSTEP} 次`);

// ==================================================================== 1
step('1. 连接');
{
  const r = await ev(`
    const d = window.__tools.dbg;
    try { await d.connect(); } catch (e){ return { err: e?.message || String(e) }; }
    await new Promise(r => setTimeout(r, 300));
    return { err: null, s: d.summary() };`);
  ok(!r.err, `连接成功：${r.s?.backend} · SWD ${r.s?.clockKhz} kHz · 断点容量 ${r.s?.bpCap}${r.err ? '（' + r.err + '）' : ''}`);
}

// ==================================================================== 2
step('2. 目标状态 + 必要时按「BOOT0=1 配方」唤醒');
{
  const r = await ev(`
    const d = window.__tools.dbg, s = d.session, p = s.probe;
    const w = await p.readMem(0x08000000, 8);
    const u32 = b => (b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)) >>> 0;
    const out = { vtSP: u32(w), vtReset: u32(w.subarray(4, 8)) };
    await s.halt().catch(() => {});
    out.pc0 = (await s.readReg('PC')) >>> 0;
    out.ipsr = (await s.readReg('XPSR')) >>> 0 & 0x1ff;
    out.code0 = [...(await p.readMem(out.pc0, 4))].map(x => x.toString(16).padStart(2, '0')).join(' ');
    out.vtor0 = u32(await p.readMem(0xE000ED08, 4));
    return out;`);
  console.log(`  向量表：SP=${hx(r.vtSP)} Reset=${hx(r.vtReset)}　VTOR=${hx(r.vtor0)}`);
  console.log(`  当前 PC=${hx(r.pc0)}（IPSR=${r.ipsr}）机器码 ${r.code0}`);
  const stuck = /^fe e7/.test(r.code0) || r.ipsr === 3 || r.pc0 === r.vtReset;
  if (!NO_WAKE && (stuck || r.vtor0 !== 0x08000000)){
    console.log('  → 目标停在异常/自旋里（或 VTOR ≠ 0x08000000）：先 AIRCR 软复位清异常态，再按 BOOT0=1 配方把固件跑起来…');
    const wake = await ev(`
      const d = window.__tools.dbg, s = d.session, p = s.probe;
      const u32 = b => (b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)) >>> 0;
      const w32 = v => Uint8Array.of(v & 0xff, (v >>> 8) & 0xff, (v >>> 16) & 0xff, (v >>> 24) & 0xff);
      const log = [];
      // ① AIRCR.SYSRESETREQ：本机实测**有效**（能把 HardFault 的 active 状态一起清掉，
      //    ICSR.VECTACTIVE 3 → 0，CFSR/HFSR 归零）。比纯手工清位干净。
      await s.halt().catch(() => {});
      try { await p.writeMem(0xE000ED0C, w32(0x05fa0004)); log.push('AIRCR SYSRESETREQ'); } catch (e){ log.push('SYSRESETREQ 失败'); }
      await new Promise(r => setTimeout(r, 400));
      await s.halt().catch(() => {});
      // ② BOOT0=1：复位后 VTOR=0（向量表指向 ROM），固件里的向量一条都用不上 —— 手工搬回来
      await p.writeMem(0xE000ED08, w32(0x08000000)); log.push('VTOR=0x08000000');
      const w = await p.readMem(0x08000000, 8);
      const sp = u32(w), pc = u32(w.subarray(4, 8));
      await s.writeReg('SP', sp); await s.writeReg('PC', pc);
      log.push('SP=' + sp.toString(16) + ' PC=' + pc.toString(16));
      await s.writeReg('PRIMASK', 0).catch(() => {});
      await s.writeReg('FAULTMASK', 0).catch(() => {});
      await s.run();
      await new Promise(r => setTimeout(r, 400));
      await s.halt();
      const pc2 = (await s.readReg('PC')) >>> 0;
      const code2 = [...(await p.readMem(pc2, 4))].map(x => x.toString(16).padStart(2, '0')).join(' ');
      const icsr = u32(await p.readMem(0xE000ED04, 4));
      return { log, pc2, code2, vectactive: icsr & 0x1ff, vtor: u32(await p.readMem(0xE000ED08, 4)) };`);
    console.log('  ' + wake.log.join(' · '));
    console.log(`  唤醒后：PC=${hx(wake.pc2)}　ICSR.VECTACTIVE=${wake.vectactive}（0 = 线程模式）　VTOR=${hx(wake.vtor)}　机器码 ${wake.code2}`);
    ok(wake.vectactive === 0, `不在异常里了（VECTACTIVE=${wake.vectactive}）`);
    ok(wake.vtor === 0x08000000, `向量表回到 flash（VTOR=${hx(wake.vtor)}）`);
    ok(wake.pc2 >= 0x08000000 && wake.pc2 < 0x08020000, `PC 落在固件的 flash 里（${hx(wake.pc2)}）`);
  } else {
    console.log('  → 目标在正常代码里，直接往下测');
  }
}

// ==================================================================== 3
step('3. 载入 ELF + 源码文件');
{
  const r = await ev(`
    const d = window.__tools.dbg;
    const resp = await fetch('${FW_URL}');
    if (!resp.ok) return { err: 'HTTP ' + resp.status };
    await d.loadElfBuffer(new Uint8Array(await resp.arrayBuffer()), '${FW.split('/').pop()}');
    await new Promise(r => setTimeout(r, 500));
    /* 源码目录：浏览器读不到本地文件，但这里可以直接 fetch 仓库里的源文件再包成 File
       （等价于用户点「选择源码目录…」选到工程根目录） */
    const files = [];
    for (const rel of ${JSON.stringify(SRC_FILES)}){
      const rr = await fetch('${'/' + SRC_DIR.replace(/\\/g, '/')}' + rel).catch(() => null);
      if (rr && rr.ok) files.push(new File([await rr.text()], rel.split('/').pop()));
    }
    if (files.length) await d._indexSrcFiles(files);
    await new Promise(r => setTimeout(r, 300));
    return { err: null, sum: d.summary(), files: files.length };`);
  if (r.err){ ok(false, '载入 ELF', r.err); }
  else {
    const e = r.sum.elf;
    ok(!!e && e.symbols > 0, `ELF：${e?.lines} 条行号 / ${e?.files} 个文件 · 符号 ${e?.symbols}`);
    ok(r.files > 0, `源码文件已索引 ${r.files} 个（fetch 仓库源文件 → File，等价于用户选目录）`);
  }
}

// ==================================================================== 4
step('4. 暂停：PC ↔ 源码高亮行互相印证');
{
  const p = await ev(`await window.__tools.dbg.session.halt(); await new Promise(r => setTimeout(r, 200)); await window.__tools.dbg.renderSource(); return await window.__dbgProbe();`);
  console.log('  ' + JSON.stringify({ pc: hx(p.pc), model: p.model && `${p.model.file}:${p.model.line}`, dom: p.dom.curLine,
    backAddr: p.backAddr && hx(p.backAddr), span: p.span && [hx(p.span[0]), hx(p.span[1])], visible: p.visible, srcReady: p.srcReady }));
  console.log(`  高亮行内容：「${p.dom.curText}」`);
  ok(p.halted, '目标处于停止状态');
  ok(!!p.model, `PC 落在有行号信息的代码里：${p.model?.file}:${p.model?.line}`);
  ok(p.dom.curLine === p.model?.line, `DOM 高亮行 == 模型行（DOM ${p.dom.curLine} / 模型 ${p.model?.line}）`);
  ok(p.roundTrip === true,
     `回环对账：addrOfLine(${p.dom.file}:${p.dom.curLine}) = ${p.backAddr != null ? hx(p.backAddr) : '—'} → at() 还是这一行`);
  ok(!!p.model && p.pc >= p.model.addr && p.pc < p.model.end,
     `PC ${hx(p.pc)} 落在它所属行的地址区间 [${p.model ? hx(p.model.addr) + ',' + hx(p.model.end) : '—'})`);
  ok(p.srcReady, `源码文本可用（索引 ${p.srcFiles} 个文件）—— 显示的**是真源码**不是占位符`);
  ok(p.dom.rows > 10, `源码区渲染了 ${p.dom.rows} 行（第 ${p.dom.firstLine}..${p.dom.lastLine} 行）`);
}

// ==================================================================== 5
step(`5. 单步 ${NSTEP} 次：PC / 行号 / 高亮同步`);
{
  const trail = [];
  let err = null, domFail = 0, spanFail = 0, rtFail = 0, modelFail = 0, moved = 0, visBad = 0, lineMoved = 0, bpFail = 0;
  let prevLine = null;
  for (let i = 0; i < NSTEP; i++){
    try { await ev(`await window.__tools.dbg.session.step(); await new Promise(r => setTimeout(r, 100)); await window.__tools.dbg.renderSource(); return 1;`); }
    catch (e){ err = e?.message || String(e); break; }
    const a = await ev('return await window.__dbgProbe();');
    if ((a.pc >>> 0) !== 0) moved++;
    if (!a.model) modelFail++;
    if (a.dom.curLine !== a.model?.line) domFail++;
    if (!(a.pc >= a.model?.addr && a.pc < a.model?.end)) spanFail++;
    if (a.roundTrip !== true) rtFail++;
    if (!a.visible) visBad++;
    if (a.bpCount) bpFail = a.bpCount;                 // 兜底用的临时比较器必须收干净
    if (prevLine != null && a.model && a.model.line !== prevLine) lineMoved++;
    prevLine = a.model?.line ?? null;
    trail.push({ i: i + 1, pc: hx(a.pc), loc: a.model ? `${a.model.file}:${a.model.line}` : '—', dom: a.dom.curLine,
                 vis: a.visible, txt: a.dom.curText.slice(0, 34) });
  }
  for (const t of trail) console.log(`  #${String(t.i).padStart(2)} PC=${t.pc}  ${t.loc}  DOM=${t.dom}${t.vis ? '' : ' ⚠不在可视区'}  「${t.txt}」`);
  ok(!err, `单步没抛错${err ? '（' + err + '）' : ''}`);
  ok(trail.length >= Math.min(6, NSTEP), `完成 ${trail.length} 次单步`);
  ok(domFail === 0, `每次单步后 DOM 高亮行 == 模型行（不一致 ${domFail} 次）`);
  ok(modelFail === 0, `每次单步后 PC 都在有行号的代码里（落空 ${modelFail} 次）`);
  ok(spanFail === 0, `每次单步后 PC 都落在 at(PC) 给出的行区间里（越界 ${spanFail} 次）`);
  ok(rtFail === 0, `每次单步后"行 → 地址 → 行"回环一致（不一致 ${rtFail} 次）`);
  ok(visBad === 0, `高亮行一直在可视区（滚出去 ${visBad} 次）`);
  ok(lineMoved > 0, `源码行真的跟着 PC 走（行号变化 ${lineMoved} 次）`);
}

// ==================================================================== 6
step('6. 断点：源码行 → 继续 → 命中后落在该行');
{
  const r = await ev(`
    const d = window.__tools.dbg, s = d.session, lines = d.sym?.lines;
    if (!lines) return { err: '没有行表' };
    /* 选断点目标：**挑一个真的会被执行到的源码地址**。
     * 🚨 不能像第一版那样取 cur.line+1 —— main.c 里那可能是"收到串口字符才走"或
     *    "g_ms 满 3 s 才走"的分支，空等 6 s 也永远不执行（那会把"断点不命中"误判成页面缺陷）。
     * 做法：先跑 24 次短的、每次停下来读 PC，统计最热的那个 PC 当目标。 */
    const hist = new Map();
    for (let i = 0; i < 24; i++){
      await s.run();
      await new Promise(r => setTimeout(r, 40 + (i % 5) * 12));
      await s.halt();
      const pc = (await s.readReg('PC')) & ~1;
      hist.set(pc, (hist.get(pc) || 0) + 1);
    }
    const hot = [...hist.entries()].sort((a, b) => b[1] - a[1])[0];
    if (!hot) return { err: '热身没采到 PC' };
    const target = { a: hot[0] >>> 0, hits: hot[1] };
    const at = lines.at(target.a);
    target.ln = at ? at.line : null;
    const file = at ? at.file : null;
    target.span = at ? [at.addr >>> 0, at.end >>> 0] : null;

    await s.halt().catch(() => {});
    await s.bpAdd(target.a);
    d.renderBps();
    const bps = d.summary().bps;
    const pcBefore = (await s.readReg('PC')) >>> 0;
    await s.cont();
    let hit = false, pollErr = null, reads = 0;
    for (let i = 0; i < 60; i++){
      await new Promise(r => setTimeout(r, 100));
      try { const v = await s.probe._readWord(0xE000EDF0); reads++; if (((v >>> 17) & 1) === 1){ hit = true; break; } }
      catch (e){ pollErr = e?.message || String(e); }
    }
    await s.refresh().catch(() => {});
    await s.refreshRegs().catch(() => {});
    await d.renderSource();
    const probe = await window.__dbgProbe();
    await s.bpDel(target.a).catch(() => {});
    d.renderBps();
    return { target, file, hist: hist.size, bps, probe, hit, pollErr, reads, pcBefore, halted: s.halted };`);
  if (r.err){ ok(false, '断点流程', r.err); }
  else {
    const bn = r.file ? String(r.file).split(/[\\/]/).pop() : '?';
    console.log(`  热身：${r.hist} 个不同落点，最热的是 ${bn}:${r.target.ln} @ ${hx(r.target.a)}（24 次里占 ${r.target.hits} 次）`);
    console.log(`  下断点 → 继续 → PC=${hx(r.probe.pc)}（高亮行 ${r.probe.dom.curLine}）· 轮询 ${r.reads} 次${r.pollErr ? '，最后报错：' + r.pollErr : ''}`);
    console.log(`  高亮行内容：「${r.probe.dom.curText}」`);
    ok(r.bps.length > 0, `断点已下（${r.bps.join(', ')}）`);
    ok(r.hit, '「继续」之后自己停下来了（断点命中）');
    ok(r.target.span && r.probe.pc >= r.target.span[0] && r.probe.pc < r.target.span[1],
       `命中位置在所断源码行的区间内（PC ${hx(r.probe.pc)} / 区间 ${r.target.span ? hx(r.target.span[0]) + '..' + hx(r.target.span[1]) : '—'}）`);
    ok(r.probe.dom.curLine === r.probe.model?.line, `命中后 DOM 高亮行 == 模型行（${r.probe.dom.curLine}/${r.probe.model?.line}）`);
  }
}

// ==================================================================== 7
step('7. 收尾');
{
  const r = await ev(`
    const d = window.__tools.dbg;
    await d.session.bpClear().catch(() => {});
    await d.session.halt().catch(() => {});
    return { bps: d.summary().bps.length, halted: d.session.halted, pc: '0x' + (d.session.pc >>> 0).toString(16) };`);
  console.log('  ' + JSON.stringify(r));
  ok(r.bps === 0 && r.halted, '断点已清空、目标停在原地（不留下失控的核）');
}
console.log(`\n${fail === 0 ? '✅' : '❌'} dbg-steptest: ${pass} 通过 / ${fail} 失败`);
ws.close();
process.exit(fail ? 1 : 0);
