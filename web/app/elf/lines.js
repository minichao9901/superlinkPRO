/**
 * DWARF `.debug_line` 行号表 —— 「停下来显示当前源码行」（Ozone 那种）的地基。
 *
 * 为什么单独一个文件：行号表是**自成一套**的字节码（自己的头、自己的状态机、
 * 自己的标准/扩展操作码），跟 `.debug_info` 那套 DIE/abbrev 只是共享"DWARF"这个名字。
 * 放在 `app/elf/` 下是因为它跟 `dwarf.js` 一样**只依赖 ELF 字节**，能在 Node 里喂真 ELF 自测。
 *
 * 输出：
 *   `at(addr)`        → `{file, line, col, addr, end, isStmt, prologueEnd, seqEnd}`：这个地址属于哪一行
 *   `addrOfLine(f,l)` → 反查地址（在源码行上点一下下断点要用）
 *   `resolveLine(f,l)`→ 同上，但**带出处**（行号表里的完整路径 + isStmt），命令行 `b 文件:行` 用它
 *   `nextStmtAddr(a)` → 这一行之后的**下一条语句**地址（源码级单步：step over / step into 的地基）
 *   `paths`           → 去重后的源文件清单（界面「选择源码目录」之后按它取文件）
 *
 * 支持 DWARF 2/3/4/5（v5 的目录/文件表是"格式描述 + 值"两段式，跟 v4 完全不同）。
 * 本仓的目标固件是 ARM GCC 的 **DWARF 4** 与 HPM SDK 的 **DWARF 5**，两种都必须过。
 *
 * 两个已知取舍：
 *   · 段选择子（segment_selector_size）非 0 的 ELF 直接放弃 —— 现实里只有 x86 分段那套会用；
 *   · `at()` 只做"地址 → 行"，不做内联函数展开（DW_TAG_inlined_subroutine）——
 *     v1 的目标是"停在哪一行"，不求 gdb 级的调用栈还原。
 */

import { cstrAt } from './elf.js';
import { Dwarf } from './dwarf.js';

// ---- 标准操作码（DW_LNS_*）----
const LNS = {
  copy: 1, advance_pc: 2, advance_line: 3, set_file: 4, set_column: 5, negate_stmt: 6,
  set_basic_block: 7, const_add_pc: 8, fixed_advance_pc: 9, set_prologue_end: 10,
  set_epilogue_begin: 11, set_isa: 12,
};
// ---- 扩展操作码（DW_LNE_*）----
const LNE = { end_sequence: 1, set_address: 2, define_file: 3, set_discriminator: 4 };
// ---- v5 的条目内容类型（DW_LNCT_*）----
const LNCT = { path: 1, directory_index: 2, timestamp: 3, size: 4, md5: 5 };
// v5 的条目表单（只列现实里会出现的）
const LNFORM = {
  string: 0x08, strp: 0x0e, udata: 0x0f, data1: 0x0b, data2: 0x05, data4: 0x06,
  data8: 0x07, data16: 0x1e, line_strp: 0x1f, block: 0x09, flag_present: 0x19,
};

/** 小端/大端都能读的小游标（ELF 是哪种端序就按哪种读） */
class Rd {
  constructor(b, o = 0, le = true){ this.b = b; this.o = o; this.le = le; }
  get eof(){ return this.o >= this.b.length; }
  get left(){ return this.b.length - this.o; }
  u8(){ return this.b[this.o++]; }
  u16(){ const b = this.b, o = this.o; this.o += 2; return this.le ? (b[o] | (b[o + 1] << 8)) : ((b[o] << 8) | b[o + 1]); }
  u32(){
    const b = this.b, o = this.o; this.o += 4;
    return this.le
      ? ((b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)) >>> 0)
      : (((b[o] << 24) | (b[o + 1] << 16) | (b[o + 2] << 8) | b[o + 3]) >>> 0);
  }
  u64(){ const lo = this.u32(), hi = this.u32(); return this.le ? hi * 4294967296 + lo : lo * 4294967296 + hi; }
  s8(){ const v = this.u8(); return v < 0x80 ? v : v - 0x100; }
  uleb(){ let r = 0, s = 0, x; do { x = this.u8(); r += (x & 0x7f) * Math.pow(2, s); s += 7; } while (x & 0x80); return r; }
  sleb(){
    let r = 0, s = 0, x;
    do { x = this.u8(); r += (x & 0x7f) * Math.pow(2, s); s += 7; } while (x & 0x80);
    if (s < 32 && (x & 0x40)) r -= Math.pow(2, s);
    return r;
  }
  cstr(){ const s = this.o; while (this.o < this.b.length && this.b[this.o]) this.o++; const v = cstrAt(this.b, s); this.o++; return v; }
}

