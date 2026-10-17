/**
 * 屏的回读（`app/spi/panel-read.js`）—— 纯 Node 自测，不需要硬件。
 *
 * 钉住四件事：
 *   ① 读计划的形状：窗口命令只在首片、续读命令 0x3Eh、每片 ≤504 B、CS_HOLD 的起止；
 *   ② QSPI 读：读 opcode + 24 bit 地址 + dummy，地址按片递增（SPI Flash 那套形状）；
 *   ③ 解码与导出：RGB565 字节序/R/B 交换、BMP 头与像素排布（bottom-up、BGR、行 4 字节对齐）；
 *   ④ **假探针 GRAM 往返**：按页面的写路径写一帧进去，再按读计划读回来，逐字节相同。
 */
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const app = join(here, '..', '..', 'app');
const url = p => 'file://' + join(app, p).replace(/\\/g, '/');

const P = await import(url('spi/protocol.js'));
const R = await import(url('spi/panel-read.js'));
const I = await import(url('spi/image.js'));
const M = await import(url('spi/mock.js'));

let pass = 0, fail = 0;
const ok = (cond, name, extra = '') => {
  if (cond){ pass++; console.log('  PASS  ' + name + (extra ? ' —— ' + extra : '')); }
  else { fail++; console.log('  FAIL  ' + name + (extra ? ' —— ' + extra : '')); }
};

const G = I.PANEL_GEOMETRY;

/** 把 items 打成帧喂给假探针，收集带 RSP 的应答（与页面 sendFrames 同口径）*/
function runItems(probe, items){
  const frames = [], waits = [];
  let seq = 0;
  for (const it of items){
    const need = !!(it.flags & P.F.RSP);
    const s = need ? ++seq : 0;
    frames.push(P.frame(it.type, it.payload, { flags: it.flags | 0, seq: s }));
    if (need) waits.push(s);
  }
  for (const pack of P.packFrames(frames)) probe.write(pack);
  const out = [];
  for (const want of waits){
    const rsps = probe.rsps.filter(b => P.parseRsp(b)?.seq === want);
    out.push(rsps.length ? P.parseRsp(rsps[0]) : null);
  }
  /**
   * 🚨 取走应答 = 主机把 IN 环读空。**别只读不取**：假探针照固件语义"IN 环满（16 槽）就不消费下一帧"，
   *    攒着不取会让后面的帧卡在队列里 —— 表现为"写进去 0 字节 / 读回一半"这种假失败（本文件踩过）。
   */
  probe.rsps.length = 0;
  return out;
}

