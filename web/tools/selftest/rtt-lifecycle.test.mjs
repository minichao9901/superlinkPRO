import assert from 'node:assert/strict';
import {RttView} from '../../app/rtt/view.js';
const gate=()=>{let resolve,reject;const promise=new Promise((r,j)=>{resolve=r;reject=j;});return {promise,resolve,reject};};
const tick=()=>new Promise(r=>setImmediate(r));
const saved=globalThis.setTimeout;
try{
 for(const error of [false,true]){
  const old=gate(),fresh=gate(),v=Object.create(RttView.prototype),seen=[];
  Object.assign(v,{probe:{fast:false},bridge:null,stats:{},interval:1000,_ingest:b=>seen.push(b[0]),rtt:{readUp:()=>old.promise}});
  v._startPoll();v.running=false;v.rtt={readUp:()=>fresh.promise};v._startPoll();
  if(error)old.reject(new Error('old disconnected'));else old.resolve({bytes:Uint8Array.of(77),lost:0,high:false,level:0,corrupt:false});await tick();
  assert.deepEqual(seen,[]);assert.equal(v.running,true);
  fresh.resolve({bytes:Uint8Array.of(88),lost:0,high:false,level:0,corrupt:false});await tick();assert.deepEqual(seen,[88]);v.running=false;await v._pollTask;
 }
}finally{globalThis.setTimeout=saved;}
console.log('rtt-lifecycle: stale successful/failed polls cannot publish, stop replacement or schedule old workers PASS');
{
 const poll=gate(),v=Object.create(RttView.prototype),events=[];
 Object.assign(v,{probe:{rttPollLimit:8192},stats:{rate:0},interval:0,
  rtt:{readUp:(_channel,limit)=>{assert.equal(limit,8192);return poll.promise;}},
  _setSuppressed(on){this.suppressed=on;events.push(['display',on]);},
  _ingest(bytes){events.push(['receive',bytes.length]);this.running=false;}});
 v._startPoll();
 poll.resolve({bytes:new Uint8Array(8192),lost:0,high:true,level:1,corrupt:false});
 await v._pollTask;
 assert.deepEqual(events,[['display',true],['receive',8192]]);
 v._highspeedGate(0);
 assert.equal(v.suppressed,true,'a slow stats sample cannot re-enable rendering while the ring is still full');
 v._ringBackpressure=false;
 v._highspeedGate(0);
 assert.equal(v.suppressed,false);
}
console.log('rtt-lifecycle: a full Wasm ring pauses rendering before ingest and resumes after the backlog clears PASS');
{
 const old=(()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};})(),events=[];
 const v=Object.create(RttView.prototype);v.probe={reset:async()=>{events.push('reset');return 'mock';}};
 v.running=true;v._pollTask=old.promise;globalThis.document={getElementById:()=>null};
 const savedTimer=globalThis.setTimeout;let scheduled;globalThis.setTimeout=f=>{scheduled=f;return 1;};
 try{
  const reset=v.resetTarget();await tick();assert.equal(v.running,false);assert.deepEqual(events,[]);
  old.resolve();await reset;assert.deepEqual(events,['reset']);v._sessionGen++;v._startRtt=()=>assert.fail('late reset timer');scheduled();
 }finally{globalThis.setTimeout=savedTimer;}
}
console.log('rtt-lifecycle: target reset drains reader and cannot rescan a replacement session PASS');
{
 const v=Object.create(RttView.prototype);let closes=0;
 Object.assign(v,{_sessionGen:0,_connectProbe:async function(){await this.disconnect();this.lastError='original connection failure';},disconnect:async function(){closes++;this._sessionGen++;this.probe=null;this.bridge=null;}});
 await v.connectProbe();assert.equal(closes,1);assert.equal(v.lastError,'original connection failure','outer finalizer cannot erase original failure');
}
console.log('rtt-lifecycle: failed connect retains its original error without duplicate cleanup PASS');
{
 const v=Object.create(RttView.prototype),messages=[];
 const savedDoc=globalThis.document,savedTimer=globalThis.setTimeout;
 globalThis.document={getElementById:id=>id==='toasts'?{appendChild:el=>messages.push(el)}:{classList:{remove(){},add(){}}},createElement:()=>({})};
 globalThis.setTimeout=()=>0;
 Object.assign(v,{rec:{needsClose:true,stop:async()=>({name:'failed.txt',bytes:42,error:new Error('disk full')})},_recordBtn(){},_uiConnected(){}});
 try{await v._disconnectNow();assert.equal(messages.length,1);assert.equal(messages[0].className,'toast t-err');assert.match(messages[0].textContent,/disk full/);}
 finally{globalThis.document=savedDoc;globalThis.setTimeout=savedTimer;}
}
console.log('rtt-lifecycle: disconnect reports recording failure instead of saved success PASS');
