// 用真页面（http://127.0.0.1:8766）解码 H743 同频记录并导出「简化源码 .c」。
// 流程完全走 UI：选记录 -> 打开记录 -> 单独解码 -> 选导出格式 -> 导出（浏览器下载）。
import fs from 'node:fs';
import path from 'node:path';

const CDP = process.env.CDP || 'http://127.0.0.1:9340';
const APP = process.env.APP || 'http://127.0.0.1:8766/';
const SID = process.env.SID || 'h743-clk30-trace';
const DL = process.env.DL || 'E:\\web-etm-trace\\tmp\\web-download';
fs.mkdirSync(DL, { recursive: true });

const watchdog = setTimeout(() => { console.error('WATCHDOG: 90s 未完成'); process.exit(9); }, 90_000);
watchdog.unref?.();

const list = await (await fetch(`${CDP}/json/list`)).json();
const page = list.find(t => t.type === 'page' && t.url.startsWith(APP.slice(0, 24)));
if (!page) { console.error('找不到页面目标:', list.map(t => t.url)); process.exit(2); }
console.log('页面:', page.url);

const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = e => rej(new Error('ws 失败')); });
let seq = 0;
const pending = new Map();
ws.onmessage = ev => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
  }
};
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++seq;
  pending.set(id, { resolve, reject });
  ws.send(JSON.stringify({ id, method, params }));
  setTimeout(() => { if (pending.delete(id)) reject(new Error(method + ' 超时')); }, 30_000);
});
const evalJs = async (expr, awaitPromise = false) => {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise });
  if (r.exceptionDetails) throw new Error('页面异常: ' + JSON.stringify(r.exceptionDetails.exception?.description || r.exceptionDetails));
  return r.result.value;
};

await send('Runtime.enable');
for (const m of [['Browser.setDownloadBehavior', { behavior: 'allow', downloadPath: DL, eventsEnabled: true }],
                 ['Page.setDownloadBehavior', { behavior: 'allow', downloadPath: DL }]]) {
  try { await send(m[0], m[1]); console.log('已设置下载目录:', m[0]); break; } catch (e) { /* 换下一个 */ }
}

const waitFor = async (label, expr, ms = 240_000) => {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    if (await evalJs(expr)) { console.log(`  ✓ ${label} (${((Date.now() - t0) / 1000).toFixed(1)}s)`); return true; }
    await new Promise(r => setTimeout(r, 500));
  }
  throw new Error('等待超时: ' + label);
};

console.log('1) 等页面就绪 & 记录列表出现目标记录');
const ready = await waitFor('记录列表含 ' + SID,
  `!![...(document.getElementById('records')?.options||[])].find(o=>o.value===${JSON.stringify(SID)})`, 60_000);
if (!ready) process.exit(3);

console.log('2) 选中并打开记录');
console.log('   ', await evalJs(`(()=>{const s=document.getElementById('records');s.value=${JSON.stringify(SID)};document.getElementById('open-record').click();return 'clicked';})()`));
await waitFor('记录已载入', `document.getElementById('service-state')?.textContent.length>0 && !document.getElementById('job-state')?.textContent.includes('解码中')`, 60_000);
await new Promise(r => setTimeout(r, 1500));

console.log('3) 点「单独解码」（走服务端 /api/decode，和页面同一个函数）');
console.log('   解码前记录状态:', await evalJs(`(()=>{const o=[...document.getElementById('records').options].find(o=>o.value===${JSON.stringify(SID)});return o?o.textContent:'无';})()`));
await evalJs(`document.getElementById('decode').click(); 'decode-clicked'`);
await waitFor('解码完成（选项里不再有「未解码」）',
  `(()=>{const o=[...document.getElementById('records').options].find(o=>o.value===${JSON.stringify(SID)});return !!o && !o.textContent.includes('未解码');})()`,
  600_000);
console.log('   解码后记录状态:', await evalJs(`(()=>{const o=[...document.getElementById('records').options].find(o=>o.value===${JSON.stringify(SID)});return o?o.textContent:'无';})()`));
await new Promise(r => setTimeout(r, 2000));

console.log('4) 导出格式选「简化源码 .c」并点导出');
console.log('   ', await evalJs(`(()=>{const s=document.getElementById('export-type');s.value='simplified';s.dispatchEvent(new Event('change'));document.getElementById('export').click();return s.value;})()`));

console.log('5) 等浏览器下载文件到', DL);
const t0 = Date.now();
while (Date.now() - t0 < 60_000) {
  const files = fs.readdirSync(DL).filter(f => f.endsWith('.c') && !f.endsWith('.crdownload'));
  if (files.length) {
    const f = files.sort().pop();
    const full = path.join(DL, f);
    console.log(`  ✓ 下载完成: ${full} (${fs.statSync(full).size} 字节)`);
    break;
  }
  await new Promise(r => setTimeout(r, 500));
}
ws.close();
process.exit(0);
