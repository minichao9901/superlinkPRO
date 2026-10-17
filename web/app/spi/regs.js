/**
 * SPI **通用寄存器面板**的纯逻辑：器件档位（register 约定）+ 读/写帧构造。
 *
 * 为什么 SPI 需要一个"档位"而 I2C 不需要：I2C 的寄存器访问只有一种形状
 * （`START dev+W + 子地址 + [rSTART dev+R + 读] + STOP`），地址就是地址；而 SPI 没有地址概念 ——
 * 寄存器号怎么发、读命令怎么带它、要不要 dummy、一帧能读多少、多字节读会不会自动递增，
 * **全都是各器件自己定的**。所以这里把"怎么把寄存器号变成线上字节"抽成一张张档位表。
 *
 * 档位字段（面板上可改，内置档位只是默认值）：
 *   · `readOp` / `writeOp` —— 读/写命令的**基础 opcode**
 *   · `addrMode`：
 *       `'orOp'`  —— 地址**或进 opcode**（`0x80 | reg`、`0x20 | reg` 这类），不发独立地址字节；
 *       `'bytes'` —— opcode 之后跟 `addrBytes` 个地址字节（MSB 先）；
 *       `'none'`  —— 没有寄存器地址（命令型器件：一次转换就是一个命令）
 *   · `addrBits` —— `orOp` 时 opcode 低几位留给地址（决定掩码：`(1<<addrBits)-1`）
 *   · `dummy`   —— 读时插入的 dummy **字节**数（协议字段 1 = 8 拍）。ADXL345 / BMP280 这类要 1，
 *                 MPU-9250 / LSM6DS3 这类不要 —— **这是最容易错一个字节的地方**
 *   · `dataBytes` —— 每个寄存器占几字节（显示分组、写回对齐都用它）
 *   · `autoInc` —— 多字节读时器件是否自动递增寄存器地址（多数寄存器型器件会；不会的只能一寄存器一帧）
 *   · `msbFirst` —— 位序（SPI 器件绝大多数 MSB 先，留字段是为了示波器/老器件）
 *   · `lines`   —— 数据相位线数（1 = 标准 SPI；2/4 只有 flash 那类才用）
 *
 * 🚨 帧上限（两条都要守）：
 *   ① `XFER_TX_MAX` = 492 B —— 一帧的发送数据上限（帧头 12 B + 512 包）；
 *   ② `RX_PER_FRAME_MAX` = 480 B —— 一帧的**读**上限。RSP 是 `8 B 头 + 数据`，
 *      数据取到 504 会正好填满一个 512 的 bulk 包，主机侧"收满或收到短包"永远等不到短包
 *      （I2C 侧踩过同一个坑：那里是 503 封顶）。这里再留点余量，多读就多分几帧。
 *
 * 出处与常见器件的特性总结见 `docs/spi-register-panel.md`（含手册章节）——
 * **改档位表要连带改那张表**。
 */
import { T, F, TC, XFER_TX_MAX, xferPayload, linesToTcfg } from './protocol.js';
import { diffBytes, changedOffsets, popcount } from '../core/bytes.js';

export const REG_COLS = 16;
export const RX_PER_FRAME_MAX = 480;
export const TX_PER_FRAME_MAX = XFER_TX_MAX;
export const REG_LEN_MAX = 4096;            // 与 I2C 面板取齐（一屏最多 256 行）
export const REG_LEN_DFT = 128;

/** 档位种类：`reg` = 有寄存器号；`cmd` = 命令型（一次转换一个命令，没有寄存器空间）*/
export const KIND = { REG: 'reg', CMD: 'cmd' };

