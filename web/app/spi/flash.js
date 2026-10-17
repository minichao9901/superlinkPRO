/**
 * 外接 SPI NOR 的测试件 —— 命令表、应答解析、批量序列生成，外加一个**器件模型**给假探针用。
 * 纯函数 + 一个纯状态类（不碰 DOM），Node 自测直接打。
 *
 * 为什么不用改固件就能做：通用 `XFER` 帧本来就带 `cmd / addr_len(0..4) / dummy(0..4) /
 * tx_len / rx_len / addr` 加上 `tcfg` 的线数（1/2/4）与"地址相位四线"开关 —— NOR 的
 * RDID(0x9F)、SFDP(0x5A)、读(0x03/0x0B/0x3B/0x6B/0xEB)、写使能(0x06)、擦除(0x20/0xD8)、
 * 编程(0x02/0x32)、读状态(0x05/0x35) 全都表达得出来。**四线读不需要切 qspi 面板档**
 * （那个档是屏专用的"每片带 cmd=0x32 + 24 bit 地址"），raw 档 + 帧内 `lines=4` 即可。
 *
 * 连续读（测速的关键）：一帧最多带 492 B 数据，大块读必须拆帧。拆帧有两种拆法 ——
 *   ① 每帧都重发 cmd+地址（简单，但每帧多 4~5 字节、且 CS 每帧一放一起，flash 要重新解码）；
 *   ② 首帧发 cmd+地址，**后续帧 `cmd_en=0` + `CS_HOLD`**，末帧 `CS_OFF`。
 * CS 一直摁着 = flash 内部地址计数器自己往前跑，这才是"连续读"，也是测速该走的路径。
 *
 * 接线（全在 J3 排针，板上那颗 NOR 挂在 XPI0 的 PX 专用脚上，**桥够不着也动不得**）：
 * 2026-09-30 起桥在 SPI2：
 *   CS  ← J3[26] PB10      SCLK ← J3[13] PB11
 *   IO0 ← J3[28] PB13      IO1  ← J3[27] PB12
 *   IO2 ← J3[10] PB14      IO3  ← J3[8]  PB15（四线才接）
 *   VCC/GND 按模块电压，WP#/HOLD# 上拉（或按模块要求）
 */
import { F, TC, XFER_TX_MAX, delayPayload, linesToTcfg, xferPayload, T } from './protocol.js';

// ============================================================================
// 命令表
// ============================================================================

export const OP = {
  WREN: 0x06, WRDI: 0x04, RDSR1: 0x05, RDSR2: 0x35, WRSR: 0x01,
  READ: 0x03, FAST_READ: 0x0B, DOR: 0x3B, QOR: 0x6B, QIOR: 0xEB,
  PP: 0x02, QPP: 0x32, SE: 0x20, BE32: 0x52, BE64: 0xD8, CE: 0xC7,
  RDID: 0x9F, RDSFDP: 0x5A, REMS: 0x90, RES: 0xAB, RUID: 0x4B,
  RSTEN: 0x66, RST: 0x99,
};
export const OP_NAME = Object.fromEntries(Object.entries(OP).map(([k, v]) => [v, k]));

/**
 * 读模式：opcode / 数据相位线数 / dummy 周期（协议里 dummy 字段的 1 = 8 拍）
 *
 * ⚠️ dummy 只是**默认值**，页面上那个 dummy 框会覆盖它（见 `readItems` 的 `dummy` 参数）：
 *   同一颗兼容片实测 0x3B DUAL OUT 要 **16 拍**（dummy=2）才对齐，而 JEDEC/原厂写的是 8 拍。
 *   线数也不是"1 线接法就只能用 1 线档"：**DUAL 用 IO0/IO1，标准 SPI 接法就能用**；
 *   只有 quad（0x6B/0xEB）才额外需要 IO2/IO3。
 */
