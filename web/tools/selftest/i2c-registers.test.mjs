/**
 * 纯 Node 自测（不需要浏览器、不需要硬件）：
 *   node tools/selftest/i2c-registers.test.mjs
 *
 * 覆盖「USB→I2C」页新加的**寄存器面板**（读一段 → 逐位改 → 写回）与它脚下的两块地层：
 *   A. `protocol.planWrite` —— 长写分片（与 planRead 对称，但每片必须自带子地址）
 *   B. `registers.js`       —— 输入解析 / 改动 diff / ASCII / bit 操作
 *   C. 会话层端到端（**假探针**）：readLong 128 B → 改位 → writeLong「只写改动」→ 回读逐字节对账
 *   D. **EEPROM 页写回卷**这条真实世界的坑：不按页分片会把数据写花、不等 tWR 第 2 片必失败
 *   E. **页对齐**（2026-10 代码审查抓到）：只给 `chunkMax` 挡不住"起始地址不是页倍数"，
 *      必须给 `pageSize` 让每一片落在同一页内 —— 正反两面都钉住（给了对、不给花）
 *
 * 这几条都是"肉眼看不出来"的：分片边界差一个字节、地址 bump 少加一、页写回卷 —— 表里都是
 * 一堆十六进制，看着都挺像对的。
 */
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const app = join(here, '..', '..', 'app');
const url = p => 'file://' + join(app, p).replace(/\\/g, '/');

const P = await import(url('i2c/protocol.js'));
const R = await import(url('i2c/registers.js'));
const { I2cSession } = await import(url('i2c/session.js'));