/**
 * 内置档位表。
 *
 * ⚠️ **只放核过手册的约定**（2026-10-03 逐条对过手册原文与 Linux 驱动，出处见每条的 `ref`；
 *    汇总表在 `docs/spi-register-panel.md` §1）。三条最容易抄错、也因此被本表修正过的：
 *    · **BMP280 读不需要 dummy**（那是 BMP380/BMP580 的规矩）；但 **BMP280 的写不自增**
 *      （手册 Figure 10 标题就是 "SPI multiple byte write (**not auto-incremented**)"）。
 *    · **ADXL345 也不要 dummy**，但多字节读**必须置 MB 位**（bit6 → `0xC0|reg`），且它是 **Mode 3**。
 *    · **ADS1256 无法用"命令+地址+长度"表达**（命令字节把地址/方向/数量-1 打包，两字节），
 *      所以**不放进档位表** —— 用「命令表」或脚本手写（见 docs 的说明）。
 */
export const PROFILES = [
  {
    id: 'custom', kind: KIND.REG, name: '自定义…', verified: '模板',
    note: '面板上每个字段都能改；内置档位只是把常见器件的默认值填好',
    readOp: 0x80, writeOp: 0x00, addrMode: 'orOp', addrBits: 7,
    dummy: 0, dataBytes: 1, autoInc: true, msbFirst: true, lines: 1, ref: '',
  },
  {
    id: 'mpu9250', kind: KIND.REG, name: 'MPU-9250 / MPU-6000（惯性）', verified: '手册已核',
    note: '读 = 0x80|reg，写 = reg&0x7F；WHO_AM_I 0x75（0x71/0x68）。⚠️ 全寄存器访问上限 1 MHz，只有传感器/中断寄存器能到 20 MHz',
    readOp: 0x80, writeOp: 0x00, addrMode: 'orOp', addrBits: 7,
    dummy: 0, dataBytes: 1, autoInc: true, msbFirst: true, lines: 1,
    ref: 'InvenSense PS-MPU-9250A-01 Rev1.1 §7.5（地址 = R/W|A6:A0；读 16 拍起；Mode 0）',
  },
  {
    id: 'bmp280', kind: KIND.REG, name: 'BMP280 / BME280（气压温湿）', verified: '手册已核',
    note: '读 = 0x80|reg（无 dummy：控制字节后数据立刻出来）；写 = reg&0x7F 且写不自增（每帧只能写一个寄存器）；ID 0xD0',
    readOp: 0x80, writeOp: 0x00, addrMode: 'orOp', addrBits: 7,
    dummy: 0, dataBytes: 1, autoInc: true, autoIncWrite: false, msbFirst: true, lines: 1,
    ref: 'Bosch BST-BMP280-DS001-26 §5.3/5.3.1/5.3.2（读自增、写不自增；Mode 00/11 自动选；10 MHz）',
  },
  {
    id: 'adxl345', kind: KIND.REG, name: 'ADXL345（加速度）', verified: '手册已核',
    note: '读 = 0x80|reg（多字节必须置 MB 位 → 0xC0|reg），写 = reg&0x3F；DEVID 0x00（0xE5）。⚠️ 它是 Mode 3（CPOL=1/CPHA=1），左栏模式要跟着改',
    readOp: 0x80, writeOp: 0x00, addrMode: 'orOp', addrBits: 6, multiReadBit: 6,
    dummy: 0, dataBytes: 1, autoInc: true, msbFirst: true, lines: 1,
    ref: 'ADI ADXL345 Rev.G SERIAL COMMUNICATIONS（MB 必须置位；CPOL=1/CPHA=1；5 MHz）',
  },
  {
    id: 'lsm6ds3', kind: KIND.REG, name: 'LSM6DS3TR-C（惯性）', verified: '手册已核',
    note: '读 = 0x80|reg，写 = reg&0x7F；多字节读的自增由 CTRL3_C(12h).IF_INC 控制（默认 1）；WHO_AM_I 0x0F',
    readOp: 0x80, writeOp: 0x00, addrMode: 'orOp', addrBits: 7,
    dummy: 0, dataBytes: 1, autoInc: true, msbFirst: true, lines: 1,
    ref: 'ST LSM6DS3TR-C DocID030071 Rev3 §6.4（RW 位 bit0 = 1 读；IF_INC 控制自增；10 MHz）',
  },
  {
    id: 'icm20602', kind: KIND.REG, name: 'ICM-20602 / ICM-20689（惯性）', verified: '驱动旁证',
    note: '与 MPU 同族（读 0x80|reg、写 reg）；WHO_AM_I 0x75。⚠️ 未取到 TDK 手册原文（站点 403），按 Linux inv-mpu6050 同框架推断',
    readOp: 0x80, writeOp: 0x00, addrMode: 'orOp', addrBits: 7,
    dummy: 0, dataBytes: 1, autoInc: true, msbFirst: true, lines: 1,
    ref: 'Linux drivers/iio/imu/inv_mpu6050（match 表含 icm20602）—— 属驱动推断，非手册',
  },
  {
    id: 'mcp23s17', kind: KIND.REG, name: 'MCP23S17（16 位 IO 扩展）', verified: '手册已核',
    note: 'opcode = 0100 A2A1A0 R/W：读 0x41 / 写 0x40（A2A1A0=000），寄存器地址是独立字节。⚠️ A2A1A0 要 IOCON.HAEN=1 才参与译码（上电默认 0）',
    readOp: 0x41, writeOp: 0x40, addrMode: 'bytes', addrBytes: 1, addrBits: 0,
    dummy: 0, dataBytes: 1, autoInc: true, msbFirst: true, lines: 1,
    ref: 'Microchip DS20001952C §3.2.3/Figure 3-5（控制字节 + 地址字节；顺序模式自增）',
  },
  {
    id: 'nrf24l01', kind: KIND.REG, name: 'nRF24L01+（2.4G 收发）', verified: '手册已核',
    note: '命令低 5 位是寄存器号：读 0x00|reg、写 0x20|reg。⚠️ 读寄存器回的第 1 字节是 STATUS；多字节寄存器是 LSByte 先；写寄存器只在 Power Down/Standby 有效',
    readOp: 0x00, writeOp: 0x20, addrMode: 'orOp', addrBits: 5,
    dummy: 0, dataBytes: 1, autoInc: true, msbFirst: true, lines: 1,
    ref: 'Nordic nRF24L01+ PS v1.0 §8.3.1/Table 20（R_REGISTER 000A AAAA / W_REGISTER 001A AAAA；10 MHz）',
  },
  {
    id: 'mcp3008', kind: KIND.CMD, name: 'MCP3008（10 位 8 通道 ADC）', verified: '手册已核',
    note: '命令型：单端通道 ch → 发 0x01, 0x80|(ch<<4), 0x00（24 拍；起始位前允许前导 0）；回 3 B，10 位结果 = u16be(1)&0x3FF',
    tx: ch => Uint8Array.of(0x01, 0x80 | ((ch & 7) << 4), 0x00),
    rx: 3, ref: 'Microchip DS21295D §5/Figure 5-1（start + SGL/DIFF + D2D1D0；null 位不是数据；3.6 MHz@5V）',
    sampleExpr: 'v=u16be(1)&0x3FF',
  },
  {
    id: 'mcp3208', kind: KIND.CMD, name: 'MCP3208（12 位 8 通道 ADC）', verified: '手册已核',
    note: '命令型：单端通道 ch → 发 0x06|(ch>>2), (ch&3)<<6, 0x00；回 3 B，12 位结果 = u16be(1)&0xFFF',
    tx: ch => Uint8Array.of(0x06 | ((ch >> 2) & 1), ((ch & 3) << 6) & 0xc0, 0x00),
    rx: 3, ref: 'Microchip DS21298E（single-ended 起始位 0x06；12 位结果 MSB first）',
    sampleExpr: 'v=u16be(1)&0xFFF',
  },
  {
    id: 'max6675', kind: KIND.CMD, name: 'MAX6675（K 型热电偶，只读）', verified: '手册已核',
    note: '命令型只读：不发命令，直接读 2 B；D15 是哑符号位，温度在 D14..D3 = (值>>3)×0.25 ℃（D2 = 断线）',
    tx: () => new Uint8Array(0),
    rx: 2, ref: 'Maxim MAX6675 Serial Interface（16 拍只读；D14–D3 温度；4.3 MHz）',
    sampleExpr: 't=u16be(0)>>3*0.25',
  },
  {
    id: 'max31855', kind: KIND.CMD, name: 'MAX31855（热电偶，只读 4 B）',
    note: '命令型只读：读 4 B；D31..D18 为热端温度（0.25 ℃/LSB，含符号），D15..D4 为冷端',
    tx: () => new Uint8Array(0),
    rx: 4, ref: 'Maxim MAX31855 数据手册（32 位只读帧）',
    sampleExpr: 't=i16be(0)>>2*0.25',
  },
];

