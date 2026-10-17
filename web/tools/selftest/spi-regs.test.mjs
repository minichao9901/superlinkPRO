/**
 * 纯 Node 自测（不需要浏览器、不需要硬件）：
 *   node tools/selftest/spi-regs.test.mjs
 *
 * 覆盖 SPI/QSPI 桥页新加的两块：
 *   A. `app/spi/regs.js` —— 器件档位（读/写 opcode、地址并入 opcode 还是独立字节、dummy、
 *      数据宽度、自增）→ 读/写帧计划；含**命令型 ADC**（MCP3008 风格）那条路
 *   B. 假器件（`mock.js` 的 `SpiRegDevice` / `SpiCmdAdc`）配合下的**端到端**：
 *      读 128 寄存器 → 改字节 → 只写改动 → 回读逐字节对账；dummy 给错 → 数据整体错位；
 *      不自增的器件连读会"原地踏步"
 *   C. `app/spi/runner.js` —— loop/定时采集：`as` 解码出变量、拍数在涨、丢拍计数、停止即停
 *   D. DSL 的定时采集语法（loop/every/once/行尾 as/every 与 `cmd=0x80|0x3B` 这种位运算表达式）
 *
 * 为什么这些值得单独钉：SPI 的寄存器约定**每个器件都不一样**，而"差一个 dummy 字节"
 * 在表里看起来只是"数据有点怪" —— 只有拿已知内容的假器件逐字节对账才能把它钉死。
 */
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const app = join(here, '..', '..', 'app');
const url = p => 'file://' + join(app, p).replace(/\\/g, '/');

const P = await import(url('spi/protocol.js'));
const G = await import(url('spi/regs.js'));
const D = await import(url('spi/frames-dsl.js'));
const { SpiSession } = await import(url('spi/session.js'));
const { MockSpiProbe, SpiRegDevice, SpiCmdAdc } = await import(url('spi/mock.js'));
const { SpiRunner } = await import(url('spi/runner.js'));
const X = await import(url('core/expr.js'));

