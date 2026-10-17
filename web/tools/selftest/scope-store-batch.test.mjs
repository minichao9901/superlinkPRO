import assert from 'node:assert/strict';
import { SampleStore } from '../../app/scope/store.js';
const vars = [{ name: 'counter', size: 4, scalar: 'u32' }];
const reference = new SampleStore(vars, 9955), batch = new SampleStore(vars, 9955);
let seed = 42, t0 = 10;
for (let packet = 0; packet < 85; packet++) {
  const bytes = new Uint8Array(1 + 124 * 4 + 2);
  const payload = bytes.subarray(1, 1 + 124 * 4 + (packet % 5 === 0 ? 2 : 0));
  const dv = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
  for (let k = 0; k < 124; k++) {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    dv.setUint32(k * 4, seed, true);
    reference.pushFrame([seed], t0 + k * 2.25);
  }
  batch.pushU32Packet(payload, 124, t0, 2.25);
  t0 += 124 * 2.25 + (packet % 7 === 0 ? 10 : 0); // timeline gaps
}
for (const key of ['count', 'frames', 'full', 'overrun', 'tsN', 't0Us', 'tLastUs'])
  assert.equal(batch[key], reference[key], key);
assert.deepEqual(batch.tsUs, reference.tsUs);
assert.deepEqual(batch.channel(0).data, reference.channel(0).data);
assert.equal(batch.channel(0).min, reference.channel(0).min);
assert.equal(batch.channel(0).max, reference.channel(0).max);
for (let k = 0; k < 3; k++) {
  assert.deepEqual(batch.channel(0).levels[k].min, reference.channel(0).levels[k].min);
  assert.deepEqual(batch.channel(0).levels[k].max, reference.channel(0).levels[k].max);
}
for (const i of [0, 63, 64, 124, 4095, 4096, 9954]) assert.equal(batch.timeAt(i), reference.timeAt(i));
assert.equal(batch.rate(), reference.rate());
const small = new SampleStore(vars, 3);
const short = new Uint8Array(9); new DataView(short.buffer).setUint32(4, 0xffffffff, true);
assert.equal(small.pushU32Packet(short, 10, 0, 2.5), 2);
assert.equal(small.count, 2); assert.equal(small.channel(0).at(1), 0xffffffff);
assert.equal(new SampleStore([{size:4, scalar:'f32'}], 10).pushU32Packet(short, 1, 0, 2), false);
batch.reset(); assert.equal(batch.pushU32Packet(short, 2, 1, 2), 2); assert.equal(batch.t0Us, 1);
console.log('scope-store-batch: random u32 accuracy, unaligned payload, LOD, gaps, timestamps, full/overrun, short payload, reset PASS');
