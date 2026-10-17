import assert from 'node:assert/strict';
import { ProbeManager, ProbeCancelled } from '../../app/core/probe-manager.js';
const gate = () => { let resolve; const promise = new Promise(r => resolve = r); return { promise, resolve }; };
const tick = () => new Promise(r => setImmediate(r));
const m = new ProbeManager({ locks: null });
const states = new Map(), events = [];
for (const [id, resources] of [['scope', ['engine', 'usb']], ['rtt', ['engine', 'usb']], ['i2c', ['i2c-pins']]])
  m.register(id, { resources, active: () => !!states.get(id), release: async () => { events.push(`close:${id}`); states.set(id, false); } });
const held = gate();
const a = m.run('scope', async () => { events.push('scope:setup'); await held.promise; states.set('scope', true); events.push('scope:ready'); });
const b = m.run('rtt', async () => { states.set('rtt', true); events.push('rtt:ready'); });
await tick(); assert.deepEqual(events, ['scope:setup']);
held.resolve(); await Promise.all([a, b]);
assert.deepEqual(events, ['scope:setup', 'scope:ready', 'close:scope', 'rtt:ready']);
await m.run('i2c', async () => states.set('i2c', true));
assert.deepEqual(m.summary().owners, ['rtt', 'i2c'], 'disjoint resources coexist');
const held2 = gate(); const c = m.run('rtt', () => held2.promise);
const cancelled = m.run('scope', () => assert.fail('cancelled setup must never touch hardware'));
const rejected = assert.rejects(cancelled, ProbeCancelled);
m.cancel('scope'); held2.resolve(); await c; await rejected;
await assert.rejects(m.run('scope', () => assert.fail(), { policy: 'reject' }), /共享资源/);
let busy = true;
m.register('flash', { label: '烧录器', resources: ['engine', 'usb'], active: () => busy, protected: () => busy, release: () => assert.fail() });
await assert.rejects(m.run('scope', () => assert.fail()), /烧录器/);
await assert.rejects(m.releaseOthers(null), /烧录器/);
busy = false;
m.clients.get('rtt').release = async () => { throw new Error('STOP failed'); };
await assert.rejects(m.run('scope', () => assert.fail()), /STOP failed/);
assert.ok(m.leases.has('rtt'), 'failed release retains resource ownership');
await assert.rejects(m.run('scope', () => assert.fail()), /释放尚未确认/, 'later takeover cannot silently bypass a failed stop');
m.clients.get('rtt').release = async () => states.set('rtt', false);
m.confirm('rtt');
await m.releaseOthers(null); assert.deepEqual(m.summary().owners, []);
console.log('probe-manager: serialized setup, coexistence, cancellation, busy flash, failed release PASS');

// Incoming handoff cancels a start waiting for another tab's control lock, avoiding a queue deadlock.
const waiting = gate();
const n = new ProbeManager({ locks: { request: async (_name, { signal }, fn) => {
  await Promise.race([waiting.promise, new Promise((_, reject) => signal.addEventListener('abort', () => reject(new DOMException('cancelled', 'AbortError')), { once: true }))]);
  return fn();
} } });
n.register('scope', { resources: ['engine'], release: async () => {}, active: () => false });
const pending = n.run('scope', () => assert.fail()); const checked = assert.rejects(pending, ProbeCancelled);
await tick(); await n.releaseOthers(null); await checked;
console.log('probe-manager: cross-tab release cancels queued Web Lock acquisition PASS');

const k = new ProbeManager({ locks: null });
let reconnect, scopeActive = false;
k.register('scope', { resources: ['engine'], active: () => scopeActive, release: async () => {
  k.cancel('scope'); await reconnect.catch(() => {}); scopeActive = false;
} });
k.register('rtt', { resources: ['engine'], active: () => false, release: async () => {} });
await k.run('scope', async () => { scopeActive = true; });
const takeover = k.run('rtt', async () => {});
reconnect = k.run('scope', () => assert.fail('cancelled reconnect must not start'));
const cancelledReconnect = assert.rejects(reconnect, ProbeCancelled);
await takeover; await cancelledReconnect;
console.log('probe-manager: preemption drains cancelled reconnect without waiting for its own queue PASS');
