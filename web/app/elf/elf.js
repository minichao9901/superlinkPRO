/**
 * ELF 容器（只读）—— 给 DWARF 解析器和"变量浏览器"提供底座。
 *
 * 与 app/rtt/elf.js 的分工：那个只有 71 行、只干一件事（按名字找 _SEGGER_RTT）；
 * 这里是**完整**的段表/符号表读取，DWARF 需要按段名拿 `.debug_*` 数据。
 * 支持 ELF32/ELF64、小端/大端（目标都是小端，大端只是顺手支持，不额外测）。
 *
 * 🚨 两个坑（都是本项目踩过的同族问题）：
 *   1. 段名在 **shstrtab**（e_shstrndx 指的那个段）里，不是固定的某个偏移 —— 必须先解析它。
 *   2. 所有偏移都要做边界检查：真工程的 ELF 有各种裁剪/追加，越界读会静默给出垃圾
 *      （Uint8Array 越界返回 undefined → NaN 传播下去很难查）。
 */
import { u32le, latin1 } from '../core/bin.js';

const SHT_SYMTAB = 2, SHT_STRTAB = 3, SHT_NOBITS = 8, SHT_PROGBITS = 1;

export class Elf {
  /** @param {ArrayBuffer|Uint8Array} buf 整个 .elf 文件 */
  constructor(buf){
    const b = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
    this.b = b;
    if (b.length < 64) throw new Error('文件太小，不是 ELF');
    if (!(b[0] === 0x7f && b[1] === 0x45 && b[2] === 0x4c && b[3] === 0x46)) throw new Error('不是 ELF 文件');
    this.bits = b[4] === 2 ? 64 : 32;
    if (b[4] !== 1 && b[4] !== 2) throw new Error(`奇怪的 ELF class：${b[4]}`);
    this.le = b[5] === 1;
    this.is64 = this.bits === 64;

    const shoff = this.is64 ? this.u64(0x28) : this.u32(0x20);
    this.shentsize = this.u16(this.is64 ? 0x3a : 0x2e);
    this.shnum = this.u16(this.is64 ? 0x3c : 0x30);
    this.shstrndx = this.u16(this.is64 ? 0x3e : 0x32);
    if (!shoff || !this.shnum || !this.shentsize) throw new Error('ELF 没有段表（被 strip/裁剪过？）');
    this.shoff = shoff;
    this._secs = null;
  }

  // ---- 基础读取（按 ELF 自己的端序）----
  u16(o){ const b = this.b; return this.le ? (b[o] | (b[o + 1] << 8)) : ((b[o] << 8) | b[o + 1]); }
  u32(o){
    const b = this.b;
    if (this.le) return u32le(b, o);
    return (((b[o] << 24) | (b[o + 1] << 16) | (b[o + 2] << 8) | b[o + 3]) >>> 0);
  }
  u64(o){
    const lo = this.u32(o), hi = this.u32(o + 4);
    return this.le ? hi * 4294967296 + lo : lo * 4294967296 + hi;
  }

  /** 段表（含段名），只解析一次 */
  sections(){
    if (this._secs) return this._secs;
    const b = this.b, n = this.shnum, ent = this.shentsize;
    const raw = [];
    for (let i = 0; i < n; i++){
      const o = this.shoff + i * ent;
      if (o + ent > b.length) break;
      raw.push(this.is64
        ? { nameOff: this.u32(o), type: this.u32(o + 4), flags: this.u64(o + 8), addr: this.u64(o + 16),
            off: this.u64(o + 24), size: this.u64(o + 32), link: this.u32(o + 40),
            info: this.u32(o + 44), addralign: this.u64(o + 48), entsize: this.u64(o + 56) }
        : { nameOff: this.u32(o), type: this.u32(o + 4), flags: this.u32(o + 8), addr: this.u32(o + 12),
            off: this.u32(o + 16), size: this.u32(o + 20), link: this.u32(o + 24),
            info: this.u32(o + 28), addralign: this.u32(o + 32), entsize: this.u32(o + 36) });
    }
    const strs = raw[this.shstrndx];
    const strTab = strs ? b.subarray(strs.off, Math.min(strs.off + strs.size, b.length)) : new Uint8Array(0);
    this._secs = raw.map(s => ({ ...s, name: cstrAt(strTab, s.nameOff) }));
    return this._secs;
  }

  section(name){
    return this.sections().find(s => s.name === name) || null;
  }