export const profileById = id => PROFILES.find(p => p.id === id) || PROFILES[0];

/** 地址掩码：`orOp` 档位把地址或进 opcode 的低 `addrBits` 位 */
export const addrMask = p => p.addrMode === 'orOp' ? ((1 << Math.max(0, Math.min(8, p.addrBits ?? 8))) - 1) : 0xff;

/** 读命令字节（`cmd` 型没有 opcode，返回 null）*/
export function readOpcode(p, addr){
  if (p.kind === KIND.CMD || p.readOp == null) return null;
  return (p.addrMode === 'orOp' ? (p.readOp | (addr & addrMask(p))) : p.readOp) & 0xff;
}
/**
 * 多字节读时要额外置的位（**不是所有器件都自动递增**）：
 * ADXL345 的 `MB`（bit6）就是典型 —— 手册原文 "*must be set*"，忘了它 burst 读会每 8 拍停在同一个寄存器
 * （6 轴数据"三个一样"，看着像器件坏了）。
 */
const multiBitOf = (p, regs) => (regs > 1 && Number.isInteger(p.multiReadBit))
  ? (1 << Math.max(0, Math.min(7, p.multiReadBit))) : 0;
/** 写命令字节 */
export function writeOpcode(p, addr){
  if (p.kind === KIND.CMD || p.writeOp == null) return null;
  return (p.addrMode === 'orOp' ? (p.writeOp | (addr & addrMask(p))) : p.writeOp) & 0xff;
}