// ---------------------------------------------------------------- 路径小工具

/** 统一成分隔符 `/`（比较/展示都用它，Windows 的 `\` 一律换掉） */
export const normSlash = p => String(p ?? '').replace(/\\/g, '/').replace(/\/{2,}/g, '/');

/** 绝对路径？`E:/x`、`/usr/x`、`//server/x` 都算 */
export function isAbsPath(p){
  const s = normSlash(p);
  return /^[A-Za-z]:\//.test(s) || s.startsWith('/');
}

/** 拼路径（后段是绝对路径就以后段为准） */
export function joinPath(dir, name){
  const n = normSlash(name);
  if (!n) return normSlash(dir);
  if (isAbsPath(n)) return n;
  const d = normSlash(dir);
  if (!d) return n;
  return d.endsWith('/') ? d + n : d + '/' + n;
}

/** 去掉 `a/./b`、`a/../b` 这类（不碰盘符与开头的 `/`） */
export function cleanPath(p){
  const s = normSlash(p);
  const drive = (/^[A-Za-z]:/.exec(s) || [''])[0];
  const abs = !!drive || s.startsWith('/');        // 🚨 有盘符就一定是绝对路径，别再按"相对"拼
  const rest = s.slice(drive.length).replace(/^\/+/, '');
  const out = [];
  for (const part of rest.split('/')){
    if (!part || part === '.') continue;
    if (part === '..'){
      if (out.length && out[out.length - 1] !== '..') out.pop();
      else if (!abs) out.push('..');
      continue;
    }
    out.push(part);
  }
  return drive + (abs ? '/' : '') + out.join('/');
}

/** `p` 相对 `base` 的路径（不在 base 下就返回 null） */
export function relTo(base, p){
  const b = cleanPath(base).replace(/\/+$/, '').toLowerCase();
  const q = cleanPath(p);
  if (!b) return null;
  const ql = q.toLowerCase();
  if (ql === b) return '';
  if (ql.startsWith(b + '/')) return q.slice(b.length + 1);
  return null;
}

const samePath = (a, b) => cleanPath(a).toLowerCase() === cleanPath(b).toLowerCase();

// ---------------------------------------------------------------- 头解析

/** v5 的条目格式描述：`(content_type, form)` 对 */
function readFormats(rd, n){
  const out = [];
  for (let i = 0; i < n; i++) out.push([rd.uleb(), rd.uleb()]);
  return out;
}

/** v5 的条目值表（目录表 / 文件表共用）*/
function readV5Entries(rd, fmts, count, lineStr, str){
  const out = [];
  for (let i = 0; i < count; i++){
    const e = {};
    for (const [ct, form] of fmts){
      let v = null;
      switch (form){
        case LNFORM.string: v = rd.cstr(); break;
        case LNFORM.line_strp: { const off = rd.u32(); v = cstrAt(lineStr, off); break; }
        case LNFORM.strp: { const off = rd.u32(); v = cstrAt(str, off); break; }
        case LNFORM.udata: v = rd.uleb(); break;
        case LNFORM.data1: v = rd.u8(); break;
        case LNFORM.data2: v = rd.u16(); break;
        case LNFORM.data4: v = rd.u32(); break;
        case LNFORM.data8: v = rd.u64(); break;
        case LNFORM.data16: rd.o += 16; break;
        case LNFORM.flag_present: v = 1; break;
        case LNFORM.block: { const n = rd.uleb(); rd.o += n; break; }
        default: throw new Error(`.debug_line 头里不认识的 form 0x${form.toString(16)}`);
      }
      if (ct === LNCT.path) e.path = v;
      else if (ct === LNCT.directory_index) e.dir = v;
    }
    out.push(e);
  }
  return out;
}

/**
 * 解析一个行号程序（`.debug_line` 里一个 CU 一段）。
 * @returns {{end:number, version:number, rows:Array, dirs:string[], files:Array<{name:string,dir:number}>}}
 */
