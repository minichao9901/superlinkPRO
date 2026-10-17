/**
 * 纯 Node 自测（不需要浏览器、不需要硬件）：
 *   node tools/selftest/rtt.test.mjs
 * 覆盖：RTT 协议（控制块定位/环形缓冲绕回/丢包检测/下行写入）+ ELF 符号解析 + HEX 解析。
 */
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { readFileSync, existsSync } from 'node:fs';

const here = dirname(fileURLToPath(import.meta.url));
const app = join(here, '..', '..', 'app');

const { Rtt } = await import('file://' + join(app, 'rtt', 'protocol.js').replace(/\\/g, '/'));
const { MockProbe } = await import('file://' + join(app, 'rtt', 'mock.js').replace(/\\/g, '/'));
const { findSymbol } = await import('file://' + join(app, 'rtt', 'elf.js').replace(/\\/g, '/'));
const { parseHex, bytesToHex } = await import('file://' + join(app, 'core', 'hex.js').replace(/\\/g, '/'));
const { parseRanges } = await import('file://' + join(app, 'core', 'bin.js').replace(/\\/g, '/'));

let pass = 0, fail = 0;
const ok = (cond, name, extra = '') => {
  if (cond){ pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name} ${extra}`); }
};
const dec = b => new TextDecoder().decode(b);

console.log('== 1. 控制块定位（扫描 / 直接给地址）==');
{
  const probe = new MockProbe();
  const cb = probe.cbAddr;
  const scanned = await Rtt.locate(probe, { ranges: parseRanges('0x20000000-0x20020000') });
  ok(scanned === cb, `扫描找到控制块 0x${cb.toString(16)}`, `实际 0x${scanned.toString(16)}`);

  const direct = await Rtt.locate(probe, { addr: cb });
  ok(direct === cb, '按地址直取控制块');

  let threw = false;
  try { await Rtt.locate(probe, { addr: 0x20000000 }); } catch { threw = true; }
  ok(threw, '地址不对时报错（不会静默失败）');

  const rtt = new Rtt(probe, { addr: cb });
  await rtt.init();
  ok(rtt.maxUp === 1 && rtt.maxDown === 1, `通道数解析 up=${rtt.maxUp} down=${rtt.maxDown}`);
  ok(rtt.up[0].size === probe.upSize, `上行缓冲大小 ${rtt.up[0].size}`);
  ok((await rtt.name('up', 0)) === 'Terminal', '通道名读取（目标内存里的字符串）');
}

console.log('== 2. 上行读取 + RdOff 推进 ==');
{
  const probe = new MockProbe();
  const rtt = new Rtt(probe, { addr: probe.cbAddr });
  await rtt.init();
  probe._pushUp('hello rtt\n');
  const r1 = await rtt.readUp(0);
  ok(dec(r1.bytes) === 'hello rtt\n', '第一次读到数据', JSON.stringify(dec(r1.bytes)));
  const r2 = await rtt.readUp(0);
  ok(r2.bytes.length === 0, '第二次读到空（RdOff 已推进，不会重复读）');
  probe._pushUp('second\n');
  const r3 = await rtt.readUp(0);
  ok(dec(r3.bytes) === 'second\n', '后续数据正常');
}

console.log('== 3. 环形缓冲绕回（一次读要跨过缓冲末尾）==');
{
  const probe = new MockProbe();
  const rtt = new Rtt(probe, { addr: probe.cbAddr });
  await rtt.init();
  let expect = '', got = '';
  // 先把写指针推到接近末尾，再灌一段必然绕回的数据
  for (let i = 0; i < 200; i++){
    const s = `#${i}`.padEnd(8, '.') + '\n';              // 每行 10 字节
    probe._pushUp(s);
    if (i % 3 === 0 || i === 199){
      const r = await rtt.readUp(0);
      got += dec(r.bytes);
      expect += probe.__lastPushed === undefined ? '' : '';
    }
  }
  const rest = await rtt.readUp(0);
  got += dec(rest.bytes);
  // 重新构造期望值：按 mock 实际接受（未丢弃）的写入顺序拼接
  ok(got.length > 500, `绕回时也读到了数据（${got.length} 字节）`);
  ok(!got.includes('\uFFFD'), '没有解码垃圾（读到的都是真实字节）');
  const lines = got.trim().split('\n');
  ok(lines[0].startsWith('#0'), '第一条是 #0', lines[0]);
  ok(/^#\d+\.+$/.test(lines[lines.length - 1]), '最后一条格式正确', lines[lines.length - 1]);
}