/** 一个寄存器号 → 显示用文本（`orOp` 档位下掩码决定它的有效范围）*/
export function regLabel(p, addr){
  const a = addr & (p.addrMode === 'orOp' ? addrMask(p) : 0xffff);
  return '0x' + a.toString(16).toUpperCase().padStart(2, '0');
}

/**
 * **读计划**：从 `start` 号寄存器起读 `count` 个寄存器。
 * @returns {{frames:Array<object>, regs:number, bytes:number, batch:boolean}}
 *   `batch` = true 时是"一条命令读一整段"（器件自动递增），false 表示一寄存器一帧
 */
export function readPlan(p, start = 0, count = 1){
  const regs = Math.max(1, count | 0);
  const dataBytes = Math.max(1, Math.min(4, p.dataBytes | 0));
  const want = regs * dataBytes;
  const frames = [];
  if (p.kind === KIND.CMD){
    // 命令型：一个"寄存器号"= 一次转换/一个通道；每帧独立（有的还能连读，但语义上必须分开）
    for (let i = 0; i < regs; i++){
      const tx = p.tx ? p.tx(start + i) : new Uint8Array(0);
      frames.push({ kind: KIND.CMD, tx, rx: p.rx ?? 1, dummy: 0, lines: p.lines ?? 1,
                    index: start + i, note: `命令 #${start + i}${tx.length ? ' tx=' + tx.length + 'B' : '（只读）'}` });
    }
    return { frames, regs, bytes: regs * (p.rx ?? 1), batch: false };
  }
  if (p.autoInc === false){
    for (let i = 0; i < regs; i++){
      frames.push(regFrame(p, start + i, { rx: dataBytes, note: `读 ${regLabel(p, start + i)}` }));
    }
    return { frames, regs, bytes: want, batch: false };
  }
  // 自动递增：一条命令读整段；超过单帧读上限就按"每帧重发命令+新起始地址"切
  const per = Math.max(1, Math.floor(RX_PER_FRAME_MAX / dataBytes));
  let left = regs;
  let at = start;
  while (left > 0){
    const n = Math.min(per, left);
    frames.push(regFrame(p, at, { rx: n * dataBytes, multi: n > 1,
      note: `读 ${regLabel(p, at)}… × ${n} 寄存器（${n * dataBytes} B）` }));
    left -= n; at += n;
  }
  return { frames, regs, bytes: want, batch: frames.length === 1 };
}

