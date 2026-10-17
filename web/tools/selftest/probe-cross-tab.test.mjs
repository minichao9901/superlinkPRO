import assert from 'node:assert/strict';
import { ProbeManager, ProbeCancelled } from '../../app/core/probe-manager.js';
import { AkaLinkHid } from '../../app/hid/probe.js';
const tick = () => new Promise(r => setImmediate(r));

// Deterministic Web Locks substitute: independent JS realms share this browser service.
const chains = new Map();
const locks = { request(name, { signal }, fn){
  let started = false, onAbort;
  const cancelled = new Promise((_, reject) => {
    onAbort = () => { if (!started) reject(new DOMException('cancelled', 'AbortError')); };
    signal.addEventListener('abort', onAbort, { once: true });
    if (signal.aborted) onAbort();
  });
  const task = (chains.get(name) || Promise.resolve()).then(async () => {
    if (signal.aborted) throw new DOMException('cancelled', 'AbortError');
    started = true; return await fn();
  });
  chains.set(name, task.catch(() => {}));
  return Promise.race([task, cancelled]).finally(() => signal.removeEventListener('abort', onAbort));
} };
const events = []; let aActive = false, bActive = false;
let a, b;
a = new ProbeManager({ locks, beforeAcquire: () => b.releaseOthers(null, 'page A') });
b = new ProbeManager({ locks, beforeAcquire: () => a.releaseOthers(null, 'page B') });
a.register('scope', { resources: ['engine'], active: () => aActive, release: async () => { aActive = false; events.push('A:close'); } });
b.register('dbg', { resources: ['engine'], active: () => bActive, release: async () => { bActive = false; events.push('B:close'); } });
const first = a.run('scope', async () => { aActive = true; events.push('A:open'); });
const loser = b.run('dbg', () => assert.fail('simultaneous losing tab must not open a handle'));
const rejected = assert.rejects(loser, ProbeCancelled);
await first; await rejected;
assert.deepEqual(events, ['A:open']);
await b.run('dbg', async () => { bActive = true; events.push('B:open'); });
assert.deepEqual(events, ['A:open', 'A:close', 'B:open']);
assert.equal(aActive, false); assert.equal(bActive, true);
await b.releaseOthers(null);
console.log('probe-cross-tab: simultaneous acquisition has one winner, later handoff drains the previous owner PASS');

Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { locks, hid: { addEventListener(){}, removeEventListener(){} } } });
function handle(){
  const listeners = new Set();
  const d = { opened: false, vendorId: 0x0d28, productId: 0x0204, sent: 0,
    open: async () => { d.opened = true; }, close: async () => { d.opened = false; },
    addEventListener: (_e, fn) => listeners.add(fn), removeEventListener: (_e, fn) => listeners.delete(fn),
    sendReport: async () => { d.sent++; },
    reply: () => { for (const fn of listeners) fn({ device: d, data: new DataView(Uint8Array.of(0, 0x31, 0).buffer) }); },
  }; return d;
}
// Distinct HIDDevice objects model two tabs; per-object queues alone cannot protect these.
const da = handle(), db = handle(), ha = new AkaLinkHid(), hb = new AkaLinkHid();
await ha.open(da); await hb.open(db);
const ra = ha.xfer(0x31), rb = hb.xfer(0x31);
await tick(); assert.equal(da.sent, 1); assert.equal(db.sent, 0);
da.reply(); await ra; await tick(); assert.equal(db.sent, 1);
db.reply(); await rb;
const held = ha.xfer(0x31), waiting = hb.xfer(0x31); const cancelled = assert.rejects(waiting, /关闭/);
await tick(); await hb.close(); await cancelled;
assert.equal(db.sent, 1, 'closing a tab cancels its command waiting for another tab');
da.reply(); await held; await ha.close();
console.log('probe-cross-tab: passive HID command responses serialize across tab handles and closing cancels a lock waiter PASS');
