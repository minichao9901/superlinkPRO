/**
 * 读回来的字节 → **有名字的数值**（`as` 子句）。纯函数，不碰 DOM。
 *
 * 为什么要有它：I2C 的读回原样是一串十六进制，看寄存器的用途下一半靠它 ——
 * MPU6050 的加速度得除以 16384 才是 g，ADS1115 的码值得乘 125 µV 才是伏。
 * 没有这层，"while(1) 读传感器"就只剩一屏跳动的十六进制，看不出是个传感器。
 *
 * ── 语法（一行里多个用逗号分隔）────────────────────────────────────────────
 *   as ax=i16be(0)/16384, ay=i16be(2)/16384, az=i16be(4)/16384
 *   as t=i16be(6)/340+36.53
 *   as v=i16be(0)*0.000125
 *   as cfg=u16be(0), mux=u16be(0)>>12&7
 *   as hex                                  只显示原始字节（等价于不写 as）
 *
 * ── 函数（括号里是**字节偏移**，省略 = 0）────────────────────────────────
 *   u8 i8 u16be i16be u16le i16le u24be u32be i32be u32le f32be f32le  （默认大端，
 *   因为 I2C 器件的多字节寄存器几乎都是大端 —— MPU6050 / ADS1115 都是）
 *
 * ── 运算（**严格从左到右，没有优先级**）──────────────────────────────────
 *   + - * / %  >>  <<  &  |
 *   `i16be(0)/340+36.53` = (raw/340)+36.53  ✓；`u16be(0)>>12&7` = ((w>>12)&7) ✓
 *   要别的顺序就自己加括号 —— 括号在**参数位置**支持，表达式里只支持一层函数调用。
 *   （理由：这是给人手写的速记，不是编程语言；从左到右正好是"先换算再偏置"的自然写法。）
 */

const FNS = {
  u8:   (b, o) => b[o] ?? 0,
  i8:   (b, o) => ((b[o] ?? 0) << 24) >> 24,
  u16be: (b, o) => ((b[o] ?? 0) << 8) | (b[o + 1] ?? 0),
  i16be: (b, o) => ((((b[o] ?? 0) << 8) | (b[o + 1] ?? 0)) << 16) >> 16,
  u16le: (b, o) => ((b[o + 1] ?? 0) << 8) | (b[o] ?? 0),
  i16le: (b, o) => ((((b[o + 1] ?? 0) << 8) | (b[o] ?? 0)) << 16) >> 16,
  u24be: (b, o) => ((b[o] ?? 0) << 16) | ((b[o + 1] ?? 0) << 8) | (b[o + 2] ?? 0),
  u32be: (b, o) => (((b[o] ?? 0) << 24) | ((b[o + 1] ?? 0) << 16) | ((b[o + 2] ?? 0) << 8) | (b[o + 3] ?? 0)) >>> 0,
  i32be: (b, o) => ((b[o] ?? 0) << 24) | ((b[o + 1] ?? 0) << 16) | ((b[o + 2] ?? 0) << 8) | (b[o + 3] ?? 0),
  u32le: (b, o) => (((b[o + 3] ?? 0) << 24) | ((b[o + 2] ?? 0) << 16) | ((b[o + 1] ?? 0) << 8) | (b[o] ?? 0)) >>> 0,
  f32be: (b, o) => new DataView(Uint8Array.from([b[o] ?? 0, b[o + 1] ?? 0, b[o + 2] ?? 0, b[o + 3] ?? 0]).buffer).getFloat32(0, false),
  f32le: (b, o) => new DataView(Uint8Array.from([b[o] ?? 0, b[o + 1] ?? 0, b[o + 2] ?? 0, b[o + 3] ?? 0]).buffer).getFloat32(0, true),
};
export const FN_NAMES = Object.keys(FNS);

const OPS = ['>>', '<<', '+', '-', '*', '/', '%', '&', '|'];

/** 函数名 → 这个函数要几个字节（越界时给个提示用的） */
const FN_BYTES = { u8: 1, i8: 1, u16be: 2, i16be: 2, u16le: 2, i16le: 2, u24be: 3, u32be: 4, i32be: 4, u32le: 4, f32be: 4, f32le: 4 };

const num = t => {
  const s = String(t).trim();
  if (/^0x[0-9a-f]+$/i.test(s)) return parseInt(s, 16);
  if (/^-?\d+(\.\d+)?([eE][-+]?\d+)?$/.test(s)) return parseFloat(s);
  return NaN;
};

/**
 * 解析一条表达式。
 * @returns {{ok:true, ast:object} | {ok:false, why:string}}
 */