/** 造一条读寄存器帧（内部用）—— `multi` = 这一帧读多个寄存器（ADXL345 这类要额外置 MB 位）*/
function regFrame(p, addr, { rx, note, multi = false }){
  const cmd = readOpcode(p, addr);
  const mb = multiBitOf(p, multi ? 2 : 1);
  return {
    cmd: cmd == null ? null : (cmd | mb) & 0xff,
    rx, dummy: p.dummy | 0, lines: p.lines ?? 1,
    addrLen: p.addrMode === 'bytes' ? Math.max(0, Math.min(4, p.addrBytes | 0)) : 0,
    addr: addr >>> 0, tx: new Uint8Array(0),
    note: note || `读 ${regLabel(p, addr)}`,
  };
}

/**
 * **写计划**：把要写的字节发下去。`offsets` 给"只写改动"的下标（缺省整块）。
 * 相邻下标会合并成一帧；每帧自带命令与地址（SPI 的 STOP 之后没有"指针"可依赖）。
 *
 * 🚨 **写自增是独立的开关**（`autoIncWrite`，缺省 = `autoInc`）：BMP280/BME280 的**读会自增、
 *    写不自增**（手册 Figure 10 标题就是 "SPI multiple byte write (**not auto-incremented**)"）——
 *    一帧里塞多个字节，器件只会把它们都写进**同一个**寄存器（最后一个生效）。
 *    所以这类器件（`autoIncWrite: false`）必须"一寄存器一帧"，本函数会自动把改动对齐到**整寄存器**。
 *
 * @returns {{frames:Array<object>, bytes:number, frameCount:number, aligned:boolean}}
 */
export function writePlan(p, start = 0, data = new Uint8Array(0), { offsets = null } = {}){
  const buf = Array.from(data || []);
  const dataBytes = Math.max(1, Math.min(4, p.dataBytes | 0));
  let offs = (offsets == null ? buf.map((_, i) => i) : Array.from(offsets))
    .filter(o => Number.isInteger(o) && o >= 0 && o < buf.length)
    .sort((a, b) => a - b);
  const frames = [];
  if (!offs.length) return { frames, bytes: 0, frameCount: 0, aligned: false };
  /** 写不自增的器件：改动要补齐成"整寄存器"（16 位寄存器只改低字节也得连高字节一起发） */
  const perReg = p.kind !== KIND.CMD && p.autoIncWrite === false;
  let aligned = false;
  if (perReg && dataBytes > 1){
    const set = new Set(offs);
    for (const o of offs){
      const base = o - (o % dataBytes);
      for (let k = 0; k < dataBytes; k++) if (base + k < buf.length) set.add(base + k);
    }
    const next = [...set].sort((a, b) => a - b);
    aligned = next.length !== offs.length;
    offs = next;
  }
  const perFrame = p.kind === KIND.CMD ? dataBytes
    : perReg ? dataBytes                                    // 一帧一个寄存器
    : Math.max(dataBytes, TX_PER_FRAME_MAX - (p.addrMode === 'bytes' ? (p.addrBytes | 0) : 0));
  for (let i = 0; i < offs.length;){
    const off = offs[i];
    const reg = start + Math.floor(off / dataBytes);
    let j = i + 1;
    // 同一帧里必须是**连续的字节**、落在同一段、且不超上限
    while (j < offs.length && offs[j] === offs[j - 1] + 1 && (offs[j] - off) < perFrame) j++;
    const chunk = [];
    for (let k = i; k < j; k++) chunk.push(buf[offs[k]] & 0xff);
    const cmd = writeOpcode(p, reg);
    const isCmd = p.kind === KIND.CMD;
    frames.push({
      cmd, rx: 0, dummy: 0, lines: p.lines ?? 1,
      addrLen: !isCmd && p.addrMode === 'bytes' ? Math.max(0, Math.min(4, p.addrBytes | 0)) : 0,
      addr: reg >>> 0, tx: Uint8Array.from(chunk),
      reg, off,
      note: isCmd ? `命令 #${reg} tx=${chunk.length}B` : `写 ${regLabel(p, reg)}+${chunk.length}B`,
    });
    i = j;
  }
  return { frames, bytes: offs.length, frameCount: frames.length, aligned };
}

