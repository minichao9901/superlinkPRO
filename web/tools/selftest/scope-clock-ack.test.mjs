import test from 'node:test';import assert from 'node:assert/strict';
import {ScopeView} from '../../app/scope/view.js';
test('legacy pending CLOCK is accepted only with exact frequency readback',async()=>{
 const previous=globalThis.document;globalThis.document={getElementById:()=>({value:'60000'})};
 const reply=new Uint8Array(51);reply[2]=156;new DataView(reply.buffer).setUint32(7,60000000,true);
 const view={usingMock:false,targetRiscv:false,_probeInfo:{model:'akaLinkPro'},signed:n=>n>127?n-256:n,hidXfer:async()=>reply};
 try{
  await ScopeView.prototype.applyCaptureClock.call(view);
  new DataView(reply.buffer).setUint32(7,45000000,true);
  await assert.rejects(ScopeView.prototype.applyCaptureClock.call(view),/未获确认/);
  reply[2]=250;await assert.rejects(ScopeView.prototype.applyCaptureClock.call(view),/未获确认/);
 }finally{globalThis.document=previous;}
});
test('legacy pending STOP needs a full stopped-status confirmation',async()=>{
 const reply=new Uint8Array(51);reply[2]=156;const events=[];
 const view={hid:{},transport:{quiesce:async()=>events.push('quiesce'),stop:async()=>events.push('stop')},signed:n=>n>127?n-256:n,hidXfer:async()=>reply};
 await ScopeView.prototype._stopData.call(view);assert.deepEqual(events,['quiesce','stop']);assert.equal(view._stopUnconfirmed,false);
 reply[3]=1;await assert.rejects(ScopeView.prototype._stopData.call(view),/无法确认采样已停止/);assert.equal(view._stopUnconfirmed,true);
});
