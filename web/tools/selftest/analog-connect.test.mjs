import assert from 'node:assert/strict';
import {AnalogSession} from '../../app/analog/session.js';
import {AkaLinkHid} from '../../app/hid/probe.js';
import {ProbeManager} from '../../app/core/probe-manager.js';
const saved={request:AkaLinkHid.prototype.request,xfer:AkaLinkHid.prototype.xfer,close:AkaLinkHid.prototype.close};
let gate=null,closes=0,closeFailure=false;
const reply=(a,b=[],rc=0)=>Uint8Array.of(8+b.length,0x38,a,rc,0,0,0,...b);
AkaLinkHid.prototype.request=async function(){if(gate)await gate;this.device={opened:true};};
AkaLinkHid.prototype.close=async function(){closes++;if(closeFailure)throw Error('native HID close failed');this.device=null;};
AkaLinkHid.prototype.xfer=async function(cmd,b){
 if(b[0]===9)return reply(9,[],4); // DAC-only future probe, no ADC DMA.
 const data=new Uint8Array(16),v=new DataView(data.buffer);data.set([68,65,67,49,1,1,12,1]);v.setUint32(8,100000,true);v.setUint16(12,1024,true);v.setUint16(14,3300,true);return reply(1,data);
};
try{
 const s=new AnalogSession();const c=await s.connect();assert.equal(c.supported,false);assert.ok(s.connected&&s.dac.caps.supported);assert.equal(s.caps,null);
 await assert.rejects(s.acquire({bits:16,rate:1000}),/未提供 ADC/);await s.disconnect();assert.equal(s.connected,false);
 const cancelled=new AnalogSession(),manager=new ProbeManager({locks:null});cancelled.probeManager=manager;
 manager.register('analog',{resources:['analog-engine'],active:()=>cancelled.connected,release:()=>cancelled.disconnect()});
 let release;gate=new Promise(resolve=>release=resolve);const connection=cancelled.connect();await new Promise(resolve=>setImmediate(resolve));manager.cancel('analog');release();
 assert.equal(await connection,false);assert.equal(cancelled.connected,false);assert.equal(cancelled.hid,null);gate=null;
 // Failed native close keeps a reachable handle and manager fault for a retry.
 const dirty=new AnalogSession();dirty.probeManager=manager;closeFailure=true;
 AkaLinkHid.prototype.xfer=async()=>{throw Error('caps failed');};
 await assert.rejects(dirty.connect(),/HID 清理未确认/);assert.ok(dirty.hid&&dirty._connectCleanupError);assert.ok(manager.failures.has('analog'));
 closeFailure=false;await dirty.disconnect();assert.equal(dirty.hid,null);assert.equal(manager.failures.has('analog'),false);
 assert.ok(closes>=3);
 console.log('Analog connect: independent DAC capability, manager cancellation has no late session, failed HID close retains handle/ownership for retry PASS');
}finally{Object.assign(AkaLinkHid.prototype,saved);}
