import assert from 'node:assert/strict';
import test from 'node:test';
import { installNativeOpenOcd } from '../../../etm/tools/node-transport/openocd-worker.mjs';
import { OpenOcdProbe } from '../../app/core/openocd-probe.js';
import { FlashView } from '../../app/flash/view.js';

const assertNReset = [0x10, 0, 0x80, 0, 0, 0, 0];
const releaseNReset = [0x10, 0x80, 0x80, 0x50, 0xc3, 0, 0];

// Model only a CMSIS-DAP adapter. Debug transfers always return no ACK.
function resetAdapter({ low = 0, high = 0x80 } = {}){
  const packets = [];
  let last;
  let closed = false;
  const result = bytes => ({ status:'ok', data:new DataView(Uint8Array.from(bytes).buffer) });
  const device = {
    vendorId:0x0d28, productId:0x0204, productName:'CMSIS-DAP', serialNumber:'offline',
    async open(){ this.configuration = { configurationValue:1 }; },
    async close(){ closed = true; },
    async claimInterface(iface){ assert.equal(iface, 0); },
    async releaseInterface(){},
    async controlTransferIn({ value }, length){
      const descriptors = {
        0x100:[18,1,16,2,0xef,2,1,64,40,13,4,2,0,1,0,2,3,1],
        0x200:[9,2,32,0,1,1,0,128,50,9,4,0,0,2,255,0,0,0,7,5,2,2,0,2,0,7,5,0x81,2,0,2,0],
        0x300:[4,3,9,4],
      };
      const text = value === 0x302 ? 'CMSIS-DAP' : 'offline';
      const string = [2 + text.length * 2, 3, ...Array.from(text, char => [char.charCodeAt(0), 0]).flat()];
      return result((descriptors[value] || string).slice(0, length));
    },
    async transferOut(ep, bytes){
      assert.equal(ep, 2);
      last = [...bytes];
      packets.push({ bytes:last, at:performance.now() });
      return { status:'ok', bytesWritten:bytes.length };
    },
    async transferIn(ep){
      assert.equal(ep, 1);
      const [op, arg, mask] = last;
      if (op === 0){
        const data = { 0xf0:[1], 0xfe:[1], 0xff:[64,0] }[arg] || [];
        return result([0, data.length, ...data]);
      }
      if (op === 5 || op === 6) return result([op, 0, 7]);
      if (op === 0x10 && mask === 0x80) return result([op, arg ? high : low]);
      return result([op, op === 2 ? 1 : 0, 0, 0, 0]);
    },
  };
  return { device, packets, closed:() => closed };
}

function viewFor(device){
  globalThis.document = { getElementById:() => null };
  const view = new FlashView();
  const statuses = [];
  view._clearProbeUsers = async () => true;
  view.selectDevice = async () => device;
  view._openTarget = () => assert.fail('global reset must not open the target');
  view._status = (text, kind) => statuses.push({ text, kind });
  view._log = () => {};
  return { view, statuses };
}

test('global reset uses real Wasm SWJ_Pins on an adapter with no target response', async () => {
  const { device, packets, closed } = resetAdapter();
  await installNativeOpenOcd({ getDevices:async () => [device] });
  const { view, statuses } = viewFor(device);
  assert.equal(await view.resetTarget(), true);
  const pulses = packets.filter(({ bytes }) => bytes[0] === 0x10 && bytes[2] === 0x80);
  assert.deepEqual(pulses.map(({ bytes }) => bytes), [assertNReset, releaseNReset]);
  assert.ok(pulses[1].at - pulses[0].at >= 19, 'NRST must be held for the pulse interval');
  assert.ok(!packets.some(({ bytes }) => [5, 6].includes(bytes[0])), 'no target DAP transfers');
  assert.equal(closed(), true);
  assert.equal(view.probe, null);
  assert.equal(statuses[0].kind, 'ok');
});

for (const [name, levels, expected] of [
  ['assertion readback mismatch', { low:0x80 }, /未确认 NRST 已拉低/],
  ['release readback mismatch', { high:0 }, /未确认 NRST 已释放/],
  ['both readbacks mismatch', { low:0x80, high:0 }, error => error instanceof AggregateError &&
    /拉低/.test(error.errors[0].message) && /释放/.test(error.errors[1].message)],
]){
  test(`${name} still sends release, reports the error and closes USB`, async () => {
    const { device, packets, closed } = resetAdapter(levels);
    await installNativeOpenOcd({ getDevices:async () => [device] });
    const { view, statuses } = viewFor(device);
    await assert.rejects(view.resetTarget(), expected);
    assert.deepEqual(packets.map(({ bytes }) => bytes).filter(bytes => bytes[0] === 0x10 && bytes[2] === 0x80),
      [assertNReset, releaseNReset]);
    assert.equal(closed(), true);
    assert.equal(view.probe, null);
    assert.equal(view.busy, false);
    assert.deepEqual(statuses, []);
  });
}

test('flash reset-after-program still runs the target software reset', async () => {
  const nodes = new Map([
    ['f-chip', { value:'stm32f103' }], ['f-speed', { value:'1000' }],
    ['f-base', { value:'0x08000000' }], ['f-verify', { checked:false }],
    ['f-reset', { checked:true }], ['f-result', {}],
  ]);
  globalThis.document = { getElementById:id => nodes.get(id) || null };
  const commands = [];
  const probe = Object.create(OpenOcdProbe.prototype);
  probe.command = async command => commands.push(command);
  probe._readWord = async address => { assert.equal(address, 0xe000edf0); return 0x10001; };
  probe.disconnect = async () => {};
  const view = new FlashView();
  view.file = { name:'fixture.bin', dataB64:'AA==' };
  view._clearProbeUsers = async () => true;
  view._openTarget = async flash => { assert.equal(flash, true); return probe; };
  view._program = async () => ({ bytes:1 });
  view._bar = view._status = () => {};
  assert.equal((await view.flash()).reset, true);
  assert.deepEqual(commands, ['reset run']);
});
