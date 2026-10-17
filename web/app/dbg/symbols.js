/**
 * 符号表（ELF）—— 调试页的 "gdb 感" 全靠它：`p g_var`、`b main`、PC 落点显示。
 *
 * 数据来源两路（都复用已有模块，不重复造）：
 *   · `Elf.symbols()`：完整符号表（函数 + 变量，含地址与大小）—— 一定能拿到；
 *   · `listSampleable(elf)`：DWARF 解析出的**带类型**的全局/静态变量（J-Scope 页在用同一个）——
 *     有它才能 `p` 出数值；没有 DWARF 时退化成"只显示地址与字节"。
 *
 * 🚨 与 J-Scope 页**刻意不同**的一点：那边只列 RAM 窗口里的可采样变量（波形只能采 RAM），
 *    调试页的 `p` 要能打印 **flash 里的常量/查表**（`p kSinHi[3]` 这种很常用），
 *    所以这里不做 RAM 过滤，符号表里的东西全都要能按名字查到。
 */

import { Elf } from '../elf/elf.js';
import { Dwarf, listSampleable, SCALARS } from '../elf/dwarf.js';
import { LineTable } from '../elf/lines.js';
import { hex32, parseNum } from './fmt.js';

const byAddr = (a, b) => a.addr - b.addr;
const baseName = p => String(p || '').replace(/\\/g, '/').split('/').pop();

/** 类型名的人话（错误信息里用；界面那列在 watch.js 的 typeNameOf） */
function typeText(t){
  if (!t) return '未知类型';
  if (t.alias) return t.alias;
  if (t.kind === 'array') return `${typeText(t.elem)}[${t.count ?? '?'}]`;
  if (t.kind === 'pointer') return '指针';
  if (t.kind === 'struct' || t.kind === 'union') return t.name || t.kind;
  if (t.kind === 'scalar') return t.name || t.scalar || '标量';
  return t.kind || '未知类型';
}

/**
 * 把 `a.b[2].c` 拆成段；**不是路径就返回 null**（单段名字不算路径，走原来的精确查）。
 * 认的写法只有：标识符 + `.成员` + `[十进制下标]`，别的（表达式、箭头、强转）一律不认——
 * 认不出就老实返回 null，绝不去猜用户想算什么。
 *
 * @returns {Array<{name:string}|{index:number}>|null}
 */
export function parsePath(text){
  const s = String(text ?? '').trim();
  if (!s) return null;
  const segs = [];
  let i = 0;
  const ident = () => {
    const m = /^[A-Za-z_$][\w$]*/.exec(s.slice(i));
    if (!m) return null;
    i += m[0].length;
    return m[0];
  };
  const head = ident();
  if (!head) return null;
  segs.push({ name: head });
  while (i < s.length){
    if (s[i] === '.'){
      i++;
      const n = ident();
      if (!n) return null;
      segs.push({ name: n });
      continue;
    }
    if (s[i] === '['){
      const m = /^\[(\d+)\]/.exec(s.slice(i));
      if (!m) return null;
      i += m[0].length;
      segs.push({ index: Number(m[1]) });
      continue;
    }
    return null;
  }
  return segs.length > 1 ? segs : null;
}

/**
 * 顺着路径往下走：累加地址、缩小类型。走到头把最终类型与地址交出去。
 * 走不通时**带原因返回**（`reason`），调用方负责显示 —— 不静默返回个错地址。
 *
 * 位域成员（`mem.bitSize`）走到底时额外给 `bit`：
 *   `inUnit` = 该位在**存储单元**（= 最终地址处）里的位偏移。
 *   DWARF 的 `bitOffset` 是"从所在结构体首字节起算"，所以要减掉成员自身的字节偏移。
 */