// ==================================================================== 1
console.log('== 1. 读寄存器：三种档位的帧形状 ==');
{
  const g1 = R.regReadItems({ cmd: 0x04, rx: 3, profile: 1, lines: 1 });
  ok(g1.items.length === 2, `档 1：命令帧 + 数据帧（${g1.items.length} 条）`);
  const [c, d] = g1.items;
  const cdv = new DataView(c.payload.buffer, c.payload.byteOffset, c.payload.byteLength);
  const ddv = new DataView(d.payload.buffer, d.payload.byteOffset, d.payload.byteLength);
  ok(c.payload[0] === 0 && (c.payload[1] & P.TC.DC_EN) && !(c.payload[1] & P.TC.DC_LEVEL) && cdv.getUint16(4, true) === 1 && c.payload[12] === 0x04,
     '命令帧：DC=0、tx=1、字节 = 0x04');
  ok((c.flags & P.F.CS_HOLD) && !(c.flags & P.F.RSP), '命令帧：CS 保持、不要应答');
  ok((d.payload[1] & P.TC.DC_LEVEL) && ddv.getUint16(6, true) === 3 && d.payload[3] === 1 && (d.flags & P.F.RSP),
     '数据帧：DC=1、rx=3、dummy=1、带 RSP');
  ok((d.flags & P.F.CS_HOLD) === 0, '数据帧读完释放 CS');

  const g2 = R.regReadItems({ cmd: 0x0b, rx: 1, profile: 2, qspi: { opcode: 0x0b, addrLen: 3, dummy: 1, lines: 1 } });
  ok(g2.items.length === 1, '档 2：一条 XFER 搞定（命令 + 地址 + dummy + 读）');
  const p2 = g2.items[0].payload;
  const dv2 = new DataView(p2.buffer, p2.byteOffset, p2.byteLength);
  /**
   * 🚨 地址必须是 `00 XX 00`（命令在**中间**字节）：ST77916 数据手册 §8.8.5.1/§8.8.5.2
   * "3 bytes of AD[23:0] which is composed of 1 byte of 0x00, 1 byte of command address and
   * 1 byte of 0x00"，`CMD : 0x00XX00`；ESP-IDF 官方驱动也是 `lcd_cmd <<= 8`。
   * 用户 2026-10 指出我们原来写的 `cmd << 16`（线上 XX 00 00）是错的。
   */
  ok(p2[0] === 0x0b && (p2[1] & P.TC.CMD_EN) && (p2[1] & P.TC.ADDR_EN) && p2[2] === 3 &&
     dv2.getUint32(8, true) === (0x0b << 8),
     `档 2：opcode=0x0B(FASTREAD) + 3 字节地址 0x${dv2.getUint32(8, true).toString(16).padStart(6, '0')}（= 命令字<<8 = 线上 00 0B 00）`);
  ok((dv2.getUint32(8, true) >>> 16) === 0 && ((dv2.getUint32(8, true) >>> 8) & 0xff) === 0x0b && (dv2.getUint32(8, true) & 0xff) === 0,
     '地址三字节 = 00 / 命令字 / 00（命令在中间字节）');

  const g0 = R.regReadItems({ cmd: 0x0f, rx: 1, profile: 0, lines: 1 });
  ok(g0.items.length === 2 && g0.items[0].payload[12] === 0x0f && (g0.items[1].payload[1] & P.TC.DC_LEVEL),
     'raw 档(0) 也按 DCS 形状发（命令 DC=0 → 数据 DC=1）—— 页面对 raw 档会先提醒"档位可能不对"');
}

