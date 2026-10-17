/**
 * 「监视」窗口的纯逻辑 —— 表达式解析、取值、显示格式化、**结构体/数组树**。
 *
 * 为什么不写在 view.js 里：跟 `cmd.js` 一样，这些规则（认不出名字怎么办、没有类型信息时显示什么、
 * 结构体怎么摆、位域怎么解、大数组怎么截断）最容易写错，而它们**不需要浏览器也不需要硬件**，
 * 喂一个假符号表 + 一段字节就能测到底。
 *
 * 支持的表达式：
 *   `g_var`            有 DWARF 类型 → 按类型解出数值；结构体/数组 → **可展开的树**（本文件的下半部分）
 *   `g_pack.u_hi`      展平的结构体成员名（DWARF 里就是这么命名的，能直接查）
 *   `g_var+4` `main-2` 符号加偏移（按 u32 读）
 *   `0x20000004`       直接看某个地址（按 u32 读）
 *
 * 明确**不做**：数组下标（`a[3]`）与指针跟踪（`*p` 跟一层）—— 那要"按表达式求值"的完整实现；
 * 这里的树已经能把结构体/联合/数组/位域摆出来（`treeRows()`），够"停下来瞅一眼"。
 */

import { hex32, readLE } from './fmt.js';
import { decodeScalar } from './cmd.js';
import { SCALARS } from '../elf/dwarf.js';

/** 树的显示上限（防止一个 4 KB 的结构体把界面撑爆 / 把 SWD 读挂） */
export const TREE_LIMITS = {
  maxDepth: 4,        // 嵌套层数
  maxRows: 96,        // 一次渲染的行数
  maxArray: 16,       // 数组最多显示几项
  maxBytes: 512,      // 一个监视项最多读多少字节
  maxStr: 32,         // 字符数组当字符串显示最多几个字符
};

/** 把一行表达式解析成监视项（认不出来返回 `{expr, error}`，界面照实显示，不猜） */
export function resolveWatch(expr, sym){
  const raw = String(expr ?? '').trim();
  if (!raw) return { expr: raw, error: '空的' };
  if (!sym) return { expr: raw, error: '还没载入 .elf' };

  // 纯地址
  const direct = /^(0x[0-9a-f]+|\d+)$/i.exec(raw);
  if (direct){
    const addr = /^0x/i.test(raw) ? parseInt(raw.slice(2), 16) : parseInt(raw, 10);
    return { expr: raw, name: raw, kind: 'addr', addr: addr >>> 0, size: 4, scalar: 'u32', typeName: 'u32', label: raw };
  }

  /**
   * 复合路径也要能解析：`g_model.flags` / `g_model.nodes[1].cell`（路径由 symbols.js 的
   * `typeOf` 顺着 DWARF 成员偏移算地址）。这里的正则只负责把「路径 + 偏移」切开，
   * `+4` / `-2` 这种后缀照样认。
   */
  const m = /^([A-Za-z_.$][\w.$]*(?:\[\d+\])*(?:\.[A-Za-z_.$][\w.$]*(?:\[\d+\])*)*)\s*([+-])\s*(0x[0-9a-f]+|\d+)$/i.exec(raw);
  const name = m ? m[1] : raw;

  /**
   * 带类型变量的正路：**先问 DWARF 的类型树**（`sym.typeOf`）—— 结构体/联合/数组要它才能展开。
   * `sym.varType()`（展平后的标量表）是退路：没有 DWARF 句柄、或这个名字不在 DWARF 全局变量里时用。
   */
  const t = sym.typeOf ? sym.typeOf(name) : null;
  const v = sym.find(name);
  if (t?.bad) return { expr: raw, error: `「${name}」：${t.reason}` };
  if (!v && !t?.type) return { expr: raw, error: `找不到符号「${name}」（可用 sym ${name} 搜）` };

  if (m){
    const d = /^0x/i.test(m[3]) ? parseInt(m[3].slice(2), 16) : parseInt(m[3], 10);
    const base = (t?.addr ?? v?.addr ?? 0) >>> 0;
    const addr = (m[2] === '+' ? base + d : base - d) >>> 0;
    return { expr: raw, name: raw, kind: 'addr', addr, size: 4, scalar: 'u32', typeName: 'u32', label: raw };
  }

  if (t?.type && t.addr != null){
    const ty = t.type;
    // 位域成员：按位取，不能当整个存储单元读（`p g_model.flags.sbits.bias` 要的是 6 位那个值）
    if (t.bit){
      return { expr: raw, name, kind: 'var', addr: t.addr >>> 0, size: Math.max(1, ty.size || 4),
               scalar: null, typeName: `${ty.name || ty.scalar || '?'} 位域`, label: name, bit: t.bit,
               note: t.bit.unresolved ? '位域偏移读不出来' : `位域 ${t.bit.size} 位` };
    }
    if (ty.kind === 'scalar' && ty.scalar){
      return { expr: raw, name, kind: 'var', addr: t.addr >>> 0, size: ty.size, scalar: ty.scalar,
               typeName: ty.name || ty.scalar, label: name };
    }
    if (ty.kind === 'struct' || ty.kind === 'union' || ty.kind === 'array'){
      const label = ty.kind === 'array' ? `${ty.name || '数组'}[${ty.count ?? '?'}]` : (ty.name || '结构体');
      return { expr: raw, name, kind: ty.kind, addr: t.addr >>> 0, size: Math.max(1, ty.size || 4),
               scalar: null, typeName: label, type: ty, label: name };
    }
    if (ty.kind === 'pointer'){
      return { expr: raw, name, kind: 'var', addr: t.addr >>> 0, size: ty.size || 4, scalar: ty.size === 4 ? 'u32' : null,
               typeName: (ty.alias || '指针'), label: name, note: '指针（只显示地址本身，v1 不跟进目标）' };
    }
    // 类型不在表里（enum/unspecified…）：当原始字节，但把原因带上
    return { expr: raw, name, kind: 'raw', addr: t.addr >>> 0, size: Math.max(1, Math.min(ty.size || 4, TREE_LIMITS.maxBytes)),
             scalar: null, typeName: ty.name || ty.reason || '未知类型', label: name, note: ty.reason || null };
  }
  if (t && t.addr == null){
    return { expr: raw, name, kind: 'raw', addr: v?.addr >>> 0, size: Math.max(1, Math.min(v?.size || 4, TREE_LIMITS.maxBytes)),
             scalar: null, typeName: '（DWARF 里没有固定地址）', label: name, note: t.reason || null };
  }

  const vt = sym.varType(name);
  if (vt?.scalarInfo){
    return {
      expr: raw, name, kind: 'var', addr: vt.addr >>> 0, size: vt.scalarInfo.size,
      scalar: vt.scalar, typeName: vt.scalar, label: name,
    };
  }
  // 只有符号表（无 DWARF）
  const size = Math.max(1, Math.min(v.size || 4, 64));
  const scalar = size === 1 ? 'u8' : size === 2 ? 'u16' : size === 4 ? 'u32' : null;
  return {
    expr: raw, name, kind: 'raw', addr: v.addr >>> 0, size,
    scalar, typeName: v.isFunc ? `函数 @ ${hex32(v.addr)}` : (size === 4 ? 'u32' : `${size} 字节`), label: name,
  };
}