export const READ_MODES = [
  { v: OP.READ, lines: 1, dummy: 0, name: 'READ 0x03 · 1 线 · 无 dummy' },
  { v: OP.FAST_READ, lines: 1, dummy: 1, name: 'FAST READ 0x0B · 1 线 · 8 拍 dummy' },
  { v: OP.DOR, lines: 2, dummy: 1, name: 'DUAL OUT 0x3B · 2 线出 · 8 拍 dummy' },
  { v: OP.QOR, lines: 4, dummy: 1, name: 'QUAD OUT 0x6B · 4 线出 · 8 拍 dummy' },
  { v: OP.QIOR, lines: 4, dummy: 1, name: 'QUAD I/O 0xEB · 4 线全开 · 8 拍 dummy' },
];

/** 擦除档：opcode / 粒度（0 = 整片，不带地址）*/
export const ERASE_MODES = [
  { v: OP.SE, size: 4096, name: '扇区擦除 0x20 · 4 KB' },
  { v: OP.BE32, size: 32768, name: '块擦除 0x52 · 32 KB' },
  { v: OP.BE64, size: 65536, name: '块擦除 0xD8 · 64 KB' },
  { v: OP.CE, size: 0, name: '整片擦除 0xC7 · 全部（慢）' },
];

export const PAGE_SIZE = 256;         // 编程粒度（页写不能跨页回绕）
export const SECTOR_SIZE = 4096;
/**
 * 线上地址**固定 3 字节**（见 `readFrame`/`writeFrame` 的 `addrLen: 3`）—— 超过它的地址在线上
 * 会**静默回绕到低地址**，于是"擦 0x1000000"实际擦的是 0x000000（通常是启动代码）。
 * 页面侧要求所有地址输入都卡在这个上限内（`bus-view.flAddr()`）。
 */
export const ADDR_MAX = 0xffffff;

// ============================================================================
// JEDEC ID 解析
// ============================================================================

/** 常见厂商（第 1 字节）。表不全就显示原始值 —— 别猜。*/
export const JEDEC_VENDORS = {
  0x01: 'Spansion / Cypress', 0x0b: 'XTX', 0x1c: 'eON', 0x1f: 'Adesto / Atmel',
  0x20: 'Micron / ST', 0x37: 'AMIC', 0x5e: 'Zbit', 0x62: 'SANYO', 0x68: 'Boya',
  0x7f: 'AMIC', 0x85: 'Puya', 0x89: 'ESMT', 0x8c: 'ESMT', 0x9d: 'ISSI',
  0xa1: 'Fudan', 0xbf: 'SST / Microchip', 0xc2: 'Macronix', 0xc8: 'GigaDevice',
  0xef: 'Winbond',
};

/**
 * 解析 RDID 的 3 个字节。
 * 第 3 字节是容量码：`0x14` = 1 MB、`0x17` = 8 MB…（容量 = 2^码 字节）
 */
export function parseJedec(bytes){
  const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || 0);
  if (b.length < 3) return null;
  const [mfr, type, cap] = b;
  const sizeBytes = (cap >= 0x11 && cap <= 0x22) ? 2 ** cap : null;
  return {
    mfr, type, cap, sizeBytes,
    vendor: JEDEC_VENDORS[mfr] || null,
    hex: [...b.subarray(0, 3)].map(x => x.toString(16).padStart(2, '0')).join(' '),
    text: (JEDEC_VENDORS[mfr] || `厂商 0x${mfr.toString(16).padStart(2, '0')}`) +
      ` · 类型 0x${type.toString(16).padStart(2, '0')} · ` +
      (sizeBytes ? fmtSize(sizeBytes) : `容量码 0x${cap.toString(16)}（不在常用表里）`),
  };
}

export function fmtSize(n){
  if (n == null) return '—';
  if (n >= 1 << 20) return (n / (1 << 20)) + ' MB';
  if (n >= 1 << 10) return (n / (1 << 10)) + ' KB';
  return n + ' B';
}

// ============================================================================
// SFDP 解析（布局以 Linux `drivers/mtd/spi-nor/sfdp.h` 为准）
// ============================================================================

/** SFDP 签名 "SFDP"（小端读成 u32 = 0x50444653）*/
export const SFDP_MAGIC = [0x53, 0x46, 0x44, 0x50];
/**
 * 只判签名（4 B）。
 * 页面「读 SFDP」现在的口径是**只出原始 256 B**（2026-10 用户要求去掉解读），
 * 所以只需要这一步做"dummy 对不对"的链路标定；下面的 parseSfdp 等解析函数保留给
 * 离线分析 / 自测用，页面不再展示它们的输出。
 */