let pass = 0, fail = 0;
const ok = (cond, name, extra = '') => {
  if (cond){ pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name} ${extra}`); }
};
const eq = (a, b, name) => ok(a === b, name, `（得到 ${JSON.stringify(a)}，期望 ${JSON.stringify(b)}）`);
const eqArr = (a, b, name) => ok(a.length === b.length && a.every((v, i) => v === b[i]), name,
  `（得到 [${a}]，期望 [${b}]）`);
const throws = (fn, name) => {
  try { fn(); fail++; console.log(`  FAIL  ${name} （没有抛错）`); }
  catch { pass++; console.log(`  PASS  ${name}`); }
};
const hex = a => Array.from(a || []).map(v => v.toString(16).padStart(2, '0')).join(' ');

// ==================================================================== A
console.log('== A. planWrite：长写分片（每片自带子地址）==');
{
  const data = Uint8Array.from({ length: 128 }, (_, i) => i & 0xff);
  const plan = P.planWrite([0x00], data);
  eq(plan.chunks, 3, '128 B / 51 → 3 片');
  eq(plan.bytes, 128, 'bytes 记账 = 128');
  eqArr(plan.cmds.map(c => c.wr.length), [51, 51, 26], '每片长度 51/51/26');
  eqArr(plan.cmds.map(c => c.addr[0]), [0x00, 0x33, 0x66], '第二、三片的子地址被 bump 到 0x33 / 0x66');
  eq(plan.cmds[1].wr[0], 51, '第二片首字节 = 数据里的第 51 个');
  ok(plan.cmds.every(c => c.wr.length <= P.WR_MAX), '没有一片超过协议上限 51');
}
{
  const data = Uint8Array.from({ length: 16 }, (_, i) => 0xa0 + i);
  const plan = P.planWrite([0x00], data, { chunkMax: 8 });
  eq(plan.chunks, 2, 'chunkMax=8 → 16 B 分 2 片（页写档位）');
  eqArr(plan.cmds.map(c => c.addr[0]), [0x00, 0x08], '按页分片的子地址 0x00 / 0x08');
}
{
  const data = Uint8Array.from([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
  const plan = P.planWrite([0x10], data, { offsets: [0, 1, 2, 6, 9] });
  eq(plan.chunks, 3, '稀疏改动 → 3 片（连续段各成一片）');
  eqArr(plan.cmds.map(c => c.off), [0, 6, 9], '每片记着起始偏移');
  eqArr(plan.cmds[0].wr, [1, 2, 3], '第一片 = 偏移 0..2 的值');
  eqArr(plan.cmds[1].addr, [0x16], '第二片的子地址 = 0x10 + 6');
}
{
  const data = Uint8Array.from({ length: 4 }, (_, i) => i);
  eq(P.planWrite([0x00], data, { offsets: [] }).chunks, 0, 'offsets=[] → 一片都不发');
  eq(P.planWrite([0x00], new Uint8Array(0)).chunks, 0, '空数据 → 一片都不发');
  eq(P.planWrite([0x00], data, { offsets: [0, 99, -1, 3] }).bytes, 2, '越界/非法下标被丢掉，只算合法的那两个');
  eq(P.planWrite([0x00], data, { offsets: [0, 1, 2, 3], chunkMax: 999 }).chunks, 1,
    'chunkMax 给大了会被夹回协议上限 51（4 B 一片）');
}
{
  const start = [0x01, 0x00];                                  // 2 字节子地址（大端）
  const data = Uint8Array.from({ length: 64 }, (_, i) => i);
  const plan = P.planWrite(start, data, { offsets: [0, 60], chunkMax: 4 });
  eqArr(plan.cmds[0].addr, [0x01, 0x00], '2 字节地址：首片 0x0100');
  eqArr(plan.cmds[1].addr, [0x01, 0x3C], '2 字节地址 + 偏移 60 → 0x013C（跨字节进位对）');
}

// ==================================================================== B
console.log('== B. registers.js：输入解析 / diff / ASCII / bit ==');
{
  eq(R.parseDev('0x50'), 0x50, "parseDev('0x50')");
  eq(R.parseDev('50'), 0x50, "parseDev('50') 也认（不带 0x）");
  eq(R.parseDev(' 0x08 '), 0x08, '首尾空格无所谓');
  throws(() => R.parseDev('0x07'), 'parseDev 拒 0x07（低于扫描范围）');
  throws(() => R.parseDev('0x78'), 'parseDev 拒 0x78（高于 7 位可用范围）');
  throws(() => R.parseDev(''), 'parseDev 拒空');
  throws(() => R.parseDev('zz'), 'parseDev 拒非十六进制');
}
{
  eqArr(R.parseStart('0x00', 1), [0x00], '1 B 地址：0x00');
  eqArr(R.parseStart('', 1), [0x00], '起始留空 = 0x00（读整个器件）');
  eqArr(R.parseStart('0x0100', 2), [0x01, 0x00], '2 B 地址：大端 0x0100 → 01 00');
  eqArr(R.parseStart('0x10', 0), [], '地址宽度 0 → 不带子地址（纯读）');
  throws(() => R.parseStart('0x1234', 1), '1 B 地址格塞 0x1234 → 报错（提示改成 2 B）');
  throws(() => R.parseStart('0x12345', 2), '2 B 地址格塞 0x12345 → 报错');
}
{
  eq(R.parseLen(''), R.REG_LEN_DFT, '长度留空 = 128');
  eq(R.parseLen('128'), 128, "parseLen('128')");
  eq(R.parseLen('0x10'), 16, "parseLen('0x10') = 16（也吃十六进制）");
  throws(() => R.parseLen('0'), 'parseLen 拒 0');
  throws(() => R.parseLen('4097'), 'parseLen 拒 > 4096');
  throws(() => R.parseLen('4.5'), 'parseLen 拒小数');
}
{
  const base = Uint8Array.from([0x00, 0x11, 0x22, 0x33]);
  const cur = Uint8Array.from([0x00, 0x91, 0x22, 0x30]);
  const d = R.diffBytes(base, cur);
  eq(d.length, 2, 'diff 找到 2 处改动');
  eqArr(d.map(x => x.off), [1, 3], 'diff 的偏移是 1 / 3');
  eqArr([d[0].from, d[0].to], [0x11, 0x91], 'diff 记着 原值→新值');
  eqArr(R.changedOffsets(base, base), [], '没改 → 没有偏移（写回会被拦下）');
}
{
  eq(R.asciiOf(Uint8Array.from([0x48, 0x69, 0x00, 0x1f, 0x7e, 0x7f])), 'Hi..~.',
    'ASCII 列：可打印原样，其余一个点（0x00 与 0x1f 各一个点）');
  eq(R.asciiOf(Uint8Array.from([0x41, 0x42, 0x43]), 1, 1), 'B', 'ASCII 支持 from/len 切片');
}
{
  eq(R.addrLabel([0x00], 0, 1), '0x00', '1 B 地址：偏移 0 → 0x00');
  eq(R.addrLabel([0x00], 0x2a, 1), '0x2A', '1 B 地址：偏移 42 → 0x2A（大写十六进制）');
  eq(R.addrLabel([0x01, 0x00], 0x3c, 2), '0x013C', '2 B 地址：0x0100 + 60 → 0x013C');
  eq(R.addrLabel([0x00, 0xf0], 0x20, 2), '0x0110', '2 B 地址跨字节进位也对');
  eq(R.addrLabel([0x00], 5, 0), '+5', '地址宽度 0（纯读）→ 只标偏移');
}
{
  eqArr(R.bitsOf(0x81), [1, 0, 0, 0, 0, 0, 0, 1], 'bitsOf(0x81)：bit0 与 bit7 为 1');
  eq(R.setBit(0x00, 3, true), 0x08, 'setBit 置位');
  eq(R.setBit(0xff, 3, false), 0xf7, 'setBit 清位');
  eq(R.toggleBit(0x0f, 4), 0x1f, 'toggleBit 翻转');
  eq(R.popcount(Uint8Array.from([0xff, 0x00, 0x81])), 10, 'popcount = 8 + 0 + 2');
  eq(R.setBit(R.toggleBit(0xaa, 0), 0), 0xaa, '翻转再翻回来 = 原值');
}
{
  ok(R.summarize({ len: 128, dev: 0x50, start: [0], addrLen: 1 }).includes('还没读'), '摘要：没读过时说清计划');
  const sum = R.summarize({ len: 2, base: Uint8Array.from([0, 1]), cur: Uint8Array.from([0, 3]),
                            dev: 0x50, start: [0], addrLen: 1 });
  ok(sum.includes('改了 1 个字节'), '摘要：报改动个数', sum);
}

// ==================================================================== C
console.log('== C. 端到端（假探针）：读 128 B → 改位 → 只写改动 → 回读对账 ==');
{
  const s = new I2cSession();
  ok(await s.connect(false, { mock: true }), 'connect(mock) 成功');
  await s.setEnabled(true);

  // Si5351 是纯寄存器器件（0x60，256 B 寄存器空间、无页写回卷）—— 先种一段有规律的数据
  const seed = Uint8Array.from({ length: 128 }, (_, i) => (i * 7 + 3) & 0xff);
  const wSeed = await s.writeLong({ dev: 0x60, addr: [0x00], data: seed }, { quiet: true });
  eq(wSeed.err, P.E.OK, '种数据成功（128 B 分片写）');

  const rd = await s.readLong({ dev: 0x60, addr: [0x00], rd: 128 }, { quiet: true });
  eq(rd.err, P.E.OK, '读 128 B 成功');
  eq(rd.data.length, 128, '读回 128 B');
  eq(rd.chunks, 3, '128 B 分了 3 笔（54/54/20）');
  ok(hex(rd.data) === hex(seed), '读回的数据与种下去的一致');

  // 模拟面板里的操作：改 3 个 bit（分散在不同字节）
  const base = rd.data.slice();
  const cur = base.slice();
  cur[0] = R.setBit(cur[0], 7, true);
  cur[5] = R.toggleBit(cur[5], 0);
  cur[127] = 0x00;
  const offs = R.changedOffsets(base, cur);
  eqArr(offs, [0, 5, 127], '面板 diff 出的改动下标 = 0 / 5 / 127');

  const w = await s.writeLong({ dev: 0x60, addr: [0x00], data: cur, offsets: offs }, { quiet: true });
  eq(w.err, P.E.OK, '「只写改动」成功');
  eq(w.bytes, 3, '只发了 3 个字节');
  eq(w.chunks, 3, '3 个分散字节 → 3 笔事务');

  const rd2 = await s.readLong({ dev: 0x60, addr: [0x00], rd: 128 }, { quiet: true });
  ok(hex(rd2.data) === hex(cur), '回读与改后的缓冲逐字节一致（没改的字节也没被动过）');
  ok(hex(rd2.data.subarray(1, 5)) === hex(seed.subarray(1, 5)), '中间没改的字节保持原样');

  // 「整块写回」= offsets null：128 B 分 3 片
  const w2 = await s.writeLong({ dev: 0x60, addr: [0x00], data: cur }, { quiet: true });
  eq(w2.chunks, 3, '整块写回 128 B → 3 片');
  const rd3 = await s.readLong({ dev: 0x60, addr: [0x00], rd: 128 }, { quiet: true });
  ok(hex(rd3.data) === hex(cur), '整块写回后回读依旧一致');

  // 2 字节子地址的器件（EEPROM 之外的常见排法）：地址 bump 要走子地址而不是数据
  const s2 = new I2cSession();
  await s2.connect(false, { mock: true });
  await s2.setEnabled(true);
  const w3 = await s2.writeLong({ dev: 0x50, addr: [0x00, 0x40], data: Uint8Array.from([1, 2, 3]) }, { quiet: true });
  eq(w3.err, P.E.OK, '2 字节子地址也能写（假 EEPROM 只取低字节，但不该报错）');
}

// ==================================================================== D
console.log('== D. EEPROM 页写回卷 + tWR（这面板为什么要给「写分片 / 片间等待」）==');
{
  const s = new I2cSession();
  await s.connect(false, { mock: true });
  await s.setEnabled(true);
  const payload = Uint8Array.from({ length: 16 }, (_, i) => 0xb0 + i);

  // ① 按页（8 B）+ 片间等 tWR → 数据完好
  const okWrite = await s.writeLong({ dev: 0x50, addr: [0x00], data: payload, chunkMax: 8, gapMs: 6 }, { quiet: true });
  eq(okWrite.err, P.E.OK, '按页写 + 等 tWR：两片都成功');
  await new Promise(r => setTimeout(r, 8));
  const back1 = await s.readLong({ dev: 0x50, addr: [0x00], rd: 16 }, { quiet: true });
  ok(hex(back1.data) === hex(payload), '按页写回读一致（这就是面板「写分片 8 B + 片间等待 6 ms」的用法）');

  // ② 不按页（一片 16 B 跨两页）→ 器件内部回卷，后半段盖掉前半段
  const bad = await s.writeLong({ dev: 0x50, addr: [0x00], data: payload, chunkMax: 16, gapMs: 0 }, { quiet: true });
  eq(bad.err, P.E.OK, '一片 16 B 跨页：桥侧看着"写成功"（器件 ACK 了）');
  await new Promise(r => setTimeout(r, 8));
  const back2 = await s.readLong({ dev: 0x50, addr: [0x00], rd: 16 }, { quiet: true });
  ok(hex(back2.data) !== hex(payload), '但回读**与写入不一致** —— 页写回卷真的会把数据写花');

  // ③ 不等 tWR 连发两片 → 第 2 片被器件 NACK（假 EEPROM 如实建模）
  const noGap = await s.writeLong({ dev: 0x50, addr: [0x00], data: payload, chunkMax: 8, gapMs: 0 }, { quiet: true });
  ok(noGap.err !== P.E.OK, '不等 tWR 连发：第 2 片失败并被如实报出来', `（err=${noGap.err}）`);
}

// ==================================================================== E
console.log('== E. 页对齐（只设 chunkMax 挡不住"起始地址不是页倍数"）==');
{
  // ① 纯逻辑：任何一片都不许跨页
  const b = n => Uint8Array.from({ length: n }, (_, i) => i);
  const noCross = (plan, base, page) => plan.cmds.every(c =>
    (P.addrNum(P.bump(base, c.off)) % page) + c.wr.length <= page);

  const plan1 = P.planWrite([0x05], b(16), { pageSize: 8, chunkMax: 8 });
  ok(noCross(plan1, [0x05], 8), '从 0x05 起按页 8 分片：没有一片跨页');
  eq(plan1.chunks, 3, '从 0x05 起 16 B 分 3 片');
  eqArr(plan1.cmds.map(c => c.off), [0, 3, 11], '分片起点跟着页边界走（0x05..07 / 08..0F / 10..14）');

  const plan2 = P.planWrite([0x00], b(16), { offsets: [6, 7, 8, 9], pageSize: 8 });
  eqArr(plan2.cmds.map(c => c.off), [6, 8], '只改 +6..+9 且跨页：拆成 +6..+7 与 +8..+9');

  const plan3 = P.planWrite([0x01, 0x05], b(64), { pageSize: 32, addrLen: 2 });
  ok(noCross(plan3, [0x01, 0x05], 32), '2 字节地址 0x0105、页 32：没有一片跨页');
  eq(plan3.cmds[0].wr.length, 27, '第一片收窄到 27 B（0x105 在页内偏移 5）');

  eq(P.planWrite([0x05], b(16), {}).chunks, 1, 'pageSize=0（寄存器型器件）：行为与以前一致，不切');

  // ② 端到端（假 AT24C02，页 8）：审查里的两个场景现在必须逐字节对上
  const s = new I2cSession();
  await s.connect(false, { mock: true });
  await s.setEnabled(true);
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const zero = new Uint8Array(16);

  await s.writeLong({ dev: 0x50, addr: [0x00], data: zero, chunkMax: 8, pageSize: 8, gapMs: 6 }, { quiet: true });
  await wait(8);
  const payA = Uint8Array.from({ length: 16 }, (_, i) => 0xa0 + i);
  const wa = await s.writeLong({ dev: 0x50, addr: [0x05], data: payA, chunkMax: 8, pageSize: 8, gapMs: 6 }, { quiet: true });
  eq(wa.err, P.E.OK, '场景 A：从 0x05 写 16 B 提交成功');
  await wait(8);
  const ra = await s.readLong({ dev: 0x50, addr: [0x05], rd: 16 }, { quiet: true });
  ok(hex(ra.data) === hex(payA), '场景 A：回读逐字节一致（审查里这里是花的）', `（回读 ${hex(ra.data)}）`);

  await s.writeLong({ dev: 0x50, addr: [0x00], data: zero, chunkMax: 8, pageSize: 8, gapMs: 6 }, { quiet: true });
  await wait(8);
  const cur = new Uint8Array(16); cur.set([0x11, 0x22, 0x33, 0x44], 6);
  await s.writeLong({ dev: 0x50, addr: [0x00], data: cur, offsets: [6, 7, 8, 9], chunkMax: 8, pageSize: 8, gapMs: 6 },
    { quiet: true });
  await wait(8);
  const rb = await s.readLong({ dev: 0x50, addr: [0x00], rd: 16 }, { quiet: true });
  ok(hex(rb.data) === hex(cur), '场景 B：只改 +6..+9（跨页）回读逐字节一致', `（回读 ${hex(rb.data)}）`);

  // ③ 反证：不给 pageSize 时**仍然会**写花 —— 证明这一格是真防线，也是"写后回读"存在的理由
  await s.writeLong({ dev: 0x50, addr: [0x00], data: zero, chunkMax: 8, gapMs: 6 }, { quiet: true });
  await wait(8);
  const bad = await s.writeLong({ dev: 0x50, addr: [0x05], data: payA, chunkMax: 8, gapMs: 6 }, { quiet: true });
  eq(bad.err, P.E.OK, '反证：不给页大小时器件照样回 OK（"写成功"是假的）');
  await wait(8);
  const rc = await s.readLong({ dev: 0x50, addr: [0x05], rd: 16 }, { quiet: true });
  ok(hex(rc.data) !== hex(payA), '反证：不给页大小时数据确实是花的', `（回读 ${hex(rc.data)}）`);
}

console.log(`\n${fail ? 'FAIL' : 'OK'}  ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
