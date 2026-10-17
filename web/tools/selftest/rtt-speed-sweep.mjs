/**
 * SWD 时钟 → RTT 吞吐 的扫描：找出这块探针 + 这块目标能跑多快。
 *
 *   node tools/selftest/rtt-speed-sweep.mjs            # 桥（OpenOCD adapter speed 扫描）
 *   SPEEDS=1000,4000,10000,20000,30000 SECS=4 node tools/selftest/rtt-speed-sweep.mjs
 *
 * 固件：tools/target-firmware/stm32f103_rtt_speed（阻塞模式死循环发 hello world，
 *      所以"主机读到的 B/s"就是 RTT 吞吐；脚本还会读目标侧 g_bytes 对账，
 *      对账不上就说明**时钟太高、数据读错了** —— 这比"看起来更快"重要得多）。
 */
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import path from 'node:path';
import { artifact, getBoard } from './board-matrix.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');
const app = join(root, 'app');
const { Rtt } = await import('file://' + join(app, 'rtt', 'protocol.js').replace(/\\/g, '/'));
const { parseRanges } = await import('file://' + join(app, 'core', 'bin.js').replace(/\\/g, '/'));
const { findSymbol } = await import('file://' + join(app, 'rtt', 'elf.js').replace(/\\/g, '/'));

const SPEEDS = (process.env.SPEEDS || '1000,2000,4000,8000,12000,20000,30000').split(',').map(Number);
const SECS = Number(process.env.SECS || 4);
const RANGE = process.env.RAM || getBoard('f103ze').viewerRange;
const ELF = join(root, artifact('f103ze', 'rtt').split('/').join(path.sep));

const syms = {};
if (existsSync(ELF)){
  const buf = readFileSync(ELF);
  for (const n of ['g_bytes', 'g_ms']) { const s = findSymbol(buf, n); if (s) syms[n] = s.addr; }
}
const u32of = (b, o) => ((b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)) >>> 0);
const kB = n => (n / 1024).toFixed(1);

const ws = new WebSocket(process.env.BRIDGE || 'ws://127.0.0.1:17321/ws');
let seq = 0; const pend = new Map();
ws.onmessage = ev => {
  const m = JSON.parse(ev.data);
  if (m.t === 'log') return;
  const p = pend.get(m.id);
  if (p){ pend.delete(m.id); m.t === 'error' ? p.rej(new Error(m.message)) : p.res(m); }
};
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('连不上桥（先启动 bridge/rtt-bridge.mjs）')); });
const call = (msg, to = 60000) => new Promise((res, rej) => {
  const id = ++seq; msg.id = id; pend.set(id, { res, rej });
  ws.send(JSON.stringify(msg));
  setTimeout(() => { if (pend.delete(id)) rej(new Error('超时 ' + msg.t)); }, to);
});
const mem = {
  async readMem(addr, len){
    const r = await call({ t: 'mem.read', addr, len });
    const b = Buffer.from(r.data, 'base64');
    if (b.length < len){ const o = Buffer.alloc(len); b.copy(o); return new Uint8Array(o); }
    return new Uint8Array(b);
  },
  async writeMem(addr, b){ await call({ t: 'mem.write', addr, data: Buffer.from(b).toString('base64') }); },
};

console.log(`扫描 adapter speed：${SPEEDS.join(', ')} kHz，每档 ${SECS}s\n`);
console.log('  速度(kHz)   吞吐          轮询/s   平均B/轮   目标侧对账');
console.log('  ' + '-'.repeat(64));
const rows = [];
for (const khz of SPEEDS){
  try {
    await call({ t: 'open', backend: 'openocd', cfg: { speed: khz } }, 90000);   // 每次 open 都会重起 OpenOCD
    const found = await Rtt.locate(mem, { ranges: parseRanges(RANGE), chunk: 1024 });
    const rtt = new Rtt(mem, { addr: found });
    await rtt.init();
    const u32 = async a => { if (!a) return null; return u32of(await mem.readMem(a, 4), 0); };
    await rtt.readUp(0);
    const g0 = await u32(syms.g_bytes);
    const t0 = Date.now();
    let total = 0, polls = 0;
    while ((Date.now() - t0) / 1000 < SECS){
      const r = await rtt.readUp(0);
      total += r.bytes.length; polls++;
    }
    const dt = (Date.now() - t0) / 1000;
    const g1 = await u32(syms.g_bytes);
    const wrote = (g1 !== null && g0 !== null) ? ((g1 - g0) >>> 0) : null;
    const ok = wrote === null ? '—' : (wrote > 0 && Math.abs(total - wrote) / Math.max(1, wrote) < 0.02 ? `✔ ${wrote} B（一致）` : `✗ 读了 ${total} / 目标写 ${wrote}（数据不可信！）`);
    const bps = total / dt;
    rows.push({ khz, bps, ok: wrote !== null && Math.abs(total - wrote) / Math.max(1, wrote) < 0.02 });
    console.log(`  ${String(khz).padStart(6)}   ${String(Math.round(bps)).padStart(7)} B/s (${kB(bps).padStart(5)} KB/s)  ${String(Math.round(polls / dt)).padStart(5)}   ${String(Math.round(total / Math.max(1, polls))).padStart(7)}   ${ok}`);
  } catch (e){
    console.log(`  ${String(khz).padStart(6)}   失败：${e.message.split('\n')[0]}`);
    rows.push({ khz, bps: 0, ok: false });
  }
}
const best = rows.filter(r => r.ok).sort((a, b) => b.bps - a.bps)[0];
console.log('\n结论：' + (best ? `最高可用档 ${best.khz} kHz → ${Math.round(best.bps)} B/s（${kB(best.bps)} KB/s）` : '没有可用档位'));
ws.close();
