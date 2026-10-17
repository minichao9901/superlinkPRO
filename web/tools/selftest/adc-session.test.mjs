import assert from 'node:assert/strict';
import {AnalogSession} from '../../app/analog/session.js';
import {AdcTransport} from '../../app/analog/transport.js';
function reply(action,payload=[],rc=0){return Uint8Array.of(8+payload.length,0x38,action,rc,0,0,0,...payload);}
function caps(){const b=new Uint8Array(20),v=new DataView(b.buffer);b.set([65,68,66,50,0x8b,2,15,6]);v.setUint32(8,2000000,true);v.setUint16(12,4096,true);v.setUint16(14,4096,true);b[17]=1;v.setUint16(18,3300,true);return b;}
function rig({lostOpen=false,continuous=false,stopFailure=false,pipeline=false,sharedBusy=0}={}){
 const s=new AnalogSession(),frames=[],readers=[],actions=[],spi=[];let count=0,owned=false,endSent=false,seq=0,index=0,closed=0;
 let depth=1;
 const push=(done=false)=>{const n=done?0:3,b=new Uint8Array(32+n*2),v=new DataView(b.buffer);b.set([65,68,83,50,2,done?2:1,16,0]);v.setUint32(8,1,true);v.setUint32(12,seq++,true);v.setUint32(16,index,true);v.setUint32(20,1000,true);v.setUint16(24,n,true);b[26]=6;b[27]=2;index+=n;
   const r={status:'ok',data:new DataView(b.buffer)};if(readers.length)readers.shift()(r);else frames.push(r);};
 s.caps={maxRate:2000000};s.transport=new AdcTransport({vendorId:1,productId:2,transferIn(){return frames.length?Promise.resolve(frames.shift()):new Promise(resolve=>readers.push(resolve));}});
  s.hid={connected:true,async xfer(cmd,b){
   if(cmd===0x35){spi.push(b[0]);return Uint8Array.of(8,0x35,b[0],0,0,0,0,0);}
   assert.equal(cmd,0x38);const a=b[0];actions.push(a);
   if(a===9){const c=caps();if(sharedBusy-->0)c[16]=2;return reply(a,c);}
  if(a===15)return pipeline?reply(a,[1,32]):reply(a,[],1);
   if(a===10){owned=true;depth=b.length===11?b[10]:1;count=new DataView(b.buffer,b.byteOffset).getUint32(6,true);seq=0;index=0;endSent=false;if(lostOpen)throw Error('OPEN response lost');return reply(a,[1,0,0,0]);}
  if(a===13){assert.equal(readers.length,depth,'all native readers before timer start');push();if(!continuous&&count){for(let i=0;i<depth;i++)push(true);endSent=true;}return reply(a);}
  if(a===11){if(stopFailure){stopFailure=false;throw Error('STOP response lost');}if(!endSent){for(let i=0;i<depth;i++)push(true);endSent=true;}return reply(a);}
  if(a===12){if(closed++<2)return reply(a,[],2);owned=false;return reply(a);}
  if(a===14){const p=new Uint8Array(24);p[0]=1;p[20]=+owned;return reply(a,p);}
  throw Error(`unexpected ${a}`);
 }};
  return {s,actions,spi,get owned(){return owned;},get readers(){return readers.length;}};
}
let r=rig(),blocks=[];await r.s.acquire({bits:16,rate:1000,count:3},b=>blocks.push(b));
assert.equal(blocks.length,1);assert.equal(r.s.busy,false);assert.equal(r.owned,false);assert.equal(r.readers,0);assert.deepEqual(r.actions.slice(0,4),[9,15,10,13]);
r=rig({lostOpen:true});await assert.rejects(r.s.acquire({bits:16,rate:1000,count:3}),/OPEN response lost/);assert.equal(r.s.busy,false);assert.equal(r.owned,false);assert.ok(r.actions.includes(14));
// An IN retire always pays one short idle timeout before START, so "wait a tick" has to
// outlast RETIRE_IN_TIMEOUT_MS (30 ms) plus slack for the rig's promise chain.
const streamSettled=()=>new Promise(resolve=>setTimeout(resolve,120));
r=rig({continuous:true});const run=r.s.acquire({bits:16,rate:1000,count:0});await streamSettled();await r.s.stopAdc();await run;assert.equal(r.s.busy,false);assert.equal(r.readers,0);
r=rig({continuous:true,stopFailure:true});const active=r.s.acquire({bits:16,rate:1000,count:0});await streamSettled();await assert.rejects(r.s.stopAdc(),/STOP response lost/);assert.equal(r.s.busy,true);await r.s.stopAdc();await active.catch(()=>{});assert.equal(r.s.busy,false);
console.log('ADC session: USB reader before START, finite/continuous stop, lost OPEN recovery, deferred CLOSE, failed STOP ownership/retry PASS');
for(const options of [{},{continuous:true},{lostOpen:true}]){
 r=rig({...options,pipeline:true});
 const run=r.s.acquire({bits:16,rate:1000,count:options.continuous?0:3});
 if(options.continuous){await streamSettled();await r.s.stopAdc();}
 if(options.lostOpen)await assert.rejects(run,/OPEN response lost/);else await run;
 assert.equal(r.readers,0);assert.equal(r.s.busy,false);assert.equal(r.owned,false);
 assert.equal(r.s.transport.inFlight,32);
}
console.log('ADC counted END: 32 native readers, finite/STOP/lost OPEN retire without orphan reads PASS');
// ADC STOP must never unlock a DAC task on this shared session.
const dacBusy=new AnalogSession();dacBusy.dac={owned:true};dacBusy.busy=true;
await assert.rejects(dacBusy.stopAdc(),/DAC/);assert.equal(dacBusy.busy,true);
// Native SPI OUT cleanup is part of ownership even before ADC OPEN.
const beforeOpen=new AnalogSession();let finishOut;
beforeOpen.transport={flush:new Promise(resolve=>finishOut=resolve)};beforeOpen.busy=true;
let cleanupDone=false;const cleanup=beforeOpen._cleanupAdc().then(()=>cleanupDone=true);
await Promise.resolve();assert.equal(cleanupDone,false);finishOut();await cleanup;
console.log('ADC ownership: DAC cannot be unlocked by ADC STOP; native SPI OUT drains before cleanup PASS');
// A busy shared buffer must never be a one-sample hard reject: the ADC page holds the
// spi-bulk lease, so it retires SPI/QSPI explicitly and re-reads CAPS. 2026-10-06 the
// probe reported flags=2 while the bridge was off and no host command could clear it,
// which bricked ADC until the probe was unplugged.
const SPI_ENABLE=1,SPI_ABORT=6;
r=rig({sharedBusy:1});await r.s.acquire({bits:16,rate:1000,count:3});
assert.deepEqual(r.spi,[SPI_ENABLE,SPI_ABORT]);assert.equal(r.s.busy,false);assert.equal(r.owned,false);

