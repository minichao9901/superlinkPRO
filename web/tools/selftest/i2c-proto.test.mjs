/**
 * 纯 Node 自测：USB→I2C 桥的**协议层 + 假探针 + 会话层**（不需要浏览器、不需要硬件）
 *   node tools/selftest/i2c-proto.test.mjs        （等价：make test-i2c）
 *
 * 这里咬三件事：
 *   ① **报文偏移**必须与固件 proto.h / 已上板验证的 i2c_bridge_test.py 一致 ——
 *      proto.h 按"含 Report ID 的 64 B"编号，网页侧 `xfer()` 已经把 Report ID 剥掉，
 *      两者差 1。这是本页最容易错一个字节的地方，所以逐字段钉死。
 *   ② **假器件要照真器件的行为**：tWR 期间不 ACK、页写跨页回绕、MPU6050 温度按手册逆运算、
 *      XFER 只登记而结果要轮询 RESULT —— 主机侧代码靠这些才被验得对。
 *   ③ **错误路径**：未使能 / 忙 / 参数越界 / 地址没 ACK，码要对得上。
 */
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const app = join(here, '..', '..', 'app');
const url = p => 'file://' + join(app, p).replace(/\\/g, '/');

const P = await import(url('i2c/protocol.js'));
const { MockI2cProbe, Eeprom24C02, Mpu6050, Ads1115, Si5351 } = await import(url('i2c/mock.js'));
const { I2cSession } = await import(url('i2c/session.js'));

