import assert from 'node:assert/strict';
import {FlashRunner} from '../../app/flash/runner.js';
import {ALGOS} from '../../app/flash/algos.js';
for(const [name,algo] of Object.entries(ALGOS)){
 const writes=new Map(),order=[];const probe={halt:async()=>{},regWrite:async(n,v)=>{writes.set(n,v);order.push(n);},run:async()=>{assert.equal(writes.get(9),algo.static_base>>>0,name);assert.equal(order.at(-1),15);},isHalted:async()=>true,regRead:async()=>0};
 const runner=new FlashRunner(probe);runner.algo=algo;await runner.runCode(algo.pc_init,{0:algo.flash_start});assert.equal(writes.get(13),algo.begin_stack);
 assert.equal(runner.chunkSize()%(algo.write_granularity||4),0,`${name}: chunk aligns to flash programming granularity`);
 if(name==='stm32h7')assert.equal(runner.chunkSize(),1024,'H743 erase sectors must not be used as the 1 KB program-buffer capacity');
}
console.log('flash-runner-abi: algorithm ABI and flash chunk geometry PASS');