console.log('ADC shared buffer: one busy sample retires SPI/QSPI (ENABLE 0 + ABORT) and re-reads CAPS, no hard reject PASS');
r=rig({sharedBusy:99});
await assert.rejects(r.s.acquire({bits:16,rate:1000,count:3}),/SPI\/QSPI 仍占着共享缓冲/);
assert.equal(r.s.busy,false);assert.equal(r.actions.filter(a=>a===9).length,12);
console.log('ADC shared buffer: a buffer that never retires fails loudly and bounded, session stays reusable PASS');

// Retirement is a hardware completion fence; CAPS follows actual state changes.
function handover({out=false,inside=false,owned=false,spiError=false,inError=false}={}){
 const s=new AnalogSession(),events=[];let finishOut,finishIn,closeTries=0;
 const outDone=new Promise(resolve=>finishOut=()=>{out=false;resolve();});
 const inDone=new Promise(resolve=>finishIn=()=>{inside=false;resolve();});
 s.streamCommand=async a=>{
  events.push(a);
  if(a===9){const c=caps();c[16]=(out?1:0)|((inside||owned)?2:0);return c;}
  if(a===14){const b=new Uint8Array(24);b[20]=+owned;return b;}
  if(a===11)return new Uint8Array();
  if(a===12){if(++closeTries<3){const e=Error('busy');e.code=2;throw e;}owned=false;return new Uint8Array();}
  throw Error(`Unexpected command ${a}`);
 };
 s.hid={async xfer(cmd,b){events.push(`spi:${b[0]}`);return Uint8Array.of(8,cmd,b[0],spiError?1:0,0,0,0,0);}};
 s.transport={async retireSpiOut(){events.push('out');await outDone;},async retireSpiIn(){events.push('in');if(inError)throw Error('native IN failed');await inDone;}};
 return {s,events,finishOut,finishIn};
}
let h=handover();await h.s._acquireSharedCaps();assert.deepEqual(h.events,[9],'cold start needs no recovery');
h=handover({out:true});let ready=false;
let fence=h.s._acquireSharedCaps().then(()=>ready=true);
await new Promise(resolve=>setTimeout(resolve,5));assert.equal(ready,false);h.finishOut();await fence;
assert.deepEqual(h.events,[9,'out',9],'OUT completion must be followed by fresh CAPS');
h=handover({inside:true});ready=false;fence=h.s._acquireSharedCaps().then(()=>ready=true);
await new Promise(resolve=>setTimeout(resolve,5));assert.equal(ready,false);h.finishIn();await fence;
assert.deepEqual(h.events,[9,14,'spi:1','spi:6','in',9]);
h=handover({inside:true,owned:true});h.finishIn();await h.s._acquireSharedCaps();
assert.equal(h.events.filter(a=>a===12).length,3);
assert.ok(h.events.indexOf('spi:1')>h.events.lastIndexOf(12),'ADC owner closes before SPI commands');
h=handover({inside:true,spiError:true});await assert.rejects(h.s._acquireSharedCaps(),/SPI 失能未确认/);
assert.equal(h.events.includes('in'),false,'failed SPI command cannot authorize takeover');
h=handover({inside:true,inError:true});await assert.rejects(h.s._acquireSharedCaps(),/native IN failed/);
assert.equal(h.events.filter(a=>a===9).length,1,'failed USB retirement is not swallowed');
console.log('ADC handover: cold start, delayed native OUT/IN, fresh CAPS fence, abandoned ADC CLOSE retries, control/USB failures PASS');
