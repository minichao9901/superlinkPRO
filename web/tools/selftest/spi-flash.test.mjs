/**
 * 纯 Node 自测：外接 SPI NOR 的测试件（解析器 + 帧序列 + 器件模型）
 *   node tools/selftest/spi-flash.test.mjs      （等价：make test-flash）
 *
 * 咬三件事：
 *   ① **解析**：JEDEC ID / SFDP 头与参数表头（布局以 Linux sfdp.h 为准）/ 状态寄存器；
 *   ② **序列**：连续读的拆帧（首帧带 cmd+地址、续读帧 cmd_en=0 + CS_HOLD、末帧 CS_OFF）、
 *      按页编程不跨页、擦除带不带地址；
 *   ③ **器件模型**：真按 NOR 的规矩答 —— WEL/BUSY、只能 1→0、跨页报错、QE=0 拒四线。
 *      模型不真，页面上的"通过"就是假的。
 */
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const app = join(here, '..', '..', 'app');
const url = p => 'file://' + join(app, p).replace(/\\/g, '/');

const P = await import(url('spi/protocol.js'));
const FL = await import(url('spi/flash.js'));
const M = await import(url('spi/mock.js'));

let pass = 0, fail = 0;
const ok = (cond, name, extra = '') => {
  if (cond){ pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name} ${extra}`); }
};
const hexOf = a => [...a].map(x => x.toString(16).padStart(2, '0')).join(' ');
/** 把 items 里的 XFER 解回来（断言用）*/
const xferOf = it => {
  const p = it.payload, dv = new DataView(p.buffer, p.byteOffset, p.byteLength);
  return {
    cmd: p[0], tcfg: p[1], cmdEn: !!(p[1] & P.TC.CMD_EN), addrEn: !!(p[1] & P.TC.ADDR_EN),
    addrLen: p[2], dummy: p[3], txLen: dv.getUint16(4, true), rxLen: dv.getUint16(6, true),
    addr: dv.getUint32(8, true), tx: p.subarray(P.XFER_HDR), lines: P.tcfgToLines(p[1]),
  };
};

/** 只挑出 XFER 帧来解（序列里还夹着 DELAY 之类的帧）*/
const xfers = items => items.filter(i => i.type === P.T.XFER).map(xferOf);

// 一个最小的"主机"：分配 seq → 组帧 → 打包 → 写进假探针 → 取回按 seq 配对的应答。
// 时间由**可控时钟**推进（假探针与器件模型共用它），所以 BUSY / 延时都能离线等到。
const mkClock = (t0 = 1000) => { const c = { t: t0 }; c.now = () => c.t; return c; };

function mkHost(probe, clock){
  let seq = 1;
  return {
    clock,
    async send(items){
      const frames = [], want = [];
      for (const i of items){
        const s = (i.flags & P.F.RSP) ? seq++ : 0;
        frames.push(P.frame(i.type, i.payload, { flags: i.flags | 0, seq: s }));
        if (s) want.push(s);
      }
      for (const pack of P.packFrames(frames)) probe.write(pack);
      // 把非阻塞延时（DELAY / STEP.delay）推完
      for (let k = 0; k < 2000 && probe.queue.length; k++){ clock.t += 5; probe.tick(clock.t); }
      const rsps = [];
      for (const s of want){
        let hit = null;
        for (let k = 0; k < 64; k++){
          const raw = probe.takeRsp();
          if (!raw) break;
          const r = P.parseRsp(raw);
          if (r && r.seq === s){ hit = r; break; }
        }
        rsps.push(hit);
      }
      return { rsps };
    },
    /** 等器件 BUSY 清掉（真页面也是这么轮的）*/
    async waitReady(maxMs = 5000){
      for (let spent = 0; spent < maxMs; spent += 10){
        const r = await this.send(FL.rdsr1Items());
        const sr = FL.parseStatus1(r.rsps[0]?.data || new Uint8Array(1));
        if (!sr.busy) return sr;
        clock.t += 10;
      }
      throw new Error('等 BUSY 超时');
    },
  };
}

async function mkProbe(opts = {}){
  const clock = opts.clock || mkClock();
  const probe = new M.MockSpiProbe({ ...opts, clock: clock.now });
  await probe.xfer(P.HID_CMD, P.hidData.enable(true));
  return { probe, clock, host: mkHost(probe, clock) };
}

// ==================================================================== 1
console.log('== 1. JEDEC ID 解析 ==');
{
  const w = FL.parseJedec(Uint8Array.of(0xef, 0x40, 0x18));
  ok(w.vendor === 'Winbond' && w.mfr === 0xef && w.type === 0x40, '厂商/类型');
  ok(w.sizeBytes === 16 * 1024 * 1024 && /16 MB/.test(w.text), `容量码 0x18 → 16 MB（${w.text}）`);
  ok(FL.parseJedec(Uint8Array.of(0xc8, 0x40, 0x17)).sizeBytes === 8 * 1024 * 1024, 'GigaDevice 0x17 → 8 MB');
  ok(FL.parseJedec(Uint8Array.of(0xc2, 0x20, 0x16)).sizeBytes === 4 * 1024 * 1024, 'Macronix 0x16 → 4 MB');
  ok(FL.parseJedec(Uint8Array.of(0xef, 0x40, 0x14)).sizeBytes === 1 * 1024 * 1024, '0x14 → 1 MB');
  ok(FL.parseJedec(Uint8Array.of(0x00, 0x00, 0x00)).vendor === null, '厂商表里没有 → 显示原始值不瞎猜');
  ok(FL.parseJedec(Uint8Array.of(0xff, 0xff, 0xff)).sizeBytes === null, '容量码不认识 → sizeBytes=null');
  ok(FL.parseJedec(Uint8Array.of(0xef, 0x40)) === null, '不足 3 字节 → null');
}

// ==================================================================== 2
console.log('== 2. SFDP 解析（布局照 Linux sfdp.h）==');
{
  const tbl = FL.makeSfdp(8 * 1024 * 1024);
  ok(hexOf(tbl.subarray(0, 4)) === '53 46 44 50', '签名 "SFDP"');
  const s = FL.parseSfdp(tbl.subarray(0, 8));
  ok(s.sigOk && s.nph === 1 && s.major === 1 && s.minor === 6, `头：rev=${s.revName} · ${s.nph} 个参数表头`);
  const s2 = FL.parseSfdp(tbl.subarray(0, 16));
  ok(s2.headers.length === 1, '能取到 1 个参数表头');
  const h = s2.headers[0];
  ok(h.idLsb === 0x00 && h.idMsb === 0x00 && h.id === 0, 'JEDEC BFPT 的 ID = 0x00');
  ok(h.name.includes('BFPT'), `表头名字：${h.name}`);
  ok(h.lengthDwords === 8 && h.ptr === 0x30 && h.major === 1 && h.minor === 6, `长度 ${h.lengthDwords} DWORD / 指针 0x${h.ptr.toString(16)}`);

  const bad = FL.parseSfdp(Uint8Array.of(0, 0, 0, 0, 0, 0, 0, 0));
  ok(bad && bad.sigOk === false && /dummy/.test(bad.text), '签名不对 → 明确提示"换 dummy 再试"');
  ok(FL.parseSfdp(Uint8Array.of(1, 2, 3)) === null, '不足 8 字节 → null');

  const dumps = FL.dumpDwords(tbl.subarray(0x30, 0x30 + 8));
  ok(dumps.length === 2 && /DWORD\s+1/.test(dumps[0]), `BFPT 原始 DWORD 可读（${dumps[0]}）`);
}

// ==================================================================== 3
console.log('== 3. 状态寄存器 ==');
{
  const busy = FL.parseStatus1(Uint8Array.of(0x03));
  ok(busy.busy && busy.wel && /BUSY/.test(busy.text) && /WEL/.test(busy.text), 'SR1 0x03 = BUSY + WEL');
  const prot = FL.parseStatus1(Uint8Array.of(0xfc));
  ok(prot.bp === 7 && prot.tb && prot.sec && prot.srp0 && !prot.busy, 'SR1 0xFC 的块保护位解出来');
  ok(FL.parseStatus2(Uint8Array.of(0x02)).qe === true, 'SR2 bit1 = QE');
  ok(/QE=0/.test(FL.parseStatus2(Uint8Array.of(0x00)).text), 'QE=0 时明确提示四线可能不出数据');
}

// ==================================================================== 4
console.log('== 4. 帧序列：读 ID / SFDP / 连续读拆帧 / 按页编程 / 擦除 ==');
{
  const id = FL.rdidItems();
  ok(id.length === 1 && xferOf(id[0]).cmd === 0x9f && xferOf(id[0]).rxLen === 3 && (id[0].flags & P.F.RSP), 'RDID：一条帧，cmd=0x9F rx=3 带 RSP');
  ok(xferOf(FL.sfdpHeadItems(1)[0]).dummy === 1 && xferOf(FL.sfdpHeadItems(1)[0]).addrLen === 3, 'SFDP 头：3 B 地址 + dummy');
  const t = FL.sfdpTableItems(0x30, 8);
  ok(xferOf(t[0]).rxLen === 32 && xferOf(t[0]).addr === 0x30, 'SFDP 表：按指针与长度读');

  // 连续读：1000 B → 492 + 492 + 16
  const r = FL.readItems(0x1000, 1000, { mode: FL.OP.QIOR });
  ok(r.length === 3, `1000 B 拆成 ${r.length} 帧`);
  const r0 = xferOf(r[0]), r1 = xferOf(r[1]), r2 = xferOf(r[2]);
  ok(r0.cmd === 0xeb && r0.cmdEn && r0.addrEn && r0.addr === 0x1000 && r0.lines === 4 && r0.dummy === 1, '首帧：cmd=0xEB + 地址 + 4 线 + dummy');
  ok((r[0].flags & P.F.CS_HOLD) && (r[1].flags & P.F.CS_HOLD), '前两帧 CS_HOLD（CS 一直摁着）');
  ok(!r1.cmdEn && r1.rxLen === 492 && r1.addrEn === false, '续读帧：不发 cmd/地址，只收数据');
  ok(!(r[2].flags & P.F.CS_HOLD) && (r[2].flags & P.F.CS_OFF) && r2.rxLen === 16, '末帧：CS_OFF 释放，且只收剩下的 16 B');
  ok(r.reduce((n, x) => n + xferOf(x).rxLen, 0) === 1000, '总长度对得上');
  ok(FL.readItems(0, 100, { mode: FL.OP.READ })[0].flags & P.F.CS_OFF, '单帧读也要收尾释放 CS');

  // 按页编程：不跨 256 B 边界，且页间插了等 tPP 的延时
  const p1 = FL.programItems(0x100, new Uint8Array(600));
  const pp = xfers(p1).filter(x => x.txLen > 0);
  ok(xfers(p1).filter(x => x.cmd === FL.OP.WREN).length === 3, '600 B → 3 页，每页前面都有 WREN');
  ok(pp.map(x => x.txLen).join(',') === '256,256,88', `页长 ${pp.map(x => x.txLen).join(',')}（不跨页）`);
  ok(p1.filter(x => x.type === P.T.DELAY).length === 2, '页与页之间插 2 条 DELAY（等 tPP）');
  const noDelay = FL.programItems(0x100, new Uint8Array(600), { pageDelayMs: 0 });
  ok(noDelay.filter(x => x.type === P.T.DELAY).length === 0, 'pageDelayMs=0 时不插延时');
  ok(FL.programPages(0x1f0, new Uint8Array(40)).map(p => p.data.length).join(',') === '16,24', 'programPages 从页中间起写会切在页尾');
  const p2 = FL.programItems(0x180, new Uint8Array(300));
  const pp2 = xfers(p2).filter(x => x.txLen > 0);
  ok(pp2[0].txLen === 128 && pp2[1].txLen === 172, `从 0x180 跨页起写：${pp2.map(x => x.txLen).join(',')}（第一笔只写到页尾）`);
  ok(xfers(FL.programItems(0x180, new Uint8Array(300), { quad: true })).some(x => x.cmd === FL.OP.QPP), 'quad 编程用 0x32');

  const e = FL.eraseItems(0x2000, { opcode: FL.OP.SE });
  ok(e.length === 2 && xfers(e)[0].cmd === 0x06 && xfers(e)[1].cmd === 0x20 && xfers(e)[1].addr === 0x2000, '扇区擦除 = WREN + 0x20(addr)');
  const ce = FL.eraseItems(0, { opcode: FL.OP.CE });
  ok(xfers(ce)[1].cmd === 0xc7 && xfers(ce)[1].addrEn === false, '整片擦除不带地址');
  let threw = false;
  try { FL.programItems(0, new Uint8Array(2 << 20)); } catch { threw = true; }
  ok(threw, '一次编程超过上限 → 抛错（别一次塞爆）');
}

// ==================================================================== 5
console.log('== 5. 真机路径在假探针上跑通（读 ID / SFDP / 状态）==');
{
  const { probe, host } = await mkProbe();
  ok(!!probe.flash, '假探针缺省就挂了一颗 NOR 模型');

  const rid = await host.send(FL.rdidItems());
  const j = FL.parseJedec(rid.rsps[0].data);
  ok(rid.rsps[0].status === P.ST.OK && j.vendor === 'Winbond' && j.sizeBytes === 16 << 20, `RDID 读回：${j.text}`);

  const sh = await host.send(FL.sfdpHeadItems(1));
  const sHead = FL.parseSfdp(sh.rsps[0].data);
  ok(sHead.sigOk && sHead.nph === 1, `SFDP 头读回：${sHead.text}`);
  const sp = await host.send(FL.sfdpParamItems(sHead.nph, 1));
  const sf = FL.parseSfdp(sp.rsps[0].data);
  ok(sf.headers.length === 1 && sf.headers[0].name.includes('BFPT'), `参数表头：${sf.headers[0]?.name}`);

  const st = await host.send(FL.sfdpTableItems(sf.headers[0].ptr, sf.headers[0].lengthDwords));
  ok(st.rsps[0].data.length === 32, `BFPT 读回 ${st.rsps[0].data.length} B`);
  const dw = FL.dumpDwords(st.rsps[0].data);
  ok(dw.length === 8, `解析出 ${dw.length} 个 DWORD`);

  const sr = await host.send(FL.rdsr1Items());
  ok(FL.parseStatus1(sr.rsps[0].data).busy === false, '空闲时 SR1 的 BUSY=0');
}

// ==================================================================== 6
console.log('== 6. 器件模型按 NOR 的规矩答（WEL / BUSY / 1→0 / 跨页 / QE）==');
{
  const { probe, host } = await mkProbe();
  const dev = probe.flash;

  // 6.1 没 WREN 就擦除 → 器件拒绝（只发擦除帧，不带 WREN）
  let r = await host.send([FL.writeFrame({ opcode: FL.OP.SE, addr: 0, addrLen: 3 })]);
  ok(probe.flashNotes.some(n => /擦除前要先 WREN/.test(n)), '没 WREN 先擦 → 模型明确拒绝（真芯片是静默忽略）');
  ok(FL.parseStatus1((await host.send(FL.rdsr1Items())).rsps[0].data).busy === false, '被拒的擦除没有产生 BUSY');

  // 6.2 正常擦除 → BUSY 起来，轮询到清掉
  r = await host.send(FL.eraseItems(0x1000, { opcode: FL.OP.SE }));
  ok(r.rsps.every(x => !x || x.status === P.ST.OK), 'WREN + SE 都回了 OK');
  const busySr = FL.parseStatus1((await host.send(FL.rdsr1Items())).rsps[0].data);
  ok(busySr.busy === true, '擦除后 SR1 的 BUSY=1（页面必须轮询）');
  const ready = await host.waitReady();
  ok(ready.busy === false && ready.wel === false, '轮询到 BUSY 清掉，且 WEL 自动归零');

  // 6.3 编程 → 读回一致
  const data = new Uint8Array(300);
  for (let i = 0; i < data.length; i++) data[i] = (i * 3 + 5) & 0xff;
  await host.send(FL.programItems(0x1000, data));
  await host.waitReady();
  const back = await host.send(FL.readItems(0x1000, 300, { mode: FL.OP.READ }));
  const got = new Uint8Array(300);
  let off = 0;
  for (const x of back.rsps){ if (x?.data){ got.set(x.data, off); off += x.data.length; } }
  ok(off === 300 && got.every((v, i) => v === data[i]), `按页写入 300 B 读回逐字节一致（收齐 ${off} B）`);

  // 6.3b 不等 tPP 连塞两页 → 第二页被器件丢掉（真芯片同样会忽略 BUSY 期间的命令）
  probe.flashNotes.length = 0;
  await host.send(FL.programItems(0x1400, new Uint8Array(600), { pageDelayMs: 0 }));
  ok(probe.flashNotes.some(n => /BUSY/.test(n)), '不等 tPP 连写两页 → 第二页被忽略（序列里必须插延时）');
  await host.waitReady();

  // 6.4 NOR 只能 1→0：不擦就覆写 → 得到 AND
  const again = new Uint8Array(16).fill(0x0f);
  await host.send(FL.programItems(0x1000, again));
  await host.waitReady();
  const andBack = await host.send(FL.readItems(0x1000, 16, { mode: FL.OP.READ }));
  ok((andBack.rsps[0].data[0] & 0x0f) === (data[0] & 0x0f), `不擦就写 = 与旧值相与（0x${data[0].toString(16)} & 0x0f = 0x${andBack.rsps[0].data[0].toString(16)}）`);

  // 6.5 跨页编程 → 模型拒绝
  probe.flashNotes.length = 0;
  await host.send([FL.cmdFrame(FL.OP.WREN), FL.writeFrame({ opcode: FL.OP.PP, addr: 0x10f0, addrLen: 3, data: new Uint8Array(32) })]);
  ok(probe.flashNotes.some(n => /跨页/.test(n)), '跨页编程 → 模型报"跨页"（真芯片会在页内回绕，更坑）');

  // 6.6 QE=0 时四线读拿不到数据
  probe.flashNotes.length = 0;
  const q = await host.send(FL.readItems(0, 32, { mode: FL.OP.QIOR }));
  ok(probe.flashNotes.some(n => /QE=0/.test(n)), 'QE=0 时四线读 → 明确提示（这是真机上最常见的"四线不出数"原因）');
  dev.sr2 = 0x02;
  probe.flashNotes.length = 0;
  const q2 = await host.send(FL.readItems(0, 32, { mode: FL.OP.QIOR }));
  ok(!probe.flashNotes.some(n => /QE=0/.test(n)) && q2.rsps[0].data.length === 32, '置 QE 后四线读正常');

  // 6.7 线数不匹配 → 提示
  probe.flashNotes.length = 0;
  await host.send([FL.readFrame({ opcode: FL.OP.QIOR, addr: 0, addrLen: 3, dummy: 1, lines: 1, rx: 4 })]);
  ok(probe.flashNotes.some(n => /要求 4 线/.test(n)), '用 0xEB 却只给 1 线 → 明确提示');

  // 6.8 续读帧没有首帧 → 提示
  probe.flashNotes.length = 0;
  await host.send([{ type: P.T.XFER, payload: P.xferPayload({ tcfg: P.TC.LINES_4, rxLen: 8 }), flags: P.F.RSP | P.F.CS_OFF, label: '裸续读' }]);
  ok(probe.flashNotes.some(n => /续读但没有/.test(n)), '没有首帧的续读 → 提示');
}

// ==================================================================== 7
console.log('== 7. 连续读真的把地址接下去了（跨帧不断）==');
{
  const { probe, host } = await mkProbe();
  // 先写一段有规律的数据，再连续读回来（首帧 + 两帧续读）
  const data = new Uint8Array(1000);
  for (let i = 0; i < data.length; i++) data[i] = (i * 7) & 0xff;
  await host.send([FL.cmdFrame(FL.OP.WREN), FL.cmdFrame(FL.OP.CE)]);
  await host.waitReady();
  await host.send(FL.programItems(0x2000, data));
  await host.waitReady();
  const r = await host.send(FL.readItems(0x2000, 1000, { mode: FL.OP.READ }));
  const got = new Uint8Array(1000);
  let off = 0;
  for (const x of r.rsps){ if (x?.data){ got.set(x.data, off); off += x.data.length; } }
  ok(off === 1000, `三帧收齐 ${off} B（续读帧的地址由器件自己往下数）`);
  ok(got.every((v, i) => v === data[i]), '连读回来的 1000 B 逐字节一致');
  const frames = probe.flash.log.filter(x => /^(读|续读)/.test(x));
  ok(frames.length === 3, `器件侧确实是 3 笔（1 首发命令 + 2 笔续读）：${frames.join(' / ')}`);
}

// ==================================================================== 8
console.log('== 8. 没把老功能踩坏：全双工仍走回环 ==');
{
  const { probe, host } = await mkProbe({ faults: { loopback: true } });
  const tx = Uint8Array.of(0x11, 0x22, 0x33, 0x44);
  const r = await host.send([{
    type: P.T.XFER, payload: P.xferPayload({ tcfg: P.TC.LINES_1, tx, rxLen: 4 }),
    flags: P.F.RSP, label: '回环',
  }]);
  ok(r.rsps[0].status === P.ST.OK && hexOf(r.rsps[0].data) === '11 22 33 44', '全双工帧仍由回环模型答（回环自检不受影响）');
  ok(probe.flash.log.every(x => !/续读/.test(x)), '这类帧没有被器件模型碰过');
}

console.log(`\n${fail ? '❌' : '✅'} spi-flash.test: ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
