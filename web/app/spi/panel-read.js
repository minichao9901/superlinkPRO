/**
 * 屏的回读 —— 读寄存器 + 读 GRAM（把屏上现在的画面读回来）。
 *
 * 为什么要单开一个模块：读是"发图"的逆向，但**两条档位的时序完全不一样**，而且都有坑：
 *
 * 档 1（4 线 SPI + DC，如 AXS15352）—— 走 MIPI DCS 的读流程：
 *   ① `CASET(2Ah)` + `RASET(2Bh)` 开窗；
 *   ② `RAMRD(2Eh)` 命令（DC=0）—— **CS 必须保持**，面板随后在数据相位把像素吐出来（DC=1）；
 *   ③ 数据相位读 N 字节（`XFER` 的 `rx_len`），**先 1 个 dummy 字节**（DCS 规定，第一字节无效）；
 *   ④ 还要接着读就发 `RAMRDC(3Eh)` 续读（读指针自己往下走，不用重新开窗）。
 *   一个 `XFER` 最多读 504 B（`rx_len ≤ FRAME_MAX`），所以一帧要切成很多片。
 *
 * 档 2（QSPI，如 ST77916）—— 跟 **SPI Flash 一模一样的形状**：
 *   一条 XFER 里带 命令相位（读 opcode，默认 `03h`）+ 24 bit 地址 + dummy + 读 N 字节，
 *   地址随片递增（RAMRD 的地址 = `0x002E00` —— `00 2E 00`，与写侧 `0x002C00` 同一个编码）。
 *   读 opcode / 地址 / dummy / 线数**都可配**（厂家之间不统一，见下方 `QSPI_READ_DEFAULT`）。
 *
 * 参考：`E:\Datasheets\05_显示_与_MIPI\03_LCD_Driver\01_sitronix\`（ST7796S / ST77916 等）。
 * 已知的口径（MIPI DCS 标准读命令，各厂一致）：
 *   `04h` RDDID(3B) · `09h` RDDST(4B) · `0Ah` RDDPM · `0Bh` RDDMADCTL · `0Ch` RDDCOLMOD ·
 *   `0Dh` RDDIM · `0Eh` RDDSM · `0Fh` RDDSDR（各 1B）· `DAh/DBh/DCh` RDID1..3 · `D3h` RDID4（ST 常见 4B）。
 *   读出来的第一字节是 **dummy**（`dummy=1` 时），所以「读回 3 字节 ID」实际要读 4 字节。
 */
import { T, F, TC, xferPayload, FRAME_MAX } from './protocol.js';
import { windowItems } from './image.js';

/** 读寄存器用的表：`rx` 是**数据字节数**（不含 dummy）*/
export const REG_READS = [
  { name: 'RDDID 04h —— 显示 ID（3 B）', cmd: 0x04, rx: 3 },
  { name: 'RDDST 09h —— 显示状态（4 B）', cmd: 0x09, rx: 4 },
  { name: 'RDDPM 0Ah —— 电源模式（1 B）', cmd: 0x0a, rx: 1 },
  { name: 'RDDMADCTL 0Bh —— 扫描方向（1 B）', cmd: 0x0b, rx: 1 },
  { name: 'RDDCOLMOD 0Ch —— 像素格式（1 B）', cmd: 0x0c, rx: 1 },
  { name: 'RDDIM 0Dh —— 图像模式（1 B）', cmd: 0x0d, rx: 1 },
  { name: 'RDDSM 0Eh —— 信号模式（1 B）', cmd: 0x0e, rx: 1 },
  { name: 'RDDSDR 0Fh —— 自诊断（1 B）', cmd: 0x0f, rx: 1 },
  { name: 'RDID1 DAh —— 厂家 ID（1 B）', cmd: 0xda, rx: 1 },
  { name: 'RDID2 DBh —— 模组/版本（1 B）', cmd: 0xdb, rx: 1 },
  { name: 'RDID3 DCh —— 驱动版本（1 B）', cmd: 0xdc, rx: 1 },
  { name: 'RDID4 D3h —— ST 常见的第 4 个 ID（4 B）', cmd: 0xd3, rx: 4 },
  { name: '自定义…', cmd: null, rx: 1 },
];