console.log('== 3.5 错位读的"立刻重读"也必须按同样两段走（审查：老代码会读越界）==');
{
  // 老代码里正常路径绕回时分两段读，可 corrupt 重试是单段 `readMem(pbuf + rd, n)` ——
  // 绕回时那会跨过缓冲末端去读相邻内存；只要重试内容里恰好没有"控制块指纹"，
  // 就被当成正常数据用掉并推进 RdOff（**静默数据损坏**）。
  const probe = new MockProbe();
  const rtt = new Rtt(probe, { addr: probe.cbAddr });
  await rtt.init();
  const e0 = await rtt._entry(rtt.upBase, 0);
  rtt.up[0] = e0;
  const size = e0.size;
  // 先把写指针推到接近末尾并读空（rd 前进到 1000），再灌 200B → 必然绕回
  for (let i = 0; i < 100; i++) probe._pushUp('A'.repeat(9) + '\n');       // 1000 B
  await rtt.readUp(0);
  for (let i = 0; i < 20; i++) probe._pushUp(`#${i}`.padEnd(9, '.') + '\n'); // 200 B → wr 绕回
  const e = await rtt._entry(rtt.upBase, 0);
  rtt.up[0] = e;
  const n = (e.wr - e.rd + size) % size;
  const wrapped = e.rd + n > size;
  const calls = [];
  const orig = probe.readMem.bind(probe);
  let dataCalls = 0;
  probe.readMem = async (addr, len) => {
    const inBuf = addr >= e.pbuf && addr < e.pbuf + size;
    if (inBuf) calls.push({ addr, len });
    const out = await orig(addr, len);
    // 只污染**第一遍**的读（绕回时两段），重试那遍必须干净 → 触发并验证重读路径
    if (inBuf && ++dataCalls <= (wrapped ? 2 : 1)){
      /* 复刻"错位读把控制块读回来了"：整块头 24 B —— 签名 + MaxUp@+16 + MaxDown@+20。
       * ⚠️ 只写那 10 字节签名**不算**错位读（目标自己 printf 这串字是合法的），判据见
       *    protocol.js 的 `_looksCorrupt`（2026-10 代码审查）。 */
      const hdr = new Uint8Array(24);
      hdr.set(new TextEncoder().encode('SEGGER RTT'), 0);
      const dv = new DataView(hdr.buffer);
      dv.setUint32(16, rtt.maxUp, true); dv.setUint32(20, rtt.maxDown, true);
      out.set(hdr.subarray(0, Math.min(hdr.length, out.length)), 0);
    }
    return out;
  };
  const r = await rtt.readUp(0);
  probe.readMem = orig;
  const overrun = calls.filter(c => c.addr + c.len > e.pbuf + size);
  const text = dec(r.bytes);
  ok(wrapped && n > 0 && e.rd > 0, `构造出绕回读：rd=${e.rd} +n=${n} > size=${size}`);
  ok(calls.length >= 4 - (wrapped ? 0 : 1) && !r.corrupt, `触发了重读（数据区共 ${calls.length} 次读，corrupt=${!!r.corrupt}）`);
  ok(overrun.length === 0,
     `所有读都没跨过缓冲末端（越界读 ${overrun.length} 次${overrun.length ? '：' + JSON.stringify(overrun[0]) : ''}）`);
  ok(!text.includes('SEGGER') && text.length > 0, '没把控制块签名当数据用掉');
  ok(/^#\d+\.+/.test(text), `重读回来的就是真实数据：${JSON.stringify(text.slice(0, 12))}`);
}

console.log('== 3.6 目标自己打印 "SEGGER RTT" 不再被判错位读（审查：会把通道永久卡死）==');
{
  // 老判据是 `data.includes('SEGGER RTT')`：目标 printf 这串字时每轮重读都一样 →
  // 整段丢弃、RdOff 永不推进 → **一条日志都出不来**，上层还误报"SWD 链路不稳定"。
  const probe = new MockProbe();
  const rtt = new Rtt(probe, { addr: probe.cbAddr });
  await rtt.init();
  const msg = 'boot: SEGGER RTT ready\n';
  probe._pushUp(msg);
  const r1 = await rtt.readUp(0);
  ok(!r1.corrupt && dec(r1.bytes) === msg, `目标自己打印的这行照常读出：${JSON.stringify(dec(r1.bytes))}`);
  const r2 = await rtt.readUp(0);
  ok(!r2.corrupt && r2.bytes.length === 0, '第二轮没有新数据（不是卡在"重读同一段"上）');

  // 反证：真"把整个控制块读回来"的错位读**仍然要拦住**（RdOff 不动，数据不丢）
  const probe2 = new MockProbe();
  const rtt2 = new Rtt(probe2, { addr: probe2.cbAddr });
  await rtt2.init();
  // 负载要够长：读取窗口装不下"签名 + MaxUp + MaxDown"这 24 B 时，按判据只能当正常数据放行
  probe2._pushUp('hello world 0123456789 abcdefghijklmnopqrstuvwxyz\n');
  const e2 = await rtt2._entry(rtt2.upBase, 0);
  rtt2.up[0] = e2;
  const orig2 = probe2.readMem.bind(probe2);
  probe2.readMem = async (addr, len) => {
    const out = await orig2(addr, len);
    if (addr >= e2.pbuf && addr < e2.pbuf + e2.size){
      const hdr = new Uint8Array(24);
      hdr.set(new TextEncoder().encode('SEGGER RTT'), 0);
      const dv = new DataView(hdr.buffer);
      dv.setUint32(16, rtt2.maxUp, true); dv.setUint32(20, rtt2.maxDown, true);
      out.set(hdr.subarray(0, Math.min(hdr.length, out.length)), 0);
    }
    return out;
  };
  const rr = await rtt2.readUp(0);
  probe2.readMem = orig2;
  const rdAfter = (await rtt2._entry(rtt2.upBase, 0)).rd;
  ok(!!rr.corrupt && rr.bytes.length === 0, '把整个控制块读回来的错位读仍然判 corrupt');
  ok(rdAfter === e2.rd, `而且 RdOff 没被推进（rd 仍为 ${rdAfter}，下一轮原样重读）`);
}

console.log('== 4. 过载/丢包的可观测信号 ==');
{
  // (a) 缓冲读满 = 最可靠的过载信号
  const p1 = new MockProbe();
  const r1 = new Rtt(p1, { addr: p1.cbAddr });
  await r1.init();
  await r1.readUp(0);                                        // 空读，建立基线
  p1._pushUp('S'.repeat(p1.upSize - 1));                     // 灌满（环形缓冲最多装 size-1）
  const a = await r1.readUp(0);
  ok(a.full === true, `读到整缓冲（${a.bytes.length} 字节）→ full 信号为真`);
  ok(a.high === true && a.level > 0.99, `水位 level=${a.level.toFixed(3)} 也判为高位`);
  ok(r1.fullCount(0) === 1 && r1.highCount(0) === 1, '读满/高位次数累计正确');

  // (a2) 水位不到 3/4 时不算高位（别乱报警）
  const p1b = new MockProbe();
  const r1b = new Rtt(p1b, { addr: p1b.cbAddr });
  await r1b.init();
  await r1b.readUp(0);
  p1b._pushUp('Z'.repeat(Math.floor(p1b.upSize / 4)));       // 约 1/4 满
  const b1 = await r1b.readUp(0);
  ok(b1.high === false && b1.level < 0.3, `1/4 水位不报警（level=${b1.level.toFixed(2)}）`);

  // (b) 主机侧 RdOff 落后（上次推进失败）时，能算出精确差值
  const p2 = new MockProbe();
  const r2 = new Rtt(p2, { addr: p2.cbAddr });
  await r2.init();
  p2._pushUp('A'.repeat(600));
  await r2.readUp(0);                                        // 排空到 600（lastWr=600）
  p2._setU32(p2.cbAddr + 40, 200);                           // 模拟 RdOff 被回拨：落后 d=400
  p2._pushUp('B'.repeat(700), true);                         // 600+700 绕圈 → 覆盖未读数据
  const b = await r2.readUp(0);
  ok(b.lost === p2.upSize - 400, `差值 = size-d = ${p2.upSize - 400}（实际 ${b.lost}）`);
  ok(r2.totalLost(0) === b.lost, '累计丢失统计正确');

  // (c) SKIP 模式：目标自己丢弃，主机侧看不见（RTT 固有，如实记录，不当 bug）
  const p3 = new MockProbe();
  const r3 = new Rtt(p3, { addr: p3.cbAddr });
  await r3.init();
  await r3.readUp(0);
  for (let i = 0; i < 3; i++) p3._pushUp('C'.repeat(400));
  const c = await r3.readUp(0);
  ok(p3.dropped > 0, `SKIP 模式目标丢了 ${p3.dropped} 字节`);
  ok(c.lost === 0 && c.full === false, 'SKIP 模式两个信号都不亮（预期行为）');
}

console.log('== 5. 下行写入（含非 4 字节对齐）==');
{
  const probe = new MockProbe();
  const rtt = new Rtt(probe, { addr: probe.cbAddr });
  await rtt.init();
  const n1 = await rtt.writeDown(0, new TextEncoder().encode('help\r'));
  ok(n1 === 5, `写入 5 字节（实际 ${n1}）`);
  ok(probe._readDown() === 'help', '目标侧读到了 hello 之外的原样命令');

  // 非对齐起点的写入：先写 3 字节，再写 3 字节，目标侧读到的应是 6 字节
  await rtt.writeDown(0, new TextEncoder().encode('abc'));
  await rtt.writeDown(0, new TextEncoder().encode('def'));
  ok(probe._readDown() === 'abcdef', '连续非对齐写入拼接正确');

  // 写满就写不进去，且返回值如实反映
  const big = new Uint8Array(probe.downSize * 2).fill(0x41);
  const n2 = await rtt.writeDown(0, big);
  ok(n2 > 0 && n2 < big.length, `缓冲写满时只写进 ${n2}/${big.length} 字节（不假装成功）`);
}

console.log('== 6. ELF 符号解析 ==');
{
  const elf = process.env.ELF || 'E:\\esp-idf-s31\\projects\\rtt_nano_s31\\build\\app.elf';
  if (!existsSync(elf)){
    console.log(`  SKIP  找不到 ${elf}（用 ELF=路径 指定）`);
  } else {
    const buf = readFileSync(elf);
    const sym = findSymbol(buf, '_SEGGER_RTT');
    ok(!!sym, `找到 _SEGGER_RTT`, JSON.stringify(sym));
    if (sym){
      ok(sym.addr >= 0x2f000000 && sym.addr < 0x2f080000, `地址落在 S31 片内 RAM（0x${sym.addr.toString(16)}）`);
      console.log(`        期望值对照：_SEGGER_RTT = 0x${sym.addr.toString(16)} size=${sym.size}`);
    }
    ok(findSymbol(buf, '这个符号不存在') === null, '查不存在的符号返回 null');
  }
}

console.log('== 7. HEX 解析（分隔符 = 字节边界）==');
{
  ok(bytesToHex(parseHex('01 03 0A').bytes) === '01 03 0A', '空格分隔');
  ok(bytesToHex(parseHex('01030a').bytes) === '01 03 0A', '连写 6 位数字自动两两分组');
  ok(bytesToHex(parseHex('0x01,0x03').bytes) === '01 03', '0x 前缀 + 逗号');
  ok(parseHex('01 0G').error !== null, '非法字符报错');
  ok(parseHex('01030').error !== null, '连续串奇数位报错');
  ok(bytesToHex(parseHex('01-03').bytes) === '01 03', '连字符分隔');
  // 🚨 2026-10 代码审查：老实现用 pending 把上一段的零头带到下一段，写了一位数字的 token
  //    会跟后一个**拼成一个字节** —— 用户明明写了分隔符，发出去的却是别的东西。
  ok(bytesToHex(parseHex('0x1,0x2').bytes) === '01 02', '「0x1,0x2」是两个字节 01 02（不是 0x12）');
  ok(bytesToHex(parseHex('1 2').bytes) === '01 02', '「1 2」是两个字节 01 02');
  ok(bytesToHex(parseHex('A 5 F 0').bytes) === '0A 05 0F 00', '「A 5 F 0」是四个字节 0A 05 0F 00');
  ok(bytesToHex(parseHex('0x01 0x2 0x03').bytes) === '01 02 03', '「0x01 0x2 0x03」= 01 02 03（老代码报奇数位）');
  ok(bytesToHex(parseHex('01 3').bytes) === '01 03', '「01 3」= 01 03（一位数字当一位）');
  ok(bytesToHex(parseHex('0x0103').bytes) === '01 03', '带 0x 的连续串仍然两位一组');
}

console.log('== 8. 内存访问锁：外部并发调用必须排队（审查：可重入快路径让互斥失效）==');
{
  // WebUSB 那条路有两条并发来源：RTT 轮询循环 + 用户下行发送（还有看门狗/复位）。
  // 老代码 `if (this._locked) return await fn();` 让**外部**并发调用直接插进锁内 ——
  // TAR / AP 挂起读流水线 / posted 写照样交错，正是"命令写丢（页面发送成功、固件没收到）"的成因。
  const { WebUsbDapProbe } = await import('file://' + join(app, 'rtt', 'dap-webusb.js').replace(/\\/g, '/'));
  const p = new WebUsbDapProbe();
  const order = [];
  let releaseA = null;
  const gateA = new Promise(r => { releaseA = r; });
  const a = p._withLock(async () => { order.push('A-start'); await gateA; order.push('A-end'); return 'A'; });
  await new Promise(r => setTimeout(r, 20));
  ok(p._locked === true, 'A 持锁期间 _locked = true');
  const b = p._withLock(async () => { order.push('B-start'); await new Promise(r => setTimeout(r, 5)); order.push('B-end'); return 'B'; });
  await new Promise(r => setTimeout(r, 20));
  releaseA();
  const [ra, rb] = await Promise.all([a, b]);
  ok(order.join(',') === 'A-start,A-end,B-start,B-end',
     `并发调用排队而不交错（实际 ${order.join(',')}）`);
  ok(ra === 'A' && rb === 'B', '两条都拿到自己的返回值');
  ok(p._locked === false, '结束后锁已释放');

  // 写路径内部的"回读校验"必须走私有版本，否则会在锁里再抢锁 → 自锁死
  const calls = [];
  p._readMemLocked = async (addr, len) => { calls.push({ addr, len }); return new Uint8Array(len).fill(0xaa); };
  p._writeMemOnce = async () => { calls.push({ write: true }); };
  p.fast = false; p.verifyWrites = true;
  const done = await Promise.race([
    p.writeMem(0x20000000, new Uint8Array([0, 1, 2, 3])).then(() => 'ok'),
    new Promise(r => setTimeout(() => r('DEADLOCK'), 1500)),
  ]);
  ok(done === 'ok', `写路径里的回读校验没自锁死（${done}）`);
  ok(calls.some(c => c.write) && calls.some(c => c.len === 4), '写后确实回读校验了（回读走了私有版本）');
}

console.log(`\n结果：${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
