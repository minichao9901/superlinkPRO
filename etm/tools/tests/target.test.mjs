import test from 'node:test';
import assert from 'node:assert/strict';
import {EtmTarget} from '../../web/app/target.js';
import {decodeClocks,targetPort,completeClocks} from '../../web/app/clock.js';
import {traceCapabilities} from '../../web/app/trace-options.js';

function h7Configuration(idr0=0x80006e1){
  const t=new EtmTarget(),mem=new Map([[0xe0041010,0x801],[0xe004103c,0x123]]),writes=[];
  t.traceFamily='h7';t.halt=async()=>{};t.wait=async()=>{};t.unlock=async()=>{};
  t.read=async a=>{if(a===0xe004103c&&(idr0&0x18)!==0x18)throw Error('unimplemented BBCTLR');return mem.get(a)||0;};
  t.write=async(a,v)=>{mem.set(a,v>>>0);writes.push([a,v>>>0]);};
  t.inspect=async()=>{t.info={profile:'stm32h7b0',traceSupported:true,config:{idr0,config:mem.get(0xe0041010)},traceOptions:traceCapabilities('stm32h7b0',{idr0}),clocks:{traceReady:true}};return t.info;};
  return {t,mem,writes};
}

test('H7 branch broadcast maps to CONFIGR bit 3, preserves other bits and avoids absent BBCTLR',async()=>{
  const {t,mem,writes}=h7Configuration();try{
    await t.configure({branchBroadcast:true,returnStack:true,cycleCount:true});
    assert.equal(mem.get(0xe0041010),0x1819);assert.ok(writes.every(([a])=>a!==0xe004103c));
    await t.configure({branchBroadcast:false});assert.equal(mem.get(0xe0041010),0x801);
  }finally{t.bus.close();}
});

test('H7 unsupported options are rejected before configuration writes',async()=>{
  for(const option of ['branchBroadcast','returnStack','cycleCount']){
    const {t,writes}=h7Configuration(0);try{await assert.rejects(t.configure({[option]:true}),/未实现/);assert.equal(writes.length,0);}finally{t.bus.close();}
  }
});

test('H7 data-trace implementation uses unrestricted BBCTLR only when broadcast is selected',async()=>{
  const {t,mem}=h7Configuration(0x80006f9);try{
    await t.configure({branchBroadcast:true});assert.equal(mem.get(0xe004103c),0);
    assert.equal(t.saved.get(0xe004103c).before,0x123);
  }finally{t.bus.close();}
});

test('PLL P and R distinguish CPU, HCLK and physical trace clocks',()=>{
  const registers={cr:(1<<17)|(1<<25),cfgr:3|(3<<3),domain:8,pllSource:2|(5<<4),pllConfig:(1<<16)|(1<<18),pllDiv:111|(1<<9)|(2<<16)|(3<<24),pllFrac:0};
  const c=decodeClocks(targetPort('stm32h7b0'),registers,25e6);
  assert.equal(c.coreHz,280e6);assert.equal(c.coreHz/c.busDivider,140e6);assert.equal(c.traceHz/2,70e6);
  assert.throws(()=>decodeClocks(targetPort('stm32h7b0'),registers,26e6),/超出范围/);
});

test('lease restores original owned bits and retains unrelated GPIO changes',async()=>{
  const t=new EtmTarget(),address=0x58021000;
  const mem=new Map([[address,0x12345678],[0x5c001004,0],[0xe000edfc,0]]);
  let halted=false;const writes=[];
  t.probe={isHalted:async()=>halted,halt:async()=>{halted=true;},run:async()=>{halted=false;}};
  t.read=async a=>a===0xe000edf0?1<<17:mem.get(a)||0;
  t.write=async(a,v)=>{mem.set(a,v>>>0);writes.push(a);};
  await t.set(0x5c001004,0x700000,0x700000);await t.set(0xe000edfc,1<<24,1<<24);
  await t.set(address,0x3ff0,0x2aa0);await t.set(address,0x3ff0,0x1550);
  const outside=mem.get(address)^0x80000000;mem.set(address,outside>>>0);
  await t.restore();assert.equal(mem.get(address),((0x12345678&0x3ff0)|(outside&~0x3ff0))>>>0);
  assert.equal(mem.get(0x5c001004),0);assert.equal(mem.get(0xe000edfc),0);assert.equal(halted,false);
  assert.equal(t.saved.size,0);assert.ok(writes.every(a=>a<0x58024400||a>=0x58024500));t.bus.close();
});

