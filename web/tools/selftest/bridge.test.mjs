/**
 * 桥的端到端自测（真硬件！）：Node 直接当客户端连本机桥，跑**和网页里同一份** RTT 协议代码。
 *
 *   node bridge/rtt-bridge.mjs --target stm32f103        # 另开一个终端
 *   node tools/selftest/bridge.test.mjs
 *
 * 覆盖：WebSocket 握手/协议 → open 后端（OpenOCD）→ 读内存 → 找 RTT 控制块 →
 *       读上行数据 → 下行写命令 → 看固件回包 → 目标复位。
 */
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const app = join(here, '..', '..', 'app');
const { Rtt } = await import('file://' + join(app, 'rtt', 'protocol.js').replace(/\\/g, '/'));
const { parseRanges } = await import('file://' + join(app, 'core', 'bin.js').replace(/\\/g, '/'));

const URL_ = process.env.BRIDGE || 'ws://127.0.0.1:17321/ws';
const RANGE = process.env.RAM || '0x20000000-0x20005000';     // STM32F103 的 20KB RAM
let pass = 0, fail = 0;
const ok = (c, name, extra = '') => { if (c){ pass++; console.log(`  PASS  ${name}`); } else { fail++; console.log(`  FAIL  ${name} ${extra}`); } };
const dec = b => new TextDecoder().decode(b);

// ---------- 极简桥客户端（和网页里 bridge.js 的协议一致） ----------
class Client {
  constructor(url){ this.url = url; this.seq = 0; this.pending = new Map(); this.stream = []; }
  async connect(){
    this.ws = new WebSocket(this.url);
    await new Promise((res, rej) => {
      const t = setTimeout(() => rej(new Error('连不上桥（先启动 bridge/rtt-bridge.mjs）')), 4000);
      this.ws.onopen = () => { clearTimeout(t); res(); };
      this.ws.onerror = () => { clearTimeout(t); rej(new Error('WebSocket 出错')); };
    });
    this.ws.onmessage = ev => {
      const m = JSON.parse(ev.data);
      if (m.t === 'log'){ if (process.env.VERBOSE) console.log('   [桥] ' + m.line); return; }
      if (m.t === 'stream.data'){ this.stream.push(Buffer.from(m.data, 'base64')); return; }
      const p = this.pending.get(m.id);
      if (!p) return;
      this.pending.delete(m.id);
      m.t === 'error' ? p.rej(new Error(m.message)) : p.res(m);
    };
  }
  call(msg, timeout = 30000){
    const id = ++this.seq;
    msg.id = id;
    return new Promise((res, rej) => {
      this.pending.set(id, { res, rej });
      this.ws.send(JSON.stringify(msg));
      setTimeout(() => { if (this.pending.delete(id)) rej(new Error('桥响应超时：' + msg.t)); }, timeout);
    });
  }
  // 与网页里 BridgeClient 相同的两个原语
  async readMem(addr, len){
    const r = await this.call({ t: 'mem.read', addr, len }, 60000);
    const b = Buffer.from(r.data || '', 'base64');
    if (b.length < len){ const o = Buffer.alloc(len); b.copy(o); return new Uint8Array(o); }
    return new Uint8Array(b);
  }
  async writeMem(addr, bytes){ await this.call({ t: 'mem.write', addr, data: Buffer.from(bytes).toString('base64') }, 60000); }
  async reset(){ await this.call({ t: 'target.reset' }); }
  close(){ this.ws.close(); }
}

console.log(`== 桥自测 (${URL_}) ==`);
const c = new Client(URL_);
await c.connect();
ok(true, 'WebSocket 连上桥');

const hello = await c.call({ t: 'hello' });
ok(!!hello.caps, 'hello/ready 握手', JSON.stringify(hello).slice(0, 120));

const t0 = Date.now();
const opened = await c.call({ t: 'open', backend: process.env.BACKEND || 'openocd' }, 60000);
ok(!!opened.info, `open 后端（${opened.info?.mode}）`, JSON.stringify(opened.info));
console.log(`        OpenOCD 版本: ${opened.info?.version}  (${Date.now() - t0} ms)`);

// 读内存：先自证能读到已知内容（0x08000000 处是向量表，前 4 字节 = 初始栈顶 0x20005000）
const vt = await c.readMem(0x08000000, 16);
const sp = vt[0] | (vt[1] << 8) | (vt[2] << 16) | (vt[3] << 24);
const pc = vt[4] | (vt[5] << 8) | (vt[6] << 16) | (vt[7] << 24);
ok(sp === 0x20005000, `向量表[0] = 初始栈顶 0x${(sp >>> 0).toString(16)}（期望 0x20005000）`);
ok(pc >= 0x08000000 && pc < 0x08010000, `向量表[1] = Reset_Handler 0x${(pc >>> 0).toString(16)}`);