export function parseExpr(text){
  let s = String(text ?? '').trim();
  if (!s) return { ok: false, why: '表达式是空的' };
  let fn = null, off = 0;
  const m = /^([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:\(\s*([^)]*)\s*\))?/.exec(s);
  if (m){
    const name = m[1].toLowerCase();
    if (name === 'hex') return { ok: true, ast: { kind: 'hex' } };
    if (!FNS[name]) return { ok: false, why: `没有这个函数：${m[1]}（可用：${FN_NAMES.join(' ')}）` };
    fn = name;
    if (m[2] != null && m[2] !== ''){
      off = num(m[2]);
      if (!Number.isFinite(off) || off < 0) return { ok: false, why: `偏移 "${m[2]}" 不是非负数字` };
      off = Math.trunc(off);
    }
    s = s.slice(m[0].length);
  } else {
    // 常量开头（少见，但 `as v=3.3` 这种写法不该报"函数不存在"）
    const c = /^-?(0x[0-9a-f]+|\d+(\.\d+)?)/i.exec(s);
    if (!c) return { ok: false, why: `表达式开头看不懂："${s}"` };
    fn = null; off = num(c[0]);
    s = s.slice(c[0].length);
  }
  const ops = [];
  s = s.trim();
  while (s){
    const op = OPS.find(o => s.startsWith(o));
    if (!op) return { ok: false, why: `这里只认运算符 ${OPS.join(' ')}："${s}"` };
    s = s.slice(op.length).trim();
    const n = /^-?(0x[0-9a-f]+|\d+(\.\d+)?([eE][-+]?\d+)?)/i.exec(s);
    if (!n) return { ok: false, why: `运算符 ${op} 后面不是数字` };
    const v = num(n[0]);
    if (!Number.isFinite(v)) return { ok: false, why: `"${n[0]}" 不是数字` };
    if ((op === '/' || op === '%') && v === 0) return { ok: false, why: `除以 0` };
    ops.push({ op, v });
    s = s.slice(n[0].length).trim();
  }
  return { ok: true, ast: { kind: 'expr', fn, off, ops } };
}

/** 求值（bytes 可以是 Uint8Array / number[]）*/
export function evalExpr(ast, bytes){
  if (ast.kind === 'hex') return NaN;
  let v = ast.fn ? FNS[ast.fn](bytes, ast.off) : ast.off;
  for (const { op, v: k } of ast.ops){
    switch (op){
      case '+': v = v + k; break;
      case '-': v = v - k; break;
      case '*': v = v * k; break;
      case '/': v = v / k; break;
      case '%': v = v % k; break;
      case '>>': v = v >> k; break;
      case '<<': v = v << k; break;
      case '&': v = v & k; break;
      case '|': v = v | k; break;
    }
  }
  return v;
}

/** 这个表达式最多会读到第几个字节（越界提示用） */
export function exprBytes(ast){
  if (ast.kind !== 'expr' || !ast.fn) return 0;
  return ast.off + (FN_BYTES[ast.fn] || 1);
}

/**
 * 解析整条 `as` 子句。
 * @returns {{ok:true, fields:Array<{name:string, ast:object, text:string}>, hexOnly:boolean}
 *          | {ok:false, why:string}}
 */
export function parseAs(text){
  const s = String(text ?? '').trim();
  if (!s) return { ok: true, fields: [], hexOnly: true };
  if (/^hex$/i.test(s)) return { ok: true, fields: [], hexOnly: true };
  const fields = [];
  for (const piece of s.split(',')){
    const p = piece.trim();
    if (!p) continue;
    const eq = p.indexOf('=');
    if (eq < 0) return { ok: false, why: `"${p}" 少一个 "="（要写成 name=表达式，例如 ax=i16be(0)/16384）` };
    const name = p.slice(0, eq).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) return { ok: false, why: `变量名 "${name}" 不合法（字母/数字/下划线，不能数字开头）` };
    if (fields.some(f => f.name === name)) return { ok: false, why: `变量名 "${name}" 重复了` };
    const r = parseExpr(p.slice(eq + 1));
    if (!r.ok) return { ok: false, why: `${name}：${r.why}` };
    fields.push({ name, ast: r.ast, text: p.slice(eq + 1).trim() });
  }
  if (!fields.length) return { ok: true, fields: [], hexOnly: true };
  return { ok: true, fields, hexOnly: false };
}

/** 数值 → 屏幕上好看的字符串（有效位随量级走，别把 1.00000000 印一屏）*/
export function fmtValue(v){
  if (!Number.isFinite(v)) return '—';
  if (Number.isInteger(v) && Math.abs(v) < 1e15) return String(v);
  const a = Math.abs(v);
  const digits = a >= 100 ? 2 : a >= 1 ? 3 : a >= 0.001 ? 5 : 8;
  return v.toFixed(digits).replace(/0+$/, '').replace(/\.$/, '');
}

/**
 * 对一组字节求值。
 * @param {{fields:Array}} spec parseAs 的结果
 * @param {Uint8Array|number[]} bytes
 * @returns {{values:Array<{name:string, value:number, text:string}>, warn:string|null}}
 */
export function applyAs(spec, bytes){
  const out = [];
  let warn = null;
  const len = bytes?.length ?? 0;
  for (const f of spec.fields || []){
    const need = exprBytes(f.ast);
    if (need > len && !warn) warn = `${f.name} 要读到第 ${need} 字节，但这笔只读回 ${len} 字节`;
    const v = evalExpr(f.ast, bytes);
    out.push({ name: f.name, value: v, text: fmtValue(v) });
  }
  return { values: out, warn };
}

/** 语法速查（页面上「语法速查」那一块直接用）*/
export const AS_HELP = `as 子句：给读回来的字节起名字 + 换算
  rd 0x68 0x3B 14 as ax=i16be(0)/16384, ay=i16be(2)/16384, az=i16be(4)/16384
  rd 0x48 0x00 2  as v=i16be(0)*0.000125
  rd 0x68 0x75 1  as id=u8(0)

函数（括号里是字节偏移，省略 = 0）：
  u8 i8 u16be i16be u16le i16le u24be u32be i32be u32le f32be f32le
  多字节默认大端（I2C 器件的寄存器几乎都是大端：MPU6050 / ADS1115 都是）

运算：+ - * / % >> << & |    严格从左到右，没有优先级
  i16be(0)/340+36.53  = (raw/340)+36.53
  u16be(0)>>12&7      = ((w>>12)&7)
`;