export function hasSfdpMagic(bytes){
  return !!bytes && bytes.length >= 4 && SFDP_MAGIC.every((v, i) => bytes[i] === v);
}
export const SFDP_MINOR_NAME = { 0: 'JESD216', 5: 'JESD216A', 6: 'JESD216B', 7: 'JESD216C', 8: 'JESD216D' };
/** 参数表 ID（高字节 0 = JEDEC 统一分配）*/
export const SFDP_TABLE_NAME = {
  0x00: 'JEDEC BFPT（基本参数表）',
  0x01: 'JEDEC 扇区映射表',
  0x02: 'JEDEC 四字节地址指令表',
  0x03: 'JEDEC xSPI 配置检测',
  0x04: 'JEDEC xSPI 配置覆盖',
  0xff: 'JEDEC xSPI Profile 1.0',
};

/**
 * 解析 SFDP 头（前 8 B）+ 参数表头（每个 8 B）：
 *   sfdp_header:  "SFDP" | minor | major | nph-1 | unused              （8 B）
 *   param_header: id_lsb | minor | major | length(DWORD) | ptr(3B) | id_msb   （8 B）
 */
export function parseSfdp(bytes){
  const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || 0);
  if (b.length < 8) return null;
  const sigOk = SFDP_MAGIC.every((v, i) => b[i] === v);
  const minor = b[4], major = b[5], nph = b[6] + 1;
  const headers = [];
  for (let i = 0; i < nph; i++){
    const off = 8 + i * 8;
    if (b.length < off + 8) break;
    const ptr = b[off + 4] | (b[off + 5] << 8) | (b[off + 6] << 16);
    const id = b[off] | (b[off + 7] << 8);
    headers.push({
      index: i, id,
      idLsb: b[off], idMsb: b[off + 7],
      minor: b[off + 1], major: b[off + 2],
      lengthDwords: b[off + 3], ptr,
      name: b[off + 7] === 0 ? (SFDP_TABLE_NAME[b[off]] || `JEDEC 保留 0x${b[off].toString(16).padStart(2, '0')}`)
                             : `厂商自定义（MSB=0x${b[off + 7].toString(16).padStart(2, '0')}）`,
    });
  }
  return {
    sigOk, minor, major, nph, headers, raw: b,
    revName: SFDP_MINOR_NAME[minor] || `minor 0x${minor.toString(16)}`,
    text: sigOk
      ? `${SFDP_MINOR_NAME[minor] || 'minor ' + minor}（major ${major}）· ${nph} 个参数表头`
      : '签名不是 "SFDP" —— dummy 周期或接线不对（换 dummy 再试）',
  };
}

/**
 * 从一张表的 DWORD 2 推容量（BFPT 的 DWORD 2 存的是「容量(bit) - 1」）。
 * 只在结果是个"像样的" 2 的幂次 MB 数时才认，否则返回 null —— 免得拿厂商私有表瞎猜。
 *
 * 用途：有些兼容片的 BFPT 内容照抄了、参数表 ID 却写成厂商自定义（0xFF00）。
 * 2026-09-30 实测那颗 W25Q64 兼容片就是：表 ID=0xFF00，但 DWORD 2 = 0x03FFFFFF → 8 MB，
 * 与 JEDEC ID 报的 8 MB 完全吻合。
 */
export function bfptDensity(bytes){
  const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || 0);
  if (b.length < 8) return null;
  const dw2 = (b[4] | (b[5] << 8) | (b[6] << 16) | (b[7] << 24)) >>> 0;
  if (dw2 === 0 || dw2 === 0xffffffff) return null;
  const mb = (dw2 + 1) / 8 / 1048576;
  if (mb < 1 || mb > 4096 || Math.abs(mb - Math.round(mb)) > 1e-6) return null;
  return `${Math.round(mb)} MB（DWORD 2 = 0x${dw2.toString(16)}，即容量(bit)-1）`;
}

