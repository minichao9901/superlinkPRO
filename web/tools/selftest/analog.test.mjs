import assert from 'node:assert/strict';
import { adcPlan, adcValue, waveform, WAVES, adcCsv, waveCsv } from '../../app/analog/model.js';
import { AnalogSession } from '../../app/analog/session.js';
const plan = adcPlan({ channel:3, bits:12, rate:333, count:1 });
assert.equal(plan.period,3); assert.equal(plan.actualRate,1000/3); assert.deepEqual([...plan.records[0].data],[3,12]);
assert.throws(()=>adcPlan({channel:3,bits:7,rate:10}),/位宽/);
assert.throws(()=>adcPlan({channel:3,bits:16,rate:1001}),/采样率/);
assert.deepEqual(adcValue(Uint8Array.of(255,255),16,3.3,2),{code:65535,volts:6.6});
assert.throws(()=>adcValue(Uint8Array.of(0,16),12,3.3),/超出/);
for (const shape of Object.keys(WAVES)) {
  const rows=waveform({shape,points:64}); assert.equal(rows.length,64);
  assert.ok(rows.every(r=>Number.isFinite(r.volts)&&r.code>=0&&r.code<=4095));
}
const sine=waveform({rate:800,frequency:100,points:16});assert.ok(Math.abs(sine[2].volts-2.65)<1e-12);
const dc=waveform({shape:'dc',offset:2,amplitude:10,points:16});assert.ok(dc.every(r=>r.volts===2));
assert.throws(()=>waveform({amplitude:2}),/offset/);assert.throws(()=>waveform({rate:700,frequency:100}),/8/);
assert.deepEqual(waveform({shape:'noise',seed:17}),waveform({shape:'noise',seed:17}));
assert.match(waveCsv(sine),/^time_s,volts,code\n/);assert.match(adcCsv([{timeMs:10,cycle:1,skipped:0,code:65535,volts:3.3}]),/10,1,0,65535,3.30000000/);
const session=new AnalogSession(); await assert.rejects(session.acquire({bits:16,rate:100,count:1}),/连接/);
session.busy=true; session.hid={connected:true};session.caps={reference:3.3};await assert.rejects(session.acquire({bits:16,rate:100,count:1}),/已有/);
console.log('analog: parameter bounds, sampling quantization, ADC scale, preset waveforms, clipping rejection and CSV PASS');