/** 读取一个监视项的值（bytes 是刚从目标读回来的原始字节）→ `{text, cls, hex}` */
export function formatWatchValue(item, bytes){
  if (item.error) return { text: item.error, cls: 'err' };
  if (!bytes || !bytes.length) return { text: '（没读到）', cls: 'warn' };
  const ty = item.typeName || item.scalar || `${item.size} 字节`;
  // 位域：按位取（`g_model.flags.sbits.bias` 这种 6 位有符号字段不能当整个 u32 读）
  if (item.bit){
    const bf = decodeBitfield(bytes, item.bit);
    if (bf) return { text: `${bf.text}  (${bf.hex})`, hex: bf.hex, cls: '', type: ty };
    return { text: '（位域读不出来）', cls: 'warn', type: ty };
  }
  if (item.scalar && SCALARS[item.scalar] && bytes.length >= SCALARS[item.scalar].size){
    const v = decodeScalar(item.scalar, bytes);
    const info = SCALARS[item.scalar];
    const hex = '0x' + readLE(bytes, 0, Math.min(info.size, bytes.length)).toString(16);
    const num = info.float ? (Number.isFinite(v) ? v.toPrecision(6) : String(v)) : String(v);
    return { text: `${num}`, hex, cls: '', type: ty };
  }
  // 没有类型：短的就摆字节，长的给前 8 字节
  const show = [...bytes.subarray(0, 8)].map(b => b.toString(16).padStart(2, '0')).join(' ');
  const hex = bytes.length <= 4 ? '0x' + readLE(bytes, 0, bytes.length).toString(16) : null;
  return { text: hex ? `${readLE(bytes, 0, bytes.length)}  (${hex})` : `${show}${bytes.length > 8 ? ' …' : ''}`, hex, cls: '', type: ty };
}

// ---------------------------------------------------------------- 结构体 / 数组树