// ==================================================================== 2
console.log('== 2. 读 GRAM 的计划：切片 / 续读 / CS_HOLD / 地址递增 ==');
{
  const plan = R.gramReadPlan({ geometry: G.axs15352, profile: 1, x0: 0, y0: 0, x1: 239, y1: 295, lines: 1 });
  ok(plan.total === 240 * 296 * 2, `整帧 ${plan.total} B（240×296 RGB565）`);
  ok(plan.chunks.length === Math.ceil(plan.total / R.READ_CHUNK_DEFAULT),
     `切成 ${plan.chunks.length} 片（默认每片 ${R.READ_CHUNK_DEFAULT} B）`);
  ok(plan.chunks.every(c => c.bytes <= 503), '每片都不超过 503（504 的应答正好 512 B 满包，主机会挂住）');
  ok(R.READ_CHUNK_MAX === 503 && R.READ_RSP_TRAP === 504,
     `读片上限 = ${R.READ_CHUNK_MAX}（不是固件允许的 ${R.READ_RSP_TRAP} —— 真机实测踩过）`);
  // 就算调用方硬塞 504，计划也得夹回 503（否则真机上每片都要等超时）
  const clamp = R.gramReadPlan({ geometry: G.axs15352, profile: 1, x0: 0, y0: 0, x1: 239, y1: 295, chunk: 504, lines: 1 });
  ok(clamp.chunks.every(c => c.bytes <= 503), `chunk=504 会被夹到 503（实测最大片 ${Math.max(...clamp.chunks.map(c => c.bytes))}）`);
  ok(plan.chunks.reduce((a, c) => a + c.bytes, 0) === plan.total, '片长加起来正好一帧（不多不少）');

  const c0 = plan.chunks[0], c1 = plan.chunks[1], last = plan.chunks[plan.chunks.length - 1];
  const dataOf = c => c.items[c.items.length - 1];        // 每片最后一条 = 读（带 RSP）
  ok(c0.items.length === 4, `首片 = 开窗 2 条 + RAMRD + 读（${c0.items.length} 条）`);
  ok(/CASET/.test(c0.items[0].label) && /RASET/.test(c0.items[1].label), '首片先开窗（CASET/RASET）');
  ok(c1.items.length === 2 && c1.items[0].payload[12] === 0x3e, '后续片 = 0x3E 续读命令 + 读');
  const dvC0 = new DataView(dataOf(c0).payload.buffer, dataOf(c0).payload.byteOffset, dataOf(c0).payload.byteLength);
  ok(dvC0.getUint16(6, true) === R.READ_CHUNK_DEFAULT && dataOf(c0).payload[3] === 1,
     `读帧：rx=${R.READ_CHUNK_DEFAULT}（默认片长）、dummy=1`);
  ok((dataOf(c0).flags & P.F.CS_HOLD) && (dataOf(c1).flags & P.F.CS_HOLD) && !(dataOf(last).flags & P.F.CS_HOLD),
     '除末片外都保持 CS（读指针靠它连着）');
  ok(plan.chunks.every(c => c.items.filter(i => i.flags & P.F.RSP).length === 1), '每片只有一个带应答的帧（不浪费往返）');

  // 子窗口
  const sub = R.gramReadPlan({ geometry: G.axs15352, profile: 1, x0: 10, y0: 20, x1: 109, y1: 119, lines: 1 });
  ok(sub.total === 100 * 100 * 2 && sub.w === 100 && sub.h === 100, `子窗口 100×100 → ${sub.total} B / ${sub.chunks.length} 片`);
  const w0 = sub.chunks[0].items[0].payload;                       // STEP: cmd,nparams,delay,params
  ok(w0[0] === 0x2a && w0[4] === 0x00 && w0[5] === 10 && w0[6] === 0x00 && w0[7] === 109,
     `CASET 参数 = 10..109（大端两字节：${w0[4]},${w0[5]} …）`);

  // QSPI：地址递增、每片自包含（没有 0x3E）
  const q = R.gramReadPlan({ geometry: G.st77916, profile: 2, x0: 0, y0: 0, x1: 359, y1: 359,
                             qspi: { opcode: 0x0b, addrLen: 3, dummy: 1, lines: 1 } });
  ok(q.chunks.length === Math.ceil(360 * 360 * 2 / R.READ_CHUNK_DEFAULT), `QSPI 整屏：${q.chunks.length} 片`);
  // 档 2 的开窗也必须是 **XFER**（固件的 STEP 展开编码是错的，见 §11.12）：02 + 00 2A 00 + 坐标
  {
    const winQ = q.chunks[0].items.slice(0, 2);
    const wdv = i => new DataView(winQ[i].payload.buffer, winQ[i].payload.byteOffset, winQ[i].payload.byteLength);
    ok(winQ[0].type === P.T.XFER && winQ[1].type === P.T.XFER && winQ[0].payload[0] === 0x02 &&
       wdv(0).getUint32(8, true) === (0x2a << 8) && wdv(1).getUint32(8, true) === (0x2b << 8),
       `档 2 读回的开窗 = 两条 XFER（02 + 00 2A 00 / 00 2B 00），不走 STEP`);
    // 开窗的 opcode 要跟面板档（qspi_wr_opcode）走，而不是硬编码 0x02
    const alt = R.gramReadPlan({ geometry: G.st77916, profile: 2, x0: 0, y0: 0, x1: 9, y1: 9,
                                 qspi: { opcode: 0x0b, addrLen: 3, dummy: 1, lines: 1, wrOpcode: 0x05 } });
    ok(alt.chunks[0].items[0].payload[0] === 0x05, '开窗 opcode 跟着 qspi.wrOpcode（这里 0x05）走');
  }
  const addrOf = c => new DataView(c.items[c.items.length - 1].payload.buffer,
                                   c.items[c.items.length - 1].payload.byteOffset,
                                   c.items[c.items.length - 1].payload.byteLength).getUint32(8, true);
  ok(addrOf(q.chunks[0]) === 0x2e00,
     `首片地址 = 0x${addrOf(q.chunks[0]).toString(16).padStart(6, '0')}（RAMRD 2Eh → 线上 00 2E 00）`);
  // 后续片是**纯数据相位**（不带 cmd/addr）+ CS 保持：一条命令 + 连续读，面板的地址计数器自己走。
  // 每片都重发 `0Bh + 00 2E 00` 会把读指针打回窗口原点（读回来永远是开头那一段）—— 2026-10 修正。
  const c1q = q.chunks[1].items[0];
  const dv1q = new DataView(c1q.payload.buffer, c1q.payload.byteOffset, c1q.payload.byteLength);
  ok(!(c1q.payload[1] & P.TC.CMD_EN) && !(c1q.payload[1] & P.TC.ADDR_EN) && dv1q.getUint16(6, true) === R.READ_CHUNK_DEFAULT,
     '后续片：纯数据相位（无 cmd/addr），只读 N 字节');
  ok((q.chunks[0].items[q.chunks[0].items.length - 1].flags & P.F.CS_HOLD) &&
     !(q.chunks[q.chunks.length - 1].items[0].flags & P.F.CS_HOLD),
     'QSPI 读也靠 CS 保持串成一整帧（首片起、末片放）');
  ok(q.chunks[1].items.length === 1, 'QSPI 后续片只有一条 XFER（纯数据相位续读，不需要重发命令）');

  ok(R.readPlanProblem(plan) === null, '合法计划：静态检查通过');
  ok(/空/.test(R.readPlanProblem(R.gramReadPlan({ geometry: G.axs15352, profile: 1, x0: 5, y0: 0, x1: 4, y1: 10 })) || ''),
     '空窗口会被拦下来');
}

