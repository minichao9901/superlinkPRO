import assert from 'node:assert/strict';
import {existsSync,readFileSync} from 'node:fs';
import {cfiRow,unwindCfi} from '../../app/dbg/cfi.js';
import {backtrace} from '../../app/dbg/backtrace.js';
import {evaluateLocation,frameLocals} from '../../app/dbg/locals.js';
import {RV_ARCH} from '../../app/dbg/rv.js';
import {Elf} from '../../app/elf/elf.js';
import {FRAME_CASES} from './dbg-frame-contract.mjs';

const u32=n=>[n&255,(n>>>8)&255,(n>>>16)&255,(n>>>24)&255];
const record=body=>[...u32(body.length),...body];
const cie=[...u32(0xffffffff),3,0,1,0x7c,1,0x0c,2,16,0x81,2,0x92,3];
const fde=[...u32(0),...u32(0x1000),...u32(0x20)];
const debugFrame=Uint8Array.from([...record(cie),...record(fde)]);
const code={name:'.text',addr:0x1000,size:0x20,flags:4};
const elf={section:name=>name==='.debug_frame'?{addr:0}:null,data:name=>name==='.debug_frame'?debugFrame:null,
  sections:()=>[code]};

const row=cfiRow(elf,0x1000,{maxRegister:32});
assert.equal(row.reg,2,'RISC-V CFA uses x2/sp');
assert.equal(row.offset,16);
assert.equal(row.ra,1,'RISC-V DWARF return-address column is x1/ra');
assert.deepEqual(row.rules.get(1),{kind:'offset',v:-8});
assert.deepEqual(row.rules.get(18),{kind:'offset',v:-12},'extended CFI registers above x15 are retained');
assert.throws(()=>cfiRow(elf,0x1000),/非核心寄存器/,'ARM register ceiling still rejects RISC-V CFI unless opted in');

// GCC's RISC-V async unwind tables use .eh_frame with a zR/pcrel-sdata4 CIE.
const ehBase=0x80001000,ehCie=record([...u32(0),3,0x7a,0x52,0,1,0x7c,1,1,0x1b,0x0c,2,16,0x81,1]);
const ehFdeOffset=ehCie.length,ehPc=0x80001100,ehLocField=ehFdeOffset+8;
const ehFde=record([...u32(ehFdeOffset+4),...u32(ehPc-(ehBase+ehLocField)),...u32(0x40),0]);
const ehBytes=Uint8Array.from([...ehCie,...ehFde]);
const ehElf={section:name=>name==='.eh_frame'?{addr:ehBase}:null,data:name=>name==='.eh_frame'?ehBytes:null,sections:()=>[code]};
const ehRow=cfiRow(ehElf,ehPc+4,{maxRegister:32});
assert.equal(ehRow.reg,2,'RISC-V .eh_frame CFA uses x2/sp');
assert.equal(ehRow.offset,16);
assert.equal(ehRow.ra,1);
assert.deepEqual(ehRow.rules.get(1),{kind:'offset',v:-4},'.eh_frame resolves pcrel FDE addresses and signed data alignment');

const sp=0x8800,regs=new Uint32Array(33);
regs[1]=0x1010;regs[2]=sp;regs[8]=0x8888;regs[9]=0x9999;regs[18]=0x1818;regs[32]=0x1000;
const words=new Map([[sp+8,0x1010],[sp+4,0x1818]]);
const readWord=async address=>{if(!words.has(address))throw new Error('unmapped stack word');return words.get(address);};
const valid=(address,size)=>address>=sp&&address+size<=sp+0x100&&address%4===0;
const unwound=await unwindCfi(regs,new Set(Array.from({length:33},(_,i)=>i)),row,readWord,valid,
  {registerCount:33,pcReg:32,spReg:2,preservedRegisters:[8,9,...Array.from({length:10},(_,i)=>i+18)]});
assert.equal(unwound.cfa,sp+16);
assert.equal(unwound.regs[1],0x1010);
assert.equal(unwound.regs[18],0x1818);
assert.equal(unwound.regs[32],0x1010,'caller PC is synthesized from x1/ra');
assert.equal(unwound.regs[2],sp+16,'caller SP is the CFA');
assert.equal(unwound.regs[8],0x8888,'RISC-V callee-saved registers survive without an explicit rule');
assert.equal(unwound.known.has(10),false,'caller-saved x10 is not guessed');

const ctx={regs,known:new Set([0,1,2,8,9,18,32]),cfa:sp+16,base:sp,read:async()=>new Uint8Array(),maxRegister:32,registerPrefix:'x'};
assert.deepEqual(await evaluateLocation(Uint8Array.of(0x90,18),ctx),{value:0x1818,direct:true},'DW_OP_regx x18');
assert.deepEqual(await evaluateLocation(Uint8Array.of(0x92,18,0x04),ctx),{value:0x181c,direct:false},'DW_OP_bregx x18+4');
await assert.rejects(()=>evaluateLocation(Uint8Array.of(0x90,28),ctx),/寄存器 x28 不可恢复/);