function walkPath(want, segs, head){
  let addr = head.addr >>> 0;
  let type = head.type;
  let bit = null;
  let reason = null;
  for (const seg of segs.slice(1)){
    if (bit){ reason = '位域成员不能再往下走'; break; }
    if (!type){ reason = '类型未知'; break; }
    if (seg.index != null){
      if (type.kind !== 'array'){ reason = `${typeText(type)} 不是数组，不能下标 [${seg.index}]`; break; }
      const n = type.count ?? 0;
      if (seg.index >= n){ reason = `下标 [${seg.index}] 越界（数组只有 ${n} 项）`; break; }
      addr = (addr + seg.index * (type.elem?.size || 0)) >>> 0;
      type = type.elem;
      continue;
    }
    if (type.kind !== 'struct' && type.kind !== 'union'){
      reason = `${typeText(type)} 不是结构体/联合，没有成员「${seg.name}」`;
      break;
    }
    const mem = (type.members || []).find(m => m.name === seg.name);
    if (!mem){
      const names = (type.members || []).map(m => m.name).slice(0, 12).join(' · ');
      reason = `没有成员「${seg.name}」（${typeText(type)} 里有：${names}${(type.members || []).length > 12 ? ' …' : ''}）`;
      break;
    }
    if (mem.offset == null){ reason = mem.reason || `成员「${seg.name}」偏移不是常量`; break; }
    addr = (addr + mem.offset) >>> 0;
    type = mem.type;
    if (mem.bitSize){
      if (mem.bitOffset == null){ bit = { unresolved: true, size: mem.bitSize }; }
      else {
        const inUnit = mem.bitOffset - mem.offset * 8;
        bit = { inUnit: inUnit < 0 ? mem.bitOffset : inUnit, size: mem.bitSize,
                scalar: type?.scalar || null, signed: !!SCALARS[type?.scalar]?.signed };
      }
    }
  }
  /**
   * 🚨 走到一半走不通（成员名写错、下标越界、标量后面又点成员…）时**必须把结果作废**：
   *    返回 `bad:true` + 原因，而不是把"走到哪算哪"的那个类型/地址交出去 ——
   *    否则 `p g_model.nope` 会拿着 `g_model` 的类型画一棵看起来正常的树（2026-10 压测抓到）。
   */
  if (reason) return { name: want, addr: null, type: null, reason, bad: true };
  return { name: want, addr, type, reason: null, bit };
}

export class SymTab {
  constructor(parts = {}){
    this.all = parts.all || [];            // 全部符号（含未命名/无大小的）
    this.funcs = (parts.funcs || []).slice().sort(byAddr);
    this.objs = (parts.objs || []).slice().sort(byAddr);
    this.vars = parts.vars || [];          // 带类型的变量（DWARF 优先）
    this.varByName = new Map();
    for (const v of this.vars) if (!this.varByName.has(v.name)) this.varByName.set(v.name, v);
    this.byName = new Map();
    // 先放已归一化的函数/数据符号（函数抹掉 Thumb 位），剩下的才轮到原始表
    for (const s of [...this.funcs, ...this.objs, ...this.all]) if (s.name && !this.byName.has(s.name)) this.byName.set(s.name, s);
    this.source = parts.source || 'symtab';
    this.note = parts.note || '';
    this.ram = parts.ram || null;
    this.versions = parts.versions || [];
    this.lines = parts.lines || null;      // 行号表（源码行显示用；没有 DWARF 行号时为 null）
    this.dwarf = parts.dwarf || null;      // DWARF 句柄（类型树查询用；没有 DWARF 时为 null）
    this.elf = parts.elf || null;          // 原始 ELF（`codeBytes()` 取指令字节用）
  }