function parseProgram(buf, start, { addrSize = 4, compDir = '', lineStr = new Uint8Array(0), str = new Uint8Array(0), le = true } = {}){
  const rd = new Rd(buf, start, le);
  let unitLength = rd.u32();
  const dwarf64 = unitLength === 0xffffffff;
  if (dwarf64) unitLength = rd.u64();
  const unitEnd = (dwarf64 ? start + 12 : start + 4) + unitLength;
  if (!unitLength || unitEnd > buf.length) throw new Error(`行号程序的 unit_length 不合理（${unitLength}）`);

  const version = rd.u16();
  if (version < 2 || version > 5) throw new Error(`不支持的 .debug_line 版本 ${version}（只做 2~5）`);
  let addressSize = addrSize;
  if (version >= 5){
    addressSize = rd.u8();
    const segSel = rd.u8();
    if (segSel) throw new Error('带段选择子的 .debug_line 不支持（x86 分段那套）');
  }
  const headerLength = dwarf64 ? rd.u64() : rd.u32();
  const progStart = rd.o + headerLength;

  const minInstLen = rd.u8();
  const maxOps = version >= 4 ? rd.u8() : 1;
  const defaultIsStmt = rd.u8();
  const lineBase = rd.s8();
  const lineRange = rd.u8();
  const opcodeBase = rd.u8();
  const stdLens = [];
  for (let i = 1; i < opcodeBase; i++) stdLens[i] = rd.u8();     // stdLens[opcode] = 操作数个数

  /**
   * 目录表 / 文件表。
   * 🚨 v4 与 v5 的**下标起点不一样**，这是这段最容易写错的地方：
   *    · v4：`include_directories` 从 1 开始，**0 = CU 的 comp_dir**（隐式）；
   *    · v5：目录表自己是 0 起的，第 0 条就是"编译时的工作目录"（producer 写进去的）。
   *    所以 v4 要手工在头上补一个 compDir，v5 直接用它的第 0 条。
   * 文件表两边都是 **1 起**（0 号位留空，状态机的 file 寄存器初值就是 1）。
   */
  let dirs, files;
  if (version <= 4){
    dirs = [compDir];
    for (;;){ const s = rd.cstr(); if (!s) break; dirs.push(s); }
    files = [{ name: '', dir: 0 }];                            // v4：0 号位留空（索引 1 起）
    for (;;){
      const name = rd.cstr();
      if (!name) break;
      const dir = rd.uleb(); rd.uleb(); rd.uleb();               // mtime / size：没人用，占位而已
      files.push({ name, dir });
    }
  } else {
    const dirFmtCount = rd.u8();
    const dirFmts = readFormats(rd, dirFmtCount);
    const dirEntries = readV5Entries(rd, dirFmts, rd.uleb(), lineStr, str);
    dirs = dirEntries.map(e => e.path || '');
    if (!dirs.length) dirs = [compDir];
    const fileFmtCount = rd.u8();
    const fileFmts = readFormats(rd, fileFmtCount);
    /**
     * 🚨 v5 的文件表是 **0 起**（第 0 条就是 CU 的主源文件），**绝不能像 v4 那样补一个空位**。
     *    2026-10 用 HPM6800EVK 的靶子固件（DWARF 5 + 101 个源文件）压出来的：
     *    补了空位之后每个文件都往后错一位 —— `engine.c` 的代码被报成 `engine.h`
     *    （行号反而是对的，所以"看着像对、停错了文件"最难查）。
     *    证据：`readelf --debug-dump=rawline` 里那条 `Set File Name to entry 2`，
     *    表里第 2 项（0 起）就是 engine.c。
     */
    files = readV5Entries(rd, fileFmts, rd.uleb(), lineStr, str).map(e => ({ name: e.path || '', dir: e.dir | 0 }));
  }

  // ---- 行号状态机 ----
  /**
   * `file` 寄存器初值按规范就是 **1**（v4/v5 都是）。
   * v5 的表虽然是 0 起，但**有的 CU 会在 0 号位放一个空条目**（主源文件落在 1 号位）——
   * 初值写 0 就会把这种 CU 的第一段序列整体指到"空文件"上（实测：riscv_dwarf5 fixture）。
   */
  const FILE0 = 1;
  let addr = 0, opIndex = 0, file = FILE0, line = 1, column = 0;
  let isStmt = !!defaultIsStmt, prologueEnd = false, epilogueBegin = false;
  let isa = 0, discriminator = 0, basicBlock = false;
  /**
   * 🚨 `addressSet`：DWARF 规定每个序列都要以 `DW_LNE_set_address` 开头，但 GCC 会在
   * 行号程序最前面先来一段**没有 set_address 的"占位序列"**（把 CU 用到的头文件列一遍，
   * 地址就从 0 开始按 min_inst_len 累加：0, 2, 4, 6…）。这些行**不是代码**，
   * 留着会让 `addrOfLine('SEGGER_RTT.c', 983)` 反查到地址 2 —— 点一下就在 0x2 下了个断点。
   */
  let addressSet = false;
  const rows = [];
  const advance = (opAdv) => {
    addr = (addr + minInstLen * Math.floor((opIndex + opAdv) / maxOps)) >>> 0;
    opIndex = (opIndex + opAdv) % maxOps;
  };
  const emit = (endSeq) => rows.push({
    addr: addr >>> 0, file, line, col: column, isStmt, endSeq: !!endSeq, prologueEnd, epilogueBegin, isa, discriminator,
    noAddr: !addressSet,
  });
  const resetFlags = () => { basicBlock = false; prologueEnd = false; epilogueBegin = false; discriminator = 0; };
  const resetAll = () => {
    addr = 0; opIndex = 0; file = FILE0; line = 1; column = 0;
    isStmt = !!defaultIsStmt; basicBlock = false; prologueEnd = false; epilogueBegin = false; isa = 0; discriminator = 0;
    addressSet = false;
  };

  const p = new Rd(buf, progStart, le);
  const stop = Math.min(unitEnd, buf.length);
  let guard = 0;
  while (p.o < stop){
    if (++guard > 4000000) throw new Error('行号程序跑飞了（操作码数超限）');
    const op = p.u8();
    if (op >= opcodeBase){                       // 特殊操作码：推进地址 + 改行号 + 出一行
      const adj = op - opcodeBase;
      line += lineBase + (adj % lineRange);
      advance(Math.floor(adj / lineRange));
      emit(false);
      resetFlags();
      continue;
    }
    if (op === 0){                               // 扩展操作码
      const len = p.uleb();
      const next = p.o + len;
      const ext = p.u8();
      switch (ext){
        case LNE.end_sequence: emit(true); resetAll(); break;
        case LNE.set_address: {
          addr = p.u32();
          if (addressSize === 8) p.o += 4;       // 只要低 32 位（我们的目标都是 32 位核）
          opIndex = 0;
          addressSet = true;
          break;
        }
        case LNE.define_file: {                  // 只有 v<=4 有
          const name = p.cstr();
          const dir = p.uleb(); p.uleb(); p.uleb();
          files.push({ name, dir });
          break;
        }
        case LNE.set_discriminator: discriminator = p.uleb(); break;
        default: break;                          // 不认识的扩展操作码：长度已经给了，整段跳过
      }
      p.o = next;
      continue;
    }
    switch (op){                                 // 标准操作码
      case LNS.copy: emit(false); resetFlags(); break;
      case LNS.advance_pc: advance(p.uleb()); break;
      case LNS.advance_line: line += p.sleb(); break;
      case LNS.set_file: file = p.uleb(); break;
      case LNS.set_column: column = p.uleb(); break;
      case LNS.negate_stmt: isStmt = !isStmt; break;
      case LNS.set_basic_block: basicBlock = true; break;
      case LNS.const_add_pc: advance(Math.floor((255 - opcodeBase) / lineRange)); break;
      case LNS.fixed_advance_pc: addr = (addr + p.u16()) >>> 0; opIndex = 0; break;
      case LNS.set_prologue_end: prologueEnd = true; break;
      case LNS.set_epilogue_begin: epilogueBegin = true; break;
      case LNS.set_isa: isa = p.uleb(); break;
      default: {                                 // 不认识的标准操作码：按头里的长度表把操作数吃掉
        const n = stdLens[op] ?? 0;
        for (let i = 0; i < n; i++) p.uleb();
        break;
      }
    }
  }
  return { end: Math.max(unitEnd, p.o), version, rows, dirs, files, addressSize };
}

