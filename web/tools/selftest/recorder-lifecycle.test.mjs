import assert from 'node:assert/strict';
import { FileRecorder, recordButtonState } from '../../app/core/recorder.js';

const gate = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const tick = () => new Promise(r => setImmediate(r));
let picker, picks = 0;
globalThis.window = { showSaveFilePicker: () => { picks++; return picker(); } };
const rec = new FileRecorder(), opening = gate(), writing = gate(), closing = gate();
let writes = [], closes = 0;
picker = () => opening.promise;
const first = rec.start(), duplicate = rec.start();
assert.equal(picks, 1); assert.equal(rec.starting, true);
opening.resolve({ name: 'old.bin', createWritable: async () => ({
  async write(bytes){ writes.push([...bytes]); await writing.promise; },
  async close(){ closes++; await closing.promise; },
}) });
assert.deepEqual(await Promise.all([first, duplicate]), ['old.bin', 'old.bin']);
rec.push(Uint8Array.of(1, 2, 3));
const stopA = rec.stop(), stopB = rec.stop();
await tick();
await assert.rejects(rec.start(), /正在落盘/); assert.equal(picks, 1);
assert.equal(rec.draining, true); writing.resolve(); await tick();
assert.equal(rec.draining, true); assert.equal(closes, 1);
closing.resolve(); const [a, b] = await Promise.all([stopA, stopB]);
assert.equal(a, b); assert.equal(rec.draining, false); assert.equal(rec._w, null);
assert.deepEqual(writes, [[1, 2, 3]]);
picker = async () => ({ name: 'new.bin', createWritable: async () => ({
  async write(bytes){ writes.push([...bytes]); }, async close(){ closes++; },
}) });
await rec.start(); rec.push(Uint8Array.of(4)); await rec.stop();
assert.deepEqual(writes, [[1, 2, 3], [4]]); assert.equal(closes, 2);

const pending = new FileRecorder(), pickGate = gate(); picker = () => pickGate.promise;
const startPending = pending.start(), stopPending = pending.stop();
pickGate.resolve({ name: 'pending.bin', createWritable: async () => ({ async close(){ closes++; } }) });
await Promise.all([startPending, stopPending]); assert.equal(pending.active, false); assert.equal(pending._w, null);
picker = async () => { throw Object.assign(new Error('cancelled'), { name: 'AbortError' }); };
await assert.rejects(pending.start(), /cancelled/); assert.equal(pending.starting, false);
console.log('recorder-lifecycle: single picker/stop, close barrier, clean restart and stop during opening PASS');

const failed = new FileRecorder(), diskError = new Error('disk full');
const failedWrite = gate(), closeFailure = gate(); let attempts = 0, notes = [];
picker = async () => ({ name: 'failed.bin', createWritable: async () => ({
  async write(){ attempts++; await failedWrite.promise; throw diskError; },
  async close(){ await closeFailure.promise; },
}) });
failed.onNote = note => notes.push(note);
await failed.start();
failed.push(Uint8Array.of(1)); failed._flush(); failed.push(Uint8Array.of(2)); failed._flush();
const failureStop = failed.stop(); failedWrite.resolve(); await tick();
assert.equal(failed.draining, true); assert.equal(failed.error, diskError); assert.equal(attempts, 1);
assert.equal(notes.length, 1); assert.match(notes[0], /写入失败/);
closeFailure.resolve(); const failureInfo = await failureStop;
assert.equal(failureInfo.error, diskError); assert.equal(failureInfo.written, 0); assert.equal(failureInfo.bytes, 2);
assert.match(recordButtonState(failed).text, /记录失败/); assert.equal(failed._w, null);

const liveError = new FileRecorder();
picker = async () => ({ name: 'live.bin', createWritable: async () => ({
  async write(){ throw diskError; }, async close(){},
}) });
await liveError.start(); liveError.push(Uint8Array.of(3)); liveError._flush(); await liveError._wq;
assert.equal(liveError.active, false); assert.match(recordButtonState(liveError).text, /结束失败记录/);
await assert.rejects(liveError.start(), /尚未关闭/);
assert.equal((await liveError.stop()).error, diskError);
picker = async () => ({ name: 'recovered.bin', createWritable: async () => ({ async write(){}, async close(){} }) });
await liveError.start(); liveError.push(Uint8Array.of(4));
assert.equal((await liveError.stop()).error, null); assert.equal(liveError.written, 1);
console.log('recorder-lifecycle: write error survives stop, remains visible, stops later batches and recovers after close PASS');

const listeners = new Map();
globalThis.addEventListener = (name, fn) => listeners.set(name, fn);
globalThis.removeEventListener = (name, fn) => { if (listeners.get(name) === fn) listeners.delete(name); };
globalThis.document = { addEventListener(){}, removeEventListener(){} };
const guarded = new FileRecorder(), finalClose = gate();
picker = async () => ({ name: 'guard.bin', createWritable: async () => ({
  async write(){}, async close(){ await finalClose.promise; },
}) });
const blocksUnload = () => {
  let blocked = false; listeners.get('beforeunload')?.({ preventDefault(){ blocked = true; } }); return blocked;
};
await guarded.start(); guarded.push(Uint8Array.of(1)); guarded._flush(); await guarded._wq;
assert.equal(guarded.backlog(), 0); assert.equal(blocksUnload(), true, 'flushed but uncommitted file stays protected');
const guardStop = guarded.stop(); await tick();
assert.equal(guarded.draining, true); assert.equal(blocksUnload(), true, 'close in progress stays protected');
finalClose.resolve(); await guardStop; assert.equal(blocksUnload(), false);
console.log('recorder-lifecycle: unload stays protected through zero backlog and pending close PASS');
