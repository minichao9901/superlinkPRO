import test from 'node:test';
import assert from 'node:assert/strict';
import {streamClockHz} from '../../app/core/stream-clock.js';
import {ScopeView} from '../../app/scope/view.js';

test('only identified Zynq RISC-V streams use Hz; legacy probes keep zero idle override',()=>{
  assert.equal(streamClockHz({model:'ZynqLinkPro'},true,25000000),25000000);
  assert.equal(streamClockHz({model:'ZynqLinkPro'},true,60000000),25000000);
  assert.equal(streamClockHz({model:'ZynqLinkPro'},true,0),10000000);
  assert.equal(streamClockHz({model:'akaLinkPro'},true,25000000),0);
  assert.equal(streamClockHz(null,true,25000000),0);
  assert.equal(streamClockHz(null,false,60000000),60000000);
});
test('native RISC-V capture configures 25MHz through shared stream command and requires ACK',async()=>{
  const before=globalThis.document,calls=[];
  globalThis.document={getElementById:()=>({value:'25000'})};
  const scope={targetRiscv:true,_probeInfo:{model:'ZynqLinkPro'},signed:v=>v>127?v-256:v,
    hidXfer:async(cmd,data)=>{calls.push([cmd,data]);return Uint8Array.of(0x31,7,0);}};
  try{
    await ScopeView.prototype.applyCaptureClock.call(scope);
    assert.equal(calls[0][0],0x31);assert.equal(calls[0][1][0],7);
    assert.equal(new DataView(calls[0][1].buffer).getUint32(1,true),25000000);
    scope.hidXfer=async()=>Uint8Array.of(0x31,7,249);
    await assert.rejects(ScopeView.prototype.applyCaptureClock.call(scope),/时钟配置/);
    scope._probeInfo=null;await ScopeView.prototype.applyCaptureClock.call(scope);
  }finally{globalThis.document=before;}
});