/** 类型名的人话（界面右侧那列） */
function typeNameOf(t){
  if (!t) return '?';
  if (t.alias) return t.alias;
  if (t.kind === 'pointer') return '指针';
  if (t.kind === 'array') return `${typeNameOf(t.elem)}[${t.count ?? '?'}]`;
  if (t.kind === 'struct' || t.kind === 'union') return t.name || (t.kind === 'union' ? 'union' : 'struct');
  if (t.kind === 'scalar') return t.name || t.scalar || '?';
  return t.kind || '?';
}

/** 从字节缓冲里按位取一个位域（**小端**：位偏移 0 = 结构体首字节的最低位） */
export function readBits(bytes, bitOffset, bitSize){
  let v = 0;
  for (let i = 0; i < bitSize; i++){
    const bit = bitOffset + i;
    const byte = bytes[bit >> 3];
    if (byte === undefined) return null;
    if ((byte >> (bit & 7)) & 1) v += Math.pow(2, i);
  }
  return v >>> 0;
}

/** 位域按声明类型做符号扩展（`int32_t bias : 6` 要能解出 -32..31） */
export function signExtend(v, bits){
  if (!(bits > 0) || bits >= 32) return v >>> 0;
  const m = 1 << (bits - 1);
  return ((v & ((1 << bits) - 1)) ^ m) - m;
}

/**
 * 解一个位域（`bit` 来自监视项：`{inUnit, size, signed}`）。
 * @returns {{value:number, text:string, hex:string, signed:boolean}|null}
 */
export function decodeBitfield(bytes, bit){
  if (!bit || bit.unresolved || !(bit.size > 0) || bit.size > 32) return null;
  const raw = readBits(bytes, bit.inUnit || 0, bit.size);
  if (raw == null) return null;
  const v = bit.signed ? signExtend(raw, bit.size) : (raw >>> 0);
  return { value: v, text: String(v), hex: '0x' + (raw >>> 0).toString(16), signed: !!bit.signed };
}

/** 标量取值（越界返回 null，绝不猜 0） */
function scalarAt(type, bytes, off){
  const info = type?.scalar ? SCALARS[type.scalar] : null;
  if (!info) return null;
  if (off < 0 || off + info.size > bytes.length) return null;
  const v = decodeScalar(type.scalar, bytes.subarray(off, off + info.size));
  const hex = '0x' + readLE(bytes, off, info.size).toString(16);
  const text = info.float ? (Number.isFinite(v) ? v.toPrecision(6) : String(v)) : String(v);
  return { text, hex, signed: !!info.signed };
}

/** u8/i8 数组能当字符串看吗（可打印 ASCII 占比够高才算） */
function asString(bytes, off, count){
  const n = Math.min(count, bytes.length - off);
  if (n <= 0) return null;
  let printable = 0, s = '';
  for (let i = 0; i < n; i++){
    const b = bytes[off + i];
    if (b === 0) break;                       // C 字符串结尾
    s += (b >= 0x20 && b < 0x7f) ? String.fromCharCode(b) : '·';
    if (b >= 0x20 && b < 0x7f) printable++;
  }
  if (s.length >= 4 && printable / s.length >= 0.85) return s.slice(0, TREE_LIMITS.maxStr);
  return null;
}

/**
 * 把结构体/数组摊平成**带缩进的树行**（全部从"一次读回来的字节"里解，不再多读内存）。
 *
 * @param {object} item 监视项（要有 `type`）
 * @param {Uint8Array} bytes 从 `item.addr` 起读回来的字节（长度 = item.size）
 * @param {object} [opts] 覆盖 TREE_LIMITS（测试里用来把上限调小）
 * @returns {Array<{depth:number,name:string,off:number,size:number,text:string,hex?:string,type:string,cls:string,bitfield?:boolean}>}
 */