/**
 * JEDEC BFPT（基本参数表，id=0000h）里**含义确定**的字段。
 *
 * 只解 JESD216 里含义明确、能一眼对上的那几个；其余字段逐位含义随 revision 变，
 * 硬解容易出错 —— 原始 DWORD 调用方已经摊开了，对着 JESD216 表 2 查即可。
 * 最有用的就是**容量**：BFPT 的 DWORD 2 存的是「容量(bit) - 1」。
 */
export function parseBfpt(bytes){
  const out = [];
  const dens = bfptDensity(bytes);
  out.push(dens ? `　· 容量：${dens}` : '　· 容量（DWORD 2）：未填/无效');
  out.push('　· 其余字段（擦除粒度 / 读模式与 dummy / 时序…）见 JESD216 表 2，按上面的 DWORD 编号逐位查');
  return out;
}

/** 把 BFPT 那样的原始 DWORD 数组排成可读文本 */
export function dumpDwords(bytes){
  const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || 0);
  const out = [];
  for (let i = 0; i + 4 <= b.length; i += 4){
    const v = (b[i] | (b[i + 1] << 8) | (b[i + 2] << 16) | (b[i + 3] << 24)) >>> 0;
    out.push(`DWORD ${String(i / 4 + 1).padStart(2)} @0x${i.toString(16).padStart(2, '0')}: 0x${v.toString(16).padStart(8, '0')}`);
  }
  return out;
}

// ============================================================================
// 状态寄存器
// ============================================================================

/** SR1（0x05）的位名 —— 位定义在厂商间基本一致，QE 那种厂商位走 SR2 */
export function parseStatus1(b){
  const v = (b instanceof Uint8Array ? b[0] : b) | 0;
  return {
    raw: v, busy: !!(v & 0x01), wel: !!(v & 0x02),
    bp: (v >> 2) & 0x07, tb: !!(v & 0x20), sec: !!(v & 0x40), srp0: !!(v & 0x80),
    text: `0x${v.toString(16).padStart(2, '0')}` +
      (v & 0x01 ? ' · BUSY' : ' · 空闲') + (v & 0x02 ? ' · WEL（写使能已置）' : '') +
      (((v >> 2) & 0x07) ? ` · BP=${(v >> 2) & 0x07}（块保护）` : '') +
      ((v & 0x40) ? ' · SEC' : '') + ((v & 0x20) ? ' · TB' : ''),
  };
}

/** SR2（0x35）—— bit1 = QE（Quad Enable，各家略有差异，这里按 Winbond/GigaDevice 常见排法）*/
export function parseStatus2(b){
  const v = (b instanceof Uint8Array ? b[0] : b) | 0;
  return {
    raw: v, qe: !!(v & 0x02), srp1: !!(v & 0x01),
    text: `0x${v.toString(16).padStart(2, '0')}` +
      (v & 0x02 ? ' · QE=1（四线使能）' : ' · QE=0（四线没开，0x6B/0xEB/0x32 可能不出数据）'),
  };
}

// ============================================================================
// 帧序列生成（页面与自测共用；都是纯数据）
// ============================================================================

const it = (type, payload, flags, label) => ({ type, payload, flags, label });

/** 一条读帧（带 RSP）*/
export function readFrame({ opcode, addr = 0, addrLen = 3, dummy = 1, lines = 1, rx = 0, flags = 0 }){
  let tcfg = linesToTcfg(lines) | TC.CMD_EN;
  if (addrLen > 0) tcfg |= TC.ADDR_EN;
  return it(T.XFER, xferPayload({ cmd: opcode, tcfg, addrLen, dummy, addr, rxLen: rx }),
    F.RSP | flags, `读 cmd=0x${opcode.toString(16).padStart(2, '0')} addr=0x${(addr >>> 0).toString(16)} rx=${rx}`);
}

/** 一条写帧（cmd + 可选地址 + 数据）*/
export function writeFrame({ opcode, addr = 0, addrLen = 0, lines = 1, data = new Uint8Array(0), flags = 0, expectRsp = true }){
  let tcfg = linesToTcfg(lines) | TC.CMD_EN;
  if (addrLen > 0) tcfg |= TC.ADDR_EN;
  return it(T.XFER, xferPayload({ cmd: opcode, tcfg, addrLen, dummy: 0, addr, tx: data, rxLen: 0 }),
    (expectRsp ? F.RSP : 0) | flags, `写 cmd=0x${opcode.toString(16).padStart(2, '0')}${addrLen ? ` addr=0x${(addr >>> 0).toString(16)}` : ''} tx=${data.length}`);
}

