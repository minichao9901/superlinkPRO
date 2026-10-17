/**
 * 「寄存器」面板的**纯逻辑**（不含 DOM、不含 HID）—— Node 自测直接打。
 *
 * 面板干的事：从某个器件地址读一段寄存器回来（默认 128 B = 16×8 一张表），
 * 每个字节都能点开 8 个 bit 单独翻，改完再按"只写改过的那几个"或"整块写回"发下去。
 * 这里只负责**能算错的部分**：
 *
 *   · 输入解析（`0x50` / `50` / `80` / 空 → 数字；地址宽度 0/1/2 B；长度夹到 1..4096）
 *   · **改动 diff**（哪些字节与"读回来的原值"不同）—— 写回时的下发范围就是它
 *   · bit 操作（点一下翻一位 / 置 0 / 置 1）
 *   · ASCII 一列（查 EEPROM 里的字符串时不用自己数）
 *
 * 分片计划不在这里：读走 `protocol.planRead`（`session.readLong`），写走 `protocol.planWrite`
 * （`session.writeLong`）—— 一份"能不能一次发出去"的知识只放在协议层，别在页面里再抄一遍。
 */
import { RD_TOTAL_MAX, hex2, bump } from './protocol.js';
/**
 * 字节级工具（diff / ASCII / bit）**从 app/core/bytes.js 转出** —— 与 `#spi` 的寄存器面板共一份实现
 * （两边的线上形状不同，但"拿到字节之后怎么显示/比改动/翻位"必须一致，否则两个面板的语义会漂）。
 */
import {
  diffBytes, changedOffsets, asciiOf, bitsOf, setBit, toggleBit, popcount, bytesEq,
} from '../core/bytes.js';

export { diffBytes, changedOffsets, asciiOf, bitsOf, setBit, toggleBit, popcount, bytesEq };

export const REG_LEN_DFT = 128;              // 默认一次读 128 B（16 列 × 8 行）
export const REG_LEN_MAX = RD_TOTAL_MAX;     // 4096（协议侧 RD_TOTAL_MAX）
export const REG_COLS = 16;                  // 每行 16 字节（照常规 hexdump）
/** 写回时的分片档位：51 = 协议上限；8/32 是 EEPROM 页写（AT24C02 页 8 B、AT24C32 页 32 B）*/
export const WRITE_CHOICES = [
  [51, '51 B（协议上限，寄存器/传感器用这个）'],
  [32, '32 B（AT24C32 页写 / 保险一点）'],
  [16, '16 B'],
  [8, '8 B（AT24C02 页写）'],
];

/**
 * 器件的**写页大小**档位。0 = 不限（寄存器/传感器类，没有页写回卷）。
 *
 * 🚨 这一格和「写分片」是**两件事**，别混：
 *   · 「写分片」只决定"一片最多几个字节"；
 *   · 「页大小」决定"一片不许越过哪条页界"。
 *   只给前者时，起始地址不是页倍数就会跨页 → 器件内部回绕，后写的盖掉先写的，
 *   而它**全程老实 ACK**（2026-10 代码审查：`planWrite` 因此会把 EEPROM 写花而界面显示成功）。
 *   典型页大小：AT24C02 = 8、AT24C04/08/16 = 16、AT24C32/64 = 32、AT24C128/256 = 64、AT24C512 = 128。
 */
export const PAGE_CHOICES = [
  [0, '不限（寄存器/传感器）'],
  [8, '8 B（AT24C02）'],
  [16, '16 B（AT24C04/08/16）'],
  [32, '32 B（AT24C32/64）'],
  [64, '64 B（AT24C128/256）'],
  [128, '128 B（AT24C512）'],
];

