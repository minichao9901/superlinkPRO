import assert from 'node:assert/strict';
import {AdcTransport,decodeAdcPacket} from '../../app/analog/transport.js';
function packet(token,seq,first,codes=[],fault=0){
 const b=new Uint8Array(32+2*codes.length),v=new DataView(b.buffer);
 b.set([65,68,83,50,2,codes.length?1:2,16,0]);
 for(const [o,n]of [[8,token],[12,seq],[16,first],[20,2000000],[28,fault]])v.setUint32(o,n,true);
 v.setUint16(24,codes.length,true);b[26]=6;b[27]=2;codes.forEach((n,i)=>v.setUint16(32+2*i,n,true));return b;
}
assert.deepEqual([...decodeAdcPacket(packet(1,0,0,[1,65535]),1).codes],[1,65535]);
for(const b of [packet(2,0,0,[1]),packet(1,0,0,[1]).subarray(0,33),new Uint8Array()])assert.throws(()=>decodeAdcPacket(b,1),/数据包|长度/);
let queue=[],reads=0,active=0,maxActive=0;
const t=new AdcTransport({vendorId:1,productId:2,async transferIn(ep,len){
 assert.equal(ep,11);assert.equal(len,4096);active++;maxActive=Math.max(maxActive,active);await Promise.resolve();active--;
 reads++;assert.ok(queue.length,'no extra native read after END');return {status:'ok',data:new DataView(queue.shift().buffer)};
}});
const blocks=[];queue=[packet(1,0,0,[1,2]),packet(1,1,2,[3]),packet(1,2,3)];t.start(1,{bits:16,onBlock:b=>blocks.push(b)});await t.drain();
assert.equal(t.done,true);assert.equal(maxActive,1);assert.equal(reads,3);assert.equal(t.error,null);assert.equal(blocks.length,2);
const tail=[],order=[];
queue=[packet(2,0,0,[1],5),packet(2,1,1,[2,3],5),packet(2,2,3,[],5)];
t.start(2,{bits:16,onBlock:b=>{tail.push(...b.codes);order.push('data');},onFault:()=>order.push('fault')});
await t.drain();assert.match(t.error.message,/缓冲满/);assert.equal(t.done,true);
assert.deepEqual(tail,[1,2,3]);assert.deepEqual(order,['data','fault','data']);
queue=[packet(3,0,0,[1]),packet(3,2,1,[2]),packet(3,3,2)];t.start(3,{bits:16});await t.drain();assert.match(t.error.message,/不连续/);assert.equal(t.done,true);
queue=[packet(4,0,0)];t.start(4,{bits:16});await t.drain();assert.equal(t.error,null);
queue=[packet(5,0,0,[1]),packet(5,1,1)];t.start(5,{bits:16,onBlock(){throw Error('view failure');}});await t.drain();assert.match(t.error.message,/view failure/);
console.log('ADC shared Bulk: compact blocks, strict token/size/config, one native reader, fault/malformed block draining, END/restart PASS');
let submissions=0,pipelineActive=0,pipelinePeak=0;
const releases=[],pipelined=new AdcTransport({vendorId:4,productId:5,transferIn(){
 const index=submissions++;pipelineActive++;pipelinePeak=Math.max(pipelinePeak,pipelineActive);
 return new Promise(resolve=>{releases[index]=b=>{pipelineActive--;resolve({status:'ok',data:new DataView(b.buffer)});};});
}});
const pipedBlocks=[];pipelined.start(6,{bits:16,inFlight:3,onBlock:b=>pipedBlocks.push(...b.codes)});
assert.equal(submissions,3);
// Promise completion order may differ from endpoint submission order.
releases[1](packet(6,1,2,[3]));releases[0](packet(6,0,0,[1,2]));
await new Promise(resolve=>setImmediate(resolve));assert.equal(submissions,5);
releases[2](packet(6,2,3));releases[3](packet(6,3,3));
await new Promise(resolve=>setImmediate(resolve));assert.ok(pipelined.pending);
await assert.rejects(pipelined.close(),/请求/);releases[4](packet(6,4,3));await pipelined.drain();
assert.deepEqual(pipedBlocks,[1,2,3]);assert.equal(pipelinePeak,3);assert.equal(pipelineActive,0);
assert.equal(submissions,5);assert.equal(pipelined.done,true);assert.equal(pipelined.error,null);
assert.throws(()=>pipelined.start(7,{inFlight:0}),/深度/);
const unplugged=new AdcTransport({vendorId:6,productId:7,transferIn:()=>Promise.reject(Error('device disconnected'))});
unplugged.start(8,{bits:16,inFlight:3});await unplugged.pending;
assert.equal(unplugged.pending,null);assert.equal(unplugged.done,false);await assert.rejects(unplugged.drain(),/disconnected/);
assert.throws(()=>unplugged.start(9,{bits:16}),/上一轮/);
console.log('ADC pipeline: FIFO despite out-of-order promises, counted END/close fence, tail delivery, native disconnect retirement PASS');
let finishOut,outs=0;
const flushing=new AdcTransport({vendorId:2,productId:3,transferOut(){outs++;return new Promise(resolve=>finishOut=resolve);}});
const flush=flushing.retireSpiOut();await Promise.resolve();
await assert.rejects(flushing.retireSpiOut(),/上一笔/);assert.equal(outs,1);
await assert.rejects(flushing.close(),/USB 请求/);finishOut({status:'ok'});await flush;
assert.equal(flushing.flush,null);
console.log('ADC SPI handoff: one native OUT only, close guarded until native completion PASS');
// Native IN retirement errors are not equivalent to an empty endpoint.
for(const result of [new Error('IN unplugged'),{status:'stall'}]){
 const bad=new AdcTransport({vendorId:20,productId:21,transferIn(){return result instanceof Error?Promise.reject(result):Promise.resolve(result);}});
 await assert.rejects(bad.retireSpiIn(),/IN unplugged|SPI IN 退场失败/);
}
// Exercise the actual Worker script, including the pending read handed to pump.
const {readFile}=await import('node:fs/promises');
const vm=await import('node:vm');
const messages=[];let workerReads=0,finishRead;
const context=vm.createContext({performance,setTimeout,clearTimeout,Uint8Array,DataView,Error,
 self:{postMessage:m=>messages.push(m)},testDevice:{transferIn(){workerReads++;return new Promise(resolve=>finishRead=resolve);}}});
