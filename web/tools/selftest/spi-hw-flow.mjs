/**
 * 「SPI/QSPI 屏」页面的**功能流程验收**（真机：探针 + 真屏）：
 *   node tools/selftest/spi-hw-flow.mjs
 *   make spi-flow
 *
 * 流程（用户 2026-09-29 指定，一步都不跳）：
 *   打开 web → 连接探针 → 初始化屏 → 发图 ×3 → **再次初始化屏** → 发图 ×3
 *
 * 严格模式：**任何一步出错就立刻停**（不再往下跑），并打印现场（页面错误 / 日志尾部 / 计数器），
 * 因为后面的步骤都建立在前面正确的前提上，带病往下跑只会掩盖真正的第一现场。
 *
 * 判"出错"的三条口径（都取**增量**，不看累积值）：
 *   1. 页面未捕获错误（`__tools.summary().errors`）—— 必须一直是 0；
 *   2. 探针侧的 `frames_err`（协议层）—— 每一步之后不得增长；
 *   3. 页面日志里 **err 级**的新条目 —— 不得增长（含"发送失败/应答超时/被拒"这类）。
 *
 * 前置：静态服务 8899 + CDP 浏览器 9333（`make page-prep` 会起）、探针与屏已接好。
 * 用法：`--panel=axs15352|st77916`（默认 axs15352）、`--sclk=40`。
 */
const CDP = process.env.CDP || 'http://127.0.0.1:9333';
const APP = process.env.APP || 'http://127.0.0.1:8899/index.html';
const argv = process.argv.slice(2);
const arg = (n, d) => { const m = argv.find(a => a.startsWith(`--${n}=`)); return m ? m.split('=')[1] : d; };
const PANEL = arg('panel', 'axs15352');
const SCLK = Number(arg('sclk', '40'));
const PANELS = {
  axs15352: { label: '天马 2P01 / AXS15352（档 1）', preset: 'axs15352', geom: 'axs15352', profile: 1 },
  st77916: { label: 'ST77916（档 2）', preset: 'st77916', geom: 'st77916', profile: 2 },
};
const P = PANELS[PANEL];
if (!P) throw new Error(`未知 --panel=${PANEL}`);

const sleep = ms => new Promise(r => setTimeout(r, ms));
let steps = 0;
const log = s => console.log(s);

