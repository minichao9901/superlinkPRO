/**
 * 波形渲染的**几何自测**（CDP，不需要硬件）：
 *   node tools/selftest/scope-render.test.mjs        （等价：make test-scope-render）
 * 前置：静态服务 8899 + 带 CDP 的浏览器 9333（没有就自己拉一个）。
 *
 * 为什么单独有这么一份：用户实测反馈过一次「放大之后线不连续了」——
 * 根因在 `render.js` 的折线分支按**像素列**取点，而放大到"每列不足 1 个样本"时，
 * 有些列一个样本都没有（`columns()` 给的是 Infinity），折线把它当成"这里没数据"就断了。
 * 那不是数据缺口，是栅格化假象。
 *
 * 所以这里不看"像不像"，直接**数路径**：把 2D 上下文的 moveTo/lineTo 包起来，
 * 断言"一串没有缺口的样本只能画出一条子路径"。断线 = moveTo 变多。
 */
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');
const CDP = process.env.CDP || 'http://127.0.0.1:9333';
const APP = process.env.APP || 'http://127.0.0.1:8899/index.html';

setTimeout(() => { console.error('[WATCHDOG] 总超时'); process.exit(9); }, 120000);
const sleep = ms => new Promise(r => setTimeout(r, ms));
let pass = 0, fail = 0;
const ok = (cond, name, extra = '') => {
  if (cond){ pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name} ${extra}`); }
};

async function ensureBrowser(){
  try { await fetch(CDP + '/json/version', { signal: AbortSignal.timeout(2500) }); return; } catch {}
  console.log('  （CDP 浏览器没在跑，自己拉一个…）');
  spawn('pwsh', ['-NoProfile', '-File', join(root, 'tools', 'selftest', 'launch-browser.ps1'), '-Port', '9333', '-Url', APP],
    { stdio: 'ignore', detached: true }).unref();
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
const send = (method, params = {}, t = 25000) => new Promise((res, rej) => {
  const id = ++seq; pend.set(id, { res, rej });
  ws.send(JSON.stringify({ id, method, params }));
  setTimeout(() => { if (pend.delete(id)) rej(new Error(method + ' 超时')); }, t);
});
async function ev(expr){
  const r = await send('Runtime.evaluate', { expression: `(async()=>{ ${expr} })()`, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error('页面里报错：' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
  return r.result.value;
}
await send('Page.enable');
await send('Runtime.enable');
try { await send('Network.enable'); await send('Network.setCacheDisabled', { cacheDisabled: true }); } catch {}
await send('Page.navigate', { url: APP + '?t=' + Date.now() });
let ready = false;
for (let i = 0; i < 60; i++){
  await sleep(400);
  try { if (await ev('return !!window.__tools?.scope;')){ ready = true; break; } } catch {}
}
if (!ready) throw new Error('页面没起来（__tools.scope 不存在）');
console.log('目标: ' + APP);

/**
 * 在页面里直接构造 store + renderer（不走采样、不碰硬件），
 * 把画布上下文的 moveTo/lineTo 包起来数一数。
 * @returns {{subpaths:number, lines:number, xs:number[], per:number, cols:number, lod:boolean}}
 */
const PROBE = `
  const BASE = location.pathname.replace(/[^/]*$/, '');      // 本地 / 线上（/web-serial-rtt-tools/）都能跑
  const { SampleStore } = await import(BASE + 'app/scope/store.js');
  const { ScopeRenderer } = await import(BASE + 'app/scope/render.js');
  window.__cv = window.__cv || (() => {
    const c = document.createElement('canvas');
    c.style.cssText = 'position:fixed;left:-9999px;top:0;width:800px;height:300px';
    document.body.appendChild(c);
    return c;
  })();
  window.__mkStore = (n, mk) => {
    const st = new SampleStore([{ name: 'sig', addr: 0, size: 4, scalar: 'f32' }], n);
    for (let i = 0; i < n; i++) st.pushFrame([mk(i)], i * 20);      // 20 µs 一个样本
    return st;
  };
  window.__draw = (store, start, end) => {
    const r = new ScopeRenderer(window.__cv);
    r.setStore(store);
    r.view = { start, end };
    r.showGrid = false;                  // 关网格：这样上下文的 moveTo/lineTo 只可能来自波形本身
    r.cursor = null; r.cursors = { a: null, b: null }; r.trigger = null;
    const ctx = r.ctx;
    const moves = [], lines = [];
    const om = ctx.moveTo.bind(ctx), ol = ctx.lineTo.bind(ctx), ob = ctx.beginPath.bind(ctx), os = ctx.stroke.bind(ctx);
    let sub = 0, ln = 0;
    ctx.moveTo = (x, y) => { sub++; moves.push([x, y]); return om(x, y); };
    ctx.lineTo = (x, y) => { ln++; lines.push([x, y]); return ol(x, y); };
    const lod = r.draw();
    ctx.moveTo = om; ctx.lineTo = ol;
    const n = store.count;
    const a = Math.max(0, Math.floor(start)), z = Math.min(n, Math.ceil(end));
    return { subpaths: sub, lines: ln, lod: !!lod, cols: r._cols, span: z - a,
             per: (z - a) / r._cols, xFirst: moves[0]?.[0],
             xLast: lines.length ? lines[lines.length - 1][0] : (moves[0]?.[0] ?? null),
             lineX: lines.map(p => p[0]), lineY: lines.map(p => p[1]),
             plotW: r.plotW, padL: r.padding.l };
  };
  return 1;
`;
await ev(PROBE);

const sig = mk => `(i) => ${mk}`;
const SINE = sig('Math.sin(i * 2 * Math.PI / 50)');

console.log('\n── 1. 深放大：每列远不到 1 个样本（per ≈ 0.04）──');
{
  const r = await ev(`
    const st = window.__mkStore(2000, ${SINE});
    return window.__draw(st, 100, 130);`);
  ok(r.subpaths === 1, `一条连续折线（子路径数 ${r.subpaths}，期望 1）`, JSON.stringify(r).slice(0, 200));
  ok(r.lines === r.span - 1, `逐样本连点：${r.lines} 段（可见样本 ${r.span}）`);
  const mono = r.lineX.every((x, i) => i === 0 || x >= r.lineX[i - 1]);
  ok(mono, 'x 单调不减（没有回头/跳位）');
  ok(r.xLast - r.xFirst > r.plotW * 0.9, `横向铺满绘图区（${Math.round(r.xFirst)} → ${Math.round(r.xLast)}，宽 ${Math.round(r.plotW)}）`);
}

console.log('\n── 2. 用户那次截图的量级：per ≈ 0.7（样本比像素稀）──');
{
  const r = await ev(`
    const st = window.__mkStore(4000, ${SINE});
    return window.__draw(st, 0, 552);`);
  ok(r.per > 0.4 && r.per < 1, `构造出 per=${r.per.toFixed(3)} 的亚像素缩放`);
  ok(r.subpaths === 1, `仍是一条连续线（子路径数 ${r.subpaths}）—— 旧代码这类缩放会断成几十段`);
}

console.log('\n── 3. 每列 1~1.5 个样本：折线分支的边界 ──');
{
  const r = await ev(`
    const st = window.__mkStore(4000, ${SINE});
    return window.__draw(st, 0, 1100);`);
  ok(r.per >= 1 && r.per < 1.5, `per=${r.per.toFixed(3)} 落在折线分支`);
  ok(r.subpaths === 1, `连续（子路径数 ${r.subpaths}）`);
}

console.log('\n── 4. 缩到全览：包络分支仍然每条通道一条闭合路径 ──');
{
  const r = await ev(`
    const st = window.__mkStore(200000, ${SINE});
    return window.__draw(st, 0, 200000);`);
  ok(r.lod === true, `走 LOD 包络（per=${r.per.toFixed(1)} 样本/列）`);
  ok(r.subpaths === 1, `一条闭合包络路径（子路径数 ${r.subpaths}）`);
  ok(r.lines > 4, `包络有上下沿（${r.lines} 段）`);
}

console.log('\n── 5. 数据里真有 NaN（缺样本）时必须断线，不能"连过去" ──');
{
  const r = await ev(`
    const st = window.__mkStore(200, (i) => (i > 90 && i < 110) ? NaN : Math.sin(i / 5));
    const n = st.count;
    st.channels[0].data[95] = NaN;
    return window.__draw(st, 0, 200);`);
  ok(r.lod === false, '200 个样本 / 800 列 → 点线分支');
  ok(r.subpaths === 2, `确实断成 2 段（子路径数 ${r.subpaths}）—— 真缺口该断`);
}

console.log('\n── 6. 放大后线宽/坐标仍在设备像素栅格上（不糊）──');
{
  const r = await ev(`
    const st = window.__mkStore(400, (i) => Math.sin(i / 9));
    const cv = window.__cv;
    const dpr = window.devicePixelRatio || 1;
    const out = window.__draw(st, 10, 60);
    out.canvasW = cv.width; out.cssW = cv.clientWidth; out.dpr = dpr;
    return out;`);
  ok(Math.abs(r.canvasW - Math.round(r.cssW * r.dpr)) <= 1, `backing store 按 dpr 放大（${r.canvasW} = ${r.cssW} × ${r.dpr}）`);
}

console.log(`\n${fail ? '❌' : '✅'} scope-render：${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