/** 把文件名按目录拼成完整路径（**下标与 prog.files 一一对应**：0 号是空位） */
function resolveFilePaths(prog, compDir){
  const out = new Array(prog.files.length).fill('');
  for (let i = 1; i < prog.files.length; i++){
    const f = prog.files[i];
    if (!f?.name) continue;
    // v4：dirs[0] 就是我们补进去的 comp_dir；v5：dirs[0] 是 producer 写的编译目录 —— 两种都能直接下标
    const dir = prog.dirs[f.dir | 0] ?? prog.dirs[0] ?? compDir ?? '';
    let full = cleanPath(joinPath(dir || compDir || '', f.name));
    // dir 本身是相对路径时（GCC 偶尔这么给），拼完还不是绝对路径 —— 再补一次 comp_dir
    if (!isAbsPath(full) && compDir) full = cleanPath(joinPath(compDir, full));
    out[i] = full;
  }
  return out;
}

// ---------------------------------------------------------------- 主类

export class LineTable {
  constructor(parts = {}){
    this.starts = parts.starts || new Uint32Array(0);      // 每条记录的起始地址（升序）
    this.ends = parts.ends || new Uint32Array(0);          // 覆盖到哪（下一条记录 / 序列结束）
    this.lines = parts.lines || new Int32Array(0);
    this.files = parts.files || new Int32Array(0);         // paths 的下标（-1 = 没路径）
    this.flags = parts.flags || new Uint8Array(0);         // bit0 isStmt / bit1 prologueEnd / bit2 seqEnd
    /**
     * **行号序列号**（每条记录属于哪一段"行号程序区间"）。
     * 🚨 为什么必须有它：DWARF 只保证"**同一条序列内**地址递增"，不同函数的序列在**链接后**
     *    地址可以交错（2026-10 真机踩到：按地址排序往前走，从 `main` 的循环跳进了 `wait_field`）。
     *    `nextStmtAddr()` 靠它把"下一行"限制在同一条序列里。
     */
    this.seqs = parts.seqs || new Int32Array(0);
    this.seqCount = parts.seqCount || 0;
    this.paths = parts.paths || [];                        // 源文件表（`/` 分隔的完整路径）
    this.versions = parts.versions || [];
    this.units = parts.units || 0;
    this.note = parts.note || '';
    this._lineCache = new Map();                           // 文件 → (行号 → 地址)
  }