/** QSPI 读的默认时序 —— 照 **ST77916 数据手册 §8.8.5.2**（Read command mode）：
 *   "host needs to send 1 byte of write command instruction (**0x0B**). Then host sends 3 bytes of
 *    AD[23:0] which is composed of 1 byte of 0x00, **1 byte of command address** and 1 byte of 0x00"
 *   读 opcode 是 **0x0B（FASTREAD）**，不是 SPI Flash 那套 0x03；地址同样是 `00 XX 00`（命令在中间字节）。
 *   （写侧同族：§8.8.5.1 的 0x02 / 0xA2 / 0x32 / 0x38 + `CMD : 0x00XX00`。）*/
export const QSPI_READ_DEFAULT = {
  opcode: 0x0b,      // 读命令相位（FASTREAD）
  wrOpcode: 0x02,    // 命令写 opcode —— 档 2 的**开窗命令**用它（面板档里的 qspi_wr_opcode）
  addrLen: 3,        // 地址字节数（QSPI 屏惯例 3）
  dummy: 1,          // dummy 周期（1 = 1 字节；0 = 不加）
  lines: 1,          // 读相位线数：多数屏读只走 1 线（QSPI 读要 4 线的话填 4）
  addrQuad: false,   // 地址相位是否 4 线
  baseAddr: 0x2e00,  // RAMRD(2Eh) 的地址 = 0x00 2E 00
};

/** 档 1 的默认时序 */
export const DCS_READ_DEFAULT = {
  ramrdCmd: 0x2e,    // RAMRD
  contCmd: 0x3e,     // RAMRDC（续读）
  dummy: 1,          // 数据相位前的 dummy 字节
};

/**
 * 一片最多读多少字节。
 *
 * 🚨 **不是 504，是 503**（2026-10 真机定标，AXS15352 + akaLinkPro）：
 *    应答包 = 8 B 头 + 读到的 N 字节；N = 504 时正好 **512 B = 一个满的 HS bulk 包**，
 *    而主机的 `transferIn` 是"**收满请求长度** 或 **收到短包**"才收尾 —— 满包后面没有短包，
 *    于是这一笔永远等不到应答（实测：504 B 超时，503 B 立刻回来）。
 *    固件那边 `rx_len ≤ SB_FRAME_MAX(504)` 是允许的，所以这个坑只有真机才撞得到：
 *    **读片 ≤ 503**（`PKT - 8 - 1`）。
 */
export const READ_CHUNK_MAX = 503;                    // = PKT(512) - RSP_HDR(8) - 1
export const READ_RSP_TRAP = 504;                     // 撞坑的那个值，写在这里给自测用

/** 每片读的默认字节数（500 是个好记的整数，且留了 3 字节余量）*/
export const READ_CHUNK_DEFAULT = 500;

// ============================================================================
// 读计划
// ============================================================================

/**
 * 读寄存器（一次事务，几十字节）。
 *
 * @param {object} o
 * @param {number} o.cmd     寄存器命令（如 0x04）
 * @param {number} o.rx      要读的数据字节数（不含 dummy）
 * @param {number} o.profile 0 raw / 1 spi_dcx / 2 qspi
 * @param {number} o.lines   数据线数（档 1 一般是 1）
 * @param {number} [o.dummy] dummy 周期
 * @param {object} [o.qspi]  档 2 的读时序（QSPI_READ_DEFAULT）
 * @returns {{items:Array, rx:number, label:string}}
 */
