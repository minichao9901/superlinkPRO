/**
 * WebUSB 通路的 SWD 时钟 → RTT 吞吐 扫描（在页面里跑，走真实的 WebUSB 代码）。
 *
 *   node tools/selftest/rtt-speed-webusb-sweep.mjs
 *   CLOCKS=1000,4000,8000,12000,20000,30000 SECS=4 node tools/selftest/rtt-speed-webusb-sweep.mjs
 *
 * 前置：tools\selftest\launch-browser.ps1（带调试端口，且已经授权过探针）
 * 固件：tools/target-firmware/stm32f103_rtt_speed
 *
 * 每一档都做**目标侧对账**（读 g_bytes 与主机读到的字节数比）——
 * 时钟过高时数据会读错，只报速度不报对账就是在骗自己。
 */
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import path from 'node:path';
import { artifact, getBoard } from './board-matrix.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');
const app = join(root, 'app');
const { findSymbol } = await import('file://' + join(app, 'rtt', 'elf.js').replace(/\\/g, '/'));

const CDP = process.env.CDP || 'http://127.0.0.1:9333';
const APP = process.env.APP || 'http://127.0.0.1:8899/index.html';
const CLOCKS = (process.env.CLOCKS || '1000,2000,4000,8000,12000,16000,20000,30000').split(',').map(Number);
const SECS = Number(process.env.SECS || 4);
/**
 * 换板子/换固件时用 ELF=<路径> 覆盖（例：STM32H7B0 那份固件）。
 * 🚨 不覆盖就会拿 F103 的 g_bytes 地址去对账，读回来是垃圾 → 每档都被判"数据不可信"（假警报）。
 * RAM= 可缩小控制块扫描范围（默认 0x20000000-0x20005000 覆盖 F103/H7B0 都够）。
 */
const ELF = process.env.ELF || join(root, artifact('f103ze', 'rtt').split('/').join(path.sep));
const RANGE = process.env.RAM || getBoard('f103ze').viewerRange;

const syms = {};
if (existsSync(ELF)){
  const buf = readFileSync(ELF);
  for (const n of ['g_bytes', 'g_ms']) { const s = findSymbol(buf, n); if (s) syms[n] = s.addr; }
}

const list = await (await fetch(CDP + '/json/list')).json();
const page = list.find(t => t.type === 'page');
if (!page) throw new Error('没有页面目标（先跑 tools\\selftest\\launch-browser.ps1）');
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('CDP 连不上')); });
let seq = 0; const pend = new Map();
ws.onmessage = ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)){ const p = pend.get(m.id); pend.delete(m.id); m.error ? p.rej(new Error(m.error.message)) : p.res(m.result); }
};
const send = (method, params = {}) => new Promise((res, rej) => {
  const id = ++seq; pend.set(id, { res, rej });
  ws.send(JSON.stringify({ id, method, params }));
  setTimeout(() => { if (pend.delete(id)) rej(new Error('超时 ' + method)); }, 240000);
});
const evalJs = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true, userGesture: true });
  if (r.exceptionDetails) throw new Error('页面异常：' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
  return r.result.value;
};
await send('Page.enable'); await send('Runtime.enable');
try { await send('Network.enable'); await send('Network.setCacheDisabled', { cacheDisabled: true }); } catch {}
await send('Page.navigate', { url: APP + '#rtt' });
await new Promise(r => setTimeout(r, 1200));

console.log(`WebUSB SWD 时钟扫描：${CLOCKS.join(', ')} kHz，每档 ${SECS}s\n`);
console.log('  时钟(kHz)   探针上报     吞吐            轮询/s   平均B/轮   目标侧对账');
console.log('  ' + '-'.repeat(76));
const rows = [];
for (const khz of CLOCKS){
  try {
    const res = await evalJs(`(async () => {
      const { WebUsbDapProbe } = await import('/app/rtt/dap-webusb.js');
      const { Rtt } = await import('/app/rtt/protocol.js');
      const { parseRanges } = await import('/app/core/bin.js');
      const dev = (await navigator.usb.getDevices())[0];
      if (!dev) return { error: '没有已授权设备' };
      let p;
      try { p = await WebUsbDapProbe.open(dev, { clockKhz: ${khz} }); }
      catch (e){ return { error: '${khz} kHz 连不上：' + e.message }; }
      const u32 = async a => { if (!a) return null; const b = await p.readMem(a, 4); return (b[0]|(b[1]<<8)|(b[2]<<16)|(b[3]<<24))>>>0; };
      let out = {};
      try {
        const found = await Rtt.locate(p, { ranges: parseRanges(${JSON.stringify(RANGE)}), chunk: 1024 });
        const rtt = new Rtt(p, { addr: found });
        await rtt.init();
        await rtt.readUp(0);
        const g0 = await u32(${syms.g_bytes || 0});
        const t0 = performance.now();
        let total = 0, polls = 0, maxChunk = 0;
        while ((performance.now() - t0) / 1000 < ${SECS}){
          const x = await rtt.readUp(0);
          total += x.bytes.length; polls++;
          if (x.bytes.length > maxChunk) maxChunk = x.bytes.length;
        }
        const dt = (performance.now() - t0) / 1000;
        const g1 = await u32(${syms.g_bytes || 0});
        out = { total, dt, polls, maxChunk, g0, g1, clock: p.clockHz, name: p.name, size: rtt.up[0].size };
      } catch (e){ out = { error: '测量失败：' + e.message }; }
      try { await p.disconnect(); } catch {}
      return out;
    })()`);

    if (res.error){ console.log(`  ${String(khz).padStart(6)}   ✗ ${res.error}`); rows.push({ khz, ok: false }); continue; }
    const bps = res.total / res.dt;
    const wrote = (res.g0 !== null && res.g1 !== null) ? ((res.g1 - res.g0) >>> 0) : null;
    const ok = wrote !== null && wrote > 0 && Math.abs(res.total - wrote) / Math.max(1, wrote) < 0.02;
    console.log(`  ${String(khz).padStart(6)}   ${String(Math.round(res.clock / 1000)).padStart(5)}kHz   ${String(Math.round(bps)).padStart(7)} B/s (${(bps / 1024).toFixed(1).padStart(6)} KB/s)   ${String(Math.round(res.polls / res.dt)).padStart(5)}   ${String(Math.round(res.total / Math.max(1, res.polls))).padStart(7)}   ` +
      (wrote === null ? '—' : ok ? `✔ ${wrote} B` : `✗ 读了 ${res.total} / 目标写 ${wrote}（数据不可信！）`));
    rows.push({ khz, bps, ok });
  } catch (e){
    console.log(`  ${String(khz).padStart(6)}   ✗ ${String(e.message).split('\n')[0]}`);
    rows.push({ khz, ok: false });
  }
}
const best = rows.filter(r => r.ok && r.bps).sort((a, b) => b.bps - a.bps)[0];
console.log('\n结论：' + (best ? `最高可用档 ${best.khz} kHz → ${Math.round(best.bps)} B/s（${(best.bps / 1024).toFixed(1)} KB/s）` : '没有可用档位'));
ws.close();
// 🚨 显式退出：CDP WebSocket / 页面侧挂起的 promise 会让 node 吊着不结束（实测踩到）
process.exit(0);