let pass = 0, fail = 0;
const ok = (cond, name, extra = '') => {
  if (cond){ pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name} ${extra}`); }
};
const eq = (a, b, name) => ok(a === b, name, `（得到 ${JSON.stringify(a)}，期望 ${JSON.stringify(b)}）`);
const eqArr = (a, b, name) => ok(Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => v === b[i]),
  name, `（得到 [${a}]，期望 [${b}]）`);
const hex = a => Array.from(a || []).map(v => v.toString(16).padStart(2, '0')).join(' ');
const sleep = ms => new Promise(r => setTimeout(r, ms));

// ==================================================================== A
console.log('== A. 档位表与帧构造（regs.js）==');
{
  const p = G.profileById('bmp280');
  eq(p.addrMode, 'orOp', 'BMP280 档：地址并入 opcode');
  /**
   * 🚨 这条是 2026-10-03 对完手册改过来的：**BMP280 读不需要 dummy**。
   * 手册 §5.3.2 Figure 11 "*SPI multiple byte read*" 里控制字节之后数据立刻出来；
   * 要 dummy 的是 BMP380/BMP580（Linux `bmp280-spi.c` 里只有它们 `.spi_read_extra_byte = true`）。
   * 网上"BMP280 要 1 个 dummy"是把 BMP180/BMP380 的规矩抄串了。
   */
  eq(p.dummy, 0, 'BMP280 档：读**不要** dummy（网传"要 1 字节"是错的）');
  eq(p.autoIncWrite, false, 'BMP280 档：**写不自增**（手册 Figure 10 标题写明 not auto-incremented）');
  eq(G.readOpcode(p, 0xD0), 0xD0, '读 0xD0（ID 寄存器）→ opcode 0xD0 = 0x80|0x50');
  eq(G.writeOpcode(p, 0xF4), 0x74, '写 0xF4 → opcode 0x74 = 0xF4 & 0x7F');
  const plan = G.readPlan(p, 0x00, 128);
  eq(plan.frames.length, 1, '128 个寄存器（1 B/寄存器）→ 一条命令连读');
  eq(plan.frames[0].rx, 128, '这条帧读 128 B');
  eq(plan.frames[0].dummy, 0, '……且不带 dummy');
  eq(plan.frames[0].addrLen, 0, '……地址不占独立字节（在 opcode 里）');

  // ADXL345：多字节读必须置 MB（bit6）→ 0xC0|reg；单寄存器读 0x80|reg
  const adxl = G.profileById('adxl345');
  eq(adxl.addrBits, 6, 'ADXL345 档：地址只占 opcode 低 6 位（bit7=读、bit6=多字节）');
  eq(adxl.dummy, 0, 'ADXL345 档：也不要 dummy（它对的是 Mode 3，别忘改左栏模式）');
  eq(G.readOpcode(adxl, 0x32), 0xB2, '单寄存器读 0x32 → 0xB2 = 0x80|0x32');
  const a1 = G.readPlan(adxl, 0x32, 1).frames[0];
  eq(a1.cmd, 0xB2, '读 1 个寄存器：不带 MB');
  const a6 = G.readPlan(adxl, 0x32, 6).frames[0];
  eq(a6.cmd, 0xF2, '读 6 个寄存器（三轴）：**自动置 MB** → 0xF2 = 0xC0|0x32');
  eq(a6.rx, 6, '……一次读 6 B');

  const mcp = G.profileById('mcp23s17');
  eq(mcp.addrMode, 'bytes', 'MCP23S17 档：地址是独立字节');
  const mp = G.readPlan(mcp, 0x12, 2);
  eq(mp.frames[0].addrLen, 1, '……地址相位 1 字节');
  eq(mp.frames[0].cmd, 0x41, '……读 opcode = 0x41');
  eq(mp.frames[0].addr, 0x12, '……地址字段 = 寄存器号');
  eq(mp.frames[0].rx, 2, '读 2 个寄存器 = 2 B');

  // ADXL345 的 MB 位在上一段（A 节开头）已单独断言过，这里不再重复

  // 不自增的器件：一寄存器一帧
  const slow = { ...G.profileById('custom'), autoInc: false, dataBytes: 1 };
  const sp = G.readPlan(slow, 0x10, 3);
  eq(sp.frames.length, 3, '不自增 → 3 个寄存器 3 帧');
  eqArr(sp.frames.map(f => f.cmd), [0x90, 0x91, 0x92], '……每帧的命令字节按寄存器号递增');

  // 16 位寄存器：一帧读多少、按 2 字节对齐
  const w16 = { ...G.profileById('custom'), dataBytes: 2, dummy: 0 };
  const p16 = G.readPlan(w16, 0x00, 8);
  eq(p16.frames[0].rx, 16, '8 个 16 位寄存器 = 16 B');

  // 单帧读上限：480 B（RSP 满包问题）→ 会自动多帧
  const big = G.readPlan({ ...G.profileById('custom'), dataBytes: 1 }, 0, 600);
  eq(big.frames.length, 2, '600 B 超过单帧 480 B 上限 → 分 2 帧');
  ok(big.frames.every(f => f.rx <= G.RX_PER_FRAME_MAX), '……每帧都不超过单帧读上限');
  eq(big.frames[1].cmd, 0x80 | (480 & 0x7f), '……第二帧按新的起始寄存器重发命令');
}
{
  const mcp = G.profileById('mcp3008');
  eq(mcp.kind, G.KIND.CMD, 'MCP3008 是命令型（没有寄存器空间）');
  const plan = G.readPlan(mcp, 3, 1);
  eq(plan.frames.length, 1, '读通道 3 = 一条命令');
  eqArr([...plan.frames[0].tx], [0x01, 0xB0, 0x00], '命令字节：0x01 + (0x80|3<<4) + 0x00');
  eq(plan.frames[0].rx, 3, '……回 3 B');
  const item = G.itemsOf(plan.frames)[0];
  const dv = new DataView(item.payload.buffer, item.payload.byteOffset, item.payload.byteLength);
  eq(dv.getUint16(4, true), 3, '……tx 长度 3（收发等长，全双工要求）');
  ok(!(item.payload[1] & P.TC.CMD_EN), '……命令型不带 cmd 相位（整条命令都在 tx 里）');
}
{
  const p = G.profileById('bmp280');
  const data = Uint8Array.from({ length: 16 }, (_, i) => 0xa0 + i);
  const w = G.writePlan(p, 0x10, data, { offsets: [0, 1, 8, 15] });
  /**
   * BMP280 **写不自增**：一帧里的多个字节会全落到同一个寄存器（只有最后一个生效），
   * 所以相邻的两处改动（偏移 0、1）也必须**分成两帧**（各自带自己的控制字节）。
   */
  eq(w.frames.length, 4, '写不自增 → 4 处改动 = 4 帧（0/1 不再合并）');
  eqArr(w.frames.map(f => f.cmd), [0x10, 0x11, 0x18, 0x1F], '……每帧命令按各自寄存器号（写 = reg&0x7F）');
  ok(w.frames.every(f => f.tx.length === 1), '……每帧只带 1 个字节');
  eq(G.writePlan(p, 0x00, data, { offsets: [] }).frames.length, 0, '没有改动 → 一帧都不发');
  const all = G.writePlan(p, 0x00, new Uint8Array(600).fill(0x11), {});
  ok(all.frames.length >= 2 && all.frames.every(f => f.tx.length <= G.TX_PER_FRAME_MAX),
    '整块写 600 B 会按单帧 tx 上限切开');

  // 写自增的器件（MPU 那种）该合并的还是要合并
  const mpu = G.profileById('mpu9250');
  const wm = G.writePlan(mpu, 0x10, data, { offsets: [0, 1, 8] });
  eq(wm.frames.length, 2, '写自增的器件：相邻改动照旧合并（2 帧）');
  eqArr([...wm.frames[0].tx], [0xa0, 0xa1], '……第一帧带 2 个字节');

  // 16 位寄存器 + 写不自增 → 改动要对齐到整寄存器（只改低字节也得连高字节一起发）
  const w16 = { ...G.profileById('custom'), dataBytes: 2, autoIncWrite: false };
  const w16plan = G.writePlan(w16, 0x00, Uint8Array.from([1, 2, 3, 4]), { offsets: [2] });
  ok(w16plan.aligned, '16 位寄存器 + 写不自增 → 改动被补齐成整寄存器');
  eqArr([...w16plan.frames[0].tx], [3, 4], '……这一帧带该寄存器的两个字节');
}
{
  // ADS1256 按手册**无法用"命令+地址+长度"表达**（命令字节打包地址/方向/数量），所以不进档位表
  ok(!G.PROFILES.some(x => x.id === 'ads1256'), '档位表里没有 ADS1256（它的命令是两字节打包，本面板表达不了）');
  ok(G.PROFILES.some(x => x.verified === '驱动旁证'), 'ICM-20602 这种没取到手册的，把握程度如实标成"驱动旁证"');
}

// ==================================================================== B
console.log('== B. 端到端（假寄存器器件）：读 → 改 → 只写改动 → 回读 ==');
{
  const session = new SpiSession();
  await session.setMock(true, { device: 'regs' });
  await session.setEnabled(true);              // 桥没使能时假探针会拒收（真固件一样）
  const probe = session.mockProbe;
  const p = G.profileById('bmp280');
  const regs = probe.regdev.regs;
  for (let i = 0; i < 128; i++) regs[i] = (i * 7 + 1) & 0xff;

  const send = items => session.sendFrames(items, { quiet: true, timeoutMs: 800 });
  const r1 = await send(G.itemsOf(G.readPlan(p, 0, 128).frames));
  eq(r1.rsps.length, 1, '假器件回了一个应答（128 寄存器 = 一条命令）');
  eq(r1.rsps[0].status, P.ST.OK, '……状态 OK');
  const data = r1.rsps[0].data;
  eq(data.length, 128, '读回 128 B');
  ok(hex(data.subarray(0, 8)) === hex(regs.subarray(0, 8)), '读回内容与器件里的图案逐字节一致');
  ok(!probe.flashNotes.length, 'dummy 给对了 → 器件没有报"错位"', JSON.stringify(probe.flashNotes));

  // dummy 给错：数据整体错位（真机症状）——把假器件换成"要 1 字节 dummy"的 BMP380 风格
  probe.flashNotes.length = 0;
  probe.regdev.dummy = 1;
  const r2 = await send(G.itemsOf(G.readPlan(p, 0, 8).frames));
  ok(hex(r2.rsps[0].data) !== hex(regs.subarray(0, 8)), 'dummy 少给 1 字节 → 读回的数据与器件不一致（错位）');
  ok(probe.flashNotes.some(n => /dummy 不符/.test(n)), '……假器件如实报出"dummy 不符"', JSON.stringify(probe.flashNotes));
  probe.regdev.dummy = 0;

  // 写：只写改动的那两个字节
  const base = new Uint8Array(regs.subarray(0, 16));
  const cur = base.slice();
  cur[2] = 0x5A; cur[9] = 0xA5;
  const w = G.writePlan(p, 0, cur, { offsets: [2, 9] });
  const r3 = await send(G.itemsOf(w.frames));
  eq(r3.rsps.filter(x => x && x.status === P.ST.OK).length, w.frames.length, '两帧写都有 OK 应答');
  eq(probe.regdev.regs[2], 0x5A, '改动写进了器件（reg[2]）');
  eq(probe.regdev.regs[9], 0xA5, '改动写进了器件（reg[9]）');
  eq(probe.regdev.regs[3], base[3], '没改的字节没被动过（reg[3]）');

  // 回读对账：读回整段应当与 cur 一致
  const r4 = await send(G.itemsOf(G.readPlan(p, 0, 16).frames));
  ok(hex(r4.rsps[0].data) === hex(cur), '回读 16 B 与写后的缓冲逐字节一致');

  /**
   * 🚨 **写不自增的现场症状**：硬塞一帧两个字节进去（面板不会这么干，但"命令表/脚本"能），
   * 器件会把两个字节都写进同一个寄存器（只有最后一个生效），并留下一条说明。
   */
  probe.flashNotes.length = 0;
  const bad = { ...G.profileById('mpu9250') };                       // 用"写自增"的档位造出 2 字节一帧
  const badPlan = G.writePlan(bad, 0x20, Uint8Array.from([0x11, 0x22]), {});
  await send(G.itemsOf(badPlan.frames));
  eq(badPlan.frames[0].tx.length, 2, '先确认这一帧确实带了 2 个字节');
  const wrProbe = probe.regdev;
  const before = wrProbe.regs[0x21];
  wrProbe.autoIncWrite = false;                                       // 假设它是 BMP280 那种器件
  await send(G.itemsOf(badPlan.frames));
  ok(probe.flashNotes.some(n => /写不自增/.test(n)), '写不自增的器件会明确说明"这一帧只最后一个生效"', JSON.stringify(probe.flashNotes));
  eq(wrProbe.regs[0x21], before, '……相邻寄存器没被写（两个字节都落在 0x20）');
  eq(wrProbe.regs[0x20], 0x22, '……0x20 里留下的是最后一个字节');
  wrProbe.autoIncWrite = true;

  // 不自增的器件：连读会"原地踏步"（每字节都是同一个寄存器）
  probe.regdev.autoInc = false;
  probe.regdev.regs[5] = 0x3C;
  const r5 = await send(G.itemsOf(G.readPlan(p, 5, 4).frames));
  eqArr([...r5.rsps[0].data], [0x3C, 0x3C, 0x3C, 0x3C], '不自增的器件连读 4 B 全是同一个寄存器（真机症状）');
  probe.regdev.autoInc = true;
}
{
  // 命令型 ADC：采样值是活的（正弦），解码表达式能算出量
  const session = new SpiSession();
  await session.setMock(true, { device: 'adc' });
  await session.setEnabled(true);
  const adc = session.mockProbe.adc;
  const p = G.profileById('mcp3008');
  const send = items => session.sendFrames(items, { quiet: true, timeoutMs: 800 });
  const r1 = await send(G.itemsOf(G.readPlan(p, 0, 1).frames));
  eq(r1.rsps[0].data.length, 3, 'ADC 回 3 B');
  const spec = X.parseAs('v=u16be(1)&0x3FF');
  ok(spec.ok, 'as 表达式解析通过（单取字节函数 + 常量掩码）');
  const v1 = X.applyAs(spec, r1.rsps[0].data).values[0].value;
  ok(v1 >= 0 && v1 <= 1023, `解出的码值在 10 位范围内（${v1}）`);
  eq(adc.lastCh, 0, '……通道 0');
  const r3 = await send(G.itemsOf(G.readPlan(p, 3, 1).frames));
  eq(adc.lastCh, 3, '命令里的通道被解析出来（通道 3）');
  ok(hex(r3.rsps[0].data) !== hex(r1.rsps[0].data), '不同通道 / 不同时刻采样值不同（假的也是活的）');
  // 缺起始位 / 差分模式 → 假器件如实拒绝
  session.mockProbe.flashNotes.length = 0;
  const badItem = { type: P.T.XFER, payload: P.xferPayload({ tcfg: 0, tx: Uint8Array.of(0x00, 0x80, 0x00), rxLen: 3 }),
                    flags: P.F.RSP, label: '坏命令' };
  await send([badItem]);
  ok(session.mockProbe.flashNotes.some(n => /起始位/.test(n)), '命令缺起始位 → 假器件报出来', JSON.stringify(session.mockProbe.flashNotes));
}

// ==================================================================== C
console.log('== C. 定时采集（runner.js）：loop + as 解码 + 计数与停止 ==');
{
  const session = new SpiSession();
  await session.setMock(true, { device: 'regs' });
  await session.setEnabled(true);
  ok(session.dataReady, '假探针数据面就绪');
  const probe = session.mockProbe;
  // 铺一段"会动"的数据：第 0x3B 起 6 B（三轴）——用一个慢正弦写进去
  let t = 0;
  probe.regdev.onRead = (reg, len) => {
    if (reg <= 0x3B && reg + len >= 0x41){
      t += 1;
      const ax = Math.round(Math.sin(t / 5) * 8000);
      probe.regdev.regs[0x3B] = (ax >> 8) & 0xff;
      probe.regdev.regs[0x3C] = ax & 0xff;
      probe.regdev.regs[0x3D] = 0x00; probe.regdev.regs[0x3E] = 0x10;
      probe.regdev.regs[0x3F] = 0x00; probe.regdev.regs[0x40] = 0x20;
    }
  };
  const parsed = D.parseFrames(`xfer cmd=0x80|0x75 rx=1 as who=u8(0)
loop 20ms
  xfer cmd=0x80|0x3B rx=6 as ax=i16be(0)/16384, az=i16be(4)/16384
end`);
  eq(parsed.errors.length, 0, 'DSL 解析零错误', JSON.stringify(parsed.errors));
  eq(parsed.stats.timed, 1, '1 条定时帧');
  eq(parsed.stats.oneShots, 1, '1 条一次性帧');
  eqArr(parsed.stats.vars, ['who', 'ax', 'az'], '解码变量名单');

  const events = [];
  const runner = new SpiRunner(session, { onEvent: e => events.push(e) });
  await runner.start(parsed);
  ok(runner.running, '采集跑起来了');
  await sleep(320);
  const ticks = runner.stat.ticks;
  ok(ticks >= 5, `20 ms 周期在 320 ms 里跑了 ${ticks} 拍（≥5）`);
  const values = events.filter(e => e.type === 'values');
  ok(values.length >= 5, '……每拍都广播了变量值');
  const names = new Set(values.flatMap(v => v.values.map(x => x.name)));
  ok(names.has('ax') && names.has('az'), '解出了 ax / az', [...names].join(','));
  const once = events.find(e => e.type === 'once');
  ok(!!once && once.sent === 1, '一次性部分先跑了一遍（WHO_AM_I）');
  const axVals = values.flatMap(v => v.values.filter(x => x.name === 'ax').map(x => x.value));
  ok(new Set(axVals.map(v => v.toFixed(3))).size > 1, 'ax 的值在变（假器件的采样是活的）');
  runner.stop('自测停止');
  const nAfter = runner.stat.ticks;
  await sleep(120);
  eq(runner.stat.ticks, nAfter, '停止之后不再采样');
  ok(!runner.running, 'runner 状态已停');
}
{
  // 轮数到了自己停（loop 20ms 3）
  const session = new SpiSession();
  await session.setMock(true, { device: 'regs' });
  await session.setEnabled(true);
  session.mockProbe.regdev.regs[0x75] = 0x71;
  const parsed = D.parseFrames('loop 15ms 3\n  xfer cmd=0x80|0x75 rx=1 as who=u8(0)\nend');
  const runner = new SpiRunner(session, { onEvent: () => {} });
  await runner.start(parsed);
  await sleep(220);
  ok(!runner.running, '跑满 3 轮后自己停了');
  eq(runner.stat.ticks, 3, '……正好 3 拍');
}

// ==================================================================== D
console.log('== D. DSL 的定时采集语法 ==');
{
  const r1 = D.parseFrames(`once
xfer cmd=0x05 rx=1
every 30ms 2
xfer cmd=0x05 rx=1 as sr=u8(0)`);
  eq(r1.errors.length, 0, 'every 写法零错误');
  eq(r1.stats.oneShots, 1, '……every 之前的那条是一次性');
  eq(r1.stats.timed, 1, '……every 之后的是定时');
  eq(r1.items[1].period, 30, '……周期 30 ms');
  eq(r1.items[1].count, 2, '……轮数 2');
  eq(r1.items[0].period, undefined, '一次性那条没有周期');
}
{
  const r = D.parseFrames('xfer cmd=0x80|0x3B rx=2 as ax=i16be(0) every 25ms 10');
  eq(r.errors.length, 0, '行尾 as + every 一起写也认');
  eq(r.items[0].period, 25, '……行尾 every 覆盖块级周期');
  eq(r.items[0].count, 10, '……行尾轮数');
  eq(r.items[0].as.length, 1, '……as 解析成 1 个变量');
}
{
  const bad = D.parseFrames('loop 20ms\nxfer cmd=0x05 rx=1');
  ok(bad.errors.some(e => /没有对应的 end/.test(e.msg)), 'loop 没闭合 → 报错说清');
  const nested = D.parseFrames('loop 20ms\nloop 30ms\nend\nend');
  ok(nested.errors.some(e => /不能嵌套/.test(e.msg)), 'loop 嵌套 → 明确拒绝');
  const badAs = D.parseFrames('xfer cmd=0x05 rx=1 as =u8(0)');
  ok(badAs.errors.some(e => /as 有问题/.test(e.msg)), 'as 写坏 → 解析期就带行号报错');
  const bits = D.parseNum('0x80|0x3B', true);
  ok(bits.ok && bits.v === 0xBB, 'cmd= 支持位运算表达式（0x80|0x3B = 0xBB）', JSON.stringify(bits));
  const sh = D.parseNum('0x40|(0x2<<1)', true);
  ok(sh.ok && sh.v === 0x44, '……括号与移位也对（0x40|(2<<1) = 0x44）', JSON.stringify(sh));
}

console.log(`\n${fail ? 'FAIL' : 'OK'}  ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