/** 0x50 / 50 / 0X50 → 80；非法就抛（错误信息里带上原样输入，方便对账）*/
export function parseNum(text, what = '数值'){
  const s = String(text ?? '').trim();
  if (!s) throw new Error(`${what}是空的`);
  const m = /^(?:0[xX])?([0-9a-fA-F]+)$/.exec(s);
  if (!m) throw new Error(`${what}「${s}」不是十六进制数（例：0x50 / 50）`);
  return parseInt(m[1], 16);
}

/** 7 位器件地址（0x08..0x77，与固件扫描范围一致）*/
export function parseDev(text){
  const v = parseNum(text, '器件地址');
  if (v < 0x08 || v > 0x77) throw new Error(`器件地址 ${hex2(v)} 超出 7 位可用范围（0x08..0x77）`);
  return v;
}

/**
 * 起始寄存器地址 → 字节数组（**大端**：0x0100 → [0x01, 0x00]，这是 I2C 器件的通行排法）。
 * @param {string} text 用户输入
 * @param {number} addrLen 0 / 1 / 2（0 = 纯读，不带子地址）
 */
export function parseStart(text, addrLen = 1){
  const n = Math.max(0, Math.min(2, addrLen | 0));
  if (n === 0) return [];
  const s = String(text ?? '').trim();
  const v = s ? parseNum(s, '起始寄存器') : 0;
  if (n === 1){
    if (v > 0xff) throw new Error(`起始寄存器 0x${v.toString(16)} 超过 1 字节 —— 把「地址宽度」改成 2 B`);
    return [v & 0xff];
  }
  if (v > 0xffff) throw new Error(`起始寄存器 0x${v.toString(16)} 超过 2 字节（最大 0xFFFF）`);
  return [(v >> 8) & 0xff, v & 0xff];
}

/** 读长：1..4096，缺省 128；非法就抛 */
export function parseLen(text){
  const s = String(text ?? '').trim();
  const v = s ? (/^0[xX]/.test(s) ? parseNum(s, '长度') : Number(s)) : REG_LEN_DFT;
  if (!Number.isFinite(v) || !Number.isInteger(v)) throw new Error(`长度「${s}」不是整数`);
  if (v < 1 || v > REG_LEN_MAX) throw new Error(`长度要在 1..${REG_LEN_MAX} 之间（给的是 ${v}）`);
  return v;
}

/** 与"读回来的原值"逐字节比，返回改动 [{off, from, to}] —— 实现在 app/core/bytes.js（与 SPI 面板共用）*/

/**
 * 起始地址 + 偏移 → 表里显示的地址文本。
 * 1 B 地址：`0x00`、`0x10`；2 B 地址：`0x0100`；地址宽度 0（纯读）没有地址可标，退化成 `+偏移`。
 * 这个函数被表格的每一行/每一格用来标地址，**必须跟着「地址宽度」走** ——
 * 否则 2 B 地址的器件会被标成只剩低字节，看着像 0x00 起的同一页。
 */
export function addrLabel(start, off, addrLen){
  if (!addrLen) return '+' + off;
  // ⚠️ 自己拼而不是 join(hex2)：hex2 每字节都带 `0x`，拼两字节会得到 `0x010x3C`（踩过）
  return '0x' + bump(start, off).slice(0, addrLen)
    .map(v => (v & 0xff).toString(16).toUpperCase().padStart(2, '0')).join('');
}

/** 面板摘要一行字（view 直接贴到 #i2-reg-sum）*/
export function summarize({ len = 0, base = null, cur = null, dev = 0, start = [], addrLen = 1 } = {}){
  const addrTxt = addrLen === 0 ? '（纯读）' : '[' + (start || []).map(hex2).join(' ') + ']';
  if (!base || !base.length) return `还没读 —— 器件 ${hex2(dev)} ${addrTxt} · 计划读 ${len} B`;
  const d = diffBytes(base, cur || base);
  const head = `${hex2(dev)} ${addrTxt} · ${base.length} B · 置 1 的位 ${popcount(base)}`;
  return d.length ? `${head} · 改了 ${d.length} 个字节（可「只写改动」）` : `${head} · 没有改动`;
}
