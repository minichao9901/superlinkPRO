import test from 'node:test';
import assert from 'node:assert/strict';
import {offlineSettings} from '../../../etm/web/app/offline-config.js';

const v3={protocol:'ETMv3',idr:0x4114f241,ctrl:0x810,ccer:0x18800,traceid:7};
const v4={idr0:0x080006e1,idr1:0x4100f401,idr2:4,idr8:1,idr9:0,idr10:0,idr11:0,idr12:1,idr13:0,config:4113,traceid:2};
test('historical session infers protocol from its own registers and preserves nondefault physical settings',()=>{
 const result=offlineSettings({config:v4,clock:5,data:[7,8,9,10],fraction:.65,glitchNs:2,elfId:'elf-history'},{clock:0,data:[1,2,3,4],fraction:.25,glitchNs:4});
 assert.equal(result.config.protocol,'ETMv4');assert.equal(result.config.config,4113);
 assert.deepEqual([result.clock,...result.data],[5,7,8,9,10]);assert.equal(result.fraction,.65);assert.equal(result.glitchNs,2);assert.equal(result.elfSha256,null);
});
test('ETMv3 and manually entered hex registers require no target and no v4 defaults',()=>{
 const result=offlineSettings({...v3,idr:'0x4114f241',ctrl:'0x810'});
 assert.deepEqual(result.config,v3);assert.equal(result.config.idr1,undefined);
});
test('missing protocol evidence, incomplete snapshots and mismatched architecture are rejected',()=>{
 for(const value of [null,[],{}, {traceid:2},{idr1:0x4100f401},{...v3,protocol:'ETMv4'}, {...v4,idr1:0x4100f201}, {...v4,traceid:0},{...v3,ctrl:true}])assert.throws(()=>offlineSettings(value));
});
test('malformed channel, phase and filter settings cannot reach import',()=>{
 for(const fields of [{clock:1},{data:[1,2,3]},{data:[1,2,3,true]},{data:[1,2,3,16]},{fraction:NaN},{fraction:1},{glitchNs:Infinity},{glitchNs:-1}])assert.throws(()=>offlineSettings({config:v4,...fields}));
});
test('only a firmware hash can identify ELF; an old random upload ID cannot reject a fresh upload',()=>{
 assert.equal(offlineSettings({config:v4,elfId:'elf-old-upload'}).elfSha256,null);
 assert.equal(offlineSettings({config:v4,elfSha256:'A'.repeat(64)}).elfSha256,'a'.repeat(64));
 assert.throws(()=>offlineSettings({config:v4,elfSha256:'not-a-hash'}));
});
