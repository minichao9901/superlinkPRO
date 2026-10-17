import test from 'node:test';
import assert from 'node:assert/strict';
import {EtmTarget} from '../../web/app/target.js';

function target(){
  const t=new EtmTarget(),writes=[],mem=new Map([[0xe000ed00,0x411fc231],[0xe0042000,0x414],
    [0xe00411e4,0x4114f241],[0xe00411e8,0x18800],[0xe0041000,0x411],[0xe0041010,2],
    [0xe0040000,0xb],[0xe0040004,1],[0xe00400f0,1],[0xe0040304,0x102],
    [0x40011800,0x44444444],[0x40021000,(1<<17)|(1<<25)],
    [0x40021004,(2<<2)|(1<<16)|(4<<18)],[0xe0002000,0x60]]);
  let halted=true;t.hseHz=8e6;
  t.read=async a=>{if(a>=0x5c000000&&a<0x5c020000)throw Error('H7 address');return a===0xe000edf0?halted?1<<17:0:mem.get(a)||0;};
  t.write=async(a,v)=>{mem.set(a,v>>>0);writes.push([a,v>>>0]);if(a===0xe0041000)mem.set(0xe0041010,v&0x400?2:0);};
  t.probe={isHalted:async()=>halted,halt:async()=>{halted=true;},run:async()=>{halted=false;}};
  return {t,writes,mem};
}

test('F103ZE reads v3 capability and 48/24 clocks; unsupported ETM ID fails',async()=>{
  const {t,mem}=target();try{const info=await t.inspect();assert.equal(info.profile,'stm32f103ze');assert.equal(info.config.protocol,'ETMv3');
    assert.equal(info.clocks.coreHz,48e6);assert.equal(info.clocks.physicalTraceHz,24e6);assert.equal(info.flashLimit,0x08080000);
    mem.set(0xe00411e4,0);await assert.rejects(t.inspect(),/未识别到/);
  }finally{t.bus.close();}
});
test('F103 configure/restore never writes H7 or switches TPIU to SWO and restores GPIO bits',async()=>{
  const {t,writes,mem}=target();try{
    await t.configure();assert.equal(mem.get(0x40011800)&0x0fffff00,0x0bbbbb00);
    assert.equal(mem.get(0xe0040004),8);assert.equal(mem.get(0xe0041200),2);assert.equal(mem.get(0xe0041000),0x810);
    const out=mem.get(0x40011800)^0x80000000;mem.set(0x40011800,out>>>0);
    await t.restore();assert.equal(mem.get(0x40011800),((out&~0x0fffff00)|(0x44444444&0x0fffff00))>>>0);
    assert.equal(mem.get(0xe0041000),0x411);assert.equal(mem.get(0xe00400f0),0);
    assert.ok(writes.every(([a,v])=>a!==0xe00400f0||v===0));assert.ok(writes.every(([a])=>a<0x5c000000||a>=0x5c020000));
  }finally{t.bus.close();}
});
test('F103 reserved return stack/cycle options fail before configuring trace',async()=>{
  const {t,writes}=target();try{await assert.rejects(t.configure({returnStack:true}),/未实现/);
    assert.ok(writes.every(([a])=>a!==0xe0041000));
  }finally{t.bus.close();}
});
test('F103 branch broadcast checkbox maps to bit 8 and restores its original value',async()=>{
  const {t,mem}=target();try{
    await t.configure({branchBroadcast:true});assert.equal(mem.get(0xe0041000),0x910);
    assert.equal(t.info.traceOptions.branchBroadcast,true);
    await t.configure({branchBroadcast:false});assert.equal(mem.get(0xe0041000),0x810);
    await t.restore();assert.equal(mem.get(0xe0041000),0x411);
  }finally{t.bus.close();}
});
test('fresh F103 programming opens the TPIU clock gate before awaiting acknowledgement',async()=>{
  const {t,mem,writes}=target();const originalRead=t.read;
  t.read=async a=>a===0xe0041010?((mem.get(0xe0042004)&0x20)&&(mem.get(0xe0041000)&0x400)?2:0):originalRead(a);
  try{
    await t.configure();
    const gate=writes.findIndex(([a,v])=>a===0xe0042004&&(v&0xe0)===0xe0);
    const programming=writes.findIndex(([a,v])=>a===0xe0041000&&(v&0x400));
    assert.ok(gate>=0&&gate<programming);
    await t.restore();assert.equal(mem.get(0xe0042004)&0xe0,0);
  }finally{t.bus.close();}
});
test('inspection failure restores only the acquired DEMCR lease without programming an unknown ETM',async()=>{
  const {t,mem,writes}=target();try{mem.set(0xe00411e4,0);await assert.rejects(t.inspect(),/未识别到/);
    await t.restore();assert.ok(writes.every(([a])=>a===0xe000edfc));assert.equal(t.saved.size,0);
  }finally{t.bus.close();}
});