test('FIFO flush precedes CTL disable and downstream remains open while draining',async()=>{
  const t=new EtmTarget(),writes=[];let enabled=1;
  t.read=async a=>a===0x5c014020?enabled:0;
  t.managed=async(a,v)=>{writes.push([a,v]);if(a===0x5c014020)enabled=v;};
  t.wait=async()=>{};
  await t.stopFifo();
  assert.deepEqual(writes.map(([a])=>a),[0x5c015304,0x5c014304,0x5c014304,0x5c014020]);
  assert.equal(writes[2][1]&0x40,0x40);assert.equal(enabled,0);t.bus.close();
});

function breakpointTarget({stopAt=0x08002002,full=false,timeout=false}={}){
  const t=new EtmTarget(),mem=new Map([[0xe0002000,0x61],[0xe0002008,0x40001001],[0xe000200c,0x1234]]);let halted=true,pc=0x08001000;
  t.read=async a=>mem.get(a)||0;t.write=async(a,v)=>{mem.set(a,v>>>0);};t.halt=async()=>{halted=true;};t.pc=async()=>pc;
  t.stopTrace=async()=>t.halt();t.inspect=async()=>({halted});t.probe={isHalted:async()=>halted,run:async()=>{halted=!timeout;pc=stopAt;}};
  if(full)for(let n=0;n<6;n++)mem.set(0xe0002008+4*n,1);
  return {t,mem};
}
test('one-shot entry preserves foreign FPB slots and stops before the instruction',async()=>{
  const {t,mem}=breakpointTarget();try{
    await t.runTo(0x08002003);assert.equal(await t.pc(),0x08002002);
    assert.equal(mem.get(0xe0002008),0x40001001);assert.equal(mem.get(0xe000200c),0x1234);
    assert.equal(t.entryBreak,null);assert.ok((await t.inspect()).halted);
  }finally{t.bus.close();}
});
test('wrong halt address and timeout clean the temporary slot',async()=>{
  for(const config of [{stopAt:0x08003000},{timeout:true}]){const {t,mem}=breakpointTarget(config);try{
    await assert.rejects(t.runTo(0x08002002,{timeout:0}));assert.equal(mem.get(0xe000200c),0x1234);assert.equal(t.entryBreak,null);
  }finally{t.bus.close();}}
});
test('no free comparator is an explicit error without clearing existing breakpoints',async()=>{
  const {t,mem}=breakpointTarget({full:true});try{
    await assert.rejects(t.runTo(0x08002002),/没有空闲/);for(let n=0;n<6;n++)assert.equal(mem.get(0xe0002008+4*n),1);
  }finally{t.bus.close();}
});
test('cancelled entry removes its comparator and keeps target halted',async()=>{
  const {t,mem}=breakpointTarget({timeout:true});t.probe.run=async()=>{t.cancelEntry=true;};
  // Keep the mock running until cancellation is processed by the polling loop.
  let halted=false;t.probe.isHalted=async()=>halted;t.halt=async()=>{halted=true;};t.stopTrace=async()=>{halted=false;};
  try{await assert.rejects(t.runTo(0x08002002),/已取消/);assert.equal(t.entryBreak,null);assert.equal(mem.get(0xe000200c),0x1234);assert.equal(halted,true);}finally{t.bus.close();}
});
test('disabled FPB with retained foreign comparators is left untouched',async()=>{
  const {t,mem}=breakpointTarget();mem.set(0xe0002000,0x60);
  try{await assert.rejects(t.runTo(0x08002002),/保留了其他/);assert.equal(mem.get(0xe0002000),0x60);assert.equal(mem.get(0xe0002008),0x40001001);}finally{t.bus.close();}
});

test('H743 adapter distinguishes CPU 60, HCLK 60 and physical TRACECLK 30',()=>{
  // 8b3af22 起 H743 由"实验适配"升为"已适配"（真机验收通过）——旧断言 traceValidated=false 已过时。
  const port=targetPort('stm32h743');assert.equal(port.traceImplemented,true);assert.equal(port.traceValidated,true);
  const c=completeClocks(port,{cr:(1<<17)|(1<<25),cfgr:27,domain:0,pllSource:2|(5<<4),pllConfig:(1<<16)|(1<<18),pllDiv:95|(7<<9)|(7<<24),pllFrac:0},25e6);
  assert.equal(c.coreHz,60e6);assert.equal(c.hclkHz,60e6);assert.equal(c.traceHz,60e6);assert.equal(c.physicalTraceHz,30e6);
});
