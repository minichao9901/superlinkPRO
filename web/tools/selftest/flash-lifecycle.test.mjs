import assert from 'node:assert/strict';
import { FlashView } from '../../app/flash/view.js';
import { createProbeManager } from '../../app/core/probe-users.js';
const nodes=new Map(['f-flash','f-idcode','f-path','f-backend','f-bar','f-chip','f-result'].map(id=>[id,{value:id==='f-backend'?'webusb':'',hidden:false}]));
globalThis.document={getElementById:id=>nodes.get(id)||null};
const tick=()=>new Promise(r=>setImmediate(r));
const gate=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
{
 const prep=gate(),write=gate(),close=gate();let calls=0;
 const v=Object.create(FlashView.prototype);Object.assign(v,{busy:false,file:{name:'fw.bin'},_clearProbeUsers:()=>prep.promise,_status(){},_hbStart(){},_hbStop(){},probe:{disconnect:()=>close.promise},_flashWebusb:async()=>{calls++;await write.promise;}});
 const first=v.flash();await v.flash();assert.equal(v.busy,true);assert.equal(nodes.get('f-flash').disabled,true);
 prep.resolve(true);await tick();assert.equal(calls,1);write.resolve();await tick();await v.flash();assert.equal(calls,1,'closing still owns operation');
 close.resolve();await first;assert.equal(v.busy,false);assert.equal(v.probe,null);assert.equal(nodes.get('f-flash').disabled,false);
}
for(const fail of [false,true]){
 const v=Object.create(FlashView.prototype);Object.assign(v,{busy:false,file:{name:'fw.bin'},_clearProbeUsers:async()=>{if(fail)throw new Error('prep');return false;}});
 if(fail)await assert.rejects(v.flash(),/prep/);else await v.flash();
 assert.equal(v.busy,false);assert.equal(nodes.get('f-flash').disabled,false);
}
console.log('flash-lifecycle: preparation, duplicate click, disconnect ownership, cancellation and errors PASS');

// A failed native close must block target handoff and be retried before another operation.
for (const entry of ['flash', 'readIdcode']){
 const v=Object.create(FlashView.prototype),events=[];
 let failClose=true;
 const handle={disconnect:async()=>{events.push('close');if(failClose)throw new Error('USB close failed');}};
 Object.assign(v,{busy:false,file:{name:'fw.bin'},_clearProbeUsers:async()=>true,
  _status(){},_log(){},_hbStart(){},_hbStop(){},
  _flashWebusb:async()=>{events.push('flash');v.probe=handle;},
  _idcodeArm:async()=>{events.push('read');v.probe=handle;},
 });
 const m=createProbeManager({flash:v},{locks:null});v.probeManager=m;
 await assert.rejects(v[entry](),/USB close failed/);
 assert.equal(v.probe,handle);assert.equal(v.busy,false);
 assert.deepEqual(m.summary().owners,['flash']);assert.equal(m.summary().failures[0].owner,'flash');
 await assert.rejects(m.run('dbg',()=>assert.fail('target must remain reserved')),/释放尚未确认/);
 let independent=false;await m.run('i2c',async()=>{independent=true;});assert.ok(independent);
 await assert.rejects(v[entry](),/USB close failed/);
 assert.deepEqual(events,[entry==='flash'?'flash':'read','close','close'],'failed recovery does not reopen or write');
 failClose=false;await v[entry]();
 assert.deepEqual(events.slice(-3),['close',entry==='flash'?'flash':'read','close']);
 assert.equal(v.probe,null);assert.deepEqual(m.summary().failures,[]);
 await m.run('dbg',async()=>events.push('debug'));
 assert.equal(events.at(-1),'debug');
}

// Preserve both the operation failure and the failed cleanup for diagnosis.
{
 const operationError=new Error('write failed'),closeError=new Error('close failed');
 const v=Object.create(FlashView.prototype);
 v.probe={disconnect:async()=>{throw closeError;}};
 await assert.rejects(v._closeProbe(operationError),e=>e instanceof AggregateError &&
  e.errors[0]===operationError && e.errors[1]===closeError);
 assert.ok(v.probe);assert.equal(v._probeCloseFailed,true);
}
console.log('flash-lifecycle: flash/IDCODE retain failed close ownership, recover before reopening, preserve both errors PASS');