vm.runInContext(await readFile(new URL('../../app/analog/adc-usb-worker.js',import.meta.url),'utf8'),context);
vm.runInContext('device=testDevice',context);
await vm.runInContext('retireIn()',context);assert.equal(workerReads,1);
const task=vm.runInContext('pump({token:30,inFlight:1})',context);
assert.equal(workerReads,1,'idle native read is reused, never orphaned');
finishRead({status:'ok',data:new DataView(packet(30,0,0).buffer)});await task;
assert.ok(messages.some(m=>m.event==='drained'&&m.complete));
vm.runInContext("device={transferIn:()=>Promise.reject(Error('Worker IN failed'))}",context);
await assert.rejects(vm.runInContext('retireIn()',context),/Worker IN failed/);
console.log('ADC retirement: main/Worker native errors propagate; Worker idle IN passes to pump without a duplicate read PASS');
// Worker OUT caller timeout retains the native RPC and prevents another OUT/close.
const savedWorker=globalThis.Worker,savedNavigator=Object.getOwnPropertyDescriptor(globalThis,'navigator');
const nativeDevice={vendorId:40,productId:41,configurations:[{configurationValue:1,interfaces:[{interfaceNumber:5,alternates:[{endpoints:[{endpointNumber:11,direction:'in',type:'bulk'},{endpointNumber:11,direction:'out',type:'bulk'}]}]}]}]};
let workerStub;
globalThis.Worker=class {
 constructor(){workerStub=this;this.requests=[];}
 postMessage(m){this.requests.push(m);if(m.action==='open'||m.action==='close')queueMicrotask(()=>this.onmessage({data:{id:m.id,ok:true}}));}
 terminate(){}
};
Object.defineProperty(globalThis,'navigator',{configurable:true,value:{usb:{getDevices:async()=>[nativeDevice]}}});
try{
 const worker=await AdcTransport.request(nativeDevice);
 const savedTimeout=globalThis.setTimeout;
 let timed;
 try{globalThis.setTimeout=(fn,ms,...args)=>savedTimeout(fn,ms===2000?5:ms,...args);timed=worker.retireSpiOut();}
 finally{globalThis.setTimeout=savedTimeout;}
 await assert.rejects(timed,/退场 SPI OUT/);
 assert.ok(worker.flush,'timeout does not release native ownership');
 await assert.rejects(worker.retireSpiOut(),/上一笔/);await assert.rejects(worker.close(),/USB 请求/);
 const out=workerStub.requests.find(m=>m.action==='out');
 workerStub.onmessage({data:{id:out.id,ok:true,status:'ok'}});
 await worker.flush;assert.equal(worker.flush,null);await worker.close();
}finally{
 globalThis.Worker=savedWorker;
 if(savedNavigator)Object.defineProperty(globalThis,'navigator',savedNavigator);else delete globalThis.navigator;
}
console.log('ADC Worker OUT: caller timeout keeps native request fenced until late completion PASS');
