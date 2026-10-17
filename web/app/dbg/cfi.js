/** Bounded DWARF32 .debug_frame reader. Debugger-only; unsupported rules fail closed.
 * DWARF 5 §6.4. No target code execution and no guessed caller registers.
 */
export class Reader {
  constructor(bytes, pos=0, end=bytes.length){if(!Number.isInteger(pos)||pos<0||end>bytes.length||pos>end)throw new Error('DWARF 偏移无效');this.b=bytes;this.p=pos;this.end=end;}
  need(n){if(n<0||this.p+n>this.end)throw new Error('DWARF 数据被截断');}
  u8(){this.need(1);return this.b[this.p++];}
  uint(n){this.need(n);let v=0;for(let i=0;i<n;i++)v+=this.b[this.p++]*2**(8*i);return v;}
  sint(n){const v=this.uint(n),bits=n*8,sign=2**(bits-1);return v>=sign?v-2**bits:v;}
  leb(signed=false){let v=0,shift=0,b;do{b=this.u8();v+=(b&127)*2**shift;shift+=7;if(shift>35)throw new Error('DWARF LEB 过长');}while(b&128);if(signed&&(b&64))v-=2**shift;return v;}
  bytes(n){this.need(n);const v=this.b.subarray(this.p,this.p+n);this.p+=n;return v;}
  str(){let s='';for(let i=0;i<256;i++){const b=this.u8();if(!b)return s;s+=String.fromCharCode(b);}throw new Error('CFI augmentation 过长');}
}
const cache=new WeakMap();
function entries(elf){
  if(cache.has(elf))return cache.get(elf);
  const b=elf.section('.debug_frame')?elf.data('.debug_frame'):null;
  const out=[],cies=new Map(); if(!b){cache.set(elf,out);return out;}
  const r=new Reader(b);
  while(r.p<b.length){
    const offset=r.p,length=r.uint(4);if(!length)continue;
    if(length<4)throw new Error('CFI 记录过短');
    if(length===0xffffffff)throw new Error('暂不支持 DWARF64 CFI');
    r.need(length);const end=r.p+length,id=r.uint(4),q=new Reader(b,r.p,end);
    if(id===0xffffffff){
      const version=q.u8(),augmentation=q.str();
      if(![1,3,4].includes(version)||augmentation)throw new Error('暂不支持该 CFI version/augmentation');
      if(version===4&&(q.u8()!==4||q.u8()!==0))throw new Error('CFI 地址格式不是32位平坦地址');
      const ca=q.leb(),da=q.leb(true),ra=version===1?q.u8():q.leb();
      if(ca<1||ra>63)throw new Error('CFI 对齐/返回寄存器无效');
      cies.set(offset,{ca,da,ra,ops:q.bytes(end-q.p)});
    }else{
      const cie=cies.get(id);if(!cie)throw new Error('CFI 引用不存在的 CIE');
      const start=q.uint(4),size=q.uint(4);if(start+size>0x100000000)throw new Error('CFI 地址溢出');
      out.push({start,end:start+size,cie,ops:q.bytes(end-q.p)});
    }
    r.p=end;
  }
  cache.set(elf,out);return out;
}
const ehCache=new WeakMap();
function ehPointer(r,encoding,fieldAddress,{range=false}={}){
  const format=range?(encoding&15):encoding&15,application=range?0:(encoding&0x70);
  let value;
  switch(format){
    case 3:value=r.uint(4);break;       // DW_EH_PE_udata4
    case 0x0b:value=r.sint(4);break;    // DW_EH_PE_sdata4
    default:throw new Error('暂不支持 .eh_frame 指针编码 0x'+encoding.toString(16));
  }
  if(range)return value;
  if(application===0x10)return (fieldAddress+value)>>>0; // DW_EH_PE_pcrel
  if(application===0)return value>>>0;
  throw new Error('暂不支持 .eh_frame 指针基址 0x'+encoding.toString(16));
}
function ehEntries(elf){
  if(ehCache.has(elf))return ehCache.get(elf);
  const section=elf.section('.eh_frame'),b=section?elf.data('.eh_frame'):null,out=[],cies=new Map();
  if(!b){ehCache.set(elf,out);return out;}
  const r=new Reader(b);
  while(r.p<b.length){
    const offset=r.p,length=r.uint(4);if(!length)break;
    if(length<4)throw new Error('.eh_frame 记录过短');
    if(length===0xffffffff)throw new Error('暂不支持 DWARF64 .eh_frame');
    r.need(length);const end=r.p+length,idField=r.p,id=r.uint(4),q=new Reader(b,r.p,end);
    if(id===0){
      const version=q.u8(),augmentation=q.str();
      if(![1,3,4].includes(version))throw new Error('暂不支持该 .eh_frame CIE version');
      if(version===4&&(q.u8()!==4||q.u8()!==0))throw new Error('.eh_frame 地址格式不是32位平坦地址');
      const ca=q.leb(),da=q.leb(true),ra=version===1?q.u8():q.leb();
      if(ca<1||ra>63)throw new Error('.eh_frame 对齐/返回寄存器无效');
      let encoding=0;
      if(augmentation){
        if(!augmentation.startsWith('z'))throw new Error('暂不支持 .eh_frame augmentation '+augmentation);
        const augLen=q.leb(),augEnd=q.p+augLen;if(augEnd>end)throw new Error('.eh_frame CIE augmentation 数据越界');
        for(const char of augmentation.slice(1)){
          if(char==='R')encoding=q.u8();
          else if(char==='S'){} // signal-frame marker has no augmentation payload
          else throw new Error('暂不支持 .eh_frame augmentation 字段 '+char);
        }
        if(q.p>augEnd)throw new Error('.eh_frame CIE augmentation 字段超出长度');
        q.p=augEnd;
      }
      if(augmentation&&!encoding)throw new Error('.eh_frame 缺少 FDE pointer encoding');
      cies.set(offset,{ca,da,ra,encoding,augmentation,ops:q.bytes(end-q.p)});
    }else{
      const cieOffset=idField-id,cie=cies.get(cieOffset);if(!cie)throw new Error('.eh_frame 引用不存在的 CIE');
      const fieldOffset=q.p,start=ehPointer(q,cie.encoding,(section.addr+fieldOffset)>>>0);
      const size=ehPointer(q,cie.encoding,0,{range:true});
      if(size<0||start+size>0x100000000)throw new Error('.eh_frame 地址范围无效');
      if(cie.encoding!==0xff&&cie.augmentation){const augLen=q.leb();q.need(augLen);q.p+=augLen;}
      out.push({start,end:start+size,cie,ops:q.bytes(end-q.p)});
    }
    r.p=end;
  }
  ehCache.set(elf,out);return out;
}
const clone=s=>({reg:s.reg,offset:s.offset,rules:new Map(s.rules)});
function program(ops,cie,state,initial,start,pc,maxRegister=15){
  const r=new Reader(ops),saved=[];let loc=start,steps=0;
  const register=()=>{const n=r.leb();if(n>maxRegister)throw new Error('CFI 非核心寄存器规则');return n;};
  while(r.p<r.end){
    if(++steps>16384)throw new Error('CFI 指令过多');
    const op=r.u8(),primary=op&0xc0,n=op&63;
    if(primary===0x40){const next=loc+n*cie.ca;if(pc<next)break;loc=next;continue;}
    if(primary===0x80){if(n>maxRegister)throw new Error('CFI 非核心寄存器');state.rules.set(n,{kind:'offset',v:r.leb()*cie.da});continue;}
    if(primary===0xc0){if(n>maxRegister)throw new Error('CFI 非核心寄存器');if(initial.rules.has(n))state.rules.set(n,initial.rules.get(n));else state.rules.delete(n);continue;}
    if(op>=1&&op<=4){const next=op===1?r.uint(4):loc+r.uint(2**(op-2))*cie.ca;if(next<loc)throw new Error('CFI PC 倒退');if(pc<next)break;loc=next;continue;}
    switch(op){
      case 0:break;
      case 5:state.rules.set(register(),{kind:'offset',v:r.leb()*cie.da});break;
      case 6:{const n=register();if(initial.rules.has(n))state.rules.set(n,initial.rules.get(n));else state.rules.delete(n);break;}
      case 7:state.rules.set(register(),{kind:'undefined'});break;
      case 8:state.rules.set(register(),{kind:'same'});break;
      case 9:state.rules.set(register(),{kind:'register',v:register()});break;
      case 10:if(saved.length>=32)throw new Error('CFI 状态栈过深');saved.push(clone(state));break;
      case 11:{const old=saved.pop();if(!old)throw new Error('CFI 状态栈为空');Object.assign(state,old);break;}
      case 12:state.reg=register();state.offset=r.leb();break;
      case 13:state.reg=register();break;
      case 14:state.offset=r.leb();break;
      case 17:state.rules.set(register(),{kind:'offset',v:r.leb(true)*cie.da});break;
      case 18:state.reg=register();state.offset=r.leb(true)*cie.da;break;
      case 19:state.offset=r.leb(true)*cie.da;break;
      case 20:state.rules.set(register(),{kind:'value',v:r.leb()*cie.da});break;
      case 21:state.rules.set(register(),{kind:'value',v:r.leb(true)*cie.da});break;
      case 0x2e:r.leb();break; // GNU_args_size: no effect on unwinding
      default:throw new Error('不支持 CFI opcode 0x'+op.toString(16));
    }
  }
  return state;
}
export function cfiRow(elf,pc,{maxRegister=15}={}){
  if(!Number.isInteger(maxRegister)||maxRegister<0||maxRegister>63)throw new Error('CFI 寄存器上限无效');
  const f=entries(elf).find(f=>pc>=f.start&&pc<f.end)??(maxRegister>15?ehEntries(elf).find(f=>pc>=f.start&&pc<f.end):null);if(!f)return null;
  const empty={reg:null,offset:0,rules:new Map()};
  const initial=program(f.cie.ops,f.cie,clone(empty),empty,0,Infinity,maxRegister);
  const row=program(f.ops,f.cie,clone(initial),initial,f.start,pc,maxRegister);
  return {...row,ra:f.cie.ra};
}
export async function unwindCfi(regs,known,row,readWord,valid,options={}){
  const registerCount=options.registerCount??16,pcReg=options.pcReg??15,spReg=options.spReg??13;
  const preservedRegisters=options.preservedRegisters??Array.from({length:8},(_,i)=>i+4);
  const raFallback=options.raFallback??true;
  if(!Number.isInteger(registerCount)||registerCount<1||registerCount>64||
     !Number.isInteger(pcReg)||pcReg<0||pcReg>=registerCount||!Number.isInteger(spReg)||spReg<0||spReg>=registerCount)
    throw new Error('CFI 架构寄存器布局无效');
  if(!Number.isInteger(row.ra)||row.ra<0||row.ra>=registerCount||row.reg>=registerCount)throw new Error('CFI 架构寄存器超出范围');
  if(row.reg==null||!known.has(row.reg))throw new Error('CFA 寄存器不可用');
  const cfa=regs[row.reg]+row.offset;if(!Number.isInteger(cfa)||!valid(cfa,0))throw new Error('CFA 超出栈边界');
  const out=new Uint32Array(registerCount),available=new Set();
  // Only ABI-preserved values may flow to a caller without an explicit CFI rule.
  for(const n of preservedRegisters){if(!Number.isInteger(n)||n<0||n>=registerCount)throw new Error('CFI 保留寄存器布局无效');if(known.has(n)){out[n]=regs[n];available.add(n);}}
  for(const [n,rule]of row.rules){
    available.delete(n);
    if(rule.kind==='undefined')continue;
    if(rule.kind==='same'){if(known.has(n)){out[n]=regs[n];available.add(n);}continue;}
    if(rule.kind==='register'){if(known.has(rule.v)){out[n]=regs[rule.v];available.add(n);}continue;}
    const a=cfa+rule.v;
    if(rule.kind==='value'){if(a<0||a>0xffffffff)throw new Error('CFI 值溢出');out[n]=a;}
    else {if(!valid(a,4))throw new Error('CFI 栈读取超出边界');out[n]=await readWord(a);}
    available.add(n);
  }
  // ABI leaf functions may keep return address in LR without emitting an explicit rule.
  if(raFallback&&!row.rules.has(row.ra)&&known.has(row.ra)){out[row.ra]=regs[row.ra];available.add(row.ra);}
  if(!available.has(row.ra))throw new Error('返回地址不可用');
  out[pcReg]=out[row.ra];out[spReg]=cfa;available.add(pcReg);available.add(spReg);
  return {regs:out,known:available,cfa};
}
