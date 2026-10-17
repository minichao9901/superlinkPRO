import test from 'node:test';
import assert from 'node:assert/strict';
import {WebUsbDapProbe} from '../../web/app/shared/rtt/dap-webusb.js';
import {EtmTarget} from '../../web/app/target.js';
import {withCleanup} from '../../web/app/operation.js';

test('keyed/self-clearing control write is sent once without generic readback or healing',async()=>{
  const p=new WebUsbDapProbe(),writes=[];
  p._writeMemOnce=async(a,b)=>writes.push([a,[...b]]);
  p._readMemLocked=async()=>{throw Error('不能回读控制写');};p._healIfFaulted=async()=>{throw Error('不能自愈');};
  await p.writeMemDiagnostic(0xe000ed0c,new Uint8Array([4,0,0xfa,5]));assert.equal(writes.length,1);
  p._faulted=true;await assert.rejects(p.writeMemDiagnostic(0xe000ed0c,new Uint8Array(4)),/FAULT/);assert.equal(writes.length,1);
});
test('controller uses single-write transport for AIRCR and preserves failing memory address',async()=>{
  const t=new EtmTarget(),writes=[];
  t.probe={writeMem:async()=>{throw Error('不能使用内存回读重写');},writeMemDiagnostic:async(a,b)=>writes.push([a,[...b]]),readMemDiagnostic:async()=>{const e=Error('SWD FAULT');e.ack=4;throw e;}};
  try{await t.write(0xe000ed0c,0x05fa0004,false);assert.equal(writes.length,1);
    await assert.rejects(t.read(0x5c014304),/读寄存器 0x5c014304.*SWD FAULT/);assert.ok(t.transportFault);
    await assert.rejects(t.write(0xe000ed0c,0x05fa0004,false),/已故障/);assert.equal(writes.length,1);
  }finally{t.bus.close();}
});
test('cleanup failure never replaces the primary operation failure',async()=>{
  const primary=Error('复位写 AIRCR 失败'),messages=[];
  await assert.rejects(withCleanup(async()=>{throw primary;},async()=>{throw Error('后续诊断 FAULT');},e=>messages.push(e.message)),e=>e===primary);
  assert.deepEqual(primary.cleanupErrors,['后续诊断 FAULT']);assert.deepEqual(messages,['后续诊断 FAULT']);
  await assert.rejects(withCleanup(async()=>42,async()=>{throw Error('清理失败');}),/清理失败/);
});
test('business breakpoint is armed before a single reset, without reset-vector catch',async()=>{
  const t=new EtmTarget(),mem=new Map([[0xe0002000,0x60],[0xe000edfc,0x01000001]]),writes=[];let pc=0x08001000;
  t.probe={isHalted:async()=>true};t.halt=async()=>{};t.stopTrace=async()=>{};t.pc=async()=>pc;t.inspect=async()=>({halted:true});
  t.read=async a=>a===0xe000edf0?1<<17:mem.get(a)||0;
  t.write=async(a,v)=>{writes.push([a,v]);mem.set(a,v>>>0);if(a===0xe000ed0c){assert.ok(mem.get(0xe0002008)&1);assert.equal(mem.get(0xe000edfc)&1,0);pc=0x08002002;}};
  try{const info=await t.reset({address:0x08002002});assert.equal(info.entryPC,pc);assert.equal(writes.filter(([a])=>a===0xe000ed0c).length,1);
    assert.equal(mem.get(0xe0002008),0);assert.equal(mem.get(0xe000edfc),0x01000001);assert.equal(t.entryBreak,null);
  }finally{t.bus.close();}
});
test('transport recovery does not resume or reset target',async()=>{
  const p=new WebUsbDapProbe();p._faulted=true;let initialized=0;
  p._targetInit=async()=>{initialized++;};p.run=async()=>{throw Error('不能继续 CPU');};p.sysReset=async()=>{throw Error('不能复位');};
  await p.recoverDebugPort();assert.equal(initialized,1);assert.equal(p._faulted,false);assert.equal(p._recovering,false);
});
