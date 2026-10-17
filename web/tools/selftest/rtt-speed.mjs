/**
 * RTT 吞吐对比脚本：同一块板子、同一份固件，分别用两条主机通路猛读，比 B/s。
 *
 *   node tools/selftest/rtt-speed.mjs bridge 10     # 走桥 + OpenOCD（Tcl RPC）
 *   node tools/selftest/rtt-speed.mjs webusb 10     # 走浏览器 WebUSB（需先启动带调试端口的浏览器）
 *
 * 固件：tools/target-firmware/stm32f103_rtt_speed（while(1) 死循环发 "hello world!\n"，
 *      RTT 配 BLOCK_IF_FIFO_FULL → 目标速率 = 主机读取速率，所以读到的 B/s 就是 RTT 吞吐）。
 * 脚本还会把目标侧的 g_bytes / g_ms 读出来对账（看目标写了多少、还活着没）。
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

const BACKEND = (process.argv[2] || 'bridge').toLowerCase();
const SECS = Number(process.argv[3] || 5);
const CDP = process.env.CDP || 'http://127.0.0.1:9333';
const APP = process.env.APP || 'http://127.0.0.1:8899/index.html';
const RANGE = process.env.RAM || getBoard('f103ze').viewerRange;
// 换板子/换固件时用 ELF=<路径> 覆盖（例：STM32H7B0 那份固件），符号只用于目标侧对账，缺了也能跑
const ELF = process.env.ELF || join(root, artifact('f103ze', 'rtt').split('/').join(path.sep));

// 从 ELF 里取几个全局量的地址，用来交叉验证
const syms = {};
if (existsSync(ELF)){
  const buf = readFileSync(ELF);
  for (const name of ['g_bytes', 'g_loops', 'g_ms', '_SEGGER_RTT']){
    const s = findSymbol(buf, name);
    if (s) syms[name] = s.addr;
  }
}
console.log(`符号: ` + Object.entries(syms).map(([k, v]) => `${k}=0x${v.toString(16)}`).join('  '));

const sleep = ms => new Promise(r => setTimeout(r, ms));
const kBps = n => (n / 1024).toFixed(1);

if (BACKEND === 'bridge'){
  /* ---------------- 走桥 + OpenOCD ---------------- */
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
  // 后端可切换：默认 openocd；BRIDGE_BACKEND=jlink 时走 J-Link 的 GDB RSP mem 模式
  const beBackend = process.env.BRIDGE_BACKEND || 'openocd';
  await call({ t: 'open', backend: beBackend, cfg: beBackend === 'jlink'
    ? { mem: true, device: process.env.JDEV || 'STM32F103C8', speed: Number(process.env.JSPEED || 4000) }
    : {} }, 60000);
  /**
   * 控制块地址：默认靠 magic "SEGGER RTT" 扫描。
   * 🚨 `CB_ADDR=0x…` 可跳过扫描直接指定 —— J-Link 的 mem 模式需要它：
   *    那时我们会**故意清掉控制块的 magic**，好让 JLinkGDBServerCL 自带的 RTT
   *    找不到控制块、不去偷读目标环缓冲（否则它会把环搬空，mem 只能读到个零头）。
   *    固件自己不依赖 magic（它直接用结构体写），所以清掉 magic 对目标毫无影响。
   */
  const found = process.env.CB_ADDR
    ? Number(process.env.CB_ADDR)
    : await Rtt.locate(mem, { ranges: parseRanges(RANGE), chunk: 1024 });
  const rtt = new Rtt(mem, { addr: found });
  await rtt.init();
  console.log(`后端 OpenOCD · 控制块 0x${found.toString(16)} · up0 缓冲 ${rtt.up[0].size} B\n`);

  const u32 = async a => { if (!a) return null; const b = await mem.readMem(a, 4); return (b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)) >>> 0; };
  await rtt.readUp(0);                                     // 先排空，别把开头的横幅算进去
  const g0 = await u32(syms.g_bytes), ms0 = await u32(syms.g_ms);
  const t0 = Date.now();
  let total = 0, polls = 0, maxChunk = 0;
  while ((Date.now() - t0) / 1000 < SECS){
    const r = await rtt.readUp(0);
    total += r.bytes.length; polls++;
    if (r.bytes.length > maxChunk) maxChunk = r.bytes.length;
  }
  const dt = (Date.now() - t0) / 1000;
  const g1 = await u32(syms.g_bytes), ms1 = await u32(syms.g_ms);
  console.log(`读 ${total} 字节 / ${dt.toFixed(2)} s = ${(total / dt).toFixed(0)} B/s (${kBps(total / dt)} KB/s)`);
  console.log(`轮询 ${polls} 次（${(polls / dt).toFixed(1)} 次/秒），平均 ${(total / polls).toFixed(0)} B/次，单次最大 ${maxChunk} B`);
  if (g0 !== null){
    const wrote = (g1 - g0) >>> 0;
    console.log(`目标侧对账：g_bytes ${g0} → ${g1}（写了 ${wrote} 字节），g_ms ${ms0} → ${ms1}`);
    console.log(`  → 目标写入/主机读取 = ${wrote}/${total} = ${(total / Math.max(1, wrote) * 100).toFixed(1)}%（阻塞模式下应当接近 100%）`);
  }
  ws.close();
} else {
  /* ---------------- 走浏览器 WebUSB ---------------- */
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
    setTimeout(() => { if (pend.delete(id)) rej(new Error('超时 ' + method)); }, 180000);
  });
  const evalJs = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true, userGesture: true });
    if (r.exceptionDetails) throw new Error('页面异常：' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
    return r.result.value;
  };
  await send('Page.enable'); await send('Runtime.enable');
  try { await send('Network.enable'); await send('Network.setCacheDisabled', { cacheDisabled: true }); } catch {}
  // 🚨 必须把 ELF 里的 RTT 控制块地址传给页面：页面会优先用 URL 里的 addr，
  //    否则它会拿 localStorage 里上一次的地址（换板子/换固件后就是错的）。
  // ⚠️ 但**前提是板子上跑的正是这个 ELF**！实测踩过：板子上还是旧固件（RTT 块在 0x20000448），
  //    而新 ELF 的符号是 0x2000000c —— 脚本一传就把页面里存的好地址覆盖成错的，
  //    之后用户手动连也连不上（"0x2000000c 处没有 SEGGER RTT 标识"）。
  //    所以要覆盖时显式给：ADDR=0x20000448 node tools/selftest/rtt-speed.mjs webusb 5
  const addrEnv = process.env.ADDR;
  const addrParam = addrEnv ? ('&addr=' + addrEnv)
    : (syms._SEGGER_RTT ? ('&addr=0x' + syms._SEGGER_RTT.toString(16)) : '');
  await send('Page.navigate', { url: APP + '?backend=webusb&auto=1' + addrParam + '#rtt' });
  for (let i = 0; i < 60; i++){
    // 必须等**探针真的连上**（probe 非空）再往下：只等 rtt 实例会撞进"自动连接还在半路"的窗口，
    // 于是读到 probe=null 报 TypeError（实测踩过好几次）。
    if (await evalJs('!!(window.__tools && window.__tools.rtt && window.__tools.rtt.probe && window.__tools.rtt.rtt)')) break;
    if (i === 20) await evalJs('window.__tools.rtt.connectProbe().catch(()=>{})');   // 自动连接没起来就手动补一刀
    await sleep(250);
  }
  console.log(`后端 WebUSB · ` + await evalJs('window.__tools.rtt.probe.name') + '\n');
  // 停掉页面的定时轮询，避免和基准循环抢总线；基准用紧凑 async 循环打满
  await evalJs('window.__tools.rtt.running = false; clearTimeout(window.__tools.rtt.timer); true');
  const res = await evalJs(`(async () => {
    const r = window.__tools.rtt.rtt, p = window.__tools.rtt.probe;
    const u32 = async a => { if (!a) return null; const b = await p.readMem(a, 4); return (b[0]|(b[1]<<8)|(b[2]<<16)|(b[3]<<24))>>>0; };
    await r.readUp(0);
    const g0 = await u32(${syms.g_bytes || 0}), ms0 = await u32(${syms.g_ms || 0});
    const t0 = performance.now();
    let total = 0, polls = 0, maxChunk = 0;
    while ((performance.now() - t0) / 1000 < ${SECS}){
      const x = await r.readUp(0);
      total += x.bytes.length; polls++;
      if (x.bytes.length > maxChunk) maxChunk = x.bytes.length;
    }
    const dt = (performance.now() - t0) / 1000;
    const g1 = await u32(${syms.g_bytes || 0}), ms1 = await u32(${syms.g_ms || 0});
    return { total, dt, polls, maxChunk, g0, g1, ms0, ms1, size: r.up[0].size };
  })()`);
  console.log(`控制块 up0 缓冲 ${res.size} B`);
  console.log(`读 ${res.total} 字节 / ${res.dt.toFixed(2)} s = ${(res.total / res.dt).toFixed(0)} B/s (${kBps(res.total / res.dt)} KB/s)`);
  console.log(`轮询 ${res.polls} 次（${(res.polls / res.dt).toFixed(1)} 次/秒），平均 ${(res.total / res.polls).toFixed(0)} B/次，单次最大 ${res.maxChunk} B`);
  if (res.g0 !== null){
    const wrote = (res.g1 - res.g0) >>> 0;
    console.log(`目标侧对账：g_bytes ${res.g0} → ${res.g1}（写了 ${wrote} 字节），g_ms ${res.ms0} → ${res.ms1}`);
    console.log(`  → 目标写入/主机读取 = ${wrote}/${res.total} = ${(res.total / Math.max(1, wrote) * 100).toFixed(1)}%（阻塞模式下应当接近 100%）`);
  }
  try { await evalJs('window.__tools.rtt.disconnect()'); } catch {}
  ws.close();
}
/**
 * 🚨 必须显式退出：CDP 的 WebSocket / 页面侧的挂起 promise 会让 node 一直吊着，
 *    表现是"测完了却不结束"（实测踩到：跑完 8 秒基准后进程还活着，只能手动杀）。
 */
process.exit(0);