export function treeRows(item, bytes, opts = {}){
  const o = { ...TREE_LIMITS, ...opts };
  const rows = [];
  const buf = bytes || new Uint8Array(0);
  const push = (row) => { if (rows.length < o.maxRows) rows.push(row); };
  const overflow = () => { if (rows.length >= o.maxRows && !rows.some(r => r.overflow)) rows.push({ depth: 0, name: '…', off: 0, text: `（只显示前 ${o.maxRows} 行）`, type: '', cls: 'dim', overflow: true }); };

  /**
   * 位域行。🚨 `baseOff` 必须是**这个位域所在结构体**在 `buf` 里的字节偏移：
   *    DWARF 的 `bitOffset` 是"从所在结构体首字节起算"的，早先直接拿它去索引 buf
   *    （等于假定结构体就在根对象开头），于是**嵌套在结构体里的位域全读成了根对象头 4 字节**
   *    —— 2026-10 压测用 `g_model.flags.bits.*` 抓到（解出来的正是 `g_model.magic` 的位）。
   */
  const bitRow = (mem, baseOff, name, depth) => {
    const size = mem.type?.size || 0;
    const off = baseOff + (mem.offset || 0);
    const type = `${typeNameOf(mem.type)} 位域`;
    const base = { depth, name: `${name} : ${mem.bitSize}`, off, size, type, bitfield: true };
    if (mem.bitOffset == null) return { ...base, text: '（位域偏移读不出来）', cls: 'dim' };
    const raw = readBits(buf, baseOff * 8 + mem.bitOffset, mem.bitSize);
    if (raw == null) return { ...base, text: '（位域超出读取范围）', cls: 'dim' };
    const v = SCALARS[mem.type?.scalar]?.signed ? signExtend(raw, mem.bitSize) : (raw >>> 0);
    const hexv = '0x' + (raw >>> 0).toString(16);
    return { ...base, text: `${v}  (${hexv})`, hex: hexv, cls: '' };
  };

  const walk = (type, off, name, depth) => {
    if (rows.length >= o.maxRows) return overflow();
    if (depth > o.maxDepth){ push({ depth, name, off, size: type?.size || 0, text: '…（嵌套太深）', type: typeNameOf(type), cls: 'dim' }); return; }
    if (!type){ push({ depth, name, off, size: 0, text: '（类型未知）', type: '?', cls: 'dim' }); return; }

    if (type.kind === 'pointer'){
      const st = type.size === 4 ? scalarAt({ scalar: 'u32' }, buf, off) : null;
      push({ depth, name, off, size: type.size || 4, text: st ? `→ ${st.hex}` : '（越界）', hex: st?.hex, type: typeNameOf(type), cls: st ? '' : 'dim' });
      return;
    }
    if (type.kind === 'scalar'){
      const st = scalarAt(type, buf, off);
      push({ depth, name, off, size: type.size || 0, text: st ? st.text : '（越界 / 读不到）', hex: st?.hex, type: typeNameOf(type), cls: st ? '' : 'dim' });
      return;
    }
    if (type.kind === 'array'){
      const n = type.count ?? 0;
      const elemSize = type.elem?.size || 0;
      push({ depth, name, off, size: type.size || 0, text: `[${n}]`, type: typeNameOf(type), cls: 'dim' });
      if (type.elem?.kind === 'scalar' && (type.elem.scalar === 'u8' || type.elem.scalar === 'i8')){
        const str = asString(buf, off, n);
        if (str) push({ depth: depth + 1, name: '(字符串)', off, size: Math.min(n, o.maxStr), text: JSON.stringify(str), type: 'char[]', cls: '' });
      }
      const shown = Math.min(n, o.maxArray);
      for (let i = 0; i < shown; i++) walk(type.elem, off + i * elemSize, `[${i}]`, depth + 1);
      if (n > shown) push({ depth: depth + 1, name: '…', off: off + shown * elemSize, size: 0, text: `还有 ${n - shown} 项`, type: '', cls: 'dim' });
      return;
    }
    if (type.kind === 'struct' || type.kind === 'union'){
      const label = type.name || (type.kind === 'union' ? 'union' : 'struct');
      push({ depth, name, off, size: type.size || 0, text: `{${(type.members || []).length} 个成员}`, type: label, cls: 'dim' });
      for (const mem of type.members || []){
        const mo = off + (mem.offset || 0);
        if (mem.offset == null){
          push({ depth: depth + 1, name: mem.name, off: mo, size: mem.type?.size || 0, text: `（${mem.reason || '偏移不是常量'}）`, type: typeNameOf(mem.type), cls: 'dim' });
          continue;
        }
        if (mem.bitSize){ push(bitRow(mem, off, mem.name, depth + 1)); continue; }
        walk(mem.type, mo, mem.name, depth + 1);
      }
      return;
    }
    push({ depth, name, off, size: type.size || 0, text: `（不支持的类型：${type.kind || '?'}）`, type: typeNameOf(type), cls: 'dim' });
  };

  const t = item?.type;
  if (!t) return rows;
  if (t.kind === 'struct' || t.kind === 'union'){
    for (const mem of t.members || []){
      if (mem.offset == null || !mem.bitSize) walk(mem.type, mem.offset || 0, mem.name, 0);
      else push(bitRow(mem, 0, mem.name, 0));      // 根对象本身：结构体首字节 = buf[0]
    }
  } else if (t.kind === 'array'){
    const n = t.count ?? 0, elemSize = t.elem?.size || 0;
    if (t.elem?.kind === 'scalar' && (t.elem.scalar === 'u8' || t.elem.scalar === 'i8')){
      const str = asString(buf, 0, n);
      if (str) push({ depth: 0, name: '(字符串)', off: 0, size: Math.min(n, o.maxStr), text: JSON.stringify(str), type: 'char[]', cls: '' });
    }
    const shown = Math.min(n, o.maxArray);
    for (let i = 0; i < shown; i++) walk(t.elem, i * elemSize, `[${i}]`, 0);
    if (n > shown) push({ depth: 0, name: '…', off: shown * elemSize, size: 0, text: `还有 ${n - shown} 项`, type: '', cls: 'dim' });
  }
  return rows;
}

