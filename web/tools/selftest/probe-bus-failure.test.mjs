import assert from 'node:assert/strict';
import {ProbeBus} from '../../app/core/probe-bus.js';
const a=new ProbeBus('requester'),b=new ProbeBus('flashing');
try{
 await new Promise(r=>setTimeout(r,30));b.onRelease=()=>{throw new Error('busy flashing');};
 await assert.rejects(a.requestRelease({settleMs:40,waitMs:200}),/busy flashing/);assert.equal(a._acked,0);
 b.onRelease=()=>{};const result=await a.requestRelease({settleMs:40,waitMs:200});assert.equal(result.acked,1);
 a._heard=new Set();a._released=new Set();a._releaseId='new';a._acked=0;
 a._on({t:'released',from:'old',requestId:'old'});assert.equal(a._acked,0,'late acknowledgement cannot satisfy another release');
}finally{a.close();b.close();}
console.log('probe-bus-failure: busy flash denial, successful retry, late reply identity PASS');