/* ---------------- CDP ---------------- */
class Cdp {
  constructor(){ this.seq = 0; this.pending = new Map(); this.prompts = []; }
  _open(url){ const ws = new WebSocket(url); return new Promise((res, rej) => { ws.onopen = () => res(ws); ws.onerror = () => rej(new Error('ws 连不上 ' + url)); ws.onmessage = e => this._dispatch(JSON.parse(e.data)); }); }
  _dispatch(m){
    if (m.id && this.pending.has(m.id)){ const p = this.pending.get(m.id); this.pending.delete(m.id); m.error ? p.rej(new Error(m.error.message)) : p.res(m.result); return; }
    if (m.method === 'DeviceAccess.deviceRequestPrompted') this.prompts.push(m.params);
  }
  async connect(){
    const ver = await (await fetch(CDP + '/json/version')).json();
    this.browserWs = await this._open(ver.webSocketDebuggerUrl);
    const list = await (await fetch(CDP + '/json/list')).json();
    const page = list.find(t => t.type === 'page');
    if (!page) throw new Error('没有页面目标');
    this.ws = await this._open(page.webSocketDebuggerUrl);
    await this.send('Page.enable'); await this.send('Runtime.enable');
    try { await this.send('Network.enable'); await this.send('Network.setCacheDisabled', { cacheDisabled: true }); } catch {}
    for (const fn of [() => this._call(this.browserWs, 'DeviceAccess.enable'), () => this.send('DeviceAccess.enable')]){
      try { await fn(); break; } catch {}
    }
  }
  send(m, p = {}){ return this._call(this.ws, m, p); }
  _call(ws, method, params){
    const id = ++this.seq;
    return new Promise((res, rej) => {
      this.pending.set(id, { res, rej });
      ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => { if (this.pending.delete(id)) rej(new Error(`CDP ${method} 超时`)); }, 180000);
    });
  }
  async eval(expr, userGesture = false){
    const r = await this.send('Runtime.evaluate', { expression: `(async()=>{ ${expr} })()`, userGesture, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error('页面异常：' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
    return r.result.value;
  }
  async settle(match, readyExpr, timeout = 20000){
    const t0 = Date.now();
    for (;;){
      if (this.prompts.length){
        const p = this.prompts.shift();
        const dev = p.devices.find(d => match.test(d.name)) || p.devices[0];
        await this._call(this.browserWs, 'DeviceAccess.selectPrompt', { id: p.id, deviceId: dev.id });
        return dev;
      }
      try { if (await this.eval(`return !!(${readyExpr});`)) return null; } catch {}
      if (Date.now() - t0 > timeout) throw new Error(`既没弹选择框也没就绪：${readyExpr}`);
      await sleep(200);
    }
  }
  async waitIdle(timeout = 180000){
    const t0 = Date.now();
    for (;;){
      const busy = await this.eval('return window.__tools.spiSession.busy;');
      if (!busy) return true;
      if (Date.now() - t0 > timeout) throw new Error('等 busy 清空超时');
      await sleep(150);
    }
  }
}

/** 取一份"健康度快照"（增量比对用） */
const snapshot = cdp => cdp.eval(`
  const s = window.__tools.spiSession;
  return { pageErrors: window.__tools.summary().errors.slice(),
           framesErr: s.counters.framesErr, framesOk: s.counters.framesOk,
           errLines: document.querySelectorAll('#pn-log .err').length,
           lastRun: window.__tools.panel.summary().lastRun,
           logTail: document.getElementById('pn-log').textContent.split('\\n').filter(l => /失败|超时|被拒|错误|ERR|FAIL/.test(l)).slice(-4) };`);

/** 一步一查：出错就抛（调用方打印现场并停止） */
async function check(label, base){
  const s = await snapshot(cdp);
  const problems = [];
  if (s.pageErrors.length > base.pageErrors.length) problems.push('页面未捕获错误：' + s.pageErrors.slice(base.pageErrors.length).join(' | '));
  if (s.framesErr > base.framesErr) problems.push(`协议层 frames_err 从 ${base.framesErr} 涨到 ${s.framesErr}`);
  if (s.errLines > base.errLines) problems.push(`页面日志里出现 ${s.errLines - base.errLines} 条 err 级新条目`);
  if (s.logTail.length) problems.push('可疑日志：' + s.logTail.join(' / '));
  if (problems.length) throw new Error(`【${label}】\n    - ` + problems.join('\n    - '));
  return s;
}

// ==================================================================== 流程
const cdp = new Cdp();
await cdp.connect();
const t0 = Date.now();

async function main(){
log(`流程验收：${P.label}　SCLK ${SCLK} MHz　（任何一步出错即停）\n`);

// ---- 0. 打开 web
await cdp.send('Page.navigate', { url: APP + '?demo=serial&t=' + Date.now() });
let ready = false;
for (let i = 0; i < 60; i++){ await sleep(400); try { if (await cdp.eval('return !!window.__tools?.spiSession;')){ ready = true; break; } } catch {} }
if (!ready) throw new Error('页面没起来');
let base = await snapshot(cdp);
log(`① 打开 web ✓　（页面错误 ${base.pageErrors.length}）`);
steps++;

// ---- 1. 连接探针
await cdp.eval(`document.querySelector('#tabs .tab[data-tab="spi"]').click();`);
await sleep(300);
await cdp.eval(`document.getElementById('sp-reconnect').click();`);
await sleep(900);
if (!(await cdp.eval('return window.__tools.spiSession.connected;'))){
  log('   （没有已授权的 HID，触发选择框…）');
  await cdp.eval(`document.getElementById('sp-connect').click();`, true);
  await cdp.settle(/akaLinkPro|CMSIS|HID/i, 'window.__tools.spiSession.connected');
  await sleep(600);
}
if (!(await cdp.eval('return window.__tools.spiSession.connected;'))) throw new Error('HID 连接失败');
await cdp.eval(`document.getElementById('sp-usb').click();`);
await cdp.settle(/akaLinkPro|CMSIS|WinUSB|Composite/i, 'window.__tools.spiSession.dataReady');
await sleep(600);
const conn = await cdp.eval(`return { hid: window.__tools.spiSession.connected, usb: window.__tools.spiSession.dataReady, iface: window.__tools.spiSession.transport?.iface };`);
if (!conn.hid || !conn.usb) throw new Error('连接不完整：' + JSON.stringify(conn));
base = await check('连接探针', base);
log(`② 连接探针 ✓　（HID + 数据端点，接口 ${conn.iface}）`);
steps++;

// ---- 2. 套推荐值 + 载入内置表
await cdp.eval(`
  document.querySelector('#tabs .tab[data-tab="panel"]').click();
  await new Promise(r => setTimeout(r, 300));
  document.getElementById('pn-preset').value = '${P.preset}';
  document.getElementById('pn-preset-apply').click();
  await new Promise(r => setTimeout(r, 1400));
  return true;`);
// SCLK 要在桥页设
await cdp.eval(`
  document.querySelector('#tabs .tab[data-tab="spi"]').click();
  await new Promise(r => setTimeout(r, 250));
  document.getElementById('sp-sclk').value = '${SCLK * 1e6}';
  document.getElementById('sp-set').click();
  await new Promise(r => setTimeout(r, 900));
  document.querySelector('#tabs .tab[data-tab="panel"]').click();
  await new Promise(r => setTimeout(r, 250));
  document.getElementById('pn-code-preset').value = '${P.preset}';
  document.getElementById('pn-code-load').click();
  await new Promise(r => setTimeout(r, 700));
  return true;`);
base = await check('套用推荐值 + 载入内置表', base);
const pre = await cdp.eval(`return { prof: window.__tools.spiSession.profile?.profile, sclk: window.__tools.spiSession.cfg?.sclkHz, rows: window.__tools.panel.summary().rows, table: document.querySelectorAll('#pn-code-body tr').length };`);
log(`③ 配置就位 ✓　档 ${pre.prof} · SCLK ${pre.sclk / 1e6} MHz · 表 ${pre.rows} 条（表格 ${pre.table} 行 —— 面板不再自动补 MADCTL/COLMOD，行数应相等）`);
steps++;

// ---- 初始化屏（可重复调用）
async function initPanel(times){
  await cdp.eval(`document.getElementById('pn-enable').click();`);
  await sleep(700);
  const t = Date.now();
  await cdp.eval(`document.getElementById('pn-code-play').click();`);
  await cdp.waitIdle(120000);
  await sleep(500);
  const r = await cdp.eval(`return { prog: document.getElementById('pn-code-prog').textContent,
                                     ok: window.__tools.spiSession.counters.framesOk,
                                     err: window.__tools.spiSession.counters.framesErr };`);
  return { ms: Date.now() - t, ...r };
}

// ---- 发图（可重复调用）
async function sendImage(label){
  await cdp.eval(`
    const b = [...document.querySelectorAll('#pn-patterns button')].find(x => x.textContent === ${JSON.stringify(label)});
    if (!b) throw new Error('没有这个图案：' + ${JSON.stringify(label)});
    b.click();
    await new Promise(r => setTimeout(r, 400));
    return true;`);
  await cdp.eval(`document.getElementById('pn-img-send').click();`);
  await cdp.waitIdle(180000);
  await sleep(500);
  const r = await cdp.eval(`return window.__tools.panel.summary().lastRun;`);
  return r;
}

// ---- 3. 初始化屏（第一次）
{
  const r = await initPanel(1);
  base = await check('初始化屏（第一次）', base);
  log(`④ 初始化屏（第一次）✓　${r.prog}　（累计 frames_ok=${r.ok} err=${r.err}）`);
  steps++;
}

// ---- 4~6. 发图 ×3
const FIRST = ['色条 8', '混色卡', '棋盘 16px'];
const SECOND = ['渐变', '对半 红|绿', '4px 网格'];
for (let i = 0; i < FIRST.length; i++){
  const r = await sendImage(FIRST[i]);
  base = await check(`发图 ${i + 1}（${FIRST[i]}）`, base);
  log(`⑤ 发图 ${i + 1}「${FIRST[i]}」✓　${r.slices} 片 · ${(r.bytes / 1024).toFixed(1)} KB · ${r.ms.toFixed(0)} ms · ${(r.kbPerSec / 1024).toFixed(2)} MB/s · 坏应答 ${r.badRsp}`);
  steps++;
}

// ---- 7. 再次初始化屏
{
  const r = await initPanel(2);
  base = await check('再次初始化屏', base);
  log(`⑥ 再次初始化屏 ✓　${r.prog}　（累计 frames_ok=${r.ok} err=${r.err}）`);
  steps++;
}

// ---- 8~10. 发图 ×3
for (let i = 0; i < SECOND.length; i++){
  const r = await sendImage(SECOND[i]);
  base = await check(`发图 ${i + 4}（${SECOND[i]}）`, base);
  log(`⑦ 发图 ${i + 4}「${SECOND[i]}」✓　${r.slices} 片 · ${(r.bytes / 1024).toFixed(1)} KB · ${r.ms.toFixed(0)} ms · ${(r.kbPerSec / 1024).toFixed(2)} MB/s · 坏应答 ${r.badRsp}`);
  steps++;
}

// ---- 收尾
await cdp.eval(`document.querySelector('#tabs .tab[data-tab="spi"]').click();`);
await sleep(200);
await cdp.eval(`document.getElementById('sp-disable').click();`);
await sleep(500);
const fin = await snapshot(cdp);
log(`\n===== 流程验收通过 =====`);
log(`步骤 ${steps} 步全部完成，用时 ${((Date.now() - t0) / 1000).toFixed(1)} s`);
log(`累计：frames_ok=${fin.framesOk}　frames_err=${fin.framesErr}　页面错误=${fin.pageErrors.length}　err 级日志=${fin.errLines}`);
log(`\n👀 屏上现在是最后一张图「${SECOND[SECOND.length - 1]}」；过程中应该依次看到：`);
log(`   ${[...FIRST, '（再次初始化）', ...SECOND].join(' → ')}`);
}

try {
  await main();
  process.exit(0);
} catch (e){
  console.error(`\n❌ 流程中断（按"出错即停"处理）：${e?.message || e}\n`);
  try {
    const s = await snapshot(cdp);
    console.error('现场：');
    console.error('  页面未捕获错误：' + (s.pageErrors.join(' | ') || '(无)'));
    console.error(`  frames_ok / frames_err：${s.framesOk} / ${s.framesErr}`);
    console.error(`  err 级日志条数：${s.errLines}`);
    if (s.lastRun) console.error(`  最近一次刷图：${s.lastRun.slices} 片 · ${s.lastRun.ms.toFixed(0)} ms · 坏应答 ${s.lastRun.badRsp}`);
    const tail = await cdp.eval(`return document.getElementById('pn-log').textContent.split('\\n').slice(-14);`);
    console.error('  日志尾部：\n    ' + tail.join('\n    '));
    const busTail = await cdp.eval(`return document.getElementById('sp-log').textContent.split('\\n').slice(-8);`);
    console.error('  桥页日志尾部：\n    ' + busTail.join('\n    '));
  } catch (e2){ console.error('（现场快照也失败了：' + (e2?.message || e2) + '）'); }
  process.exit(1);
}
