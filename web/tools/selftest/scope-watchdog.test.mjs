import assert from 'node:assert/strict';
import {ScopeView} from '../../app/scope/view.js';
import {SCOPE_FLAG} from '../../app/scope/protocol.js';
const savedTimer=globalThis.setInterval,savedPerformance=globalThis.performance;let cb,now=0;
globalThis.setInterval=f=>{cb=f;return 1;};globalThis.performance={now:()=>now};
try{
 const v=Object.create(ScopeView.prototype);let errors=0;
 Object.assign(v,{_capturing:true,_captureGen:1,_periodUs:100000,_frameBytes:4,_stopWatchdog(){},_onDataPlaneDead(){errors++;}});
 v._startWatchdog();now=3000;cb();assert.equal(errors,0);now=13000;cb();assert.equal(errors,0);now=38000;cb();assert.equal(errors,1);
 now=0;v._periodUs=3;v._startWatchdog();now=2600;cb();assert.equal(errors,2,'fast capture still detects stall promptly');
 v._captureGen++;cb();assert.equal(errors,2,'old watcher cannot stop new session');
 cb=null;v._captureFlags=SCOPE_FLAG.DISCARD;v._startWatchdog();assert.equal(cb,null,'discard does not expect USB packets');
}finally{globalThis.setInterval=savedTimer;globalThis.performance=savedPerformance;}
console.log('scope-watchdog: slow packet cadence, fast failure, stale generation, discard mode PASS');
