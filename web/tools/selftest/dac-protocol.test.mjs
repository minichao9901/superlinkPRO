import assert from 'node:assert/strict';
import { DacClient, CMD, ACT, DacError, RC, configData, beginData, writeData, startData, stopData, parseCaps, parseStatus } from '../../app/analog/dac-protocol.js';
import { dacTable } from '../../app/analog/model.js';
import { AnalogSession } from '../../app/analog/session.js';
import { buildRequest } from '../../app/hid/probe.js';
const view=b=>new DataView(b.buffer,b.byteOffset,b.byteLength);
const reply=(a,b=new Uint8Array(),rc=0)=>{const r=new Uint8Array(7+b.length);r[0]=r.length+1;r[1]=CMD;r[2]=a;view(r).setUint32(3,rc,true);r.set(b,7);return r;};
function caps(channels=2){const b=new Uint8Array(16);b.set([...Buffer.from('DAC1'),1,channels,channels?12:0,channels?1:0]);if(channels){view(b).setUint32(8,100000,true);view(b).setUint16(12,128,true);view(b).setUint16(14,3300,true);}return b;}
function fixture(){
 const f={calls:[],generation:10,loaded:0,points:0,running:false,codes:[],stopFails:0};
 f.status=()=>{const b=new Uint8Array(20);b[0]=f.channel;b[1]=+f.running;b[2]=12;b[3]=f.cleanup?1:0;view(b).setUint32(4,f.generation,true);view(b).setUint16(8,f.loaded,true);view(b).setUint32(16,f.config?.rate||1000,true);return b;};
 f.xfer=async(cmd,data)=>{
  assert.equal(cmd,CMD);const a=data[0],b=data.subarray(1);f.calls.push(a);
  assert.equal(buildRequest(cmd,data).length,63,'actual HID builder accepts every packet');
  switch(a){
   case ACT.CAPS:return reply(a,caps());
   case ACT.CONFIG:f.channel=b[0];f.config={rate:view(b).getUint32(2,true)};return reply(a,b);
   case ACT.BEGIN:f.points=view(b).getUint16(1,true);f.loaded=0;f.codes=[];f.generation++;{const r=new Uint8Array(4);view(r).setUint32(0,f.generation,true);if(f.lostBegin)throw Error('BEGIN response lost');return reply(a,r);}
   case ACT.WRITE:assert.equal(view(b).getUint32(1,true),f.generation);assert.equal(view(b).getUint16(5,true),f.loaded);for(let i=0;i<b[7];i++)f.codes.push(view(b).getUint16(8+i*2,true));f.loaded+=b[7];await f.onWrite?.();{const r=new Uint8Array(2);view(r).setUint16(0,f.loaded+(f.badAck?1:0),true);return reply(a,r);}
   case ACT.START:assert.equal(f.loaded,f.points);assert.equal(view(b).getUint32(1,true),f.generation);f.running=true;await f.onStart?.();{const r=new Uint8Array(4);view(r).setUint32(0,f.config.rate,true);return reply(a,r);}
   case ACT.STOP:assert.equal(view(b).getUint32(1,true),f.generation);if(f.stopFails-->0)throw Error('STOP transport failure');f.running=false;return reply(a,f.status());
   case ACT.STATUS:f.cleanup=false;return reply(a,f.status());
   default:throw Error('unexpected action');
  }
 };return f;
}
const none=new DacClient(async(c,d)=>reply(d[0],caps(0)));assert.equal((await none.capabilities()).supported,false);await assert.rejects(none.startLut({channel:0,bits:12,rate:1000},Array(8).fill(0)),e=>e instanceof DacError&&e.code===RC.UNSUPPORTED);
const legacy=new DacClient(async(c,d)=>reply(d[0],new Uint8Array(),RC.RANGE));assert.equal((await legacy.capabilities()).supported,false);
const truncated=new DacClient(async()=>Uint8Array.of(24,CMD,1));await assert.rejects(truncated.capabilities(),/不完整/);
assert.throws(()=>parseCaps(new Uint8Array(16)),/版本/);
assert.throws(()=>parseStatus(new Uint8Array(19)),/长度/);
assert.throws(()=>writeData(0,1,0,Array(26).fill(0)),/分片/);
assert.throws(()=>writeData(0,1,0,[-1]),/分片/);
const boundary=writeData(0,0x12345678,100,Array(25).fill(4095));assert.equal(boundary.length,58);assert.equal(view(boundary).getUint32(1,true),0x12345678);assert.equal(view(boundary).getUint16(5,true),100);
const options={shape:'sine',rate:3200,frequency:100,amplitude:1,offset:1.65,points:1024};
const c=parseCaps(caps()),table=dacTable(options,c);assert.equal(table.codes.length,32);assert.equal(table.actualFrequency,100);
const short=fixture(),shortClient=new DacClient(short.xfer);await shortClient.capabilities();
await assert.rejects(shortClient.startLut({channel:0,bits:12,rate:3200},Array(7).fill(0)),/超出/);
assert.deepEqual(short.calls,[ACT.CAPS],'invalid LUT cannot modify CONFIG or reserve BEGIN');
assert.equal(dacTable({...options,frequency:99},c).actualFrequency,100);
assert.throws(()=>dacTable({...options,frequency:1},c),/点数/);
assert.equal(dacTable({...options,rate:800,frequency:100},c).codes.length,8);
const f=fixture(),client=new DacClient(f.xfer);await client.capabilities();await client.startLut({channel:1,bits:12,rate:3200},table.codes);assert.deepEqual(f.codes,table.codes);assert.equal(f.calls.filter(a=>a===ACT.WRITE).length,2);assert.equal((await client.status()).running,true);
f.stopFails=1;await assert.rejects(client.stop(),/STOP transport/);assert.ok(client.owned&&client.stopError);f.cleanup=true;await client.stop();assert.ok(f.calls.includes(ACT.STATUS));assert.equal(client.owned,false);assert.equal(client.stopError,null);
const bad=fixture(),badClient=new DacClient(bad.xfer);await badClient.capabilities();bad.badAck=true;await assert.rejects(badClient.startLut({channel:0,bits:12,rate:3200},table.codes),/进度/);assert.ok(!bad.calls.includes(ACT.START)&&bad.calls.includes(ACT.STOP));
for(const stage of ['onWrite','onStart']){
 const f=fixture(),s=new AnalogSession();s.hid={connected:true,xfer:f.xfer};s.dac=new DacClient(f.xfer);await s.dac.capabilities();
 let entered,release;const seen=new Promise(r=>entered=r),held=new Promise(r=>release=r);f[stage]=async()=>{entered();await held;};
 const start=s.startDac({...options,channel:0});start.catch(()=>{});await seen;const stop=s.stopDac();release();await assert.rejects(start,/取消/);await stop;
 assert.equal(f.running,false);assert.equal(s.busy,false);assert.equal(f.calls.at(-1),ACT.STOP);
 if(stage==='onWrite')assert.ok(!f.calls.includes(ACT.START));
}
console.log('DAC reservation: capabilities, legacy fallback, HID sizes, coherent LUT, chunk ACK, STOP retry and cancellation during upload/START PASS');

const lost=fixture(),lostClient=new DacClient(lost.xfer);await lostClient.capabilities();lost.lostBegin=true;
await assert.rejects(lostClient.startLut({channel:0,bits:12,rate:3200},table.codes),/BEGIN response lost/);
assert.ok(lost.calls.includes(ACT.STATUS)&&lost.calls.includes(ACT.STOP));assert.equal(lostClient.owned,false);
const unknown=fixture(),unknownClient=new DacClient(unknown.xfer);await unknownClient.capabilities();unknown.lostBegin=true;
const base=unknown.xfer;unknownClient.xfer=async(c,d)=>{if(d[0]===ACT.STATUS)throw Error('STATUS transport failure');return base(c,d);};
await assert.rejects(unknownClient.startLut({channel:0,bits:12,rate:3200},table.codes),/停止未确认/);
assert.ok(unknownClient.owned&&unknownClient.beginUncertain);
unknownClient.xfer=base;await unknownClient.stop();assert.equal(unknownClient.owned,false);
console.log('DAC uncertain BEGIN: lost ACK recovered by STATUS/STOP, failed recovery retains ownership for retry PASS');