/** 只发命令字节（无地址无数据）：WREN / WRDI / CE / RSTEN / RST */
export function cmdFrame(opcode, flags = 0){
  return it(T.XFER, xferPayload({ cmd: opcode, tcfg: TC.CMD_EN }), F.RSP | flags,
    `命令 0x${opcode.toString(16).padStart(2, '0')}`);
}

export const rdidItems = () => [readFrame({ opcode: OP.RDID, addrLen: 0, dummy: 0, lines: 1, rx: 3 })];
/** SFDP 头（8 B）：签名 + 版本 + 参数表头个数 */
export const sfdpHeadItems = (dummy = 1) => [readFrame({ opcode: OP.RDSFDP, addr: 0, addrLen: 3, dummy, lines: 1, rx: 8 })];
/** SFDP 头 + 全部参数表头（8 + nph×8 B）—— 一般一次读 16 B 就够 nph=1 的器件 */
export const sfdpParamItems = (nph = 1, dummy = 1) =>
  [readFrame({ opcode: OP.RDSFDP, addr: 0, addrLen: 3, dummy, lines: 1, rx: Math.min(64, 8 + Math.max(1, nph) * 8) })];
/**
 * **整片 SFDP 空间**（JESD216 规定是 256 B 的只读区）—— 「完整读 SFDP」用它，
 * 把头、所有参数表头、以及表与表之间的空隙一次拿全，原始字节可以整片摊开看。
 */
export const sfdpFullItems = (dummy = 1) =>
  [readFrame({ opcode: OP.RDSFDP, addr: 0, addrLen: 3, dummy, lines: 1, rx: 256 })];
export const sfdpTableItems = (ptr, lengthDwords, dummy = 1) =>
  [readFrame({ opcode: OP.RDSFDP, addr: ptr, addrLen: 3, dummy, lines: 1, rx: Math.min(64, Math.max(4, lengthDwords * 4)) })];
export const rdsr1Items = () => [readFrame({ opcode: OP.RDSR1, addrLen: 0, dummy: 0, lines: 1, rx: 1 })];
export const rdsr2Items = () => [readFrame({ opcode: OP.RDSR2, addrLen: 0, dummy: 0, lines: 1, rx: 1 })];
export const wrenItems = () => [cmdFrame(OP.WREN)];
export const wrdiItems = () => [cmdFrame(OP.WRDI)];

/**
 * 读一段数据：单帧（≤492 B）或按连续读拆帧。
 *
 * `dummy` 可覆盖模式自带的默认值 —— **必须能覆盖**：dummy 拍数随器件不同
 * （同一颗兼容片实测 0x3B DUAL OUT 要 16 拍，而 JEDEC/原厂的 0x3B 是 8 拍），
 * 且它只影响**首帧**（续读帧靠 CS_HOLD 让 flash 自己往下数）。
 */
export function readItems(addr, len, { mode = OP.QIOR, chunk = XFER_TX_MAX, dummy } = {}){
  const m = READ_MODES.find(x => x.v === mode) || READ_MODES[0];
  const dm = Number.isFinite(dummy) ? Math.max(0, Math.min(4, dummy | 0)) : m.dummy;
  const c = Math.max(1, Math.min(XFER_TX_MAX, chunk | 0));
  const items = [];
  let off = 0, first = true;
  while (off < len){
    const n = Math.min(c, len - off);
    if (first){
      items.push(readFrame({ opcode: m.v, addr: (addr + off) >>> 0, addrLen: 3, dummy: dm, lines: m.lines, rx: n, flags: F.CS_HOLD }));
      first = false;
    } else {
      // 续读帧：不发 cmd / 地址，靠 CS_HOLD 让 flash 自己往下数
      items.push(it(T.XFER, xferPayload({ tcfg: linesToTcfg(m.lines), rxLen: n }),
        F.RSP | F.CS_HOLD, `续读 rx=${n}`));
    }
    off += n;
  }
  if (items.length) items[items.length - 1].flags = (items[items.length - 1].flags & ~F.CS_HOLD) | F.CS_OFF;
  return items;
}