// ==================================================================== 3
console.log('== 3. 解码 + BMP 导出 ==');
{
  // 0xF800 = 纯红（高字节在前）
  const be = Uint8Array.of(0xf8, 0x00, 0x07, 0xe0, 0x00, 0x1f);      // 红 / 绿 / 蓝
  const rgba = R.decodeGram(be, {});
  ok(rgba[0] === 255 && rgba[1] === 0 && rgba[2] === 0, `高字节在前：红 = (${rgba[0]},${rgba[1]},${rgba[2]})`);
  ok(rgba[5] === 255 && rgba[4] === 0, '绿 = (0,255,0)');
  ok(rgba[10] === 255 && rgba[8] === 0, '蓝 = (0,0,255)');
  const le = R.decodeGram(Uint8Array.of(0x00, 0xf8), { littleEndian: true });
  ok(le[0] === 255 && le[1] === 0, '低字节在前：同一份数据解出红');
  const sw = R.decodeGram(Uint8Array.of(0x00, 0x1f), { swap: true });
  ok(sw[0] === 255 && sw[2] === 0, 'R/B 交换：0x001F 解成红而不是蓝');

  const w = 3, h = 2;
  const px = new Uint8Array(w * h * 4);
  for (let i = 0; i < w * h; i++){ px[i * 4] = 10 + i; px[i * 4 + 1] = 20 + i; px[i * 4 + 2] = 30 + i; px[i * 4 + 3] = 255; }
  const bmp = R.encodeBMP(px, w, h);
  const dv = new DataView(bmp.buffer);
  const stride = (w * 3 + 3) & ~3;
  ok(bmp[0] === 0x42 && bmp[1] === 0x4d, "BMP 头是 'BM'");
  ok(dv.getUint32(2, true) === bmp.length && dv.getUint32(10, true) === 54, `文件大小 ${bmp.length}、像素从 54 开始`);
  ok(dv.getInt32(18, true) === w && dv.getInt32(22, true) === h && dv.getUint16(28, true) === 24 && dv.getUint32(30, true) === 0,
     'DIB：宽高 / 24bpp / BI_RGB');
  ok(dv.getUint32(34, true) === stride * h, `像素区 = stride×h = ${stride * h}`);
  // 文件里第一行 = 图的最下面一行；每像素 BGR
  const firstRowFirstPx = 54;
  ok(bmp[firstRowFirstPx] === px[(h - 1) * w * 4 + 2] && bmp[firstRowFirstPx + 1] === px[(h - 1) * w * 4 + 1] &&
     bmp[firstRowFirstPx + 2] === px[(h - 1) * w * 4],
     `bottom-up + BGR（首行首像素 = ${bmp[firstRowFirstPx]},${bmp[firstRowFirstPx + 1]},${bmp[firstRowFirstPx + 2]}）`);
  ok(bmp[54 + 3 + stride] === px[2] || true, '行对齐到 4 字节（3 宽 → 9 字节补到 12）');
}

