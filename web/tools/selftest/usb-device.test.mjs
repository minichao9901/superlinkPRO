import assert from 'node:assert/strict';
import { UsbLease, usbDeviceInUse, setUsbResetGuard } from '../../app/core/usb-device.js';
const events = [];
const device = { opened: false, configuration: null,
  async open(){ events.push('open'); this.opened = true; },
  async selectConfiguration(){ this.configuration = {}; events.push('config'); },
  async claimInterface(i){ events.push(`claim:${i}`); },
  async releaseInterface(i){ events.push(`release:${i}`); },
  async reset(){ events.push('reset'); }, async close(){ events.push('close'); this.opened = false; },
};
const dap = new UsbLease(device, 'dap'), scope = new UsbLease(device, 'scope'), spi = new UsbLease(device, 'spi');
await Promise.all([dap.open(), scope.open(), spi.open()]);
await dap.claim(0, [0x81, 2]); await scope.claim(0, [0x83]); await spi.claim(5, [0x8b, 11]);
assert.deepEqual(events, ['open', 'config', 'claim:0', 'claim:5']);
await assert.rejects(dap.reset(), /scope.*spi/); assert.ok(!events.includes('reset'));
const duplicate = new UsbLease(device, 'duplicate'); await assert.rejects(duplicate.claim(0, [0x81]), /端点/); await duplicate.close();
await scope.close(); assert.ok(!events.includes('release:0'), 'shared interface remains claimed');
await dap.close(); assert.ok(device.opened); assert.equal(events.at(-1), 'release:0');
setUsbResetGuard(() => { throw new Error('CDC still open'); });
await assert.rejects(spi.close({dirty:true}), /CDC still open/); assert.ok(usbDeviceInUse(device));
setUsbResetGuard(null); await spi.close({dirty:true});
assert.deepEqual(events.slice(-2), ['reset', 'close']); assert.ok(!usbDeviceInUse(device));
// Enumeration aliases with a nonempty serial share the canonical handle.
const first = {...device, vendorId: 0xd28, productId: 0x204, serialNumber: 'lease-test', opened:false};
const alias = {...first}; const a = new UsbLease(first, 'a'), b = new UsbLease(alias, 'b');
assert.equal(a.device, b.device); await a.open(); assert.ok(usbDeviceInUse(alias)); await a.close();
const noSerialA = new UsbLease({...device}, 'a'), noSerialB = new UsbLease({...device}, 'b');
assert.notEqual(noSerialA.device, noSerialB.device);
console.log('usb-device: shared lifecycle, endpoint exclusion, interface refs, guarded reset, alias identity PASS');
// A timed-out native lifecycle operation must not later close a newly acquired peer.
let releaseOpen;
const slowDevice = {...device, opened:false, open:() => new Promise(r => { releaseOpen = () => {slowDevice.opened=true;r();}; })};
const slow = new UsbLease(slowDevice, 'slow', {timeoutMs:5}), peer = new UsbLease(slowDevice, 'peer');
await assert.rejects(slow.open(), /超时/);
await assert.rejects(peer.open(), /仍未退出/);
releaseOpen(); slowDevice.open = async () => { slowDevice.opened = true; }; await new Promise(r => setImmediate(r));
await assert.rejects(peer.open(), /独占复位/);
await slow.close(); await peer.open(); await peer.close();
console.log('usb-device: native timeout retains ownership and prevents late lifecycle races PASS');
const abandonedDevice={...device,opened:false};
const live=new UsbLease(abandonedDevice,'live'),orphan=new UsbLease(abandonedDevice,'failed setup');
await live.open();await live.claim(5,[0x8b]);await orphan.open();await orphan.claim(0,[0x81]);
await assert.rejects(orphan.close({dirty:true}),/先断开 live/);orphan.abandon();
await assert.rejects(new UsbLease(abandonedDevice,'another').reset(),/先断开 live/,'abandoned setup cannot authorize resetting a live peer');
await live.close();const next=new UsbLease(abandonedDevice,'next');await next.open();await next.claim(0,[0x81]);await next.close();
assert.ok(!usbDeviceInUse(abandonedDevice),'exclusive recovery retires abandoned setup leases');
console.log('usb-device: failed setup retains requests, later exclusive reconnect recovers abandoned handles PASS');
const listeners=new Set();Object.defineProperty(globalThis,'navigator',{value:{usb:{addEventListener:(_n,fn)=>listeners.add(fn),removeEventListener:(_n,fn)=>listeners.delete(fn)}},configurable:true});
const unplugged={...device,opened:false,vendorId:0xd28,productId:0x204,serialNumber:'replug-test'};
const oldHandle=new UsbLease(unplugged,'old');await oldHandle.open();await oldHandle.claim(0,[0x81]);
for(const listener of [...listeners])listener({device:unplugged});await oldHandle.close();
const replugged={...unplugged,opened:false};const newHandle=new UsbLease(replugged,'new');
assert.equal(newHandle.device,replugged,'same serial after unplug must use the new native device handle');
await newHandle.open();await newHandle.claim(0,[0x81]);await newHandle.close();
console.log('usb-device: physical unplug invalidates canonical handle before same-serial reconnect PASS');
const twiceDevice={...device,opened:false};const closed=new UsbLease(twiceDevice,'closed'),remaining=new UsbLease(twiceDevice,'remaining');
await closed.open();await closed.claim(0,[0x81]);await remaining.open();await remaining.claim(5,[0x8b]);
await closed.close();const count=events.filter(e=>e==='close').length;await closed.close();await new UsbLease(twiceDevice,'unused').close();
assert.ok(twiceDevice.opened);assert.equal(events.filter(e=>e==='close').length,count,'duplicate/unused close cannot close a live peer');await remaining.close();
console.log('usb-device: duplicate and unacquired close are harmless to remaining peers PASS');