  /** @param {ArrayBuffer|Uint8Array} buf .elf 文件内容 */
  static fromBuffer(buf){
    const elf = buf instanceof Elf ? buf : new Elf(buf);
    const all = elf.symbols().filter(s => s.name);
    /**
     * 🚨 Thumb 函数的符号值**带 bit0=1**（0x08000041 = SysTick_Handler）
     *    —— 那是"这是 Thumb 代码"的标记，不是地址的一部分。
     *    不抹掉的话 `funcAt(0x08000044)` 会算出 +3 的偏移、`nameOf` 显示 +3、断点也会偏 1。
     *    （数据符号没有这个标记，原样保留。）
     */
    const funcs = all.filter(s => s.isFunc && s.addr).map(s => ({ ...s, addr: (s.addr & ~1) >>> 0 }));
    const objs = all.filter(s => s.isObject && s.addr);
    let vars = [], source = 'symtab', note = '', ram = null, versions = [];
    /**
     * DWARF 句柄**建一次、两处用**：① 列可采样变量；② 结构体监视要的"完整类型树"按名字现查。
     * 解析 `.debug_info` 很贵，建两遍纯属浪费（大 ELF 上是几百毫秒级）。
     */
    let dwarf = null;
    try { if (Dwarf.available(elf)) dwarf = new Dwarf(elf); } catch { dwarf = null; }
    try {
      const r = listSampleable(elf, dwarf ? { dwarf } : {});
      vars = (r.sampleable || []).map(v => ({
        name: v.name, addr: v.addr >>> 0, size: v.size >>> 0,
        scalar: v.scalar || null, typeName: v.typeName || null, path: v.path || v.name,
      }));
      source = r.source || 'symtab';
      note = r.note || '';
      ram = r.ram || null;
      versions = r.versions || [];
    } catch (e){
      note = `DWARF 解析失败（${e?.message || e}）：只能按符号表显示地址与原始字节`;
    }
    /**
     * 行号表（「停下来显示当前源码行」的地基）。**失败不算致命**：
     * `-g0` 构建或被 strip 的 ELF 没有 `.debug_line`，符号/变量/断点照常用，
     * 只是源码视图显示"这份 ELF 没有行号信息"。
     */
    let lines = null;
    try {
      if (LineTable.available(elf)) lines = LineTable.fromElf(elf);
    } catch (e){
      note = (note ? note + '；' : '') + `行号表解析失败（${e?.message || e}）`;
    }
    return new SymTab({ all, funcs, objs, vars, source, note, ram, versions, lines, dwarf, elf });
  }

  /**
   * 按运行地址取**文件里那份只读字节**（`.text` / `.rodata` 等，不含可写段）。
   * 两处用：① 指令取指；② flash 只读数据读不到时的兜底（见 `session._codeBytes()` 与
   * `RiscvDebugSession.memRead()` 的注释）。拿不到就返回 null（不猜）。
   */
  codeBytes(addr, len = 2){
    try { return this.elf?.bytesAt?.(addr >>> 0, len >>> 0, { ro: true }) || null; } catch { return null; }
  }

  /**
   * 这个地址落在**当前 ELF 的已分配段**里吗（`SHF_ALLOC` + 有尺寸）。
   *
   * 用途只有一个：判断"上一次会话留下的地址还属不属于当前目标"。载入新 ELF 时，
   * localStorage 里可能还躺着上一块板子的地址（真机现场：ARM 板留下的 `0x0800_0300`，
   * 在 HPM 上没映射），自动刷新去读它就会触发 SBA 报错 → 自愈复位 DM → 把用户的目标状态搅掉。
   * 注意它**只约束自动刷新**：手动输入的地址照样读 —— 外设寄存器、栈、堆都可能不在 ELF 里，
   * 用"不在 ELF 就拒绝"去挡手动请求会挡住正常用法。
   */
  covers(addr, len = 1){
    if (!Number.isInteger(addr) || addr < 0 || addr > 0xffffffff ||
        !Number.isInteger(len) || len < 1 || addr + len > 0x100000000) return false;
    let sections;
    try { sections = this.elf?.sections?.() || []; } catch { return false; }
    const ranges = sections.filter(s => (s.flags & 2) && s.size > 0)
      .map(s => [s.addr, s.addr + s.size]).sort((a,b) => a[0] - b[0]);
    let cursor = addr;
    for (const [start,end] of ranges){
      if (start > cursor) break;
      if (end > cursor) cursor = end;
      if (cursor >= addr + len) return true;
    }
    return false;
  }

  get size(){ return this.all.length; }
  get varCount(){ return this.vars.length; }

  /** 一句话摘要（界面状态条用） */
  summary(){
    const s = `${this.size} 个符号（函数 ${this.funcs.length} · 数据 ${this.objs.length}）`;
    const t = this.varCount ? `，其中 ${this.varCount} 个带类型变量（${this.source === 'dwarf' ? 'DWARF' : '符号表'}）` : '，没有类型信息（无 DWARF）';
    const l = this.lines ? `，行号表 ${this.lines.size} 条` : '';
    return s + t + l + (this.note ? `　⚠ ${this.note}` : '');
  }

  /** 地址 → 源码位置（没有行号表就返回 null） */
  at(addr){ return this.lines ? this.lines.at(addr) : null; }

