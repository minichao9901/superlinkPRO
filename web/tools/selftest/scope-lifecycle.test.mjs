import assert from 'node:assert/strict';
import {ScopeView} from '../../app/scope/view.js';
import {buildData, buildDef, PACKET, PacketStream, SeqTracker} from '../../app/scope/protocol.js';
const gate=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
const tick=()=>new Promise(r=>setImmediate(r));
globalThis.document={getElementById:id=>({value:id==='sc-period'?'100':'0'})};
for(const fail of [false,true]){
 const cfg=gate(),configured=gate(),v=Object.create(ScopeView.prototype),events=[];let calls=0;
 Object.assign(v,{selected:[{addr:0x20000000,scalar:'u32',name:'x',size:4}],usingMock:true,hid:{},running:false,transport:{running:false,start:async function(){calls++;this.running=true;},stop:async function(){events.push('USB-stop');this.running=false;}},renderer:{setStore(){},setTrigger(){},clearMarks(){}},periodUs:()=>100,seconds:()=>1,isReal:()=>false,updatePlan:()=>({}),applyTrigger(){},configureScope:()=>{configured.resolve();return cfg.promise;},_startWatchdog(){},_stopWatchdog(){},_absorbeStatusBackend(){},benchFresh:()=>false,setStatusText(){},syncButtons(){},hidXfer:async(cmd,data)=>{events.push(data[0]===0?'HID-stop':'HID-start');return Uint8Array.of(0,0,0);}});
 const start=v.start();await v.start();await configured.promise;const stop=v.stop('cancel');await tick();cfg.resolve(fail?null:Uint8Array.of(0,0,0));await Promise.all([start,stop]);
 assert.equal(calls,0);assert.equal(v.running,false);assert.equal(v._capturing,false);assert.deepEqual(events,['HID-stop','USB-stop']);assert.equal(v._starting,false);
}
{
 const status=gate(),reached=gate(),v=Object.create(ScopeView.prototype);
 Object.assign(v,{selected:[{addr:0x20000000,scalar:'u32',name:'x',size:4}],usingMock:true,hid:{},running:false,transport:{running:false,start:async function(){this.running=true;},stop:async function(){this.running=false;}},renderer:{setStore(){},setTrigger(){},clearMarks(){}},periodUs:()=>100,seconds:()=>1,isReal:()=>false,updatePlan:()=>({}),applyTrigger(){},configureScope:async()=>Uint8Array.of(0,0,0),_startWatchdog(){},_stopWatchdog(){},_absorbeStatusBackend(){},benchFresh:()=>false,setStatusText(){},syncButtons(){},hidXfer:async(cmd,data)=>{if(data[0]===2){reached.resolve();return status.promise;}return Uint8Array.of(0,0,0);}});
 const start=v.start();await reached.promise;const stop=v.stop();status.resolve(Uint8Array.of(0,0,0));await Promise.all([start,stop]);
 assert.equal(v.running,false);assert.equal(v.transport.running,false);assert.equal(v._capturing,false);
}
console.log('scope-lifecycle: double start, cancellation during CONFIG/STATUS, STOP before USB drain PASS');
{
 const v=Object.create(ScopeView.prototype);let drained=false;
 Object.assign(v,{hid:{},transport:{stop:async()=>{drained=true;}},hidXfer:async()=>{throw new Error('HID unavailable');}});
 await assert.rejects(v._stopData(),/无法确认采样已停止/);assert.equal(drained,true,'USB still drained when control stop fails');
}
{
 const v=Object.create(ScopeView.prototype);let stopped=0;
 Object.assign(v,{selected:[],usingMock:true,mockVars:()=>[],syncButtons(){},setStatusText(){},_stopWatchdog(){},_stopData:async()=>{stopped++;}});
 await v.start();assert.equal(stopped,0,'invalid start cannot stop someone else\'s engine');
}
console.log('scope-lifecycle: STOP errors propagate while USB drains; rejected input does not send STOP PASS');
{
 const v=Object.create(ScopeView.prototype),events=[];let fail=true;
 const hid={close:async()=>{if(fail)throw new Error('HID close failed');}};
 Object.assign(v,{hid,stop:async()=>{},syncButtons(){},setStatusText(){},probeManager:{cancel(){},fail:(owner,e)=>events.push([owner,e.message]),forget:owner=>events.push(['forget',owner])}});
 await assert.rejects(v.releaseProbe(),/HID close failed/);assert.equal(v.hid,hid);
 assert.deepEqual(events,[['scope','HID close failed']]);
 fail=false;await v.releaseProbe();assert.equal(v.hid,null);assert.deepEqual(events.at(-1),['forget','scope']);
}
console.log('scope-lifecycle: HID close failure retains handle and ownership until successful retry PASS');
{
 const v=Object.create(ScopeView.prototype),events=[];
 Object.assign(v,{usingMock:false,running:true,hid:{connected:true,close:async()=>events.push('HID close')},
  transport:{running:true,close:async()=>events.push('USB close')},
  stop:async reason=>events.push(`stop:${reason}`),syncButtons(){},
  setStatusText:text=>events.push(`status:${text}`),
  probeManager:{cancel(){},forget:owner=>events.push(`forget:${owner}`)}});
 assert.equal(await v.disconnect(),true);
 assert.deepEqual(events.slice(0,4),['stop:用户关闭探针','USB close','HID close','forget:scope']);
 assert.equal(v.transport,null);assert.equal(v.hid,null);assert.match(events.at(-1),/status:探针连接已关闭/);
}
{
 const v=Object.create(ScopeView.prototype),events=[];
 Object.assign(v,{usingMock:true,setMock:async enabled=>events.push(`mock:${enabled}`),setStatusText:text=>events.push(text)});
 assert.equal(await v.disconnect(),true);assert.deepEqual(events,['mock:false','探针连接已关闭']);
}
console.log('scope-lifecycle: user disconnect stops capture and closes USB/HID; mock disconnect exits mock mode PASS');
{
 const v=Object.create(ScopeView.prototype);
 const vars=[{addr:0x24001200,scalar:'u32',size:4}];
 const def=buildDef({seq:0,periodUs:80,vars,spans:1});
 const data=buildData({seq:1,tUs:80,n:1,payload:Uint8Array.of(1,2,3,4)});
 const old=buildData({seq:99,tUs:7600,n:1,payload:Uint8Array.of(4,3,2,1)});
 const chunk=new Uint8Array(PACKET*3);chunk.set(old);chunk.set(def,PACKET);chunk.set(data,PACKET*2);
 Object.assign(v,{captureRaw:true,raw:[],rawBytes:0,_awaitDef:true,packets:0,lost:0,
  stream:new PacketStream(),seqT:new SeqTracker(),setBackend(){},updatePlan(){}});
 v.onChunk(chunk);
 assert.equal(v.stalePackets,1);
 assert.equal(v.rawBytes,PACKET*2);
 assert.deepEqual(v.raw,[def,data]);
}
console.log('scope-lifecycle: JSP export begins at the new DEF after residual DATA PASS');