function fakeDwarf(){
 const attrs=(values)=>new Map(values),root={offset:0,parent:-1,tag:0x11,depth:0,cu:{version:4,addrSize:4},attrs:attrs([])};
 const fn={offset:1,parent:0,tag:0x2e,depth:1,cu:root.cu,attrs:attrs([[3,{value:'engine_frame_rv'}],[0x11,{form:1,value:0x1000}],[0x12,{form:6,value:0x20}]])};
 const direct={offset:2,parent:1,tag:0x34,depth:2,cu:root.cu,attrs:attrs([[3,{value:'saved_x18'}],[0x49,{value:1}],[2,{value:Uint8Array.of(0x90,18)}]])};
 const lost={offset:3,parent:1,tag:0x34,depth:2,cu:root.cu,attrs:attrs([[3,{value:'lost_x28'}],[0x49,{value:1}],[2,{value:Uint8Array.of(0x90,28)}]])};
 const records=[root,fn,direct,lost];
 return { _arr:records,index(){},dieAt:id=>records.find(r=>r.offset===id),childrenOf:r=>records.filter(x=>x.parent===r.offset),merged:r=>r,
  attr:(r,id)=>r?.attrs.get(id),num:(r,id)=>typeof r?.attrs.get(id)?.value==='number'?r.attrs.get(id).value:null,
  name:r=>r?.attrs.get(3)?.value||'',type:()=>({kind:'scalar',size:4,scalar:'u32'}),elf:{data:()=>null} };
}
const dwarf=fakeDwarf(),localSession={halted:true,arch:RV_ARCH,sym:{dwarf},memRead:async()=>new Uint8Array()};
const locals=await frameLocals(localSession,{kind:'cfi',lookup:0x1004,cfa:sp+16,regs:Array.from(regs),known:[...ctx.known]});
assert.equal(locals.rows.find(r=>r.name==='saved_x18').value,String(0x1818));
assert.match(locals.rows.find(r=>r.name==='lost_x28').error,/寄存器 x28 不可恢复/);

const frameReads=[];
const mockSession={halted:true,pc:0x1000,arch:RV_ARCH,sym:{elf,nameOf:address=>address<0x1010?'engine_frame_leaf':'engine_frame_caller',at:()=>({file:'dbg_frames.c',line:1})},
 refresh:async()=>{},readReg:async name=>name==='sp'?sp:name==='pc'?0x1000:regs[Number(name.slice(1))],
 memRead:async(address,length)=>{frameReads.push([address,length]);if(length===4){const value=words.get(address);if(value==null)throw new Error('unmapped stack word');return Uint8Array.from(u32(value));}
  return Uint8Array.from(Array.from({length},(_,i)=>words.get(address+i)??0));}};
words.set(sp+8,0x1010);words.set(sp+20,0x1818);words.set(sp+24,0x1002);
const bt=await backtrace(mockSession,{depth:2,stackBytes:128});
assert.equal(bt.scan,false);
assert.equal(bt.frames.length,2);
assert.equal(bt.frames[1].pc,0x1010);
assert.equal(bt.frames[1].sp,sp+16);
assert.equal(bt.frames[1].lookup,0x100f,'RISC-V return lookup backs into the call instruction by one byte');
assert.equal(bt.frames[1].kind,'cfi');
assert.equal(bt.frames[1].known.includes(10),false);
assert.ok(frameReads.length>=2);

const hpmElfPath=process.env.HPM_DBG_FRAME_ELF;
if(hpmElfPath){
 assert.ok(existsSync(hpmElfPath),`HPM_DBG_FRAME_ELF does not exist: ${hpmElfPath}`);
 const hpm=new Elf(new Uint8Array(readFileSync(hpmElfPath))),symbols=hpm.symbols(true);
 for(const c of FRAME_CASES){
  const symbol=symbols.find(s=>s.name===c.checkpoint);assert.ok(symbol,`HPM fixture includes ${c.checkpoint}`);
  const realRow=cfiRow(hpm,symbol.addr,{maxRegister:32});
  assert.ok(realRow,`${c.checkpoint} resolves to .debug_frame`);
  assert.equal(realRow.reg,2,`${c.checkpoint} CFI CFA is based on RISC-V x2/sp`);
  assert.equal(realRow.ra,1,`${c.checkpoint} CFI return column is RISC-V x1/ra`);
 }
}

console.log('dbg-frame-riscv: DWARF CFI x0-x31/PC columns, CFA/ra/SP, callee-saved rules, regx/bregx locals and bounded two-frame unwind PASS');