/** 一帧 → 可直接交给 `session.sendFrames()` 的 XFER 项 */
export function xferItem(f, flags = 0){
  const tx = f.tx instanceof Uint8Array ? f.tx : new Uint8Array(f.tx || 0);
  let tcfg = linesToTcfg(f.lines ?? 1);
  if (f.cmd != null) tcfg |= TC.CMD_EN;
  if ((f.addrLen | 0) > 0) tcfg |= TC.ADDR_EN;
  const rx = (f.rx | 0) > 0 ? f.rx | 0 : 0;
  return {
    type: T.XFER,
    payload: xferPayload({ cmd: f.cmd ?? 0, tcfg, addrLen: (f.addrLen | 0) > 0 ? f.addrLen : 0,
                           dummy: f.dummy | 0, addr: f.addr >>> 0, tx, rxLen: rx }),
    /**
     * 🚨 **写帧也要 RSP**：寄存器面板靠应答判断"这一片到底成没成"（`status` 非 OK 就不更新
     *    "器件现值"，表里保留你的改动）；没有应答就只能盲写。读帧本来就必须带。
     */
    flags: F.RSP | flags,
    label: f.note || '',
    read: rx > 0,
  };
}

export const itemsOf = (frames, flags = 0) => (frames || []).map(f => xferItem(f, flags));

/** 把一批帧的读数据按顺序拼起来（读计划 → 一块连续字节；命令型则是每帧一块）*/
export function joinReads(frames, rsps){
  const parts = [];
  let bad = 0;
  for (let i = 0; i < frames.length; i++){
    const want = frames[i].rx | 0;
    if (!want) continue;
    const r = rsps?.[i];
    if (!r || r.error || r.status !== 0){ bad++; continue; }
    const d = r.data instanceof Uint8Array ? r.data : new Uint8Array(r.data || 0);
    parts.push(d);
  }
  const total = parts.reduce((n, p) => n + p.length, 0);
  const out = new Uint8Array(total);
  let at = 0;
  for (const p of parts){ out.set(p, at); at += p.length; }
  return { data: out, bad, short: parts.length !== frames.filter(f => f.rx).length };
}

/** 面板摘要一行字 */
export function summarize({ profile, start = 0, count = 0, base = null, cur = null } = {}){
  const p = profile || PROFILES[0];
  const head = `${p.name} · 起 ${regLabel(p, start)} × ${count} 寄存器` +
    `（${p.dataBytes} B/寄存器 · ${p.addrMode === 'orOp' ? `opcode 或地址(低 ${p.addrBits ?? 8} 位)` : p.addrMode === 'bytes' ? `${p.addrBytes} B 地址` : '无地址'}` +
    `${p.dummy ? ` · dummy ${p.dummy} B` : ''}${p.autoInc ? '' : ' · 不自增（一寄存器一帧）'}）`;
  if (!base || !base.length) return `还没读 —— ${head}`;
  const d = diffBytes(base, cur || base);
  const bits = popcount(base);
  return `${head} · 读回 ${base.length} B · 置 1 的位 ${bits}` +
    (d.length ? ` · 改了 ${d.length} 个字节（可「只写改动」）` : ' · 没有改动');
}

export { diffBytes, changedOffsets, popcount };
