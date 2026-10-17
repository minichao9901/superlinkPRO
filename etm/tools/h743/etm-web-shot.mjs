// 给正在显示解码结果的页面拍一张截图（不用 deviceMetricsOverride，避免把用户窗口钉住）。
import fs from 'node:fs';

const CDP = process.env.CDP || 'http://127.0.0.1:9340';
const APP = process.env.APP || 'http://127.0.0.1:8766/';
const OUT = process.env.OUT || 'E:\\web-etm-trace\\captures\\h743-sweep\\30mhz\\web-page-decode.png';

const list = await (await fetch(`${CDP}/json/list`)).json();
const page = list.find(t => t.type === 'page' && t.url.startsWith(APP.slice(0, 24)));
if (!page) { console.error('找不到页面'); process.exit(2); }
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('ws')); });
let seq = 0; const pending = new Map();
ws.onmessage = ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); }
};
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++seq; pending.set(id, { resolve, reject });
  ws.send(JSON.stringify({ id, method, params }));
  setTimeout(() => { if (pending.delete(id)) reject(new Error(method + ' 超时')); }, 20000);
});
await send('Page.enable');
await send('Runtime.enable');
await send('Page.bringToFront');
await new Promise(r => setTimeout(r, 800));
const shot = await send('Page.captureScreenshot', { format: 'png' });
fs.writeFileSync(OUT, Buffer.from(shot.data, 'base64'));
console.log('截图:', OUT, fs.statSync(OUT).size, '字节');
ws.close();
process.exit(0);
