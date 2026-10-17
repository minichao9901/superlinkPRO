/**
 * 给 README 截图（CDP）：三个标签页各来一张，另外一张 WebUSB 真机 RTT。
 *   node tools/selftest/shots.mjs
 * 产物在 docs/shots/。
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const CDP = process.env.CDP || 'http://127.0.0.1:9333';
const BASE = process.env.APP || 'http://127.0.0.1:8899/index.html';
const outDir = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'docs', 'shots');
mkdirSync(outDir, { recursive: true });
const sleep = ms => new Promise(r => setTimeout(r, ms));

const list = await (await fetch(CDP + '/json/list')).json();
const page = list.find(t => t.type === 'page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('ws 错')); });
let seq = 0; const pend = new Map();
ws.onmessage = ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)){ const p = pend.get(m.id); pend.delete(m.id); m.error ? p.rej(new Error(m.error.message)) : p.res(m.result); }
};
const send = (method, params = {}) => new Promise((res, rej) => {
  const id = ++seq; pend.set(id, { res, rej });
  ws.send(JSON.stringify({ id, method, params }));
  setTimeout(() => { if (pend.delete(id)) rej(new Error('超时 ' + method)); }, 60000);
});
await send('Page.enable'); await send('Runtime.enable');
try { await send('Network.enable'); await send('Network.setCacheDisabled', { cacheDisabled: true }); } catch {}
await send('Emulation.setDeviceMetricsOverride', { width: 1500, height: 950, deviceScaleFactor: 1, mobile: false });

const evaluate = async expression => (await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })).result?.value;

async function shot(url, waitMs, name, prep){
  await send('Page.navigate', { url });
  await sleep(waitMs);
  if (prep) await prep();
  const r = await send('Page.captureScreenshot', { format: 'png' });
  const f = join(outDir, name);
  writeFileSync(f, Buffer.from(r.data, 'base64'));
  console.log('  截图 ' + name + '  ' + Buffer.from(r.data, 'base64').length + ' B');
}

console.log('截图到 ' + outDir);
await shot(BASE + '?demo=serial&autoconnect=1#serial', 3000, '1-serial.png');
await shot(BASE + '?demo=serial&autoconnect=1#terminal', 3000, '2-terminal.png');
await shot(BASE + '?backend=mock&auto=1#rtt', 4000, '3-rtt-mock.png');
await shot(BASE + '?t=' + Date.now() + '#gen', 2500, '4-gen.png', async () => {
  // 让截图里是个像样的工程名（默认是 project 占位符）
  await evaluate(`(()=>{const e=document.getElementById('g-project');e.value='STM32F103_Develop';e.dispatchEvent(new Event('input',{bubbles:true}));return true})()`);
  await sleep(500);
});
ws.close();
