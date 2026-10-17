/**
 * 浏览器端 UI 自测的**驱动**（CDP）：
 *   node tools/selftest/ui.page.test.mjs        （等价：make test-ui）
 * 前置：静态服务在 8899；浏览器带 CDP 在 9333（没有就自己拉一个）。
 *
 * 它打开 index.html?demo=serial&selftest=1&hid=mock，等页面内的 runUiSelfTest()
 * （tools/selftest/ui.selftest.mjs，跑的是真界面对象）把结果写进 #selftest，再逐条报出来。
 * 关掉 HTTP 缓存 —— 否则刚改完 JS 就跑，会拿到上一次的模块，报出莫名其妙的假失败。
 */
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');
const CDP = process.env.CDP || 'http://127.0.0.1:9333';
const APP = process.env.APP || 'http://127.0.0.1:8899/index.html';
const URL_ = APP + '?demo=serial&selftest=1&hid=mock&t=' + Date.now();

setTimeout(() => { console.error('[WATCHDOG] 总超时'); process.exit(9); }, 120000);
const sleep = ms => new Promise(r => setTimeout(r, ms));

// 浏览器没起来就自己拉一个（和 make browser 同一个脚本）
async function ensureBrowser(){
  try {
    await fetch(CDP + '/json/version', { signal: AbortSignal.timeout(2500) });
    return;
  } catch {}
  console.log('  （CDP 浏览器没在跑，自己拉一个…）');
  const ps = spawn('pwsh', ['-NoProfile', '-File', join(root, 'tools', 'selftest', 'launch-browser.ps1'), '-Port', '9333', '-Url', APP],
    { stdio: 'ignore', detached: true });
  ps.unref();
  for (let i = 0; i < 90; i++){          // Edge 冷启动偶尔要十几秒，给足 45s
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
const send = (method, params = {}, t = 20000) => new Promise((res, rej) => {
  const id = ++seq; pend.set(id, { res, rej });
  ws.send(JSON.stringify({ id, method, params }));
  setTimeout(() => { if (pend.delete(id)) rej(new Error(method + ' 超时')); }, t);
});

await send('Page.enable');
await send('Runtime.enable');
try { await send('Network.enable'); await send('Network.setCacheDisabled', { cacheDisabled: true }); } catch {}
await send('Page.navigate', { url: URL_ });
console.log('目标: ' + URL_);

const t0 = Date.now();
while (Date.now() - t0 < 90000){
  await sleep(1000);
  const r = await send('Runtime.evaluate', { expression: `document.getElementById('selftest')?.textContent || ''`, returnByValue: true });
  const s = r.result.value;
  if (!s || !s.startsWith('{')) continue;
  const d = JSON.parse(s);
  const steps = d.selftest || [];
  console.log(`页面自检: ok=${d.ok}  errors=${JSON.stringify(d.errors)}  tabs=${JSON.stringify(d.tabs)}`);
  for (const st of steps) console.log(`  ${st.ok ? 'PASS' : 'FAIL'}  ${st.name}${st.note ? '  ' + st.note : ''}${st.error ? '  → ' + st.error : ''}`);
  const good = d.ok && steps.length > 0 && steps.every(x => x.ok);
  console.log(`\n${good ? 'OK' : 'FAIL'}  ${steps.filter(x => x.ok).length} / ${steps.length} 步通过`);
  ws.close();
  process.exit(good ? 0 : 1);
}
console.error('FAIL  90s 内页面自检没出结果（看 #selftest 有没有内容 / 页面是否报错）');
ws.close();
process.exit(1);
