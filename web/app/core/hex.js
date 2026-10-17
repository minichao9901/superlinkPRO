/**
 * HEX / 文本 / 字节 互转。
 *
 * `parseHex` 的分词规则（**按分隔符判断，不靠猜**）：
 *   · 用空格 / 逗号 / 分号 / 冒号 / 下划线 / 连字符 / 竖线 / 换行 **或 `0x` 前缀**隔开的每一段
 *     就是**一个字节**，写一位也当它是一位（`1` = `01`）——
 *     `"0x1,0x2"` → `[01 02]`、`"A 5 F 0"` → `[A5 F0 00...]` 四个字节；
 *   · **没有分隔符的连续串**才两位一组拆：`"01030a"` → `[01 03 0A]`、`"0x0103"` → `[01 03]`；
 *   · 非法字符、或连续串是奇数位 → 返回错误说明，由界面提示。
 *
 * 🚨 老实现是"把所有非十六进制字符删掉、再两位一组切"，中间还用一个 `pending` 把**上一段的
 *    零头带到下一段** —— 于是 `"0x1,0x2"`（用户明确写了两个字节）被拼成 0x12 一个字节、
 *    `"1 2"` 也变成 0x12、`"0x01 0x2 0x03"` 直接报"位数是奇数"。
 *    已经写了分隔符还被悄悄改变语义，是最难查的一类（串口助手/RTT 下行都走这里，
 *    2026-10 代码审查抓到）。现在的规则与它一致：**分隔符 = 字节边界**。
 */

const HEXCH = /^[0-9a-fA-F]+$/;

/** @returns {{bytes:Uint8Array, error:string|null}} */
export function parseHex(text){
  const s = String(text || '').replace(/0[xX]/g, ' ').replace(/[,;:_\-\t\r\n|]/g, ' ');
  const toks = s.split(/\s+/).filter(Boolean);
  const out = [];
  for (const t of toks){
    if (!HEXCH.test(t)) return { bytes: new Uint8Array(0), error: `HEX 里有非法字符："${t}"` };
    if (t.length <= 2){ out.push(parseInt(t, 16)); continue; }        // 有分隔符 → 这一整段就是一个字节
    if (t.length % 2) return { bytes: new Uint8Array(0), error: `HEX 位数是奇数："${t}"` };
    for (let i = 0; i < t.length; i += 2) out.push(parseInt(t.substr(i, 2), 16));
  }
  return { bytes: Uint8Array.from(out), error: null };
}

/** 字节 → 十六进制字符串 */
export function bytesToHex(b, sep = ' '){
  let s = '';
  for (let i = 0; i < b.length; i++){
    if (i) s += sep;
    s += b[i].toString(16).padStart(2, '0').toUpperCase();
  }
  return s;
}

/** 字节 → HEX 视图文本（每行 16 字节，遇到 0x0A 也换行） */
export function bytesToHexView(b){
  const rows = [];
  let row = [];
  for (let i = 0; i < b.length; i++){
    row.push(b[i].toString(16).padStart(2, '0').toUpperCase());
    if (row.length === 16 || b[i] === 0x0a){ rows.push(row.join(' ')); row = []; }
  }
  if (row.length) rows.push(row.join(' '));
  return rows.length ? rows.join('\n') + '\n' : '';
}

/** 可打印字符判定（ASCII 视图用它把不可打印字符显示成 ·） */
export function isPrintable(code){
  return code === 0x09 || (code >= 0x20 && code !== 0x7f);
}

// 控制字符（保留 \t \n \r）→ ·，其余原样 —— ANSI 转义序列属于"原样"，看得见才方便排错
const CTRL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g;
export const ctrlMap = s => s.replace(CTRL, '·');

/**
 * 字节 → 文本。
 * 🚨 必须按 UTF-8 解码：早期版本是逐字节 String.fromCharCode（Latin-1），
 * 设备发中文时整句变成 `···`（自检用例抓到的）。不可打印字节仍显示成 ·。
 * @param {Uint8Array} b
 * @param {TextDecoder} [decoder] 传进来就按流式解码（跨包的多字节字符不会被拆坏）
 */
export function bytesToText(b, decoder){
  const s = decoder ? decoder.decode(b, { stream: true }) : new TextDecoder('utf-8', { fatal: false }).decode(b);
  return ctrlMap(s);
}

/** 追加行尾 */
export function withEol(text, eol){
  switch (eol){
    case 'crlf': return text + '\r\n';
    case 'cr': return text + '\r';
    case 'lf': return text + '\n';
    default: return text;
  }
}

/** 文本 → 字节（UTF-8） */
export function textToBytes(text){
  return new TextEncoder().encode(text);
}

export const EOL_LABEL = { none: '无', crlf: '\\r\\n', cr: '\\r', lf: '\\n' };