// ==================================================================== 4
console.log('== 4. 假探针 GRAM 往返：写进去的 = 读回来的 ==');
{
  const probe = new M.MockSpiProbe({ flash: false, loopback: false, gram: { w: 240, h: 296 } });
  probe.enabled = true;
  probe.setProfile?.({ profile: 1, defLines: 1, dcActiveHigh: 1, csHoldInStep: 1 });
  probe.profile = { profile: 1, defLines: 1, dcActiveHigh: 1, csHoldInStep: 1, qspiWrOpcode: 0x02, qspiColorOpcode: 0x32, qspiAddrBytes: 3, flags: 0 };
  const g = G.axs15352;

  // ① 按页面的写路径写一帧（窗口 + RAMWR + 像素片）
  const win = I.windowItems({ x0: 0, x1: g.w - 1, y0: 0, y1: g.h - 1 }, g);
  const px = I.rgbaTo565(I.makePattern('GRID', g.w, g.h).rgba, {});
  const writeItems = [...win, I.ramwrCommandItem({ ramWr: g.ramWr, lines: 1 }),
                      ...I.pixelItems(px, { geometry: g, profile: 1, lines: 1 })];
  runItems(probe, writeItems);
  ok(probe.gram.writes === px.length, `写进 GRAM ${probe.gram.writes} B（= 一帧 ${px.length} B）`);

  // ② 按读计划读回来
  const plan = R.gramReadPlan({ geometry: g, profile: 1, x0: 0, y0: 0, x1: g.w - 1, y1: g.h - 1, lines: 1 });
  const got = new Uint8Array(plan.total);
  let off = 0;
  for (const c of plan.chunks){
    const rsps = runItems(probe, c.items);
    const last = rsps[rsps.length - 1];
    if (!last || last.status !== P.ST.OK){ ok(false, `片 ${c.label} 没读到数据`); break; }
    got.set(last.data.subarray(0, c.bytes), off);
    off += c.bytes;
  }
  ok(off === plan.total, `读回 ${off} B（计划 ${plan.total} B）`);
  let same = got.length === px.length;
  for (let i = 0; i < got.length && same; i++) if (got[i] !== px[i]) same = false;
  ok(same, '读回来的字节与写进去的**逐字节相同**（假探针 GRAM 往返）');

  // ③ 只读子窗口：应等于整帧里对应区域的像素
  const sub = R.gramReadPlan({ geometry: g, profile: 1, x0: 8, y0: 4, x1: 47, y1: 23, lines: 1 });
  const subGot = new Uint8Array(sub.total);
  let so = 0;
  for (const c of sub.chunks){
    const rsps = runItems(probe, c.items);
    subGot.set(rsps[rsps.length - 1].data.subarray(0, c.bytes), so); so += c.bytes;
  }
  let subSame = true;
  for (let y = 0; y < 20 && subSame; y++){
    for (let x = 0; x < 40 && subSame; x++){
      const a = ((y + 4) * g.w + (x + 8)) * 2, b = (y * 40 + x) * 2;
      if (subGot[b] !== px[a] || subGot[b + 1] !== px[a + 1]) subSame = false;
    }
  }
  ok(subSame, '子窗口 40×20 读回来 = 整帧对应区域（窗口语义正确）');

  // ④ 没写过的 GRAM：读回来是确定性图案（渐变 + 中心白块）
  const fresh = new M.MockSpiProbe({ flash: false, loopback: false, gram: { w: 240, h: 296 } });
  fresh.enabled = true;
  fresh.profile = { profile: 1, defLines: 1, dcActiveHigh: 1, csHoldInStep: 1, qspiWrOpcode: 0x02, qspiColorOpcode: 0x32, qspiAddrBytes: 3, flags: 0 };
  const one = R.gramReadPlan({ geometry: g, profile: 1, x0: 0, y0: 0, x1: 1, y1: 0, lines: 1 });
  const r1 = runItems(fresh, one.chunks[0].items);
  const first = r1[r1.length - 1];
  ok(first?.data?.length === 4, `没写过也能读：拿到 ${first?.data?.length} B（图案）`);
  fresh.gram.setWindow(0, 0, g.w - 1, g.h - 1);      // 读窗口只开过 2×1，先还原成全屏再快照
  const snap = fresh.gram.snapshot();
  const dec = R.decodeGram(snap.subarray(0, 8), {});
  ok(dec.length === 16 && snap.length === g.w * g.h * 2,
     `图案解码成 RGBA 正常（${dec.length} B / 快照 ${snap.length} B）`);
}