  /** 「文件:行号」的人话（PC 显示、状态条用） */
  locText(addr){
    const r = this.at(addr);
    return r && r.file ? `${baseName(r.file)}:${r.line}` : '';
  }

  /**
   * RTT 控制块符号：`_SEGGER_RTT` 是最常见的名字，但有的工程改过名。
   * 找不到就按名字模糊搜一把 —— 界面「RTT 输出」那格要把它显示出来。
   */
  rttSym(){
    for (const n of ['_SEGGER_RTT', 'SEGGER_RTT', '_SEGGER_RTT_', 'g_rtt', 'rtt_cb']){
      const s = this.find(n);
      if (s?.addr) return { name: n, addr: s.addr >>> 0, size: s.size >>> 0 };
    }
    const hit = this.search('SEGGER_RTT', 8).find(s => s.addr);
    return hit ? { name: hit.name, addr: hit.addr >>> 0, size: hit.size >>> 0 } : null;
  }

  /**
   * 符号面板用的一行行清单：**带类型的变量排前面**（`p`/`w` 最常用的就是它们），
   * 然后是函数，最后是没有类型的数据对象。`filter` 是子串（大小写不敏感）。
   * @returns {{rows:Array, total:number, truncated:boolean}}
   */
  list({ filter = '', limit = 300 } = {}){
    const q = String(filter || '').trim().toLowerCase();
    const rows = [];
    let total = 0;
    const take = (row) => { total++; if (rows.length < limit) rows.push(row); };
    for (const v of this.vars){
      if (q && !v.name.toLowerCase().includes(q)) continue;
      take({ name: v.name, addr: v.addr >>> 0, size: v.size >>> 0, kind: 'var', scalar: v.scalar || null, typeName: v.typeName || v.scalar || '' });
    }
    for (const s of this.funcs){
      if (q && !s.name.toLowerCase().includes(q)) continue;
      take({ name: s.name, addr: s.addr >>> 0, size: s.size >>> 0, kind: 'func', scalar: null, typeName: '' });
    }
    for (const s of this.objs){
      if (q && !s.name.toLowerCase().includes(q)) continue;
      if (this.varByName.has(s.name)) continue;                  // DWARF 那份已经列过了
      take({ name: s.name, addr: s.addr >>> 0, size: s.size >>> 0, kind: 'obj', scalar: null, typeName: '' });
    }
    return { rows, total, truncated: total > rows.length };
  }

  /** 地址落在哪个函数里（PC 落点显示）：返回 {name, addr, off, exact} */
  funcAt(addr){
    /**
     * 🚨 `& ~1` 在 JS 里是**有符号**32 位运算：0x8000578c 会变成负数，
     *    于是下面 `f.addr > addr` 对**每一个**函数都成立 → 循环第一次就 break → 返回 null。
     *    ARM 的 0x08xxxxxx 碰不到这一档，**RISC-V 的 XIP flash 代码（0x8000xxxx）必中**：
     *    现象是 PC 落点、断点列表、`bl` 里的函数名全变成裸地址（2026-10 HPM6800EVK 压测抓到）。
     */
    addr = ((addr >>> 0) & ~1) >>> 0;
    let best = null;
    for (const f of this.funcs){
      if (f.addr > addr) break;
      best = f;
    }
    if (!best) return null;
    const off = (addr - best.addr) >>> 0;
    const exact = best.size ? off < best.size : true;       // 大小为 0（汇编/裁剪过）时只能"就近算"
    return { name: best.name, addr: best.addr, size: best.size, off, exact };
  }

  /** 地址 → 人话："main+0x14" / "0x08000123+0x14?" */
  nameOf(addr){
    const f = this.funcAt(addr);
    if (!f) return hex32(addr);
    const tail = f.off ? '+0x' + f.off.toString(16) : '';
    return f.name + tail + (f.exact ? '' : '?');
  }

  /** 按名字精确查（变量 → 函数 → 数据对象） */
  find(name){
    const n = String(name || '').trim();
    if (!n) return null;
    const v = this.varByName.get(n);
    if (v) return { ...v, kind: 'var' };
    const s = this.byName.get(n);
    if (s) return { name: s.name, addr: s.addr >>> 0, size: s.size >>> 0, kind: s.isFunc ? 'func' : 'obj' };
    return null;
  }

