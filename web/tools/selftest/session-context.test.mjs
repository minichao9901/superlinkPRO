import test from 'node:test';
import assert from 'node:assert/strict';
import {SessionContext,sessionContextBusy} from '../../app/core/session-context.js';
const target=(id,arch='arm')=>({id,arch,label:id});
function elf(machine=40,marker=0){const bytes=new Uint8Array(64);bytes.set([127,69,76,70,1,1]);new DataView(bytes.buffer).setUint16(18,machine,true);bytes[63]=marker;return bytes.buffer;}

test('ELF identity uses bytes, and consumers cannot mutate retained data',async()=>{
  const c=new SessionContext();c.setTarget(target('f103'));
  const bytes=elf();const a=await c.publishElf(bytes,'same.elf','flash');
  new Uint8Array(bytes)[63]=1;
  new Uint8Array(c.elfBytes())[63]=2;
  assert.equal(new Uint8Array(c.elfBytes())[63],0);
  const b=await c.publishElf(elf(40,3),'same.elf','dbg');
  assert.notEqual(a.sha256,b.sha256);assert.equal(b.targetId,'f103');
  assert.ok(c.canReuse('arm'));assert.ok(!c.canReuse('riscv'));
  assert.ok(!c.canReuse('arm','h743'));assert.ok(c.canReuse('arm','f103'));
});

test('changing the selected board invalidates bound ELF even within one architecture',async()=>{
  const c=new SessionContext();c.setTarget(target('f103'));
  await c.publishElf(elf(),'a.elf','flash');c.setTarget(target('h743'));
  assert.equal(c.elf,null);assert.equal(c.elfBytes(),null);
  await c.publishElf(elf(),'b.elf','dbg');c.setTarget(target('hpm','riscv'));
  assert.equal(c.elf,null);
});

test('target change during digest cannot revive a stale ELF',async()=>{
  const c=new SessionContext();c.setTarget(target('f103'));
  const pending=c.publishElf(elf(),'old.elf','flash');assert.equal(c.pending,true);
  c.setTarget(target('hpm','riscv'));assert.equal(await pending,null);
  assert.equal(c.elf,null);assert.equal(c.pending,false);
});

test('newest publish wins and a cleared image cannot be restored by an old digest',async()=>{
  const c=new SessionContext();
  const old=c.publishElf(elf(),'old.elf','rtt');
  const latest=c.publishElf(elf(243),'new.elf','flash');
  await Promise.all([old,latest]);assert.equal(c.elf.name,'new.elf');
  const pending=c.publishElf(elf(),'discard.elf','dbg');c.clearElf();
  await pending;assert.equal(c.elf,null);assert.equal(c.pending,false);
});

test('invalid files and unsupported machines do not become reusable ELF',async()=>{
  const c=new SessionContext();await assert.rejects(c.publishElf(new ArrayBuffer(8),'bad.elf','flash'),/有效 ELF/);
  await assert.rejects(c.publishElf(elf(62),'host.elf','flash'),/ARM/);
  assert.equal(c.elf,null);assert.equal(c.pending,false);
});

test('an unbound source ELF does not inherit a board identity from another page',async()=>{
  const c=new SessionContext();c.setTarget(target('f103'));
  await c.publishElf(elf(),'unknown.elf','scope',target('arm'));
  assert.equal(c.elf.targetId,null);assert.equal(c.target.id,'f103');
});

test('open handles, pending connection/cleanup and recovery failures all block reuse',()=>{
  assert.equal(sessionContextBusy({}),false);
  for(const path of [['hid','dev','connected'],['scope','transport'],['scope','_hidConnectPromise'],
    ['scope','_usbConnectPromise'],['rtt','_disconnectPromise'],['dbg','session','connected'],['etm','busy']]){
    const tools={};let node=tools;
    for(const key of path.slice(0,-1))node=node[key]={};
    node[path.at(-1)]=true;assert.equal(sessionContextBusy(tools),true,path.join('.'));
  }
  assert.equal(sessionContextBusy({probeManager:{summary:()=>({owners:[],pending:[],failures:[{}]})}}),true);
});
