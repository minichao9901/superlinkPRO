import test from 'node:test';
import assert from 'node:assert/strict';
import {navigatorUsb} from '../node-transport/usb-adapter.mjs';
import {finishTargetCommand} from '../node-transport/handoff.mjs';
test('USB adapter forwards transfers unchanged and removes its hotplug callbacks on shutdown',async()=>{
  const seen=[],callback=()=>{},device={serialNumber:'exact'};
  const native={getDevices:async()=>[device],requestDevice:async o=>{seen.push(o);return device;},
    addEventListener:(...a)=>seen.push(['add',...a]),removeEventListener:(...a)=>seen.push(['remove',...a]),nativeEmitterQueue:Promise.resolve()};
  const usb=navigatorUsb(native);assert.equal((await usb.getDevices())[0],device);
  const options={filters:[{vendorId:0x0d28}]};assert.equal(await usb.requestDevice(options),device);assert.equal(seen[0],options);
  usb.addEventListener('disconnect',callback);await usb.dispose();
  assert.deepEqual(seen.at(-1),['remove','disconnect',callback]);
});
test('an explicitly removed callback is not removed twice at disposal',async()=>{
  const removed=[],callback=()=>{};
  const usb=navigatorUsb({addEventListener(){},removeEventListener:(t,c)=>removed.push([t,c]),nativeEmitterQueue:Promise.resolve()});
  usb.addEventListener('disconnect',callback);usb.removeEventListener('disconnect',callback);await usb.dispose();assert.equal(removed.length,1);
});
test('debugger handback cannot restore a manual Trace configuration behind the CLI state file',async()=>{
  const calls=[],target={saved:new Map([[1,{}]]),disconnect:async()=>calls.push('disconnect')};
  const dbg={connect:async()=>calls.push('connect')},context={target,dbg,hasSuspended:()=>true};
  await finishTargetCommand({action:'target',op:'close',restore:false},context);assert.deepEqual(calls,[]);
  target.saved.clear();await finishTargetCommand({action:'target',op:'close',restore:false},context);
  assert.deepEqual(calls,['disconnect','connect']);
});