  /** 模糊搜（`sym <子串>`），最多 limit 条 */
  search(sub, limit = 40){
    const q = String(sub || '').toLowerCase();
    if (!q) return [];
    const out = [];
    for (const v of this.vars) if (v.name.toLowerCase().includes(q)) out.push({ ...v, kind: 'var', typed: true });
    for (const s of [...this.funcs, ...this.objs]){
      if (out.length >= limit) break;
      if (!s.name.toLowerCase().includes(q)) continue;
      if (out.some(o => o.name === s.name)) continue;
      out.push({ name: s.name, addr: s.addr >>> 0, size: s.size >>> 0, kind: s.isFunc ? 'func' : 'obj', typed: false });
    }
    return out.slice(0, limit);
  }

  /**
   * 把命令行里的一段文本解析成地址。支持：
   *   `0x08000123` / `1234`（十进制） / `main` / `main+0x10` / `g_var-4` / `&g_var` / `*0x20000000`
   * @returns {{addr:number, sym:object|null, off:number, deref:boolean}|null}
   */
  resolve(text){
    const s = String(text ?? '').trim();
    if (!s) return null;
    const deref = s.startsWith('*');
    let t = deref ? s.slice(1).trim() : s;
    const amp = t.startsWith('&');                            // &var：要的是变量自己的地址
    if (amp) t = t.slice(1).trim();
    const direct = parseNum(t);
    if (direct !== null && /^(0x|0b|0o|\d)/i.test(t)) return { addr: direct >>> 0, sym: null, off: 0, deref };
    // 符号 ± 偏移
    const m = /^([A-Za-z_.$][\w.$]*)\s*([+-])\s*(0x[0-9a-f]+|\d+)$/i.exec(t);
    const name = m ? m[1] : t;
    const sym = this.find(name);
    if (!sym) return null;
    let addr = sym.addr;
    if (m){
      const delta = parseNum(m[3]) || 0;
      addr = m[2] === '+' ? (addr + delta) : (addr - delta);
    }
    return { addr: addr >>> 0, sym, off: m ? (parseNum(m[3]) || 0) : 0, deref };
  }

  /** 变量的类型信息（`p` 用）：scalar 是 SCALARS 里的名字时才能解出数值 */
  varType(name){
    const v = this.varByName.get(String(name || '').trim());
    if (!v) return null;
    return { ...v, scalarInfo: v.scalar && SCALARS[v.scalar] ? SCALARS[v.scalar] : null };
  }

  /**
   * 变量的**完整类型树**（结构体/联合/数组本身也要，监视窗口的树与 `p 结构体` 靠它）。
   *
   * 走 DWARF（`listSampleable` 只把成员摊平成 `g_pack.u_hi` 这种名字，树在那边丢了）。
   * 没有 DWARF、或者这个名字在 DWARF 里没有固定地址（被优化掉）→ 返回 null，
   * 调用方要如实告诉用户"看不见"，**不要猜一个大小出来**。
   *
   * 两种写法都认（2026-10 压测补的第二种）：
   *   ① 顶层变量名 `g_model`；
   *   ② **复合路径** `g_model.flags` / `g_model.nodes[1].cell` / `g_model.word.halves.lo`
   *      —— 从顶层变量出发按 DWARF 成员偏移/数组元素大小一路算地址。
   *      为什么要有②：DWARF 里只有**顶层**变量，`p g_model.flags` 走"精确查名字"必然查不到，
   *      而 gdb/Ozone 里这是最自然的写法；摊平名（`g_model.flags.bits.level`）虽然也能查，
   *      但那是采样器的命名，用户不该被迫去猜它。
   *
   * @returns {{name:string, addr:number|null, type:object|null, reason:string|null, bit?:object}|null}
   */
  typeOf(name){
    if (!this.dwarf) return null;
    const want = String(name ?? '').trim();
    const r = this.dwarf.varType(want);
    if (r){
      if (r.addr == null){
        // DWARF 里有这个名字但位置不固定（优化进寄存器/栈）→ 用符号表兜底地址，类型仍然给它
        const s = this.varByName.get(want) || this.byName.get(want);
        return { name: r.name, addr: s?.addr ?? null, type: r.type, reason: r.reason || null };
      }
      return r;
    }
    // ② 复合路径：从顶层变量往下走（`a.b[2].c`）
    const segs = parsePath(want);
    if (!segs) return null;
    const head = this.dwarf.varType(segs[0].name);
    if (!head?.type) return null;
    return walkPath(want, segs, head);
  }

