/**
 * 参数扫描：在页面里逐个组合试 SWD 初始化，看哪一组能读到 IDCODE（ACK=OK）。
 *   node tools/selftest/debug-sweep.mjs
 */
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
  setTimeout(() => { if (pend.delete(id)) rej(new Error('超时 ' + method)); }, 180000);
});
const evalJs = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true, userGesture: true });
  return r.exceptionDetails ? 'EXC:' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text) : r.result.value;
};
await send('Page.enable'); await send('Runtime.enable');
try { await send('Network.enable'); await send('Network.setCacheDisabled', { cacheDisabled: true }); } catch {}
await send('Page.navigate', { url: APP });
await sleep(900);

const out = await evalJs(`(async () => {
  const { WebUsbDapProbe } = await import('/app/rtt/dap-webusb.js');
  const dev = (await navigator.usb.getDevices())[0];
  if (!dev) return '没有已授权设备（先在页面上连接过一次探针）';
  const CMD = { Connect:0x02, CFG:0x04, XFER:0x05, CLK:0x11, SEQ:0x12, SWDC:0x13 };
  const le = v => [v & 0xff, (v>>>8)&0xff, (v>>>16)&0xff, (v>>>24)&0xff];
  const ACT = [0x9e,0xe7,0xff,0xff,0xff,0xff,0xff,0xff,0xff,0xff,0x00];
  const hex = b => [...b].map(x=>x.toString(16).padStart(2,'0')).join(' ');
  const res = [];

  const variants = [];
  for (const swdc of ['skip', 0, 1, 2, 3]) for (const act of [true, false])
    variants.push({ swdc, act, clk: 1000000, cfgIdle: 0 });
  variants.push({ swdc: 0, act: true, clk: 1000000, cfgIdle: 1 });
  variants.push({ swdc: 0, act: true, clk: 500000, cfgIdle: 0 });
  variants.push({ swdc: 0, act: true, clk: 4000000, cfgIdle: 0 });
  variants.push({ swdc: 1, act: true, clk: 1000000, cfgIdle: 0, connectTimes: 1 });

  for (const v of variants){
    let line = 'swdc=' + v.swdc + ' act=' + (v.act ? 'Y' : 'N') + ' clk=' + (v.clk/1000) + 'k idle=' + v.cfgIdle + ' → ';
    let p;
    try {
      p = await WebUsbDapProbe.open(dev, { skipTargetInit: true, skipInfo: true, skipClearHalt: true });
      const port = await p._ctrl(CMD.Connect, Uint8Array.of(1));
      await p.setClock(v.clk);
      if (v.swdc !== 'skip') await p._ctrl(CMD.SWDC, Uint8Array.of(v.swdc));
      if (v.act) await p._ctrl(CMD.SEQ, new Uint8Array([88, ...ACT]));
      await p._ctrl(CMD.CFG, Uint8Array.of(v.cfgIdle, 0xe8, 0x03, 0, 0));
      const r = await p._ctrl(CMD.XFER, Uint8Array.of(0,1,0x02,0,0,0,0));
      const ack = r[1] & 7;
      const idc = ((r[2]|(r[3]<<8)|(r[4]<<16)|(r[5]<<24))>>>0).toString(16);
      line += 'ack=' + ack + (ack === 1 ? ' ★OK IDCODE=0x' + idc : '  raw=' + hex(r));
    } catch (e){ line += '异常 ' + e.message; }
    try { await p?.disconnect(); } catch {}
    res.push(line);
    await new Promise(r => setTimeout(r, 150));
  }
  return res.join('\\n');
})()`);
console.log(out);
ws.close();