  /** 段的原始字节（NOBITS 段没有文件内容，返回空） */
  data(name){
    const s = this.section(name);
    if (!s || s.type === SHT_NOBITS) return new Uint8Array(0);
    return this.b.subarray(s.off, Math.min(s.off + s.size, this.b.length));
  }

  /**
   * 按**运行地址**取文件里的字节（找包含该地址的 PROGBITS 段）。
   *
   * 用途：指令取指（`si` 解调用目标、断点单步算落点）与"flash 只读数据读不到"的兜底 ——
   * 见 `app/dbg/session.js` 的 `_codeBytes()`：HPM 这颗芯片上 **SBA 读 XIP 窗口（0x8000_0000）
   * 会失败/挂住**，而调试器要读的恰恰就是那儿的代码与 const。
   * 载入的 ELF 与板上跑的固件是同一份（行号/符号本来就依赖这一点），所以从这里取字节是对的。
   *
   * @param {object} [opts] `ro:true` = **只认只读段**（不含 SHF_WRITE）。
   *   `.text`/`.rodata` 在 flash 里运行期不会变，文件里那份是权威的；
   *   而 `.data`/`.bss` 在 RAM 里会被程序改写，**绝不能**拿文件内容冒充"目标当前内存"。
   * @returns {Uint8Array|null} 相邻已分配段可拼接；空洞、越界、NOBITS 返回 null
   */
  bytesAt(addr, len, { ro = false } = {}){
    if (!Number.isInteger(addr) || addr < 0 || addr > 0xffffffff ||
        !Number.isInteger(len) || len < 0 || addr + len > 0x100000000) return null;
    if (!len) return new Uint8Array(0);
    const end = addr + len, parts = [];
    let cursor = addr;
    const sections = this.sections().filter(s => s.type === SHT_PROGBITS && s.size &&
      (s.flags & 2) && (!ro || !(s.flags & 1))); // allocated bytes; no RAM initializers for RO reads
    while (cursor < end){
      const s = sections.find(s => cursor >= s.addr && cursor < s.addr + s.size);
      if (!s) return null; // A hole is unavailable, never synthetic zero data.
      const take = Math.min(end - cursor, s.addr + s.size - cursor);
      const off = s.off + cursor - s.addr;
      if (off < 0 || off + take > this.b.length) return null;
      parts.push(this.b.subarray(off, off + take));
      cursor += take;
    }
    if (parts.length === 1) return parts[0];
    const out = new Uint8Array(len);
    let offset = 0;
    for (const part of parts){ out.set(part, offset); offset += part.length; }
    return out;
  }

  /** 段名 → 原始字节（不存在的段返回空）*/
  has(name){ return !!this.section(name); }

  /**
   * 符号表（默认 SHT_SYMTAB；`all` 时含 .dynsym）。
   * 返回的对象里 `isObject` 表示 STT_OBJECT（变量/常量数据）。
   */
  symbols(all = false){
    const out = [];
    for (const sh of this.sections()){
      if (sh.type !== SHT_SYMTAB && !(all && sh.name === '.dynsym')) continue;
      const strSh = this.sections()[sh.link];
      const strTab = strSh ? this.b.subarray(strSh.off, Math.min(strSh.off + strSh.size, this.b.length)) : new Uint8Array(0);
      const ent = sh.entsize || (this.is64 ? 24 : 16);
      const cnt = Math.floor(sh.size / ent);
      for (let k = 0; k < cnt; k++){
        const o = sh.off + k * ent;
        if (o + ent > this.b.length) break;
        const nameOff = this.u32(o);
        const info = this.b[o + (this.is64 ? 4 : 12)];
        const shndx = this.u16(o + (this.is64 ? 6 : 14));
        const value = this.is64 ? this.u64(o + 8) : this.u32(o + 4);
        const size = this.is64 ? this.u64(o + 16) : this.u32(o + 8);
        const type = info & 0x0f, bind = info >> 4;
        out.push({
          name: cstrAt(strTab, nameOff), addr: value, size, type, bind, shndx,
          isObject: type === 1, isFunc: type === 2,
        });
      }
    }
    return out;
  }
}

/** 从字符串表里取一个 0 结尾的串（越界返回空串）*/
export function cstrAt(tab, off){
  if (!tab || off < 0 || off >= tab.length) return '';
  let e = off;
  const end = tab.length;
  while (e < end && tab[e] !== 0) e++;
  return latin1(tab.subarray(off, e));
}

/** 便于外部：是不是 ELF */
export function looksLikeElf(buf){
  const b = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  return b.length >= 64 && b[0] === 0x7f && b[1] === 0x45 && b[2] === 0x4c && b[3] === 0x46;
}
