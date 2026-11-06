/** Selected configuration only; this module never opens or controls a probe. */
export function sessionContextBusy(t){
  const manager=t.probeManager?.summary();
  return !!(manager?.owners.length||manager?.pending.length||manager?.failures.length||
    t.flash?.busy||t.flash?.probe||t.rtt?.probe||t.rtt?._connectPromise||t.rtt?._disconnectPromise||
    t.scope?.hid||t.scope?.transport||t.scope?.running||t.scope?._starting||t.scope?._stopPromise||
    t.scope?._hidConnectPromise||t.scope?._usbConnectPromise||t.scope?._releasing||
    t.dbg?.session?.connected||t.dbg?.session?.busy||t.dbg?._connecting||t.dbg?._disconnecting||
    t.hid?.dev?.connected||t.hid?._connecting||t.hid?._disconnecting||t.hid?.last?.running||t.hid?._bridgeRequested||
    t.etm?.probe||t.etm?.busy||t.swo?.capture?.active||
    t.spiSession?.connected||t.spiSession?.enabled||t.i2c?.session?.connected||
    t.analog?.session?.connected||t.spiCdc?.session?.connected);
}

export class SessionContext {
  constructor(){this.target=null;this.elf=null;this.pending=false;this._bytes=null;this._revision=0;this.listeners=new Set();}
  subscribe(fn){this.listeners.add(fn);return ()=>this.listeners.delete(fn);}
  notify(){for(const fn of this.listeners)fn();}
  clearElf(){this._revision++;this.pending=false;this.elf=null;this._bytes=null;this.notify();}
  setTarget(target){
    if(!target||!['arm','riscv'].includes(target.arch))throw new Error('目标类型无效');
    if(this.target?.id===target.id&&this.target.arch===target.arch)return;
    this.target=Object.freeze({...target});this._revision++;this.pending=false;
    if(this.elf&&(this.elf.arch!==target.arch||this.elf.targetId&&this.elf.targetId!==target.id)){
      this.elf=null;this._bytes=null;
    }
    this.notify();
  }
  async publishElf(bytes,name,source,sourceTarget=this.target){
    const revision=++this._revision;
    this.pending=true;this.notify();
    try {
      const copy=bytes instanceof Uint8Array?bytes.slice().buffer:bytes.slice(0);
      const b=new Uint8Array(copy);
      if(b.length<64||b[0]!==127||b[1]!==69||b[2]!==76||b[3]!==70)throw new Error('不是有效 ELF');
      const machine=new DataView(copy).getUint16(18,b[5]===1);
      const arch=machine===40?'arm':machine===243?'riscv':null;
      if(!arch)throw new Error('共享 ELF 仅支持 ARM / RISC-V');
      const digest=await crypto.subtle.digest('SHA-256',copy);
      if(revision!==this._revision)return null;
      const sha256=Array.from(new Uint8Array(digest),v=>v.toString(16).padStart(2,'0')).join('');
      this._bytes=copy;
      this.elf=Object.freeze({name:name||'target.elf',size:copy.byteLength,sha256,arch,source,
        targetId:sourceTarget?.arch===arch&&sourceTarget.id!==arch?sourceTarget.id:null});
      return this.elf;
    } finally {if(revision===this._revision){this.pending=false;this.notify();}}
  }
  elfBytes(){return this._bytes?.slice(0)||null;}
  canReuse(arch,targetId=this.target?.id){return !this.pending&&!!this.elf&&this.elf.arch===arch&&(!this.elf.targetId||this.elf.targetId===targetId&&this.elf.targetId===this.target?.id);}
}
