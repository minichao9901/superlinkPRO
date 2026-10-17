import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { OpenOcdBackend } from '../../bridge/rtt-bridge.mjs';
const tick = () => new Promise(r => setImmediate(r));
function socket(){ const s = new EventEmitter(); s.sent = []; s.write = line => s.sent.push(line); s.destroy = () => { s.destroyed = true; }; return s; }

const backend = new OpenOcdBackend(), old = socket(); backend.sock = old;
const timeout = assert.rejects(backend.rpc('first', 10), /命令超时/);
const queued = assert.rejects(backend.rpc('second', 100), /未连接|替换/);
await Promise.all([timeout, queued]); assert.equal(old.destroyed, true); assert.equal(backend.sock, null);
assert.equal(old.sent.length, 1); old.emit('data', Buffer.from('late-first\x1a'));
assert.equal(old.listenerCount('data'), 0); assert.equal(old.listenerCount('close'), 0);
const fresh = socket(); backend.sock = fresh;
const a = backend.rpc('fresh-a'), b = backend.rpc('fresh-b'); await tick();
assert.equal(fresh.sent.length, 1); fresh.emit('data', Buffer.from('new-a\x1a')); assert.equal(await a, 'new-a');
await tick(); assert.equal(fresh.sent.length, 2); fresh.emit('data', Buffer.from('new-b\x1a')); assert.equal(await b, 'new-b');
const closed = assert.rejects(backend.rpc('closing', 5000), /断开/); await tick(); fresh.emit('close'); await closed;
assert.equal(backend.sock, null);
const broken = socket(); broken.write = () => { throw new Error('write failed'); }; backend.sock = broken;
await assert.rejects(backend.rpc('broken'), /write failed/); assert.equal(broken.destroyed, true);
console.log('bridge-rpc: timeout/close/write failure retire the socket and queued requests; fresh connection stays aligned PASS');