export function regReadItems(o = {}){
  const cmd = o.cmd & 0xff, rx = Math.max(1, Math.min(o.rx | 0 || 1, READ_CHUNK_MAX));
  const dummy = o.dummy == null ? 1 : o.dummy | 0;
  const lines = o.lines === 4 ? TC.LINES_4 : o.lines === 2 ? TC.LINES_2 : TC.LINES_1;
  const label = `读寄存器 0x${cmd.toString(16).padStart(2, '0')}（${rx} B）`;
  if (o.profile === 2){
    const q = { ...QSPI_READ_DEFAULT, ...(o.qspi || {}) };
    const tcfg = (q.lines === 4 ? TC.LINES_4 : q.lines === 2 ? TC.LINES_2 : TC.LINES_1) |
                 TC.CMD_EN | TC.ADDR_EN | (q.addrQuad ? TC.ADDR_QUAD : 0);
    return { rx, label, items: [{
      type: T.XFER,
      payload: xferPayload({ cmd: q.opcode, tcfg, addrLen: q.addrLen, addr: ((cmd << 8) >>> 0),
                             dummy: q.dummy, tx: new Uint8Array(0), rxLen: rx }),
      flags: F.RSP, label,
    }] };
  }
  if (o.profile !== 2){
    // 档 0（raw）与档 1（spi_dcx）都走"命令相位(DC=0) → 数据相位(DC=1)"这条 DCS 路：
    // raw 档真机上其实没有 DC 脚，页面会先提醒"档位可能不对"；但形状按 DCS 发，
    // 这样假探针 / 接了 DC 的板子都能读到东西（读不到就是档位问题，日志里已经写了）。
    // 命令相位（DC=0）→ 数据相位（DC=1）：中间 **CS 不能抬**，否则面板会把读指针丢掉
    return { rx, label, items: [
      { type: T.XFER, payload: xferPayload({ cmd: 0, tcfg: lines | TC.DC_EN, addrLen: 0, addr: 0,
                                             dummy: 0, tx: Uint8Array.of(cmd), rxLen: 0 }),
        flags: F.CS_HOLD, label: `命令 0x${cmd.toString(16).padStart(2, '0')}（DC=0，CS 保持）` },
      { type: T.XFER, payload: xferPayload({ cmd: 0, tcfg: lines | TC.DC_EN | TC.DC_LEVEL, addrLen: 0, addr: 0,
                                             dummy, tx: new Uint8Array(0), rxLen: rx }),
        flags: F.RSP, label: `${label}（DC=1）` },
    ] };
  }
  // 档 2：一条 XFER（读命令 + 地址 + dummy + 数据）
  const q = { ...QSPI_READ_DEFAULT, ...(o.qspi || {}) };
  const tcfg = (q.lines === 4 ? TC.LINES_4 : q.lines === 2 ? TC.LINES_2 : TC.LINES_1) |
               TC.CMD_EN | TC.ADDR_EN | (q.addrQuad ? TC.ADDR_QUAD : 0);
  return { rx, label, items: [{
    type: T.XFER,
    payload: xferPayload({ cmd: q.opcode, tcfg, addrLen: q.addrLen, addr: ((cmd << 8) >>> 0),
                           dummy: q.dummy, tx: new Uint8Array(0), rxLen: rx }),
    flags: F.RSP, label,
  }] };
}

/**
 * 读 GRAM（一帧画面）→ 切成若干"片"，每片一次 `sendFrames`。
 *
 * 档 1 的片：`[RAMRD(或 3Eh 续读) 命令帧(CS_HOLD)] + [读 XFER(rx=片长, DC=1, CS_HOLD 到末片)]`
 * 档 2 的片：`[读 XFER(opcode + 地址 + dummy + rx, 地址按片递增)]`
 *
 * @param {object} o
 * @param {object} o.geometry   屏几何（PANEL_GEOMETRY 里那一项）
 * @param {number} o.profile
 * @param {number} o.x0,o.y0,o.x1,o.y1  要读的窗口（含端点）
 * @param {number} [o.chunk]    每片读多少字节（≤504）
 * @param {number} [o.lines]
 * @param {object} [o.dcs]      档 1 时序（DCS_READ_DEFAULT）
 * @param {object} [o.qspi]     档 2 时序（QSPI_READ_DEFAULT）
 * @returns {{chunks:Array<{items:Array,bytes:number,label:string}>, total:number, w:number, h:number}}
 */
