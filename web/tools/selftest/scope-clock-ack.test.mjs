import test from 'node:test';import assert from 'node:assert/strict';
import {ScopeView} from '../../app/scope/view.js';
import {ACT, PACKET, TIME_HZ} from '../../app/scope/protocol.js';
import {VendorEpTransport} from '../../app/scope/transport.js';
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

function tailStatus(flags, rc = 0){
 const res = Uint8Array.of(50, 0x32, rc, 0, ...new Uint8Array(47));
 res[3] = 32 | flags;
 return res;
}
const readResult = (size, value = 0) => ({
 status: 'ok', data: new DataView(new Uint8Array(size).fill(value).buffer),
});
const tick = () => new Promise(r => setImmediate(r));
class PendingUsb {
 pending = [];
 calls = 0;
 transferIn(){
  this.calls++;
  return new Promise(resolve => this.pending.push(resolve));
 }
}

test('STOP delivers the short tail in read order and retires exactly the remaining USB reads',async()=>{
 const usb = new PendingUsb(), t = new VendorEpTransport(usb,{inFlight:4,chunkBytes:PACKET*2});
 const seen = [], commands = [];
 await t.start(b => seen.push([b.length,b[0]]));
 const view = {
  hid:{},transport:t,supportsStopTail:true,signed:ScopeView.prototype.signed,
  hidXfer:async(_,data)=>{
   commands.push([...data]);
   if(data[0] === ACT.STOP){
    const [full,short] = usb.pending.splice(0,2);
    short(readResult(PACKET,2)); await tick();
    assert.deepEqual(seen,[],'short native completion cannot overtake a prior full read');
    full(readResult(PACKET*2,1));
    return tailStatus(64,156);
   }
   if(data[0] === ACT.RETIRE_READS){
    assert.deepEqual(seen,[[PACKET*2,1],[PACKET,2]]);
    assert.equal(data[1],2);
    for(const done of usb.pending.splice(0)) done(readResult(0));
    return tailStatus(64);
   }
   return tailStatus(0);
  },
 };
 await ScopeView.prototype._stopData.call(view);
 assert.deepEqual(commands,[[ACT.STOP,1],[ACT.STATUS],[ACT.RETIRE_READS,2],[ACT.STATUS]]);
 assert.equal(usb.calls,4,'drain must not rearm a populated queue');
 assert.equal(t.stalledInFlight,0);
 await t.start(b => seen.push([b.length,b[0]]));
 usb.pending.shift()(readResult(PACKET*2,3)); await tick();
 assert.deepEqual(seen.at(-1),[PACKET*2,3]);
 const stop = t.stop(); for(const done of usb.pending.splice(0)) done(readResult(0)); await stop;
});

test('exact-length final packet requires one more read for EOF; a malformed short read fails',async()=>{
 const usb = new PendingUsb(), t = new VendorEpTransport(usb,{inFlight:1,chunkBytes:PACKET});
 const seen = [];
 await t.start(b => seen.push(b.length));
 const eof = t.beginStopDrain();
 usb.pending.shift()(readResult(PACKET)); await tick();
 assert.deepEqual(seen,[PACKET]); assert.equal(usb.calls,2);
 usb.pending.shift()(readResult(0));
 assert.equal((await eof).remaining,0);
 await t.stop();
 await t.start(b => seen.push(b.length));
 const bad = t.beginStopDrain();
 usb.pending.shift()(readResult(PACKET-1));
 await assert.rejects(bad,/传输无效/);
 assert.deepEqual(seen,[PACKET]);
 await t.stop();
});

test('STOP timeout and reset stay unconfirmed without replaying the write',async()=>{
 for(const flags of [64,128]){
  const commands = [];
  const t = {running:true,beginStopDrain:async()=>{throw Error('未收到结束标记');},stop:async()=>{}};
  // A real transport attaches a rejection handler while STOP is in flight.
  const begin = t.beginStopDrain; t.beginStopDrain = () => {
   const pending = begin(); pending.catch(()=>{}); return pending;
  };
  const view = {hid:{},transport:t,supportsStopTail:true,signed:ScopeView.prototype.signed,
   hidXfer:async(_,data)=>{commands.push([...data]);return tailStatus(flags);}};
  await assert.rejects(ScopeView.prototype._stopData.call(view),/无法确认采样已停止/);
  assert.equal(view._stopUnconfirmed,true);
  assert.deepEqual(commands,[[ACT.STOP,1]]);
 }
});

test('final metrics use the declared HID length and warn on missing or invalid samples',async()=>{
 const response = new Uint8Array(63).fill(255);
 response[0]=50;
 const dv = new DataView(response.buffer,3);
 dv.setUint32(0,0x31535348,true); dv.setUint32(8,TIME_HZ,true);
 dv.setUint32(12,5,true);
 const view = {
  hid:{},supportsMetrics:true, readScopeMetrics:ScopeView.prototype.readScopeMetrics,
  hidXfer:async()=>response, _stopWatchdog(){}, _stopData:async()=>{}, syncButtons(){},
  setStatusText(msg){this.display=msg;}, receivedSamples:4, transport:{epAddr:0x83},
 };
 const legacy = await view.readScopeMetrics();
 assert.equal(legacy.invalidSamples,null);
 view.running = true;
 await ScopeView.prototype._stopOnce.call(view);
 assert.match(view.display,/样本未完整接收/);
 assert.equal(view.tailCountDelta,-1);
 response[0]=54; dv.setUint32(48,1,true);
 view.supportsStopTail = true;
 view.running = true;
 await ScopeView.prototype._stopOnce.call(view);
 assert.match(view.display,/1 个样本未能完成读取/);
 assert.equal(view.tailCountDelta,0);
});
test('legacy pending STOP needs a full stopped-status confirmation',async()=>{
 const reply=new Uint8Array(51);reply[2]=156;const events=[];
 const view={hid:{},transport:{quiesce:async()=>events.push('quiesce'),stop:async()=>events.push('stop')},signed:n=>n>127?n-256:n,hidXfer:async()=>reply};
 await ScopeView.prototype._stopData.call(view);assert.deepEqual(events,['quiesce','stop']);assert.equal(view._stopUnconfirmed,false);
 reply[3]=1;await assert.rejects(ScopeView.prototype._stopData.call(view),/无法确认采样已停止/);assert.equal(view._stopUnconfirmed,true);
});
