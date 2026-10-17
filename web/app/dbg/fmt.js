/**
 * 调试页的纯格式化 / 解析工具（零依赖，Node 里可直接跑自测）。
 *
 * 单独一个模块的理由：命令行的输出与输入的解析规则**必须**能被自测覆盖
 * （`md 0x20000000 64`、`mw 0x20000000 01 02 03` 这类字符串是最容易出边界问题的地方），
 * 而它们跟硬件一点关系都没有。
 */

/** 32 位十六进制（固定 8 位，补零） */
export const hex32 = n => '0x' + ((n >>> 0) >>> 0).toString(16).padStart(8, '0');
/** 16 位十六进制 */
export const hex16 = n => '0x' + ((n >>> 0) & 0xffff).toString(16).padStart(4, '0');
/** 8 位十六进制 */
export const hex8 = n => ((n >>> 0) & 0xff).toString(16).padStart(2, '0').toUpperCase();

/** 0x 前缀的十六进制（不补零），用于"值"的显示 */
export const hexShort = n => '0x' + ((n >>> 0) >>> 0).toString(16);

/**
 * 解析一个数字。接受（都忽略大小写与 `_` 分隔）：
 *   0x1a / 1Ah（尾部 h）/ 0b1010 / 0o17 / 十进制 42
 * @returns {number|null} null = 解析不了（调用方负责报错，别静默当 0）
 */
export function parseNum(text){
  const s = String(text ?? '').trim().replace(/_/g, '');
  if (!s) return null;
  let m;
  if ((m = /^0x([0-9a-f]+)$/i.exec(s))) return Number.parseInt(m[1], 16) >>> 0;
  if ((m = /^([0-9a-f]+)h$/i.exec(s))) return Number.parseInt(m[1], 16) >>> 0;
  if ((m = /^0b([01]+)$/i.exec(s))) return Number.parseInt(m[1], 2) >>> 0;
  if ((m = /^0o([0-7]+)$/i.exec(s))) return Number.parseInt(m[1], 8) >>> 0;
  if ((m = /^\d+$/.exec(s))) return Number.parseInt(s, 10) >>> 0;
  return null;
}

/**
 * 解析一串字节。接受：
 *   `01 02 ff` / `0x01,0x02` / `01-02-03` / 连续的 `0102ff`
 * 🚨 连续写法的歧义（`123` 是 0x01,0x23 还是 0x12,0x3？）按 **必须偶数长度** 处理，
 *    奇数长度直接报错 —— 猜错的代价是往目标内存里写错字节，宁可让用户写清楚。
 * @returns {Uint8Array} 解析失败抛错（带人话原因）
 */
export function parseBytes(text){
  const s = String(text ?? '').trim();
  if (!s) return new Uint8Array(0);
  const sep = /[\s,;]+/;
  let parts;
  if (/^0x[0-9a-f]{1,2}$/i.test(s) || sep.test(s) || /^[0-9a-f]{1,2}$/i.test(s)) parts = s.split(sep);
  else parts = null;
  if (!parts){
    const t = s.replace(/^0x/i, '');
    if (!/^[0-9a-f]+$/i.test(t)) throw new Error(`看不懂的字节串：「${s}」（例：01 02 ff）`);
    if (t.length % 2) throw new Error(`连续写法要偶数个十六进制字符（「${t}」是 ${t.length} 个）—— 请写成 01 02 ff 这样`);
    parts = t.match(/../g);
  }
  const out = [];
  for (const p of parts){
    if (!p) continue;
    const v = parseNum(/^0x/i.test(p) ? p : ('0x' + p));
    if (v === null || v > 0xff) throw new Error(`不是一个字节：「${p}」（要 00~ff）`);
    out.push(v & 0xff);
  }
  return Uint8Array.from(out);
}

/** 可打印 ASCII（0x20~0x7e），其余显示成 `.` */
export const printable = b => (b >= 0x20 && b <= 0x7e) ? String.fromCharCode(b) : '.';

/**
 * gdb 风格的十六进制 dump。
 * @param {Uint8Array} bytes 数据
 * @param {number} base 起始地址（显示用）
 * @param {{width?:number, ascii?:boolean}} opts
 * @returns {string[]} 每行一条
 */
export function hexdump(bytes, base = 0, opts = {}){
  const width = opts.width || 16;
  const ascii = opts.ascii !== false;
  const lines = [];
  for (let i = 0; i < bytes.length; i += width){
    const chunk = bytes.subarray(i, Math.min(i + width, bytes.length));
    const cells = [];
    for (let k = 0; k < width; k++){
      cells.push(k < chunk.length ? hex8(chunk[k]) : '  ');
      if (k === width / 2 - 1) cells.push('');            // 中间多一个空格（跟 gdb 一样好读）
    }
    let s = hex32((base + i) >>> 0) + '  ' + cells.join(' ');
    if (ascii){
      let a = '';
      for (let k = 0; k < chunk.length; k++) a += printable(chunk[k]);
      s += '  |' + a.padEnd(width, ' ') + '|';
    }
    lines.push(s);
  }
  return lines;
}

/** 读取小端整数（长度 1/2/4/8） */
export function u32leBytes(v){ return Uint8Array.of(v & 0xff, (v >>> 8) & 0xff, (v >>> 16) & 0xff, (v >>> 24) & 0xff); }

/**
 * 半字对齐（Thumb 地址）。
 * 🚨 必须 `>>> 0` 归一化：`x & ~1` 是**32 位有符号**位运算，PPB 地址（≥0x80000000）
 *    会变成负数 —— 本仓在 dap-webusb.js 里为同一个坑写过整段说明（subarray 越界读回空数组）。
 */
export const align2 = x => (((x >>> 0) & ~1) >>> 0);
/** 字对齐 */
export const align4 = x => (((x >>> 0) & ~3) >>> 0);

/** 从字节数组按小端读一个无符号整数（最多 8 字节） */
export function readLE(bytes, off = 0, len = bytes.length - off){
  let v = 0;
  for (let i = Math.min(len, 8) - 1; i >= 0; i--) v = v * 256 + (bytes[off + i] || 0);
  return v;
}
