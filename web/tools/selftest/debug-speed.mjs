/** 调试：量 WebUSB 探针每一步的耗时（纯 USB 往返 vs SWD 操作）。 */
const CDP = process.env.CDP || 'http://127.0.0.1:9333';
const APP = process.env.APP || 'http://127.0.0.1:8899/index.html';
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
  setTimeout(() => { if (pend.delete(id)) rej(new Error('超时 ' + method)); }, 120000);
});
const evalJs = async (expr, userGesture = false) => {
  const r = await send('Runtime.evaluate', { expression: expr, userGesture, awaitPromise: true, returnByValue: true });
  return r.exceptionDetails ? 'EXC:' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text) : r.result.value;
};

await send('Page.enable'); await send('Runtime.enable');
await send('Page.navigate', { url: APP });
await sleep(900);
await evalJs(`document.getElementById('r-range').value='0x20000000-0x20005000'`);
await evalJs(`document.getElementById('r-usb-connect').click()`, true);
await sleep(2500);
console.log('探针就绪:', await evalJs('!!window.__tools.rtt.probe'));

const bench = await evalJs(`(async () => {
  const p = window.__tools.rtt.probe;
  const t = async (n, fn) => { const t0 = performance.now(); for (let i=0;i<n;i++) await fn(); return (performance.now()-t0)/n; };
  const info   = await t(20, () => p._ctrl(0x00, new Uint8Array([0xff])));     // 纯 USB 往返
  const setTar = await t(20, () => p._setTAR(0x20000000));
  const rd4    = await t(20, () => p.readMem(0x20000000, 4));
  const rd64   = await t(20, () => p.readMem(0x20000000, 64));
  const rd512  = await t(20, () => p.readMem(0x20000000, 512));
  const rd2k   = await t(10, () => p.readMem(0x20000000, 2048));
  const wr64   = await t(20, () => p.writeMem(0x20004f00, new Uint8Array(64)));
  return { info, setTar, rd4, rd64, rd512, rd2k, wr64,
           kBps: 512/rd512*1000, kBps2k: 2048/rd2k*1000 };
})()`);
console.log('每步耗时（ms）：');
for (const [k, v] of Object.entries(bench)) console.log(`  ${k.padEnd(8)} ${typeof v === 'number' ? v.toFixed(2) : v}`);
ws.close();
