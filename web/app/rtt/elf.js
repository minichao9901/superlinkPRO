/**
 * 从 ELF 里找符号（默认找 _SEGGER_RTT）。
 * 为什么要它：控制块在 RAM 里的地址不固定，扫描要好几秒；ELF 里符号是现成的。
 * 支持 ELF32 / ELF64、大小端；没有符号表（strip 过的）就返回 null，让调用方退回扫描。
 */
import { u32le, latin1 } from '../core/bin.js';

const rd16 = (b, o, le) => le ? (b[o] | (b[o + 1] << 8)) : ((b[o] << 8) | b[o + 1]);
const rd32 = (b, o, le) => le ? u32le(b, o)
  : (((b[o] << 24) | (b[o + 1] << 16) | (b[o + 2] << 8) | b[o + 3]) >>> 0);
const rd64 = (b, o, le) => {
  const lo = rd32(b, o, le), hi = rd32(b, o + 4, le);
  return le ? hi * 4294967296 + lo : lo * 4294967296 + hi;
};

/**
 * @param {ArrayBuffer|Uint8Array} buf 整个 .elf 文件
 * @param {string} name 符号名
 * @returns {{addr:number,size:number,name:string}|null}
 */
export function findSymbol(buf, name = '_SEGGER_RTT'){
  const b = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  if (b.length < 64) return null;
  if (!(b[0] === 0x7f && b[1] === 0x45 && b[2] === 0x4c && b[3] === 0x46)) throw new Error('不是 ELF 文件');

  const is64 = b[4] === 2;
  const le = b[5] === 1;
  const shoff = is64 ? rd64(b, 0x28, le) : rd32(b, 0x20, le);
  const shentsize = rd16(b, is64 ? 0x3a : 0x2e, le);
  const shnum = rd16(b, is64 ? 0x3c : 0x30, le);
  if (!shoff || !shnum || !shentsize) return null;

  const shdr = i => {
    const o = shoff + i * shentsize;
    if (o + shentsize > b.length) return null;
    return is64
      ? { name: rd32(b, o, le), type: rd32(b, o + 4, le), addr: rd64(b, o + 16, le),
          off: rd64(b, o + 24, le), size: rd64(b, o + 32, le), link: rd32(b, o + 40, le), entsize: rd64(b, o + 56, le) }
      : { name: rd32(b, o, le), type: rd32(b, o + 4, le), addr: rd32(b, o + 12, le),
          off: rd32(b, o + 16, le), size: rd32(b, o + 20, le), link: rd32(b, o + 24, le), entsize: rd32(b, o + 36, le) };
  };
  const strAt = (tblOff, tblSize, off) => {
    const s = tblOff + off;
    if (s >= b.length) return '';
    let e = s;
    const end = Math.min(tblOff + tblSize, b.length);
    while (e < end && b[e] !== 0) e++;
    return latin1(b.subarray(s, e));
  };

  for (let i = 0; i < shnum; i++){
    const sh = shdr(i);
    if (!sh || sh.type !== 2) continue;                    // SHT_SYMTAB
    const str = shdr(sh.link);
    if (!str) continue;
    const ent = sh.entsize || (is64 ? 24 : 16);
    const cnt = Math.floor(sh.size / ent);
    for (let k = 0; k < cnt; k++){
      const o = sh.off + k * ent;
      if (o + ent > b.length) break;
      const nameOff = rd32(b, o, le);
      if (!nameOff) continue;
      const nm = strAt(str.off, str.size, nameOff);
      if (nm !== name) continue;
      const value = is64 ? rd64(b, o + 8, le) : rd32(b, o + 4, le);
      const size = is64 ? rd64(b, o + 16, le) : rd32(b, o + 8, le);
      return { addr: value, size, name: nm };
    }
  }
  return null;
}
