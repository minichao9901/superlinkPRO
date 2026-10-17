/** 调试：点「连接探针」之后页面内部的状态。 */
const CDP = process.env.CDP || 'http://127.0.0.1:9333';
const APP = process.env.APP || 'http://127.0.0.1:8899/index.html';
const sleep = ms => new Promise(r => setTimeout(r, ms));

const list = await (await fetch(CDP + '/json/list')).json();
const page = list.find(t => t.type === 'page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('ws 错')); });
let seq = 0; const pend = new Map(); const events = [];
ws.onmessage = ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)){ const p = pend.get(m.id); pend.delete(m.id); m.error ? p.rej(new Error(m.error.message)) : p.res(m.result); return; }
  if (m.method) events.push(m.method);
};
const send = (method, params = {}) => new Promise((res, rej) => {
  const id = ++seq; pend.set(id, { res, rej });
  ws.send(JSON.stringify({ id, method, params }));
  setTimeout(() => { if (pend.delete(id)) rej(new Error('超时 ' + method)); }, 30000);
});
const evalJs = async (expr, userGesture = false) => {
  const r = await send('Runtime.evaluate', { expression: expr, userGesture, awaitPromise: true, returnByValue: true });
  return r.exceptionDetails ? 'EXC:' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text) : r.result.value;
};
const dump = async () => console.log(await evalJs(`JSON.stringify({
  err: document.getElementById('r-err').textContent,
  toasts: document.getElementById('toasts').textContent,
  errors: window.__tools.errors,
  probe: !!window.__tools.rtt.probe,
  usbAuth: window.__tools.rtt.probe ? 'probe-set' : 'none',
})`));

await send('Page.enable'); await send('Runtime.enable');
try { await send('Network.enable'); await send('Network.setCacheDisabled', { cacheDisabled: true }); } catch {}
await send('Page.navigate', { url: APP });
await sleep(900);
console.log('页面里的模块含最新修复标记:', await evalJs(`fetch('/app/rtt/dap-webusb.js',{cache:'reload'}).then(r=>r.text()).then(t=>t.includes('位计数只有 1 个字节') + ' | ' + t.length + ' 字节')`));
console.log('getDevices:', await evalJs('navigator.usb.getDevices().then(d=>JSON.stringify(d.map(x=>({n:x.productName,v:x.vendorId,p:x.productId,opened:x.opened}))))'));
console.log('后端选择框值:', await evalJs(`document.getElementById('r-backend').value`));
await evalJs(`document.getElementById('r-range').value='0x20000000-0x20005000'`);
console.log('--- 点「连接探针」 ---');
await evalJs(`document.getElementById('r-usb-connect').click()`, true);
for (let i = 0; i < 8; i++){
  await sleep(1000);
  process.stdout.write(`t=${i + 1}s `);
  await dump();
  const done = await evalJs('!!window.__tools.rtt.rtt');
  if (done){ console.log('RTT 已就绪'); break; }
}
console.log('收到的事件类型:', [...new Set(events)].join(', '));
ws.close();
