import assert from 'node:assert/strict';
import { AkaLinkHid } from '../../app/hid/probe.js';
const tick = () => new Promise(r => setImmediate(r));
const devices = [];
function device(){
  const listeners = new Set(), sent = [];
  const d = { opened: false, closes: 0, sent,
    open: async () => { d.opened = true; },
    close: async () => { d.opened = false; d.closes++; },
    addEventListener: (_e, fn) => listeners.add(fn), removeEventListener: (_e, fn) => listeners.delete(fn),
    sendReport: async (_id, bytes) => sent.push(bytes[1]),
    reply: cmd => { for (const fn of listeners) fn({ device: d, data: new DataView(Uint8Array.of(0, cmd, 0).buffer) }); },
  }; devices.push(d); return d;
}
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { hid: { addEventListener(){}, removeEventListener(){} } } });
const d = device(), a = new AkaLinkHid(), b = new AkaLinkHid();
await Promise.all([a.open(d), b.open(d)]);
const first = a.xfer(0x32), second = b.xfer(0x32);
await tick(); assert.deepEqual(d.sent, [0x32], 'one command in flight across clients');
d.reply(0x32); await first; await tick(); assert.deepEqual(d.sent, [0x32, 0x32]);
d.reply(0x32); await second;
await a.close(); assert.equal(d.closes, 0, 'other client retains the HID handle');
const active = b.xfer(0x31); const rejected = assert.rejects(active, /关闭/);
await tick(); await b.close(); await rejected; assert.equal(d.closes, 1);

const d2 = device(), d3 = device(), c = new AkaLinkHid(), e = new AkaLinkHid();
await c.open(d2); await e.open(d3);
const x = c.xfer(0x10), y = e.xfer(0x10); await tick();
assert.equal(d2.sent.length, 1); assert.equal(d3.sent.length, 1, 'different devices execute concurrently');
d2.reply(0x10); d3.reply(0x10); await Promise.all([x, y]);
const held = c.xfer(0x31), queued = c.xfer(0x32);
const heldCheck = assert.rejects(held, /关闭/), queuedCheck = assert.rejects(queued, /关闭|替换/);
await tick(); await c.close(); await Promise.all([heldCheck, queuedCheck]);
assert.deepEqual(d2.sent, [0x10, 0x31], 'queued request cannot reclaim a closed session');
await e.close();
console.log('hid-channel: shared response queue, handle reference ownership, independent devices and close cancellation PASS');

const blocked = device(), stalled = new AkaLinkHid();
blocked.sendReport = () => new Promise(() => {});
await stalled.open(blocked);
await assert.rejects(stalled.xfer(0x31, undefined, 30), /没响应/, 'a stalled report write cannot hold the queue forever');
await assert.rejects(stalled.xfer(0x31), /未同步/);
await stalled.close();
console.log('hid-channel: timeout and close settle a response even while sendReport is stalled PASS');

const lateDevice = device(), oldClient = new AkaLinkHid(), peer = new AkaLinkHid();
await oldClient.open(lateDevice); await peer.open(lateDevice);
await assert.rejects(oldClient.xfer(0x36, Uint8Array.of(4), 10), /没响应/);
await assert.rejects(peer.xfer(0x36, Uint8Array.of(1, 0)), /未同步/);
assert.deepEqual(lateDevice.sent, [0x36], 'no new same-command request can consume the old response');
lateDevice.reply(0x36); // only the orphan consumes this reply; no caller receives it
const recovered = peer.xfer(0x36, Uint8Array.of(1, 0)); await tick();
assert.deepEqual(lateDevice.sent, [0x36, 0x36]); lateDevice.reply(0x36); await recovered;
await oldClient.close(); await peer.close();

const unsettled = device(), pendingNative = new AkaLinkHid(); let releaseWrite;
unsettled.sendReport = () => new Promise(r => { releaseWrite = r; });
await pendingNative.open(unsettled);
await assert.rejects(pendingNative.xfer(0x31, undefined, 10), /没响应/);
unsettled.reply(0x31);
await assert.rejects(pendingNative.xfer(0x31), /未同步/, 'late reply alone cannot release an unsettled native write');
releaseWrite(); await tick();
unsettled.sendReport = async () => {};
const fresh = pendingNative.xfer(0x31); await tick(); unsettled.reply(0x31); await fresh; await pendingNative.close();
console.log('hid-channel: timed-out shared channel quarantines late replies until native write and reply settle PASS');

{
 const d=device(),client=new AkaLinkHid();let finish,attempts=0;
 d.close=()=>{attempts++;return new Promise((resolve,reject)=>{finish={resolve,reject};});};
 await client.open(d);
 const first=client.close(),second=client.close();
 const checks=[assert.rejects(first,/close failed/),assert.rejects(second,/close failed/)];
 await tick();assert.equal(attempts,1);await assert.rejects(client.xfer(0x31),/正在关闭/);
 finish.reject(new Error('close failed'));await Promise.all(checks);
 assert.equal(client.device,d);assert.equal(client.connected,true);
 const reply=client.xfer(0x31);await tick();d.reply(0x31);await reply;
 const retry=client.close();await tick();d.opened=false;finish.resolve();await retry;
 assert.equal(attempts,2);assert.equal(client.device,null);
}
{
 const d=device(),client=new AkaLinkHid();await client.open(d);
 d.close=async()=>{throw new Error('unplugged handle');};
 client._handleDisconnect({device:d});await tick();
 assert.equal(client.device,null,'physical unplug does not retain a dead handle');
}
console.log('hid-channel: native close failure retains shared registration for retry; duplicate close coalesces PASS');