/** 擦除：WREN + 擦除帧（整片擦除不带地址）*/
export function eraseItems(addr, { opcode = OP.SE } = {}){
  const m = ERASE_MODES.find(x => x.v === opcode) || ERASE_MODES[0];
  const items = [cmdFrame(OP.WREN)];
  if (m.size === 0) items.push(cmdFrame(m.v));
  else items.push(writeFrame({ opcode: m.v, addr: addr >>> 0, addrLen: 3 }));
  return items;
}

/** 按页切分（不跨 256 B 边界）—— 编程计划的纯数据部分，页面与自测共用 */
export function programPages(addr, data){
  const d = data instanceof Uint8Array ? data : new Uint8Array(data || 0);
  const pages = [];
  let off = 0;
  while (off < d.length){
    const abs = (addr + off) >>> 0;
    const n = Math.min(PAGE_SIZE - (abs % PAGE_SIZE), d.length - off);
    pages.push({ addr: abs, data: d.subarray(off, off + n) });
    off += n;
  }
  return pages;
}

/**
 * 按页编程：每页一条 WREN + 一条 PP（页内不能跨 256 B 边界）。
 *
 * 🚨 **页与页之间必须等器件写完**（`tPP`，NOR 典型 0.4~3 ms）：器件 BUSY 期间**会直接忽略**
 * 后续命令，一口气把两页塞下去，第二页就静默丢了（假探针的模型就按这个规矩答，自测里咬着）。
 * 这里用**有序的 `DELAY` 帧**来等（固件侧是非阻塞的：延时期间后面所有帧都排队等它），
 * 不用主机来回轮询，一个 batch 就能灌完；编完再回读校验一次即可确认延时够不够。
 */
export function programItems(addr, data, { quad = false, pageDelayMs = 3, maxBytes = 1 << 20 } = {}){
  const d = data instanceof Uint8Array ? data : new Uint8Array(data || 0);
  if (d.length > maxBytes) throw new Error(`一次最多写 ${fmtSize(maxBytes)}（收到 ${fmtSize(d.length)}）—— 分几次来`);
  const items = [];
  const pages = programPages(addr, d);
  pages.forEach((pg, i) => {
    items.push(cmdFrame(OP.WREN));
    items.push(writeFrame({ opcode: quad ? OP.QPP : OP.PP, addr: pg.addr, addrLen: 3, lines: quad ? 4 : 1, data: pg.data }));
    if (pageDelayMs > 0 && i < pages.length - 1) items.push(it(T.DELAY, delayPayload(pageDelayMs * 1000), 0, `等 tPP ${pageDelayMs}ms`));
  });
  return items;
}

export const sectorOf = addr => (addr >>> 0) & ~(SECTOR_SIZE - 1);

// ============================================================================
// 器件模型（给假探针用；真实感够用：WEL/BUSY/页回绕/只能 1→0/擦除时长）
// ============================================================================

/**
 * 一个 SPI NOR 的简化模型。**故意保留两条真实性格**：
 *   · 编程只能把 1 写成 0（NOR 的物理事实），所以"没擦就写"会得到与旧数据的 AND；
 *   · 擦除/编程期间 BUSY=1，`RDSR` 会一直读出 BUSY，直到时间到 —— 页面那套"轮询 BUSY"能被真测到。
 * 时间用注入的时钟（默认 performance.now），便于单测把时间"拨"过去。
 */
export class FlashDevice {
  constructor(opts = {}){
    this.jedec = Uint8Array.from(opts.jedec || [0xef, 0x40, 0x18]);   // 默认 W25Q128（16 MB）
    this.sizeBytes = opts.sizeBytes ?? (2 ** this.jedec[2]);
    this.memSize = Math.min(opts.memSize ?? 0x10000, this.sizeBytes);  // 假器件只留 64 KB 真内存
    this.mem = new Uint8Array(this.memSize).fill(0xff);
    this.tPP = opts.tPP ?? 0.6;      // 页编程 ms
    this.tSE = opts.tSE ?? 40;       // 扇区擦除 ms
    this.tCE = opts.tCE ?? 3000;     // 整片擦除 ms
    this.now = opts.clock || (() => (typeof performance !== 'undefined' ? performance.now() : Date.now()));

    // SFDP：一张最小的合法表（签名 + 1 个 JEDEC BFPT 表头，BFPT 8 个 DWORD）
    this.sfdp = opts.sfdp || makeSfdp(this.sizeBytes);

    this.sr1 = 0; this.sr2 = 0;
    this.busyUntil = 0;
    this.csActive = false;      // 上一帧是否摁着 CS（跨帧连续读靠它）
    this.pendingAddr = null;    // 连续读的当前位置
    this.pendingMode = null;
    this.log = [];              // 器件侧动作流水（自测对账用）
  }