  get size(){ return this.starts.length; }
  get fileCount(){ return this.paths.length; }

  static available(elf){ return !!elf.section('.debug_line')?.size; }

  /**
   * @param {import('./elf.js').Elf} elf
   * @param {Dwarf} [dwarf] 已经建好的 Dwarf（省一次解析）；不给就自己建
   */
  static fromElf(elf, dwarf = null){
    const buf = elf.data('.debug_line');
    if (!buf.length) throw new Error('这个 ELF 没有 .debug_line —— 编译时没带 -g，或者被 strip 过');
    const str = elf.data('.debug_str');
    const lineStr = elf.data('.debug_line_str');
    const le = elf.le !== false;

    // CU → 行号程序：v<=4 的行号头里**没有地址宽度**，只能问 CU；顺便拿 comp_dir 解析相对路径
    const cuByOffset = new Map();
    let d = dwarf;
    if (!d && Dwarf.available(elf)){ try { d = new Dwarf(elf); } catch { d = null; } }
    if (d && typeof d.cuList === 'function'){
      try {
        for (const cu of d.cuList()) if (cu.stmtList != null && !cuByOffset.has(cu.stmtList)) cuByOffset.set(cu.stmtList, cu);
      } catch { /* CU 表读不出来也能干活：退化成"没有 comp_dir" */ }
    }

    const recs = [];                              // {s,e,line,path,flags}
    const pathIdx = new Map();                    // 路径（小写）→ 下标
    const paths = [];
    const versions = [];
    /**
     * 代码段范围（SHF_EXECINSTR 的 PROGBITS 段）。行号记录**只能落在代码里** ——
     * 有些编译器（本仓的 STM32 固件就是）会在行号程序最前面发一段 `set_address 0`
     * 的"占位序列"（把用到的头文件列一遍，地址从 0 开始按指令长度累加），
     * 不滤掉的话 `addrOfLine('x.h', 42)` 会反查到地址 2 —— 点一下就在 0x2 下了个断点。
     */
    const execRanges = elf.sections()
      .filter(s => s.size && s.type === 1 && (s.flags & 0x4))
      .map(s => [s.addr >>> 0, (s.addr + s.size) >>> 0])
      .sort((a, b) => a[0] - b[0]);
    const inCode = (a) => !execRanges.length || execRanges.some(([lo, hi]) => a >= lo && a < hi);
    let off = 0, units = 0, note = '', seqBase = 0;
    while (off < buf.length){
      const cu = cuByOffset.get(off);
      let prog;
      try {
        prog = parseProgram(buf, off, {
          addrSize: cu?.addrSize || (elf.bits === 64 ? 8 : 4),
          compDir: cleanPath(cu?.compDir || ''),
          lineStr, str, le,
        });
      } catch (e){
        note = note || `行号程序 @${off} 解析失败：${e?.message || e}`;
        break;                                    // 头都读不出来就不知道下一段在哪 —— 只能收工
      }
      const local = resolveFilePaths(prog, cu?.compDir || '');
      versions.push(cu?.version || prog.version);   // 报"这份固件是 DWARF x"要用 CU 的版本（行号程序版本号是另一回事）
      const map = local.map(pth => {
        if (!pth) return -1;
        const k = pth.toLowerCase();
        let i = pathIdx.get(k);
        if (i === undefined){ i = paths.length; paths.push(pth); pathIdx.set(k, i); }
        return i;
      });
      seqBase += flattenProgram(prog, map, recs, inCode, seqBase);
      units++;
      if (prog.end <= off) break;                 // 防死循环
      off = prog.end;
    }

    recs.sort((a, b) => (a.s - b.s) || (a.e - b.e));
    const n = recs.length;
    const starts = new Uint32Array(n), ends = new Uint32Array(n);
    const lines = new Int32Array(n), files = new Int32Array(n), flags = new Uint8Array(n), seqs = new Int32Array(n);
    for (let i = 0; i < n; i++){
      starts[i] = recs[i].s; ends[i] = recs[i].e; lines[i] = recs[i].line; files[i] = recs[i].path;
      flags[i] = recs[i].flags; seqs[i] = recs[i].seq;
    }
    return new LineTable({ starts, ends, lines, files, flags, seqs, seqCount: seqBase, paths, versions, units, note });
  }