  /**
   * 把命令行里的一段文本解析成**断点目标**。支持：
   *   `main.c:192`   文件:行（文件按后缀匹配，见 LineTable.resolveLine）
   *   `main:192`     函数:行（用该函数所在文件 + 绝对行号；文件名对不上时才当函数解）
   *   `+5` / `-3`    相对**当前 PC 所在行**（需要 pc；gdb 的 `break +5` 同款）
   *   `main` / `0x08000123` / `g_var+4`   地址或符号（与 `resolve()` 同一套）
   *
   * 纯逻辑、不碰会话 —— 命令行与自测都走这里。
   * @returns {{addr:number, via:string, file:string, line:number, label:string}|{error:string}}
   */
  breakSpec(text, { pc = null } = {}){
    const s = String(text ?? '').trim();
    if (!s) return { error: '空的' };
    const lines = this.lines;
    const needLines = () => '这份 ELF 没有行号信息（编译时没带 -g，或被 strip 过）—— 只能按地址/符号下断点';

    // ① 相对当前行：+N / -N
    const rel = /^([+-])(\d+)$/.exec(s);
    if (rel){
      if (pc == null) return { error: `「${s}」是相对当前 PC 的行号：先停下来（目标在跑时没有"当前行"）` };
      if (!lines) return { error: needLines() };
      const at = lines.at(pc >>> 0);
      if (!at?.file) return { error: 'PC 不在有行号信息的代码里，用不了 +N/-N' };
      const want = at.line + (rel[1] === '+' ? 1 : -1) * Number(rel[2]);
      const r = want >= 1 ? lines.resolveLine(at.file, want) : null;
      if (!r) return { error: `${baseName(at.file)} 里没有第 ${want} 行的代码（当前是第 ${at.line} 行）` };
      return { addr: r.addr, via: 'rel', file: r.file, line: want, label: `${baseName(r.file)}:${want}` };
    }

    // ② 名字:数字
    const m = /^(.*?):(\d+)$/.exec(s);
    if (m){
      if (!lines) return { error: needLines() };
      const name = m[1].trim(), line = Number(m[2]);
      if (!name) return { error: `「${s}」缺少文件名/函数名` };
      if (!(line >= 1)) return { error: `行号要是 ≥1 的整数：「${s}」` };
      const r = lines.resolveLine(name, line);
      if (r) return { addr: r.addr, via: 'file', file: r.file, line, label: `${baseName(r.file)}:${line}` };
      const f = this.find(name);
      if (f?.kind === 'func' && f.addr){
        const at = lines.at(f.addr);
        if (at?.file){
          const r2 = lines.resolveLine(at.file, line);
          if (r2) return { addr: r2.addr, via: 'func', file: r2.file, line, label: `${f.name} → ${baseName(r2.file)}:${line}` };
        }
        return { error: `函数 ${name} 所在文件里没有第 ${line} 行的代码` };
      }
      const same = lines.paths.filter(p => baseName(p).toLowerCase() === baseName(name).toLowerCase());
      let hint;
      if (same.length){
        const rg = lines.lineRange?.(name);
        hint = (rg && (line < rg.min || line > rg.max))
          ? `（${baseName(name)} 里只有第 ${rg.min}~${rg.max} 行有代码，没有第 ${line} 行）`
          : '（文件对上了，但这一行没有代码 —— 可能是空行/声明/被优化掉）';
      } else {
        hint = `（行号表里没有叫「${name}」的源文件，它也不是函数名；用 src 看有哪些文件）`;
      }
      return { error: `解析不了「${s}」：${hint}` };
    }

    // ③ 地址 / 符号（±偏移、&、*）
    const a = this.resolve(s);
    if (a) return { addr: a.addr, via: a.sym ? 'sym' : 'addr', file: '', line: 0, label: a.sym?.name || '' };
    return { error: `认不出地址/符号/文件行：「${s}」` };
  }
}