let pass = 0, fail = 0;
const ok = (cond, name, extra = '') => {
  if (cond){ pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name} ${extra}`); }
};
const eq = (a, b, name) => ok(a === b, name, `期望 ${JSON.stringify(b)}，实际 ${JSON.stringify(a)}`);
const hex = a => [...a].map(x => x.toString(16).padStart(2, '0')).join(' ');
const throws = (fn, name) => {
  try { fn(); ok(false, name, '本该抛错'); } catch { ok(true, name); }
};

// ==================================================================== 1
console.log('== 1. 报文偏移（与 proto.h / i2c_bridge_test.py 对账）==');
{
  // proto.h: req[0]=reportID req[1]=len req[2]=0x36 req[3]=action req[4]=flags req[5]=dev
  //          req[6]=addr_len req[7]=wr_len req[8]=rd_len req[9..12]=addr req[13..]=wr_data
  // 网页 data = [action, ...参数] → data[0]=action、data[1]=flags、data[2]=dev …
  eq(P.HID_CMD, 0x36, 'HID 命令码 = 0x36');
  const d = P.actXfer({ dev: 0x50, addr: [0x00], wr: [0xaa, 0xbb], rd: 8 });
  eq(d[0], P.ACT.XFER, 'data[0] = action(XFER=5)');
  eq(d[1], 0, 'data[1] = flags（保留，必须 0）');
  eq(d[2], 0x50, 'data[2] = dev');
  eq(d[3], 1, 'data[3] = addr_len');
  eq(d[4], 2, 'data[4] = wr_len');
  eq(d[5], 8, 'data[5] = rd_len');
  eq(hex(d.slice(6, 10)), '00 00 00 00', 'data[6..9] = addr 4 B（子地址按原序，后面补 0）');
  eq(hex(d.slice(10)), 'aa bb', 'data[10..] = wr_data');
  eq(d.length, 1 + P.XFER_HDR + 2, '总长 = 1 + 9 + wr_len');

  // 子地址按**原序**发（proto.h 特意记过这个坑：早期按 u32 小端取低字节 → addr_len=1 恒发 0x00）
  const d2 = P.actXfer({ dev: 0x50, addr: [0x12, 0x00], wr: [], rd: 4 });
  eq(hex(d2.slice(6, 8)), '12 00', '子地址两个字节按原序发出（不是小端翻转）');

  eq(P.actEnable(true)[1], 1, 'ENABLE 的 req[4] = 1（开）');
  eq(P.actEnable(false)[1], 0, 'ENABLE 的 req[4] = 0（关）');
  eq(P.actScan()[0], 7, 'SCAN = action 7');
  eq(P.actResult()[0], 6, 'RESULT = action 6');
  eq(P.actPinTest()[0], 11, 'PINTEST = action 11');
  eq(P.actDbg()[0], 10, 'DBG = action 10');
  eq(P.actBitProbe(0x5a)[1], 0x5a, 'BITPROBE 的 req[4] = 7 位地址');

  // 配置块 16 B：scl_hz(4) pullup(1) retries(1) flags(1) 保留(1) actual(4) 保留(4)
  const cfg = P.packCfg({ sclHz: 400000, pullup: 1, retries: 3 });
  eq(cfg.length, 16, '配置块 16 B');
  eq(hex(cfg.slice(0, 4)), '80 1a 06 00', 'scl_hz 小端 400000 = 0x00061A80');
  eq(cfg[4], 1, '偏移 4 = pullup');
  eq(cfg[5], 3, '偏移 5 = retries');
  eq(cfg[6], 0, '偏移 6 = flags（必须 0）');
  const back = P.parseCfg(cfg);
  eq(back.sclHz, 400000, 'parseCfg 读回 sclHz');

  // 响应侧：res[0]=长度 res[1]=0x36 res[2]=action res[3..6]=状态字 res[7..]=数据
  // 状态字凑成：ENABLED|BUS_OK|SDA|SCL + lastErr=7 + done=0x35 → 0x0035071D
  const res = Uint8Array.from([0x10, 0x36, 0x06, 0x1d, 0x07, 0x35, 0x00, 0x07, 0x02, 0xaa, 0xbb]);
  eq(res[1], 0x36, 'res[1] = 0x36（命令回显 —— AkaLinkHid._handleInput 按它匹配）');
  eq(res[2], 6, 'res[2] = action 回显');
  const st = P.parseStatus(res);
  eq(st.raw, 0x0035071d, '状态字 = res[3..6] 小端');
  eq(st.enabled, true, 'bit0 ENABLED');
  eq(st.pending, false, 'bit1 PENDING');
  eq(st.busOk, true, 'bit2 BUS_OK');
  eq(st.sda, true, 'bit3 SDA 电平');
  eq(st.scl, true, 'bit4 SCL 电平');
  eq(st.lastErr, 0x07, 'bit8..15 = 最近一次完成事务的错误码');
  eq(st.done, 0x35, 'bit16..23 = 完成计数');
  eq(st.cmdRc, 0x00, 'bit24..31 = 本命令结果码');
  const rr = P.parseResult(res);
  eq(rr.err, 0x07, 'RESULT：res[7] = 错误码');
  eq(rr.n, 0x02, 'RESULT：res[8] = 长度');
  eq(hex(rr.data), 'aa bb', 'RESULT：res[9..] = 数据');
}

// ==================================================================== 2
console.log('== 2. 计数器 / 扫描位图 / PINTEST / 分片计划 ==');
{
  const b = new Uint8Array(40);
  const dv = new DataView(b.buffer);
  [5, 1, 100, 200, 3, 4, 0, 2, 400000, 5243].forEach((v, i) => dv.setUint32(i * 4, v, true));
  const c = P.parseCounters(b);
  eq(c.framesOk, 5, 'frames_ok');
  eq(c.framesErr, 1, 'frames_err');
  eq(c.bytesTx, 100, 'bytes_tx');
  eq(c.bytesRx, 200, 'bytes_rx');
  eq(c.nackAddr, 3, 'nack_addr');
  eq(c.nackData, 4, 'nack_data');
  eq(c.busRecover, 2, 'bus_recover');
  eq(c.actualSclHz, 400000, 'actual_scl_hz');
  eq(Math.round(P.ticksToUs(c.lastTicks)), 218, 'last_ticks → µs（24 MHz tick）');

  // SCAN 位图：14 B，bit0 = 0x08
  const bm = new Uint8Array(P.SCAN_BYTES);
  bm[0] = 0b0000_0001;            // 0x08
  bm[8] = 0b0001_0000;            // 0x08 + 64 + 4 = 0x4C
  eq(P.scanBitmapToAddrs(bm).join(','), '8,76', '位图 → 地址列表（bit0 = 0x08）');
  eq(P.scanBitmapToAddrs(new Uint8Array(P.SCAN_BYTES)).length, 0, '全 0 位图 = 没有器件');

  const pt = P.parsePinTest(Uint8Array.from([0x03, 0x00, 0x03, 0x00]));   // bit0/1 高 + bit16/17 高
  eq(pt.idleSda, 1, 'PINTEST 空闲 SDA');
  eq(pt.idleScl, 1, 'PINTEST 空闲 SCL');
  eq(pt.droveScl, 1, 'PINTEST 事务中拉低过 SCL');
  eq(pt.prob, 0, '问题位图取 bit8..15（bit0/1 是"空闲电平=1"这两个正常信号）');
  eq(pt.bridgeOk, true, 'bit16=1 且问题位图=0 ⇒ 桥侧正常');
  const bad = P.parsePinTest(Uint8Array.from([0x00, 0x02, 0x01, 0x00]));  // 问题位图 bit1 = 空闲 SCL 常低
  eq(bad.bridgeOk, false, '问题位图非 0 ⇒ 桥侧异常');
  ok(bad.problems[0].includes('常低'), '问题文案说清了是什么', bad.problems[0]);

  eq(P.guessDevice(0x50), 'AT24Cxx EEPROM', '0x50 认成 EEPROM');
  eq(P.guessDevice(0x68), 'MPU6050 / DS1307', '0x68 认成 MPU6050');
  eq(P.guessDevice(0x48), 'ADS1115 ADC', '0x48 认成 ADS1115');

  // 分片读：先"设地址指针"再续读
  const pr = P.planRead([0x00], 256, { mode: 'ptr' });
  eq(pr.cmds[0].note, '设地址指针', '第一笔是设地址指针');
  eq(pr.cmds[0].rd, 0, '设地址指针那笔不读');
  eq(pr.cmds.length, 1 + 5, '256 B → 1 笔设指针 + 5 笔 54/54/54/54/40');
  eq(pr.cmds[1].rd, 54, '第一片 54 B');
  eq(pr.cmds[5].rd, 40, '末片 40 B');
  eq(pr.cmds[1].addr.length, 0, '续读片不带子地址（靠器件内部指针自增）');
  const pr2 = P.planRead([0x10], 60, { mode: 'reset' });
  eq(pr2.cmds.length, 2, 'reset 模式：每片都带子地址');
  eq(hex(pr2.cmds[1].addr), '46', 'reset 模式的第二片子地址 = 0x10 + 54 = 0x46');

  // describeXfer 的四种线序
  ok(P.describeXfer({ dev: 0x50, addr: [], wr: [], rd: 0 }).includes('只问 ACK'), '探测的线序描述');
  ok(P.describeXfer({ dev: 0x50, addr: [0x00], wr: [], rd: 8 }).includes('rSTART'), '写子地址再读要有 repeated START');
  ok(!P.describeXfer({ dev: 0x50, addr: [], wr: [], rd: 8 }).includes('rSTART'), '纯读没有 repeated START');
  ok(!P.describeXfer({ dev: 0x50, addr: [0x00], wr: [1, 2], rd: 0 }).includes('rSTART'), '只写没有 repeated START');
}

// ==================================================================== 3
console.log('== 3. 组包上限：越界必须在主机侧就拦住 ==');
{
  throws(() => P.actXfer({ dev: 0x50, addr: [0], wr: new Array(52).fill(0), rd: 0 }), '写 52 B 被拒（上限 51）');
  throws(() => P.actXfer({ dev: 0x50, addr: [0], wr: [], rd: 55 }), '读 55 B 被拒（上限 54）');
  throws(() => P.actXfer({ dev: 0x50, addr: [0, 0, 0, 0, 0], wr: [], rd: 1 }), '子地址 5 B 被拒（上限 4）');
  throws(() => P.actXfer({ dev: 0x80, addr: [], wr: [], rd: 1 }), '地址 bit7 置位被拒（7 位地址）');
  ok(true, 'actXfer({dev:0x7f,…}) 不抛（7 位地址上界）', '');
  P.actXfer({ dev: 0x7f, addr: [], wr: [], rd: 1 });
  eq(P.errText(P.E.BUSY).includes('重发'), true, 'E_BUSY 的文案告诉用户"等一下重发"');
  eq(P.errText(P.E.NO_ADDR).includes('没 ACK'), true, 'E_NO_ADDR 的文案指向器件侧');
}

// ==================================================================== 4
console.log('== 4. 假器件：EEPROM 的页写与 tWR ==');
{
  let t = 1000;
  const now = () => t;
  const ee = new Eeprom24C02(0x50);
  // 页写 8 B
  eq(ee.transact([0x00], [1, 2, 3, 4, 5, 6, 7, 8], 0, now()), null, '页写成功');
  // tWR 期间（≤5 ms）**不能**读 —— 真器件这时连地址都不 ACK
  const withinTwr0 = ee.transact([0x00], [], 8, now());
  ok(withinTwr0 && withinTwr0.err === P.E.NO_ADDR, '写完之后立刻读被 NACK（tWR 还没过）', JSON.stringify(withinTwr0));
  t += 1;
  const withinTwr = ee.transact([0x00], [], 8, now());
  ok(withinTwr && withinTwr.err === P.E.NO_ADDR, 'tWR 中读仍被 NACK', JSON.stringify(withinTwr));
  t += 10;
  eq(hex(ee.transact([0x00], [], 8, now())), '01 02 03 04 05 06 07 08', 'tWR 过后读回原值');

  // 跨页写会绕回本页页首
  ee.transact([0x00], [0xa1, 0xa2, 0xa3, 0xa4, 0xa5, 0xa6], 0, now());
  t += 10;
  ee.transact([0x06], [0xb7, 0xb8, 0xb9, 0xba], 0, now());   // 0x06 起写 4 B → 0x06,0x07,0x00,0x01
  t += 10;
  eq(hex(ee.transact([0x00], [], 8, now())), 'b9 ba a3 a4 a5 a6 b7 b8',
     '跨页写在页内回绕（后两个字节盖回页首 —— 这就是"必须页对齐"的原因）');

  // 地址自增读
  t += 10;
  eq(hex(ee.transact([0x00], [], 3, now())), 'b9 ba a3', '读自增 ①');
  eq(hex(ee.transact([], [], 3, now())), 'a4 a5 a6', '地址指针在片间自增（不带子地址也接着读）');

  // 写保护
  ee.writeProtected = true;
  const wp = ee.transact([0x00], [1], 0, now());
  ok(wp && wp.err === P.E.NO_ACK, '写保护 → 数据相位 NACK', JSON.stringify(wp));
}

// ==================================================================== 5
console.log('== 5. 假器件：MPU6050 / ADS1115 的换算要对得上手册 ==');
{
  let t = 0;
  const mpu = new Mpu6050(0x68, () => t * 1000);
  mpu.transact([], [], 1);
  eq(mpu.regs[0x75], 0x68, 'WHO_AM_I = 0x68');
  // 温度：手册 TEMP_OUT = (T℃ − 36.53) × 340 → 主机按 raw/340+36.53 解回 T
  t = 0;
  const raw = mpu.transact([0x41], [], 2);
  const tC = ((raw[0] << 8 | raw[1]) << 16 >> 16) / 340 + 36.53;
  ok(Math.abs(tC - 36.5) < 0.1, '温度按手册逆运算存（解出来 ≈36.5 ℃，不是 73 ℃）', `得到 ${tC.toFixed(2)}`);
  // 加速度：z 轴应当是 ~1 g（重力）
  t = 0;
  const a = mpu.transact([0x3b], [], 6);
  const az = (((a[4] << 8 | a[5]) << 16 >> 16)) / 16384;
  ok(Math.abs(az - 1.0) < 0.05, '加速度 z ≈ 1 g（重力压在 z 上）', `得到 ${az.toFixed(3)}`);
  // 唤醒寄存器默认 SLEEP=1
  eq(mpu.regs[0x6b], 0x40, 'PWR_MGMT_1 上电默认 0x40（SLEEP=1，真器件如此）');

  const ads = new Ads1115(0x48, () => t2);
  let t2 = 0;
  const c0 = ads.transact([0x01], [], 2);
  eq(hex(c0), '85 83', 'ADS1115 配置寄存器上电默认 0x8583');
  const c1 = ads.transact([0x00], [], 2);
  eq(hex(c1), '00 00', '未启动转换时结果是 0');
  // 写配置启动单次转换（0xC383 = AIN0/GND ±4.096V 单次 128SPS）
  ads.transact([0x01], [0xc3, 0x83], 0);
  const cfgBack = ads.transact([0x01], [], 2);
  const word = (cfgBack[0] << 8) | cfgBack[1];
  eq(word & 0x7fff, 0xc383 & 0x7fff, '配置字写进去了（bit15 OS 是"读回状态"，会变）');
  eq((word >> 12) & 7, 4, 'MUX = 100（AIN0 对 GND）');
  eq((word >> 9) & 7, 1, 'PGA = 001（±4.096 V）');
  // 把时钟挪到 300 s（正弦正好过零）并让 9 ms 的转换窗口过去
  t2 = 300000;
  const conv = ads.transact([0x00], [], 2);
  const v = (((conv[0] << 8 | conv[1]) << 16 >> 16)) * 0.000125;
  ok(Math.abs(v - 1.0) < 0.02, 'AIN0 按 125 µV/LSB 解出来 ≈1.000 V（正弦在 300 s 过零）', `得到 ${v.toFixed(4)} V`);
  const osBit = (ads.transact([0x01], [], 2)[0] >> 7) & 1;
  eq(osBit, 1, '转换完成后 OS 读回 1（＝空闲）');

  // 🚨 真器件是"**到点才更新**转换寄存器"：没转完就读，读到的还是上一次的结果。
  //    假器件要是每次读都现算一个新值，示例里少写一个 delay 也测不出来（那正是 ADS1115 示例警告的事）。
  let t3 = 0;
  const ads3 = new Ads1115(0x48, () => t3);
  ads3.transact([0x01], [0xc3, 0x83], 0);            // 启动单次转换（128 SPS → 约 9 ms）
  eq(hex(ads3.transact([0x00], [], 2)), '00 00', '转换没到点：读到的还是上一次的结果（上电后是 0）');
  t3 = 9;
  const conv3 = ads3.transact([0x00], [], 2);
  ok(conv3[0] !== 0 || conv3[1] !== 0, '……过了 1/DR 才出新的转换结果');
  ads3.transact([0x01], [0x04, 0x83], 0);            // MODE=0 连续转换
  eq(hex(ads3.transact([0x00], [], 2)), hex(ads3.transact([0x00], [], 2)),
     '连续模式：比数据率还快的第二次读还是同一个值（受 DR 节流）');
}

// ==================================================================== 5b
console.log('== 5b. 假件 Si5351 的寄存器地址（按数据手册 / AN619）==');
{
  // 2026-10 手册对账：假件原来照抄了示例里错的地址（把 0x03 当 CLK0 控制），一起修掉
  const si = new Si5351(0x60);
  eq(hex(si.transact([0x03], [], 1)), '00', '0x03 输出使能：复位值 0x00 = 四路都开着');
  eq(hex(si.transact([0x10], [], 1)), '00', '0x10 = CLK0 控制（AN619 复位值 0x00）');
  si.transact([0x03], [0x0f], 0);
  eq(hex(si.transact([0x03], [], 1)), '0f', '往 0x03 写 0x0f 回读还是 0x0f（它只管开关，= 关掉 CLK0..CLK3）');
  si.transact([0x10], [0x0f], 0);
  eq(hex(si.transact([0x10], [], 1)), '0f', 'CLK0 控制写在 0x10 上（不是 0x03）');
  si.transact([0xb1], [0xac], 0);
  eq(hex(si.transact([0xb1], [], 1)), '00', '0xB1 PLL 软复位是自清位：写完回读就是 0（寄存器文件够大，没绕回 0x31）');
}

// ==================================================================== 6
console.log('== 6. 假探针：登记 + 轮询 RESULT 的语义 ==');
{
  const probe = new MockI2cProbe({ pendingExtra: 1 });
  const st0 = P.parseStatus(await probe.xfer(0x36, P.actStatus()));
  eq(st0.enabled, false, '刚上电未使能');
  eq(st0.cmdRc, P.E.OK, 'STATUS 恒成功');

  const refused = await probe.xfer(0x36, P.actXfer({ dev: 0x50, addr: [0], wr: [], rd: 1 }));
  eq(P.parseStatus(refused).cmdRc, P.E.DISABLED, '未使能时 XFER 回 E_DISABLED（与上游 a4d5205 的修法一致）');

  await probe.xfer(0x36, P.actEnable(true));
  eq(P.parseStatus(await probe.xfer(0x36, P.actStatus())).enabled, true, 'ENABLE 1 之后已使能');

  const r1 = await probe.xfer(0x36, P.actXfer({ dev: 0x50, addr: [0], wr: [], rd: 4 }));
  eq(P.parseStatus(r1).cmdRc, P.E.OK, 'XFER 被受理');
  eq(P.parseStatus(r1).pending, true, '受理后 PENDING=1（事务在主循环里做）');
  // 上一条没做完就再发 → E_BUSY
  const busy = await probe.xfer(0x36, P.actXfer({ dev: 0x50, addr: [0], wr: [], rd: 4 }));
  eq(P.parseStatus(busy).cmdRc, P.E.BUSY, '并发 XFER 回 E_BUSY（不是排队执行）');
  // 轮询
  const p1 = await probe.xfer(0x36, P.actResult());
  eq(P.parseStatus(p1).pending, true, '第一轮 RESULT 还在做（pendingExtra=1）');
  const p2 = await probe.xfer(0x36, P.actResult());
  const st2 = P.parseStatus(p2);
  eq(st2.pending, false, '第二轮做完了');
  eq(st2.done, 1, '完成计数 +1');
  const rr = P.parseResult(p2);
  eq(rr.err, 0, '错误码 0');
  eq(rr.n, 4, '读回 4 B');

  // 参数越界
  const bad = await probe.xfer(0x36, Uint8Array.from([P.ACT.XFER, 0, 0x50, 1, 52, 0, 0, 0, 0, 0]));
  eq(P.parseStatus(bad).cmdRc, P.E.RANGE, 'wr_len=52 越界 → E_RANGE');
  // 保留位非 0
  await probe.xfer(0x36, P.actResult());
  const badFlag = await probe.xfer(0x36, Uint8Array.from([P.ACT.XFER, 1, 0x50, 1, 0, 1, 0, 0, 0, 0]));
  eq(P.parseStatus(badFlag).cmdRc, P.E.RANGE, 'flags 非 0 → E_RANGE');

  // RESET 清计数器、保配置
  await probe.xfer(0x36, P.actSetCfg({ sclHz: 400000 }));
  await probe.xfer(0x36, P.actReset());
  const after = P.parseCounters(P.dataOf(await probe.xfer(0x36, P.actStatus())));
  eq(after.framesOk, 0, 'RESET 清计数器');
  eq(after.busRecover, 1, 'bus_recover 计 1 次');
  eq(after.actualSclHz, 400000, 'RESET 保留配置（400 kHz 还在）');
}

// ==================================================================== 7
console.log('== 7. 会话层端到端（假探针）：扫描 / EEPROM 写读 / 计数器 ==');
{
  const s = new I2cSession();
  ok(await s.connect(false, { mock: true }), 'connect(mock) 成功');
  eq(s.connected, true, '会话已连接');
  eq(s.enabled, false, '连上时桥还没使能');
  await s.setEnabled(true);
  eq(s.enabled, true, 'setEnabled(true) 之后已使能');
  eq(s.actualSclHz, 100000, '默认档 100 kHz');

  const { addrs } = await s.scan();
  eq(addrs.join(','), [0x48, 0x50, 0x60, 0x68].join(','), '扫描到四个假器件（按地址升序）');

  // EEPROM 页写 + 等 tWR + 回读
  const w = await s.transaction({ dev: 0x50, addr: [0x00], wr: [0xa5, 0x5a, 0xde, 0xad, 0xbe, 0xef, 0x12, 0x34], rd: 0 });
  eq(w.err, 0, '页写成功');
  await new Promise(r => setTimeout(r, 12));                  // tWR
  const r = await s.transaction({ dev: 0x50, addr: [0x00], wr: [], rd: 8 });
  eq(r.err, 0, 'tWR 过后回读成功');
  eq(hex(r.data), 'a5 5a de ad be ef 12 34', '回读逐字节对得上');
  ok(r.ms >= 0, '每笔事务都有耗时记录', '');

  // 不存在的地址
  const no = await s.transaction({ dev: 0x21, addr: [0], wr: [], rd: 1 }, { quiet: true });
  eq(no.err, P.E.NO_ADDR, '不存在的地址 → E_NO_ADDR');

  // 配置
  const cfg = await s.applyCfg({ sclHz: 400000, pullup: 1, retries: 2 });
  eq(cfg.actualSclHz, 400000, '配置生效并回读');
  eq(cfg.pullup, 1, 'pullup 回读');

  // PINTEST
  const pt = await s.pinTest();
  eq(pt.bridgeOk, true, 'PINTEST 判"桥这一侧正常"');

  // 串行链：并发发三笔也不能互相踩（HID 只允许一条在飞）
  const three = await Promise.all([
    s.transaction({ dev: 0x68, addr: [0x75], wr: [], rd: 1 }, { quiet: true }),
    s.transaction({ dev: 0x68, addr: [0x75], wr: [], rd: 1 }, { quiet: true }),
    s.transaction({ dev: 0x68, addr: [0x75], wr: [], rd: 1 }, { quiet: true }),
  ]);
  ok(three.every(x => x.err === 0 && x.data[0] === 0x68), '并发三笔被串行化，全部拿到 0x68', JSON.stringify(three.map(x => [x.err, hex(x.data)])));

  // 计数器：到这儿一共 1 扫描 + 1 写 + 1 读 + 3 并发读 = 6 笔成功，1 笔地址 NACK
  const { counters } = await s.readStatus({ quiet: true });
  eq(counters.framesOk, 6, 'frames_ok 记下 6 笔成功事务');
  eq(counters.nackAddr, 1, 'nack_addr 记下那次不存在地址');
  eq(counters.framesErr, 1, 'frames_err 记下 1 笔失败');

  await s.disconnect();
  eq(s.connected, false, 'disconnect 之后已断开');
}

// ==================================================================== 8
console.log('== 8. 失联记账：连续超时要置 lost 并停轮询 ==');
{
  const s = new I2cSession();
  await s.connect(false, { mock: true });
  // 打桩：让底层 xfer 一直抛错（模拟 web-handoff §10 的"探针整个哑掉"）
  s.hid.xfer = async () => { throw new Error('The device was disconnected'); };
  for (let i = 0; i < 3; i++){
    try { await s.readStatus({ quiet: true }); } catch { /* 预期 */ }
  }
  eq(s.lost, true, '连续 3 次超时 → lost');
  ok(s.ring.some(e => e.text.includes('拔插')), '日志里明确提示拔插 USB');
  let msg = '';
  try { await s.transaction({ dev: 0x50, addr: [0], wr: [], rd: 1 }, { quiet: true }); }
  catch (e){ msg = e.message; }
  ok(msg.includes('失联'), '失联后事务直接拒绝并说明原因', msg);
  await s.disconnect();
}

// ==================================================================== 9
console.log('== 9. 执行器：稳态不刷日志（否则日志环十几秒就被冲干净）==');
{
  const { ScriptRunner } = await import(url('i2c/runner.js'));
  const D = await import(url('i2c/dsl.js'));
  const s = new I2cSession();
  await s.connect(false, { mock: true });
  await s.setEnabled(true);

  const ast = D.parseScript('loop 20ms\n  rd 0x68 0x75 1 as id=u8(0)\nend');
  eq(ast.errors.length, 0, '循环脚本解析零错误');
  const samples = [];
  const runner = new ScriptRunner(s, { onEvent: e => {
    if (e.type === 'item' && e.phase === 'done' && e.values?.length) samples.push(e.values[0].value);
  } });
  s.ring.length = 0;
  const p = runner.run(ast.items);
  await new Promise(r => setTimeout(r, 700));
  runner.stop();
  await p;
  const okLogs = s.ring.filter(e => e.kind === 'ok').length;
  ok(samples.length >= 15, `20 ms 周期跑 0.7 s 采了 ${samples.length} 次`);
  ok(samples.every(v => v === 0x68), '每次采样都是 0x68（读数正确）');
  ok(okLogs <= 3, `🚨 成功日志只有前几拍（${okLogs} 条），没有按采样次数刷屏`);
  ok(s.ring.length < samples.length, `日志条数（${s.ring.length}）远少于采样次数（${samples.length}）`);

  // 错误路径：器件一直 NACK 时错误日志要限量，**而且每一笔不能白等超时**
  const badAst = D.parseScript('loop 20ms\n  rd 0x21 0x00 1\nend');
  s.ring.length = 0;
  const r2 = new ScriptRunner(s, { onEvent: () => {} });
  const t0 = Date.now();
  const p2 = r2.run(badAst.items);
  await new Promise(r => setTimeout(r, 700));
  r2.stop();
  const res2 = await p2;
  const errLogs = s.ring.filter(e => e.kind === 'e').length;
  ok(res2.stats.errors >= 15, `器件不存在 → 记下 ${res2.stats.errors} 笔失败`);
  // 限量规则：前 2 拍（LOUD_TICKS）照常报 + 之后最多再补 5 条（ERR_LOGS_AFTER）
  ok(errLogs <= 8, `错误日志被限量（${errLogs} 条，远少于 ${res2.stats.errors} 笔失败）`);
  // 🚨 回归：曾经拿"完成计数变了"当跳出条件 → 每一笔**失败**都白等满 2 s（实测 20 笔要 40 s）
  const perFail = (Date.now() - t0) / Math.max(1, res2.stats.errors);
  ok(perFail < 100, `每笔失败事务的耗时 ${perFail.toFixed(1)} ms（不许接近 2 s 的 resultTimeout）`);
  await s.disconnect();
}

// ==================================================================== 10
console.log('== 10. readLong：长读自动分片（对上层就是"读 N 个字节"）==');
{
  const s = new I2cSession();
  await s.connect(false, { mock: true });
  await s.setEnabled(true);

  // 先往 EEPROM 写个已知图案（分 8 次页写，每页 5 B 避开页边界）
  const ee = s.hid.devices.get(0x50);
  for (let i = 0; i < 256; i++) ee.mem[i] = i & 0xff;   // 直接铺内存，省得等 tWR

  // ① 短读走原路（不分片）
  const one = await s.readLong({ dev: 0x50, addr: [0x00], rd: 8 });
  eq(one.err, 0, '短读成功');
  eq(one.chunks, 1, '短读只有 1 笔（不分片）');
  eq(hex(one.data), '00 01 02 03 04 05 06 07', '短读内容对');

  // ② 256 B 长读：默认 reset 模式，自动 5 笔拼回来
  s.ring.length = 0;
  const big = await s.readLong({ dev: 0x50, addr: [0x00], rd: 256 });
  eq(big.err, 0, '256 B 长读成功');
  eq(big.data.length, 256, '拼回来 256 B');
  eq(big.chunks, 5, '自动分成 5 笔（54×4 + 40）');
  eq(big.mode, 'reset', '默认 reset 模式');
  ok([...big.data].every((v, i) => v === (i & 0xff)), '256 B 逐字节与写入的图案一致（分片拼接没串位）');
  const okLogs = s.ring.filter(e => e.kind === 'ok').length;
  eq(okLogs, 1, '🚨 中间 5 笔**不往日志里写**，只出一行结果（"让外部看不到"就靠这条）');
  ok(s.ring[0].text.includes('256 B') && s.ring[0].text.includes('分 5 笔'), '……那一行说清了总长与笔数', s.ring[0].text);

  // ③ ptr 模式：先一笔"设地址指针"，之后续片不带子地址
  const calls = [];
  const origXfer = s.hid.xfer.bind(s.hid);
  s.hid.xfer = async (cmd, data) => { if (data[0] === P.ACT.XFER) calls.push({ addrLen: data[3], rd: data[5] }); return origXfer(cmd, data); };
  s.ring.length = 0;
  const ptr = await s.readLong({ dev: 0x50, addr: [0x00], rd: 120, chunk: 'ptr' });
  s.hid.xfer = origXfer;
  eq(ptr.err, 0, 'ptr 模式 120 B 长读成功');
  eq(ptr.data.length, 120, '……读回 120 B');
  eq(ptr.mode, 'ptr', '……mode 标成 ptr');
  eq(calls.length, 4, 'ptr 模式：1 笔设指针 + 3 片 = 4 笔');
  eq(calls[0].addrLen, 1, '第一笔带子地址（设地址指针）');
  eq(calls[0].rd, 0, '……而且不读数据');
  ok(calls.slice(1).every(c => c.addrLen === 0), '后续每一片都**不带**子地址（靠器件内部指针自增）');
  eq(calls[1].rd, 54, '续片 54 B');
  eq(calls[3].rd, 12, '末片 12 B');
  ok([...ptr.data].every((v, i) => v === (i & 0xff)), 'ptr 模式拼回来的内容也对');

  // ④ 中途失败：必须说清断在第几片，而且**不返回半截数据**
  const shortDev = {
    addr: 0x30, name: '测试器件',
    n: 0,
    transact(addr, wr, rd){
      this.n++;
      if (this.n > 2) return { err: P.E.NO_ADDR };
      const out = new Uint8Array(rd);
      for (let i = 0; i < rd; i++) out[i] = (addr[0] + i) & 0xff;
      return out;
    },
  };
  s.hid.devices.set(0x30, shortDev);
  s.ring.length = 0;
  const bad = await s.readLong({ dev: 0x30, addr: [0x00], rd: 200 });
  eq(bad.err, P.E.NO_ADDR, '第 3 片失败 → 整个长读报那个错误码');
  eq(bad.data.length, 0, '🚨 失败时**不返回半截数据**（读到一半的值没有意义）');
  eq(bad.chunks, 3, '报告断在第 3 片');
  ok(bad.failNote.includes('第 3/4 片'), '……并且说清"第几片/共几片"', bad.failNote);
  eq(s.ring.filter(e => e.kind === 'e').length, 1, '失败也只出一行日志');
  ok(s.ring[0].text.includes('已读回 108 B'), '……那一行说清了已经读回多少（方便判断器件是不是半路掉线）', s.ring[0].text);

  await s.disconnect();
}

console.log(`\n== 结果：${pass} 项通过 / ${fail} 项失败 ==`);
process.exit(fail ? 1 : 0);