  /** 地址 → 源码位置（找不到返回 null）。`addr` 是指令地址，Thumb 的 bit0 会被忽略 */
  at(addr){
    const n = this.starts.length;
    if (!n) return null;
    // 🚨 `x & ~1` 在 JS 里是**有符号** 32 位运算：0x80000000 & ~1 === -2147483648。
    //    忘了最后那个 `>>> 0` 的话，0x8xxxxxxx 的地址（HPM / QSPI 代码）会一条都查不到。
    const a = ((addr >>> 0) & 0xfffffffe) >>> 0;
    let lo = 0, hi = n - 1, idx = -1;
    while (lo <= hi){                              // 最后一个 start <= a
      const mid = (lo + hi) >> 1;
      if (this.starts[mid] <= a){ idx = mid; lo = mid + 1; } else hi = mid - 1;
    }
    if (idx < 0) return null;
    const s0 = this.starts[idx];
    let last = idx;                                // 同地址的一组：只有最后一条有非零区间
    while (last + 1 < n && this.starts[last + 1] === s0) last++;
    const e = this.ends[last];
    if (a < s0 || a >= e) return null;             // 落在序列的空隙里（这段没有行号信息）
    // 组里若有 `is_stmt` 那条就用它 —— 优化之后一个地址常常挂多个行号，
    // 取 is_stmt 的才是"这一行的开头"（gdb 也是这么挑的）
    let pick = last;
    for (let k = last; k >= idx; k--) if (this.flags[k] & 1){ pick = k; break; }
    const fi = this.files[pick];
    return {
      addr: s0, end: e, line: this.lines[pick], file: fi >= 0 ? this.paths[fi] : '',
      isStmt: !!(this.flags[pick] & 1), prologueEnd: !!(this.flags[pick] & 2),
      seqEnd: !!(this.flags[pick] & 4), seq: this.seqs[pick] | 0, idx: pick,
    };
  }

  /** 源码位置 → 地址（在源码行上点一下下断点用；找不到返回 null） */
  addrOfLine(file, line){
    const r = this.resolveLine(file, line);
    return r ? r.addr : null;
  }

  /**
   * 源码位置 → **带出处**的地址：`{addr, file(行号表里的完整路径), line, isStmt}`。
   * `addrOfLine` 是它的薄封装（老调用点不用改）。
   *
   * 文件按**后缀**匹配（`main.c` 能匹配 `/proj/src/main.c`），同一个文件里同一行取
   * **最小地址**、`is_stmt` 优先 —— 这两条与 gdb 的挑法一致。
   */
  resolveLine(file, line){
    const map = this._lineMap(cleanPath(file).toLowerCase());
    const i = map.get(line);
    if (i === undefined) return null;
    const fi = this.files[i];
    return { addr: this.starts[i] >>> 0, file: fi >= 0 ? this.paths[fi] : '', line, isStmt: !!(this.flags[i] & 1), idx: i };
  }