const found = await Rtt.locate(c, { ranges: parseRanges(RANGE), chunk: 1024 });
ok(!!found, `RAM 里找到 RTT 控制块 0x${(found || 0).toString(16)}`);

const rtt = new Rtt(c, { addr: found });
await rtt.init();
ok(rtt.maxUp === 2, `上行通道数 = ${rtt.maxUp}（固件声明 2 个：ch0 Terminal / ch1 Log）`);
ok(rtt.maxDown === 1, `下行通道数 = ${rtt.maxDown}`);
const n0 = await rtt.name('up', 0);
const n1 = await rtt.name('up', 1);
ok(n0 === 'Terminal' && n1 === 'Log', `通道名 = "${n0}" / "${n1}"`);

// 读一会儿上行数据（固件每秒往 ch0 打一条带 ANSI 的行）
let got = '', tries = 0;
while (got.length < 40 && tries++ < 60){
  const r = await rtt.readUp(0);
  if (r.bytes.length) got += dec(r.bytes);
  else await new Promise(r => setTimeout(r, 60));
}
ok(got.includes('RTT ch0') || got.includes('tick='), `ch0 收到固件日志 ${got.length} 字节`, JSON.stringify(got.slice(0, 80)));
ok(got.includes('\x1b['), 'ch0 里带 ANSI 颜色（终端渲染用得上）');

// 读 ch1（第二个通道）
let log1 = '', t1 = 0;
while (log1.length < 10 && t1++ < 60){
  const r = await rtt.readUp(1);
  if (r.bytes.length) log1 += dec(r.bytes);
  else await new Promise(r => setTimeout(r, 60));
}
ok(log1.includes('ch1 Log'), `ch1 收到日志 ${log1.length} 字节`, JSON.stringify(log1.slice(0, 60)));

// 下行：给固件发命令
const sent = await rtt.writeDown(0, new TextEncoder().encode('help\r'));
ok(sent === 5, `下行写入 ${sent} 字节`);
let resp = '', t2 = 0;
while (!resp.includes('测试固件命令') && t2++ < 80){
  const r = await rtt.readUp(0);
  if (r.bytes.length) resp += dec(r.bytes);
  else await new Promise(r => setTimeout(r, 60));
}
ok(resp.includes('测试固件命令'), '固件收到命令并回包（help）', JSON.stringify(resp.slice(0, 100)));
// help 很长（>300 字节），一次读不一定读完，继续读到看到 flood 为止
let t2b = 0;
while (!resp.includes('flood') && t2b++ < 40){
  const r = await rtt.readUp(0);
  if (r.bytes.length) resp += dec(r.bytes);
  else await new Promise(r => setTimeout(r, 60));
}
ok(resp.includes('flood') && resp.includes('reboot'), '回包里能看到全部命令列表');

// 非对齐写：给固件发一个不长的命令（走 8 位宽写路径）
await rtt.writeDown(0, new TextEncoder().encode('echo 中文测试\r'));
let resp2 = '', t3 = 0;
while (!resp2.includes('echo:') && t3++ < 80){
  const r = await rtt.readUp(0);
  if (r.bytes.length) resp2 += dec(r.bytes);
  else await new Promise(r => setTimeout(r, 60));
}
ok(resp2.includes('echo: 中文测试'), 'echo 命令回显正确（含 UTF-8）', JSON.stringify(resp2.slice(0, 80)));

// 过载：flood 一次灌 8KB 进 4KB 缓冲（主机读速只有 ~17KB/s）→ 水位必然打到高位
await rtt.writeDown(0, new TextEncoder().encode('flood 8\r'));
let t4 = 0, flooded = 0;
while (t4++ < 120){
  const r = await rtt.readUp(0);
  flooded += r.bytes.length;
  if (r.high) break;
  await new Promise(r => setTimeout(r, 30));
}
const peak = Math.round((rtt.peak || 0) * 100);
ok(rtt.highCount(0) > 0 || peak >= 75, `flood 触发「缓冲高位」信号（${rtt.highCount(0)} 次，峰值水位 ${peak}%）`);
ok(flooded < 8192, `主机只收到 ${flooded}/8192 字节 —— 其余被固件丢弃（RTT 不重传，如实记账）`);

// 目标复位（固件重启后会重新打印横幅）
await c.reset();
await new Promise(r => setTimeout(r, 1500));
let after = '', t5 = 0;
while (after.length < 20 && t5++ < 80){
  const r = await rtt.readUp(0);
  if (r.bytes.length) after += dec(r.bytes);
  else await new Promise(r => setTimeout(r, 60));
}
ok(after.includes('STM32F103') || after.includes('RTT'), `复位后重新读到启动横幅（${after.length} 字节）`, JSON.stringify(after.slice(0, 80)));

c.close();
console.log(`\n结果：${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
