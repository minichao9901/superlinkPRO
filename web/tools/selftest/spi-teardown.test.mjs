import assert from 'node:assert/strict';
import { WebUsbSpiTransport } from '../../app/spi/transport.js';
import { UsbLease } from '../../app/core/usb-device.js';
import { SpiSession } from '../../app/spi/session.js';
import * as P from '../../app/spi/protocol.js';

const tick = () => new Promise(resolve => setImmediate(resolve));
function fixture(){
  const reads = [], events = [];
  const device = { opened: false,
    configuration: { interfaces: [{ interfaceNumber: 5, alternates: [{ endpoints: [
      { endpointNumber: 11, direction: 'in', type: 'bulk' },
      { endpointNumber: 11, direction: 'out', type: 'bulk' },
    ] }] }] },
    async open(){ this.opened = true; }, async claimInterface(){}, async clearHalt(){},
    async releaseInterface(){ events.push('release'); },
    async close(){ events.push('close'); this.opened = false; },
    async reset(){ events.push('reset'); while (reads.length) reads.shift()({ status: 'ok' }); },
    transferIn(){ return new Promise(resolve => reads.push(resolve)); },
  };
  return { device, reads, events };
}
function response(){
  const res = new Uint8Array(63);
  res.set([12, P.HID_CMD, P.ACT.DRAIN]);
  new DataView(res.buffer).setUint32(7, 0x314e5244, true);
  return res;
}
assert.deepEqual([...P.hidData.drain(255)], [9, 16]);
assert.equal(P.supportsDrain(response()), true);
assert.equal(P.supportsDrain(new Uint8Array(63)), false);
const truncated = response(); truncated[0] = 8;
assert.equal(P.supportsDrain(truncated), false);
for (const supported of [true, false]){
  const { device, reads, events } = fixture();
  const peer = new UsbLease(device, 'scope'); await peer.open();
  const session = new SpiSession();
  session.hid = { connected: true, async xfer(cmd, data){
    assert.equal(cmd, P.HID_CMD); assert.deepEqual([...data], [P.ACT.DRAIN, 4]);
    if (supported) while (reads.length) reads.shift()({ status: 'ok', data: new DataView(new ArrayBuffer(8)) });
    return supported ? response() : new Uint8Array(63);
  } };
  const transport = new WebUsbSpiTransport(device, { drainReads: count => session._drainReads(count) });
  await transport.open();
  let delivered = 0; await transport.start(() => delivered++);
  await tick(); assert.equal(reads.length, 4);
  if (supported){
    await Promise.all([transport.stop(), transport.stop()]);
    await assert.rejects(transport.sendRaw(Uint8Array.of(1)), /已停止/);
    await transport.close();
    assert.equal(delivered, 0, 'late teardown replies must not reach the stream parser');
    assert.equal(transport.stalledInFlight, 0);
    assert.ok(device.opened); assert.deepEqual(events, ['release']);
    await transport.close(); assert.ok(device.opened, 'repeated close cannot affect the peer');
    await peer.close();
  } else {
    await assert.rejects(transport.close(), /先断开 scope/);
    assert.equal(transport.stalledInFlight, 4);
    assert.ok(!events.includes('reset'));
    assert.match(transport.lastError, /更新探针固件/);
    await peer.close(); await transport.close();
    assert.ok(events.includes('reset'));
  }
}
// A native OUT that timed out stays owned even when all INs drain successfully.
{
  const { device, reads, events } = fixture();
  const peer = new UsbLease(device, 'scope'); await peer.open();
  let finish;
  device.transferOut = () => new Promise(resolve => { finish = resolve; });
  const t = new WebUsbSpiTransport(device, { outTimeoutMs: 5, drainReads: async () => {
    while (reads.length) reads.shift()({ status: 'ok' });
  } });
  await t.open(); await t.start(() => {});
  await assert.rejects(t.sendRaw(Uint8Array.of(1)), /写超时/);
  await assert.rejects(t.close(), /先断开 scope/);
  assert.equal(t.stalledInFlight, 1); assert.ok(!events.includes('reset'));
  finish({ status: 'ok', bytesWritten: 1 }); await tick();
  await peer.close(); await t.close();
}
console.log('spi-teardown: EP11 drain preserves peer, cancels delivery/rearm, guards old firmware and timed-out OUT PASS');
{
 const s=new SpiSession(),events=[];let fail=true;
 const hid={close:async()=>{if(fail)throw new Error('HID close failed');}};
 s.hid=hid;s._disableBridge=async()=>{};
 s.probeManager={cancel(){},fail:(owner,e)=>events.push([owner,e.message]),forget:owner=>events.push(['forget',owner])};
 await assert.rejects(s.teardown(),/HID close failed/);assert.equal(s.hid,hid);
 assert.deepEqual(events,[['spi','HID close failed']]);
 fail=false;await s.teardown();assert.equal(s.hid,null);assert.deepEqual(events.at(-1),['forget','spi']);
}
console.log('spi-teardown: HID close failure retains handle and ownership until successful retry PASS');