/** 折叠状态下的一行摘要：`{f_sin=0.905, f_tri=0.28, …}` / `[16] 1, 2, 3, …` */
export function summarizeTree(item, bytes, opts = {}){
  const rows = treeRows(item, bytes, { ...opts, maxRows: 10, maxArray: 4, maxDepth: 1 });
  const scalarRows = rows.filter(r => !r.overflow && !/^[\[{…]/.test(String(r.text)));
  /**
   * 优先用**有名字的成员**做摘要（`MaxNumUpBuffers=1` 比 `[0]=12` 有用得多）——
   * 结构体第一个成员常常是个 char 数组，全用数组元素会看不出这是哪个结构体。
   */
  const named = scalarRows.filter(r => !/^\[/.test(String(r.name)));
  const pick = named.length ? named : scalarRows;
  const parts = [];
  for (const r of pick){
    const nm = String(r.name || '').replace(/^\(/, '').replace(/\)$/, '');
    parts.push(`${nm}=${r.text}`);
    if (parts.length >= 4) break;
  }
  const more = pick.length > parts.length ? ', …' : '';
  return parts.length ? `{${parts.join(', ')}${more}}` : '{…}';
}

/** 监视表（顺序即界面顺序；add/remove 都由它管，好在 Node 里测） */
export class WatchList {
  constructor(items = []){
    this.items = items.map(it => ({ ...it, value: null }));
  }

  get length(){ return this.items.length; }
  indexOf(expr){
    const e = String(expr || '').trim().toLowerCase();
    return this.items.findIndex(it => String(it.expr).trim().toLowerCase() === e);
  }

  /** 加一项；重复（同名）就返回 `{dup:true}` 而不是加第二条 */
  add(expr, sym){
    const item = resolveWatch(expr, sym);
    const i = this.indexOf(item.expr);
    if (i >= 0) return { dup: true, index: i, item: this.items[i] };
    this.items.push({ ...item, value: null });
    return { dup: false, index: this.items.length - 1, item: this.items[this.items.length - 1] };
  }

  /** 删一项：参数可以是编号（1 起）、名字、或 `all` */
  remove(what){
    const s = String(what ?? '').trim();
    if (!s) return { removed: 0 };
    if (s === 'all' || s === '*'){ const n = this.items.length; this.items = []; return { removed: n }; }
    if (/^\d+$/.test(s)){
      const i = parseInt(s, 10) - 1;
      if (i < 0 || i >= this.items.length) return { removed: 0 };
      this.items.splice(i, 1);
      return { removed: 1 };
    }
    const i = this.indexOf(s);
    if (i < 0) return { removed: 0 };
    this.items.splice(i, 1);
    return { removed: 1 };
  }

  /** 展开/折叠一项（结构体/数组才有意义）；返回新的展开状态 */
  toggle(i){
    const it = this.items[i];
    if (!it) return null;
    it.expanded = !it.expanded;
    return !!it.expanded;
  }

  clear(){ this.items = []; }

  /** 存 store 用（值不带进去；**展开状态要留着**，刷新页面别把用户展开的树收起来） */
  toJSON(){ return this.items.map(({ expr, name, kind, addr, size, scalar, typeName, label, expanded }) =>
    ({ expr, name, kind, addr, size, scalar, typeName, label, expanded: !!expanded })); }

  static fromJSON(arr, sym){
    if (!Array.isArray(arr)) return new WatchList();
    const list = new WatchList();
    for (const it of arr){
      const e = it?.expr;
      if (!e) continue;
      // 重新解析一遍：地址/大小/类型可能因为换了 ELF 而不一样，但**认不出来时保留原表达式并标错**
      const fresh = sym ? resolveWatch(e, sym) : { ...it, value: null };
      list.items.push({ ...fresh, expanded: !!it.expanded, value: null });
    }
    return list;
  }
}
