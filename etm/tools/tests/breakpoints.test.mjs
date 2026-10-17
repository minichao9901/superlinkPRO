import test from 'node:test';
import assert from 'node:assert/strict';
import {EtmTarget} from '../../web/app/target.js';
import {identifyTarget,targetPort,completeClocks} from '../../web/app/clock.js';

function fixture(){
  const t=new EtmTarget(),mem=new Map([[0xe0002000,0x61],[0xe0002008,0x40001001],[0xe000200c,0x1234]]);
  t.read=async a=>mem.get(a)||0;t.write=async(a,v)=>mem.set(a,a===0xe0002000?((mem.get(a)&~3)|(v&3))>>>0:v>>>0);t.halt=async()=>{};
  return {t,mem,b:t.breakpoints};
}
test('persistent breakpoint suspension/restoration owns only allocated slots',async()=>{
  const {t,mem,b}=fixture();try{
    const one=b.add({address:0x08002002}),two=b.add({address:0x08003000});
    await b.resume();assert.equal(one.status,'已生效');assert.equal(b.add({address:one.address}),one);
    b.hit(one.address);assert.equal(one.status,'已命中');assert.equal(two.status,'已生效');
    await b.suspend();assert.equal(mem.get(0xe0002008),0x40001001);assert.equal(mem.get(0xe000200c),0x1234);
    await b.resume();assert.ok(one.lease);await b.clear();assert.equal(mem.get(0xe000200c),0x1234);assert.equal(b.items.length,0);
  }finally{t.bus.close();}
});
test('capacity, disabled foreign slots and changed ownership fail explicitly',async()=>{
  const {t,mem,b}=fixture();try{
    for(let i=0;i<5;i++)await b.arm(b.add({address:0x08002000+4*i}));
    await assert.rejects(b.arm(b.add({address:0x08004000})),/没有空闲/);
    const owned=b.items[0];mem.set(owned.lease.slot,0x777);await assert.rejects(b.disarm(owned),/拒绝覆盖/);
  }finally{t.bus.close();}
  const f=fixture();try{f.mem.set(0xe0002000,0x60);await assert.rejects(f.b.arm(f.b.add({address:0x08002000})),/保留了其他/);assert.equal(f.mem.get(0xe0002000),0x60);}finally{f.t.bus.close();}
});
test('page enabled FPB is disabled again after releasing last owned slot',async()=>{
  const {t,mem,b}=fixture();try{mem.set(0xe0002000,0x60);mem.set(0xe0002008,0);const bp=b.add({address:0x08002000});await b.arm(bp);await b.disarm(bp);assert.equal(mem.get(0xe0002000)&1,0);}finally{t.bus.close();}
});
test('continue past owned breakpoint clears step interrupt masking and rearms it',async()=>{
  const {t,mem,b}=fixture();let pc=0x08002002,halted=true;const writes=[];const write=t.write;
  t.write=async(a,v)=>{writes.push([a,v]);await write(a,v);if(a===0xe000edf0&&(v&4))pc+=4;};
  t.pc=async()=>pc;t.wait=async()=>{};t.probe={isHalted:async()=>halted,run:async()=>{halted=false;}};
  try{const bp=b.add({address:pc});await b.arm(bp);await t.run();assert.equal(halted,false);assert.ok(bp.lease);assert.ok(writes.some(([a,v])=>a===0xe000edf0&&v===0xa05f0003));}finally{t.bus.close();}
});
test('failed step leaves core halted with breakpoint restored',async()=>{
  const {t,b}=fixture();t.pc=async()=>0x08002002;t.wait=async()=>{};let ran=false;t.probe={isHalted:async()=>true,run:async()=>{ran=true;}};
  try{const bp=b.add({address:0x08002002});await b.arm(bp);await assert.rejects(t.run(),/未推进/);assert.ok(bp.lease);assert.equal(ran,false);}finally{t.bus.close();}
});
test('preset identity is checked against both CPU and DEV_ID',()=>{
  assert.equal(identifyTarget(0x410fc231,0x410).id,'stm32f1');assert.equal(identifyTarget(0x410fc241,0x413).id,'stm32f4');
  assert.equal(identifyTarget(0x411fc271,0x450).id,'stm32h743');assert.equal(identifyTarget(0x411fc271,0x480).id,'stm32h7b0');
  assert.equal(identifyTarget(0x410fc231,0x413),null);assert.throws(()=>identifyTarget(0x411fc271,0x480,'stm32f4'),/不符/);
});
test('restore only the suspended subset, leaving unverified pending breakpoints unarmed',async()=>{
  const {t,b}=fixture();try{
    const active=b.add({address:0x08002000});await b.arm(active);const pending=b.add({address:0x08004000});
    await b.suspend();await b.resume([active]);assert.ok(active.lease);assert.equal(pending.lease,null);
  }finally{t.bus.close();}
});
test('F1 inspection reads its own RCC/DBG and blocks H7-only trace writes',async()=>{
  const t=new EtmTarget(),writes=[],mem=new Map([[0xe000ed00,0x410fc231],[0xe0042000,0x410],
    [0x40021000,(1<<17)|(1<<25)],[0x40021004,(2<<2)|(1<<16)|(7<<18)],[0xe0002000,0x60]]);
  t.hseHz=8e6;t.read=async a=>{if(a>=0x5c000000&&a<0x5c020000)throw Error('不能访问 H7 寄存器');return mem.get(a)||0;};
  t.write=async(a,v)=>writes.push([a,v]);t.probe={isHalted:async()=>false};
  try{const info=await t.inspect();assert.equal(info.clocks.coreHz,72e6);assert.equal(info.traceSupported,false);assert.equal(info.config.traceIoEnabled,false);
    await assert.rejects(t.configure(),/尚未验收/);assert.equal(writes.length,0);
  }finally{t.bus.close();}
});
test('F1/F4 RCC clock calculation requires board HSE and derives DDR clock',()=>{
  const f1={cr:(1<<17)|(1<<25),cfgr:(2<<2)|(1<<16)|(7<<18)};
  const c1=completeClocks(targetPort('stm32f1'),f1,8e6);assert.equal(c1.coreHz,72e6);assert.equal(c1.physicalTraceHz,36e6);assert.equal(completeClocks(targetPort('stm32f1'),f1).coreHz,null);
  const f4={cr:(1<<17)|(1<<25),cfgr:2<<2,pll:8|(336<<6)|(1<<22)};
  const c4=completeClocks(targetPort('stm32f4'),f4,8e6);assert.equal(c4.coreHz,168e6);assert.equal(c4.physicalTraceHz,84e6);
});