export function gramReadPlan(o = {}){
  const g = o.geometry || {};
  const x0 = Math.max(0, o.x0 | 0), y0 = Math.max(0, o.y0 | 0);
  const x1 = Math.min((g.w ?? 1) - 1, o.x1 == null ? (g.w ?? 1) - 1 : o.x1 | 0);
  const y1 = Math.min((g.h ?? 1) - 1, o.y1 == null ? (g.h ?? 1) - 1 : o.y1 | 0);
  const w = Math.max(0, x1 - x0 + 1), h = Math.max(0, y1 - y0 + 1);
  const total = w * h * 2;
  const chunk = Math.max(2, Math.min(o.chunk | 0 || READ_CHUNK_DEFAULT, READ_CHUNK_MAX));  const lines = o.lines === 4 ? 4 : o.lines === 2 ? 2 : 1;
  const linesC = lines === 4 ? TC.LINES_4 : lines === 2 ? TC.LINES_2 : TC.LINES_1;
  const dcs = { ...DCS_READ_DEFAULT, ...(o.dcs || {}) };
  const q = { ...QSPI_READ_DEFAULT, ...(o.qspi || {}) };

  // 开窗：档 0/1 走 STEP（固件按档展开）；档 2 **直接发 XFER**（固件的 STEP 展开编码是错的，见 §11.12）
  // opcode 用面板档里的"命令写 opcode"（qspi_wr_opcode，默认 0x02），与像素写侧同一个来源
  const win = windowItems({ x0, x1, y0, y1 }, g,
                           { profile: o.profile, qspiWrOpcode: q.wrOpcode, qspiAddrBytes: q.addrLen });
  const chunks = [];
  let off = 0, idx = 0;
  while (off < total){
    const n = Math.min(chunk, total - off);
    const last = off + n >= total;
    let items;
    if (o.profile === 2){
      /**
       * QSPI 读也是**一条命令 + 连续读**（与写侧、与 ESP-IDF 的做法一致）：
       *   · 首片：`读 opcode(0Bh) + AD[23:0]=00 2E 00 + dummy` 然后读 n 字节；
       *   · 后续片：**纯数据相位**（不带 cmd/addr），CS 一路保持到末片 —— 面板的读地址计数器自己走。
       *   每片都重发 `0Bh + 00 2E 00` 会把读指针打回窗口原点（读回来的永远是开头那一段）。
       */
      const base = (q.baseAddr ?? (dcs.ramrdCmd << 8)) >>> 0;
      if (idx === 0){
        const tcfg0 = (q.lines === 4 ? TC.LINES_4 : q.lines === 2 ? TC.LINES_2 : TC.LINES_1) |
                      TC.CMD_EN | TC.ADDR_EN | (q.addrQuad ? TC.ADDR_QUAD : 0);
        items = [{
          type: T.XFER,
          payload: xferPayload({ cmd: q.opcode, tcfg: tcfg0, addrLen: q.addrLen, addr: base,
                                 dummy: q.dummy, tx: new Uint8Array(0), rxLen: n }),
          flags: F.RSP | (last ? 0 : F.CS_HOLD),
          label: `读首片 @0x${base.toString(16)}（opcode 0x${q.opcode.toString(16)} + ${n} B，CS 保持）`,
        }];
        items = [...win, ...items];
      } else {
        const tcfg = (q.lines === 4 ? TC.LINES_4 : q.lines === 2 ? TC.LINES_2 : TC.LINES_1);
        items = [{
          type: T.XFER,
          payload: xferPayload({ cmd: 0, tcfg, addrLen: 0, addr: 0, dummy: 0,
                                 tx: new Uint8Array(0), rxLen: n }),
          flags: F.RSP | (last ? 0 : F.CS_HOLD),
          label: `续读 ${n} B（纯数据相位，CS 保持）`,
        }];
      }
    } else {
      const cmdByte = idx === 0 ? (dcs.ramrdCmd & 0xff) : (dcs.contCmd & 0xff);
      const cmdItem = {
        type: T.XFER,
        payload: xferPayload({ cmd: 0, tcfg: linesC | TC.DC_EN, addrLen: 0, addr: 0,
                               dummy: 0, tx: Uint8Array.of(cmdByte), rxLen: 0 }),
        flags: F.CS_HOLD,
        label: idx === 0 ? `RAMRD 0x${cmdByte.toString(16)}（DC=0，CS 保持）`
                         : `续读 0x${cmdByte.toString(16)}（DC=0，CS 保持）`,
      };
      const dataItem = {
        type: T.XFER,
        payload: xferPayload({ cmd: 0, tcfg: linesC | TC.DC_EN | TC.DC_LEVEL, addrLen: 0, addr: 0,
                               dummy: dcs.dummy, tx: new Uint8Array(0), rxLen: n }),
        flags: F.RSP | (last ? 0 : F.CS_HOLD),
        label: `读片 ${idx}（${n} B${last ? '' : '，CS 保持'}）`,
      };
      items = idx === 0 ? [...win, cmdItem, dataItem] : [cmdItem, dataItem];
    }
    chunks.push({ items, bytes: n, label: `片 ${idx}（${n} B）` });
    off += n; idx++;
  }
  return { chunks, total, w, h, x0, y0, x1, y1 };
}