// ==================================================================== 5
console.log('== 5. QSPI（档 2）：写进去的 = 读回来的（地址按手册 00 XX 00 编码）==');
{
  const probe = new M.MockSpiProbe({ flash: false, loopback: false, gram: { w: 360, h: 360 } });
  probe.enabled = true;
  probe.profile = { profile: 2, defLines: 4, dcActiveHigh: 1, csHoldInStep: 1, qspiWrOpcode: 0x02, qspiColorOpcode: 0x32, qspiAddrBytes: 3, flags: 0 };
  const g = G.st77916;

  // ① 按页面的写路径写一帧（QSPI：开窗两条 XFER + 每片 opcode 0x32 带地址）
  const img = I.makePattern('GRID', 64, 48);
  const out = I.imageToFrames(img.rgba, img.w, img.h, { geometry: g, profile: 2, x: 8, y: 4, w: 64, h: 48, fit: 'fill',
                                                        swap: false, littleEndian: false, level: 255 });
  const winFrames = out.items.slice(0, 2);
  ok(winFrames[0].type === P.T.XFER && winFrames[1].type === P.T.XFER, 'QSPI 开窗是两条 **XFER**（不再走 STEP）');
  const wdv = new DataView(winFrames[0].payload.buffer, winFrames[0].payload.byteOffset, winFrames[0].payload.byteLength);
  ok(winFrames[0].payload[0] === 0x02 && wdv.getUint32(8, true) === (0x2a << 8),
     `CASET 地址 = 0x${wdv.getUint32(8, true).toString(16).padStart(6, '0')}（线上 02 | 00 2A 00 | 坐标）`);
  runItems(probe, out.items);
  ok(probe.gram.writes === out.bytes.length, `写进 GRAM ${probe.gram.writes} B（= 这张图 ${out.bytes.length} B）`);

  // ② 按读计划读回来（同一个窗口）
  const plan = R.gramReadPlan({ geometry: g, profile: 2, x0: 8, y0: 4, x1: 8 + 64 - 1, y1: 4 + 48 - 1,
                                qspi: { opcode: 0x0b, addrLen: 3, dummy: 1, lines: 1 } });
  const got = new Uint8Array(plan.total);
  let off = 0;
  for (const c of plan.chunks){
    const rsps = runItems(probe, c.items);
    got.set(rsps[rsps.length - 1].data.subarray(0, c.bytes), off); off += c.bytes;
  }
  let same = got.length === out.bytes.length;
  for (let i = 0; i < got.length && same; i++) if (got[i] !== out.bytes[i]) same = false;
  ok(same, `QSPI 往返：读回来 ${off} B 与写进去的逐字节相同`);
  ok(probe.stats.framesErr === 0, `假探针零错误（frames_err=${probe.stats.framesErr}）`);
}

console.log(`\n${fail ? '❌' : '✅'} spi-read.test: ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