  /** 文件（后缀匹配）→ 行号映射；建一次就缓存住（`resolveLine` 与 `lineRange` 共用） */
  _lineMap(key){
    const hit = this._lineCache.get(key);
    if (hit) return hit;
    const map = new Map();
    for (let i = 0; i < this.starts.length; i++){
      const fi = this.files[i];
      if (fi < 0) continue;
      const p = cleanPath(this.paths[fi]).toLowerCase();
      if (p !== key && !p.endsWith('/' + key)) continue;
      const ln = this.lines[i];
      const cur = map.get(ln);
      // 同一行取**最小地址**；isStmt 的那条优先
      if (cur === undefined) map.set(ln, i);
      else if ((this.flags[i] & 1) && !(this.flags[cur] & 1)) map.set(ln, i);
      else if (this.starts[i] < this.starts[cur] && (this.flags[i] & 1) === !!(this.flags[cur] & 1)) map.set(ln, i);
    }
    this._lineCache.set(key, map);
    return map;
  }

  /**
   * 某个源文件在行号表里的**行号范围**（`{min,max,count,path}`；行号表里没有这个文件时 null）。
   * 用途：行号写错时把"这个文件只有 1~95 行"直接说出来，
   * 而不是含糊地报"这一行没有代码 —— 可能是空行/声明/被优化掉"（2026-10 压测发现的说人话问题）。
   */
  lineRange(file){
    const map = this._lineMap(cleanPath(file).toLowerCase());
    if (!map.size) return null;
    let min = Infinity, max = 0, path = '';
    for (const [ln, i] of map){
      if (ln < min) min = ln;
      if (ln > max) max = ln;
      if (!path) path = this.paths[this.files[i]] || '';
    }
    return { min, max, count: map.size, path };
  }

  /**
   * **当前行之后的下一条语句地址**（源码级单步的地基：step over / step into 都靠它）。
   *
   * 规则（与 MDK/gdb 在硬件断点下的做法一致）：
   *   · 从"包含 addr 的那条记录"往后找，跳过同一行的其它记录（`is_stmt` 才是语句开头）；
   *   · 遇到本条就是序列末尾（`seqEnd`）→ 返回 null（**没有下一行**：函数最后一行 / 汇编块）；
   *   · 找不到 is_stmt 记录时退一步用第一条记录（有些编译器整段不发 is_stmt）。
   *
   * ⚠️ 这是"**地址序**的下一条语句"，不是"源码行号 +1"：对 `for`/`while` 回跳、
   *    以及 `?:`、逗号表达式这类一行多个语句的情况，落点与 MDK 的"下一行"可能差一条语句
   *    （MDK 同样受行号表精度限制）。要精确跨过某一行时用 `rc <文件:行>` 明确指定。
   *
   * @returns {{addr:number,end:number,line:number,file:string,isStmt:boolean}|null}
   */
  nextStmtAddr(addr){
    const n = this.starts.length;
    if (!n) return null;
    const cur = this.at(addr);
    if (!cur) return null;
    if (cur.seqEnd) return null;                    // 已经是函数/序列的最后一行
    let fallback = -1;
    for (let i = (cur.idx ?? 0) + 1; i < n; i++){
      /**
       * 🚨 **只在同一条序列里找**：不同函数的序列在链接后地址可能交错，
       *    不判序列就会"从 main 的循环一步跨进 wait_field"（2026-10 真机踩到：
       *    目标行被算成另一个函数里的行号，临时断点永远不命中）。
       */
      if ((this.seqs[i] | 0) !== (cur.seq | 0)) break;
      if (this.starts[i] < cur.end) continue;       // 同一行的其它记录
      const fi = this.files[i];
      if (fallback < 0) fallback = i;
      if (this.flags[i] & 1){
        return { addr: this.starts[i] >>> 0, end: this.ends[i] >>> 0, line: this.lines[i], file: fi >= 0 ? this.paths[fi] : '', isStmt: true, idx: i, tail: false };
      }
    }
    if (fallback < 0) return null;
    const fi = this.files[fallback];
    /**
     * `tail: true` = **这条记录之后同一个序列里再也没有 `is_stmt` 了** ——
     * 也就是说当前这一行已经是函数的最后一条语句，后面只剩编译器给"收尾/右花括号"
     * 挂的那些非语句记录（实测：engine_linear 的 0x800020c/0x8000212 都是 L42 且 is_stmt=0）。
     * 源码级单步靠它决定"该跳出函数了"：gdb 的 `next` 在这种情况下会直接回到调用者，
     * 而只按"下一条记录"停的话，用户要白按两次 F10（每次都停在右花括号上，看着像没反应）。
     */
    const seq = cur.seq | 0;
    let tail = true;
    for (let i = fallback + 1; i < this.starts.length; i++){
      if ((this.seqs[i] | 0) !== seq) break;
      if (this.flags[i] & 1){ tail = false; break; }
    }
    return { addr: this.starts[fallback] >>> 0, end: this.ends[fallback] >>> 0, line: this.lines[fallback], file: fi >= 0 ? this.paths[fi] : '', isStmt: false, idx: fallback, tail };
  }