// ============================================================================
// 解码 / 导出
// ============================================================================

/**
 * 读回来的 RGB565 字节流 → 预览用的 RGBA。
 *
 * ⚠️ 与 `rgb565ToRgba()` 的区别：这里要处理**字节序**（读回来的高低字节谁在前与 MADCTL/面板有关）
 * 与 **R/B 交换**。默认与发图侧同一套口径（高字节在前、不交换），不对时只翻一个开关。
 */
export function decodeGram(bytes, opts = {}){
  const n = Math.floor(bytes.length / 2);
  const out = new Uint8Array(n * 4);
  const le = !!opts.littleEndian, swap = !!opts.swap;
  for (let i = 0; i < n; i++){
    const b0 = bytes[i * 2], b1 = bytes[i * 2 + 1];
    const v = le ? ((b1 << 8) | b0) : ((b0 << 8) | b1);
    let r = ((v >> 11) & 31), g = ((v >> 5) & 63), b = (v & 31);
    if (swap){ const t = r; r = b; b = t; }
    out[i * 4] = (r * 255 / 31) | 0;
    out[i * 4 + 1] = (g * 255 / 63) | 0;
    out[i * 4 + 2] = (b * 255 / 31) | 0;
    out[i * 4 + 3] = 255;
  }
  return out;
}

/**
 * RGBA → 24 位 BMP（bottom-up、BGR、行按 4 字节对齐）。
 * 为什么不用 canvas.toBlob('image/bmp')：浏览器**不支持**导出 BMP（只能导入），所以自己拼头。
 */
export function encodeBMP(rgba, w, h){
  const stride = (w * 3 + 3) & ~3;
  const px = stride * h;
  const out = new Uint8Array(54 + px);
  const dv = new DataView(out.buffer);
  out[0] = 0x42; out[1] = 0x4d;                    // 'BM'
  dv.setUint32(2, out.length, true);
  dv.setUint32(10, 54, true);                      // 像素数据偏移
  dv.setUint32(14, 40, true);                      // DIB 头大小
  dv.setInt32(18, w, true);
  dv.setInt32(22, h, true);                        // 正数 = bottom-up
  dv.setUint16(26, 1, true);                       // planes
  dv.setUint16(28, 24, true);                      // bpp
  dv.setUint32(30, 0, true);                       // BI_RGB
  dv.setUint32(34, px, true);
  for (let y = 0; y < h; y++){
    const src = (h - 1 - y) * w * 4;               // 文件里第一行 = 图的最下面一行
    let dst = 54 + y * stride;
    for (let x = 0; x < w; x++){
      out[dst++] = rgba[src + x * 4 + 2];          // B
      out[dst++] = rgba[src + x * 4 + 1];          // G
      out[dst++] = rgba[src + x * 4];              // R
    }
  }
  return out;
}

/** 静态检查：窗口/片数在不在合法范围（页面按钮的禁用逻辑与自测共用）*/
export function readPlanProblem(plan){
  if (!plan || !plan.chunks?.length) return '窗口是空的（x0≤x1、y0≤y1）';
  if (plan.total > 8 * 1024 * 1024) return `要读 ${(plan.total / 1048576).toFixed(1)} MB，太大了（先缩小窗口）`;
  for (const c of plan.chunks){
    for (const it of c.items){
      if (it.payload.length > FRAME_MAX && it.type === T.XFER) return '有一片超过单帧上限';
      if (it.type === T.XFER && it.payload.length < 12) return 'XFER 帧头不完整';
    }
  }
  return null;
}
