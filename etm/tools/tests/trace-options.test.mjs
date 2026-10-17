import test from 'node:test';
import assert from 'node:assert/strict';
import {traceSettings,traceCapabilities,traceSettingsText} from '../../web/app/trace-options.js';

test('trace options use the actual v3/v4 register layout and report missing snapshots',()=>{
  assert.equal(traceSettings(null),null);
  assert.equal(traceSettings({protocol:'ETMv3'}),null);
  assert.equal(traceSettings({idr0:0x80006e1}),null);
  assert.deepEqual(traceSettings({protocol:'ETMv3',ctrl:0x910}),{branchBroadcast:true,returnStack:false,cycleCount:false});
  assert.equal(traceSettings({ctrl:0x810}).branchBroadcast,false);
  assert.deepEqual(traceSettings({config:0x1019}),{branchBroadcast:true,returnStack:true,cycleCount:true});
  assert.match(traceSettingsText({ctrl:0x810}),/分支广播 关/);
  assert.match(traceSettingsText(null),/未记录/);
});

test('unsupported chip capabilities stay disabled and H7 capabilities come from IDR0',()=>{
  assert.deepEqual(traceCapabilities('stm32f103ze',{}),{branchBroadcast:true,returnStack:false,cycleCount:false});
  assert.deepEqual(traceCapabilities('stm32f1',{}),{branchBroadcast:false,returnStack:false,cycleCount:false});
  assert.deepEqual(traceCapabilities('stm32h7b0',{idr0:0}),{branchBroadcast:false,returnStack:false,cycleCount:false});
  assert.deepEqual(traceCapabilities('stm32h7b0',{idr0:0x80006e1}),{branchBroadcast:true,returnStack:true,cycleCount:true});
  assert.deepEqual(traceCapabilities('stm32h743',{idr0:0x80006e1}),{branchBroadcast:true,returnStack:true,cycleCount:true});
});
