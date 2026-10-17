import {test} from 'node:test';import assert from 'node:assert/strict';
import {EtmTarget} from '../../web/app/target.js';
import {embeddedHost,runEmbedded} from '../../web/app/embedding.js';
test('standalone has no host; embedded page requires its same-origin owner',()=>{
 const w={};w.parent=w;assert.equal(embeddedHost(w),null);
 const host={};assert.equal(embeddedHost({document:{body:{dataset:{embedded:'1'}}},parent:{__tools:{etm:host}}}),host);
 assert.throws(()=>embeddedHost({document:{body:{dataset:{embedded:'1'}}},parent:{}}),/必须同源/);
});
test('embedded target reuses parent USB driver and has no nested BroadcastChannel',async()=>{
 const probeType={};const t=new EtmTarget(()=>{},{host:{probeType}});
 assert.equal(t.Probe,probeType);assert.equal(t.bus._channel,undefined);await t.bus.requestRelease();t.bus.close();
 let acquired=0,ran=0;assert.equal(await runEmbedded({run:fn=>{acquired++;return fn();}},async()=>{ran++;return 42;}),42);
 assert.equal(acquired,1);assert.equal(ran,1);
});