  get busy(){ return this.now() < this.busyUntil; }
  get wel(){ return !!(this.sr1 & 0x02); }

  _wr(addr, byte){
    const a = addr % this.memSize;
    this.mem[a] &= byte;                 // NOR：只能 1 → 0
  }
  _rd(addr){ return this.mem[addr % this.memSize]; }

  _delay(ms){ this.busyUntil = Math.max(this.busyUntil, this.now() + ms); }

  /**
   * 执行一条 XFER（模型侧）。
   * @param {{cmd:number,cmdEn:boolean,addr:number,addrEn:boolean,dummy:number,lines:number,
   *          tx:Uint8Array,rxLen:number,csHold:boolean}} x
   * @returns {{rx:Uint8Array|null, why?:string}} `why` 非空 = 这条命令被器件拒绝（页面应提示）
   */
  exec(x){
    if (!this.csActive){ this.pendingAddr = null; this.pendingMode = null; }   // 上一帧放了 CS → 新窗口
    this.csActive = !!x.csHold;
    if (!x.cmdEn){
      // 续读帧：接着上次的地址往下吐
      if (this.pendingAddr == null) return { rx: new Uint8Array(x.rxLen), why: '续读但没有正在进行的读（首帧没发 cmd/地址？）' };
      const out = new Uint8Array(x.rxLen);
      for (let i = 0; i < out.length; i++) out[i] = this._rd(this.pendingAddr + i);
      this.pendingAddr += out.length;
      this.log.push(`续读 ${out.length} B @0x${(this.pendingAddr - out.length).toString(16)}`);
      return { rx: out };
    }

    const cmd = x.cmd;
    if (this.busy && (cmd === OP.PP || cmd === OP.SE || cmd === OP.BE32 || cmd === OP.BE64 || cmd === OP.CE)){
      return { rx: null, why: '器件 BUSY，命令被忽略（等 RDSR 的 BUSY 清掉再发）' };
    }
    switch (cmd){
      case OP.RDID: return { rx: this.jedec.slice(0, 3) };

      case OP.RDSFDP: {
        const out = new Uint8Array(x.rxLen);
        for (let i = 0; i < out.length; i++) out[i] = this.sfdp[(x.addr + i) % this.sfdp.length];
        return { rx: out };
      }

      case OP.RDSR1: return { rx: Uint8Array.of(this._sr1()) };
      case OP.RDSR2: return { rx: Uint8Array.of(this.sr2) };

      case OP.WREN: this.sr1 |= 0x02; this.log.push('WREN'); return { rx: null };
      case OP.WRDI: this.sr1 &= ~0x02; this.log.push('WRDI'); return { rx: null };
      case OP.WRSR:
        if (!this.wel) return { rx: null, why: 'WRSR 前要先 WREN' };
        this.sr1 = (this.sr1 & 0x02) | (x.tx[0] & 0xbc); this.sr1 &= ~0x02;
        this.log.push(`WRSR 0x${(x.tx[0] || 0).toString(16)}`);
        return { rx: null };

      case OP.SE:
      case OP.BE32:
      case OP.BE64:
      case OP.CE: {
        if (!this.wel) return { rx: null, why: '擦除前要先 WREN' };
        this.sr1 &= ~0x02;
        if (cmd === OP.CE){
          this.mem.fill(0xff); this._delay(this.tCE); this.log.push('CE 整片擦除');
        } else {
          const size = cmd === OP.SE ? 4096 : cmd === OP.BE32 ? 32768 : 65536;
          const base = (x.addr >>> 0) & ~(size - 1);
          for (let i = 0; i < size; i++) this.mem[(base + i) % this.memSize] = 0xff;
          this._delay(this.tSE * (size / 4096));
          this.log.push(`${cmd === OP.SE ? 'SE' : 'BE'} 0x${base.toString(16)} (${size} B)`);
        }
        return { rx: null };
      }

      case OP.PP:
      case OP.QPP: {
        if (!this.wel) return { rx: null, why: '编程前要先 WREN' };
        if ((x.addr % PAGE_SIZE) + x.tx.length > PAGE_SIZE) return { rx: null, why: `编程跨页了（0x${(x.addr >>> 0).toString(16)} + ${x.tx.length} B 越过 256 B 边界）` };
        this.sr1 &= ~0x02;
        for (let i = 0; i < x.tx.length; i++) this._wr(x.addr + i, x.tx[i]);
        this._delay(this.tPP);
        this.log.push(`PP 0x${(x.addr >>> 0).toString(16)} ${x.tx.length} B`);
        return { rx: null };
      }

      case OP.READ:
      case OP.FAST_READ:
      case OP.DOR:
      case OP.QOR:
      case OP.QIOR: {
        const want = { [OP.READ]: 1, [OP.FAST_READ]: 1, [OP.DOR]: 2, [OP.QOR]: 4, [OP.QIOR]: 4 }[cmd];
        if (want !== x.lines) return { rx: new Uint8Array(x.rxLen), why: `0x${cmd.toString(16)} 要求 ${want} 线数据相位（帧里是 ${x.lines} 线）` };
        if (want > 1 && !(this.sr2 & 0x02)) return { rx: new Uint8Array(x.rxLen), why: 'QE=0：四线/双线读要先在 SR2 里置 QE' };
        const out = new Uint8Array(x.rxLen);
        for (let i = 0; i < out.length; i++) out[i] = this._rd(x.addr + i);
        this.pendingAddr = (x.addr + out.length) >>> 0;
        this.pendingMode = cmd;
        this.log.push(`读 0x${cmd.toString(16)} @0x${(x.addr >>> 0).toString(16)} ${out.length} B`);
        return { rx: out };
      }

      default:
        return { rx: x.rxLen ? new Uint8Array(x.rxLen) : null, why: `模型没实现 0x${cmd.toString(16).padStart(2, '0')}` };
    }
  }