  /** 指定地址区间里的全部行记录（源码视图画上下文、找函数序言结束点用；已按地址升序） */
  rowsInRange(from, to, max = 4000){
    const out = [];
    for (let i = 0; i < this.starts.length; i++){
      const s = this.starts[i];
      if (s < from) continue;
      if (s >= to || out.length >= max) break;
      const fi = this.files[i];
      out.push({
        addr: s, end: this.ends[i], line: this.lines[i], file: fi >= 0 ? this.paths[fi] : '',
        isStmt: !!(this.flags[i] & 1), prologueEnd: !!(this.flags[i] & 2), seqEnd: !!(this.flags[i] & 4),
        seq: this.seqs[i] | 0,
      });
    }
    return out;
  }

  summary(){
    if (!this.size) return this.note || '没有行号信息';
    const v = [...new Set(this.versions)].join('/');
    return `行号表：${this.units} 段 · ${this.size} 条记录 · ${this.fileCount} 个源文件（DWARF ${v || '?'}）`
      + (this.note ? `　⚠ ${this.note}` : '');
  }
}

// ---------------------------------------------------------------- 内部

/** 把一个程序的行记录摊平成"地址区间"（写进 recs）；返回"用掉了几条序列" */
function flattenProgram(prog, pathMap, recs, inCode = () => true, seqBase = 0){
  const rows = prog.rows;
  let seqs = 0;
  let curSeq = -1;
  /**
   * 先合并"同一地址的多条记录"：只有最后一条能形成区间（前面的区间长度是 0）。
   * 但 `is_stmt`/`prologue_end` 这些标记可能只挂在前面的那条上 —— 直接丢掉的话
   * "这一行是语句开头"就没了，源码视图会把函数序言当成断点行。所以往后面的记录上并。
   */
  for (let i = 0; i < rows.length - 1; i++){
    const a = rows[i], b = rows[i + 1];
    if (a.endSeq || b.endSeq) continue;
    if (a.addr === b.addr){
      b.isStmt = b.isStmt || a.isStmt;
      b.prologueEnd = b.prologueEnd || a.prologueEnd;
      a.skip = true;
    }
  }
  for (let i = 0; i < rows.length; i++){
    const r = rows[i];
    if (r.endSeq || r.skip || r.noAddr) continue;
    /**
     * 地址 0 与"不在任何代码段里"的记录一并丢掉（双保险：真实代码不会落在地址 0 ——
     * Cortex-M 的 flash 在 0x08000000，HPM 在 0x80000000）。
     */
    if (!r.addr || !inCode(r.addr)) continue;
    if (curSeq < 0) curSeq = seqBase + seqs++;   // 本程序的第一条有效记录 = 新序列
    const next = rows[i + 1];
    const end = next ? next.addr : (r.addr + 1);
    if (end <= r.addr) continue;                   // 兜底：空区间不要
    /**
     * `seqEnd`（flag bit2）= **这一条是它所在"行号序列"的最后一条**（下一条原始记录就是
     * `end_sequence`）。源码级单步要靠它判断"这一行已经是函数最后一行"——
     * 序列边界不在输出里的话，"下一行"会跑到隔壁函数去（DWARF 只保证序列内地址递增）。
     */
    let j = i;
    while (j + 1 < rows.length && rows[j + 1].skip) j++;
    const nxRaw = rows[j + 1];
    const seqEnd = (!nxRaw || !!nxRaw.endSeq) ? 1 : 0;
    recs.push({
      s: r.addr >>> 0, e: end >>> 0, line: r.line | 0,
      path: pathMap[r.file] ?? -1,
      seq: curSeq,
      flags: (r.isStmt ? 1 : 0) | (r.prologueEnd ? 2 : 0) | (seqEnd ? 4 : 0),
    });
    if (seqEnd) curSeq = -1;                       // 下一条有效记录开启新序列
  }
  return seqs;
}

export { parseProgram as _parseLineProgram, samePath };
