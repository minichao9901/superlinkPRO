/**
 * 字节级小工具（纯函数，无 DOM、无协议依赖）—— **两个「寄存器面板」共用**：
 *   · `#i2c` 的寄存器面板（app/i2c/registers.js 从这里转出这些函数）
 *   · `#spi` 的寄存器面板（app/spi/regs.js）
 *
 * 抽出来的理由：I2C 与 SPI 的**线上形状完全不同**（一个是子地址 + 读，一个是 opcode/地址/dummy），
 * 但"拿到一串字节之后怎么显示、怎么比改动、怎么翻某一位"是一模一样的 —— 那部分只该有一份实现，
 * 否则两个面板的 bit 语义、ASCII 列、diff 口径迟早会漂。
 *
 * 这里**不放**任何与具体总线有关的东西：地址文本怎么标（I2C 的子地址 bump、SPI 的寄存器号）
 * 归各自的模块。
 */

/** 0x2A 风格的两字符十六进制（**自带 `0x` 前缀** —— 拼多字节时别直接 join，会得到 `0x010x3C`）*/
export const hex2 = v => '0x' + (v & 0xff).toString(16).toUpperCase().padStart(2, '0');

/** 不带前缀的两位十六进制（表格里的字节文本用它）*/
export const h2 = v => (v & 0xff).toString(16).toUpperCase().padStart(2, '0');

/** 一串字节 → "0xAA 0xBB"（带前缀，日志用）*/
export const hexBytes = a => Array.from(a || []).map(hex2).join(' ');

/** 一串字节 → "AA BB"（不带前缀，紧凑显示用）*/
export const hexList = (a, sep = ' ') => Array.from(a || []).map(h2).join(sep);

/** 逐字节比较，返回改动 [{off, from, to}]（长度取两者较短者）*/
export function diffBytes(base, cur){
  const out = [];
  const n = Math.min(base?.length || 0, cur?.length || 0);
  for (let i = 0; i < n; i++) if (base[i] !== cur[i]) out.push({ off: i, from: base[i], to: cur[i] });
  return out;
}

/** 改动字节的下标（写回「只写改动」用的就是它）*/
export const changedOffsets = (base, cur) => diffBytes(base, cur).map(d => d.off);

/** hexdump 那种 ASCII 列：可打印 0x20..0x7E 原样，其余一个点 */
export function asciiOf(bytes, from = 0, len = null){
  const end = from + (len == null ? bytes.length : len);
  let s = '';
  for (let i = from; i < end && i < bytes.length; i++){
    const v = bytes[i];
    s += v >= 0x20 && v <= 0x7e ? String.fromCharCode(v) : '.';
  }
  return s;
}

/** 8 个 bit（下标 0 = LSB）*/
export function bitsOf(v){
  const out = new Array(8);
  for (let k = 0; k < 8; k++) out[k] = (v >> k) & 1;
  return out;
}

/** 置/清一位（返回新值，0..255）*/
export const setBit = (v, k, on) => (on ? (v | (1 << k)) : (v & ~(1 << k))) & 0xff;
/** 翻一位 */
export const toggleBit = (v, k) => (v ^ (1 << k)) & 0xff;

/** 「1 字节 = 多少位被置 1」—— 摘要里看一眼密度就知道这段是数据还是空白 */
export function popcount(bytes){
  let n = 0;
  for (const b of bytes || []){ let v = b & 0xff; while (v){ v &= v - 1; n++; } }
  return n;
}

/** 两串字节是否逐字节相同（自测里到处要用）*/
export const bytesEq = (a, b) => !!a && !!b && a.length === b.length && a.every((v, i) => v === b[i]);
