/** ARMv7-M address watchpoints. Armv8-M FUNCTION encodings differ: never guess. */
import { u32leBytes } from './fmt.js';
export const DWT = { CTRL:0xe0001000, COMP:0xe0001020, MASK:0xe0001024,
  FUNCTION:0xe0001028, DEMCR:0xe000edfc, CPUID:0xe000ed00, DFSR:0xe000ed30,
  LAR:0xe0001fb0, LSR:0xe0001fb4 };
export const dwtReg = (base, slot) => (base + slot*16) >>> 0;
export function watchSpec(addr, size=4, mode='w'){
  if (!Number.isInteger(addr) || addr < 0 || addr > 0xffffffff) throw new Error('观察点地址须为 u32');
  if (!Number.isInteger(size) || size < 1 || size > 0x80000000 || !Number.isInteger(Math.log2(size)))
    throw new Error('观察范围须为 2 的幂（1/2/4/8…字节）');
  if (addr % size || addr + size > 0x100000000) throw new Error('观察地址须按范围大小对齐且不能跨 u32 边界');
  const fn = {r:5,w:6,rw:7}[mode];
  if (!fn) throw new Error('观察模式只能是 r / w / rw');
  return {addr, size, mode, fn, mask:Math.log2(size)};
}
export class DwtWatchpoints {
  constructor(session){ this.s=session; this.items=[]; this.capacity=null; }
  read(a){ return this.s.probe._readWord(a); }
  write(a,v){ return this.s.probe.writeMem(a,u32leBytes(v)); }
  async init(){
    if (this.s.arch.name !== 'arm') throw new Error('DWT 观察点仅适用于 Cortex-M；RISC-V 不支持此命令');
    if (this.capacity != null) return;
    const cpu = (await this.read(DWT.CPUID) >>> 4) & 0xfff;
    if (![0xc23,0xc24,0xc27].includes(cpu)) throw new Error('当前内核不是已支持的 Cortex-M3/M4/M7（Armv8-M 编码不同）');
    const demcr = await this.read(DWT.DEMCR);
    await this.write(DWT.DEMCR, demcr | 0x01000000); // preserve vector catch/other trace bits
    if (!((await this.read(DWT.DEMCR)) & 0x01000000)) throw new Error('DEMCR.TRCENA 无法使能');
    try { if ((await this.read(DWT.LSR) & 3) === 3) await this.write(DWT.LAR,0xc5acce55); } catch { /* optional lock registers */ }
    this.capacity = (await this.read(DWT.CTRL) >>> 28) & 15;
    if (!this.capacity) throw new Error('该内核未提供可用 DWT 比较器');
  }
  async program(item){
    const f=dwtReg(DWT.FUNCTION,item.slot);
    try {
      await this.write(f,0);
      await this.write(dwtReg(DWT.COMP,item.slot),item.addr);
      await this.write(dwtReg(DWT.MASK,item.slot),item.mask);
      if (((await this.read(dwtReg(DWT.MASK,item.slot))) & 31) !== item.mask)
        throw new Error('此比较器不支持请求的观察范围');
      if ((await this.read(dwtReg(DWT.COMP,item.slot)) >>> 0) !== item.addr) throw new Error('DWT 地址回读不一致');
      await this.write(f,item.fn);
      if (((await this.read(f)) & 15) !== item.fn) throw new Error('DWT FUNCTION 回读不一致');
    } catch(e){ await this.write(f,0).catch(()=>{}); throw e; }
  }
  async add(addr,size=4,mode='w'){
    const spec=watchSpec(addr,size,mode); await this.init();
    const duplicate=this.items.find(x=>x.addr===addr&&x.size===size&&x.mode===mode);
    if (duplicate) return duplicate;
    for (let slot=0;slot<this.capacity;slot++){
      if (this.items.some(x=>x.slot===slot) || ((await this.read(dwtReg(DWT.FUNCTION,slot))) & 15)) continue;
      const item={...spec,slot}; await this.program(item); this.items.push(item); return item;
    }
    throw new Error('DWT 比较器已用完（已有 trace/其他调试器占用也计入），请先删除观察点');
  }
  async remove(id){
    const item=this.items.find(x=>x.slot+1===id);
    if (!item) throw new Error('找不到观察点编号（wpl 查看）');
    await this.write(dwtReg(DWT.FUNCTION,item.slot),0);
    this.items=this.items.filter(x=>x!==item); return item;
  }
  async clear(){ for (const item of [...this.items]) await this.remove(item.slot+1); }
  async rearm(){
    if (!this.items.length) return;
    const demcr=await this.read(DWT.DEMCR); await this.write(DWT.DEMCR,demcr|0x01000000);
    try { if ((await this.read(DWT.LSR)&3)===3) await this.write(DWT.LAR,0xc5acce55); } catch {}
    for (const item of this.items) await this.program(item);
  }
  async haltReason(){
    if (!this.items.length || !((await this.read(DWT.DFSR)) & 4)) return null;
    const hit=[];
    for (const item of this.items) if ((await this.read(dwtReg(DWT.FUNCTION,item.slot))) & 0x01000000) hit.push(item.slot+1);
    return `DWT 数据访问命中${hit.length?' #'+hit.join(', #'):''}（PC 可能在访问指令之后）`;
  }
}