  /** CS 被显式释放（CS 帧）*/
  releaseCs(){ this.csActive = false; this.pendingAddr = null; }

  _sr1(){
    let v = this.sr1 & ~0x01;
    if (this.busy) v |= 0x01;
    return v;
  }
}

/** 造一张最小的合法 SFDP：签名 + 1 个 JEDEC BFPT 表头 + 8 个 DWORD 的表 */
export function makeSfdp(sizeBytes = 1 << 24){
  const bfptDwords = 8;
  const ptr = 0x30;                                  // BFPT 放在 0x30
  const out = new Uint8Array(ptr + bfptDwords * 4).fill(0xff);
  out.set(SFDP_MAGIC, 0);
  out[4] = 0x06; out[5] = 0x01; out[6] = 0x00; out[7] = 0xff;   // minor=6(JESD216B) major=1 nph=1
  out[8] = 0x00; out[9] = 0x06; out[10] = 0x01; out[11] = bfptDwords;
  out[12] = ptr & 0xff; out[13] = (ptr >> 8) & 0xff; out[14] = (ptr >> 16) & 0xff; out[15] = 0x00;
  const dw = (i, v) => { out[ptr + i * 4] = v & 0xff; out[ptr + i * 4 + 1] = (v >> 8) & 0xff; out[ptr + i * 4 + 2] = (v >> 16) & 0xff; out[ptr + i * 4 + 3] = (v >> 24) & 0xff; };
  dw(0, 0x50444653);                                  // 有些实现把签名也放这儿（保持一致）
  dw(1, 0x00100000 | 0x0c);                           // 最小擦除粒度 4 KB 的示意
  const density = Math.round(Math.log2(sizeBytes));
  dw(2, (density & 0x7f) << 24 | 0x00000001);         // 容量（bit31 必须为 1）
  for (let i = 3; i < bfptDwords; i++) dw(i, 0);
  return out;
}
