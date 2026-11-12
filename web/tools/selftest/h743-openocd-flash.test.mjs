import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { OpenOcdTargetSession } from '../../app/core/openocd-session.js';
import { makeOpenOcdProfile } from '../../app/core/openocd-profile.js';
import { createOpenOcdDispatcher } from '../../app/core/openocd-worker.mjs';
import { OpenOcdH7FlashProbe } from '../../app/flash/openocd-h7.js';

function h743FlashUsb({ ap0Id, ap2Id, flashKb = 2048, control = 0x00100000 }){
  const ports = new Map([[0,{ csw:0, tar:0 }],[2,{ csw:0, tar:0 }]]);
  const reads = [], writes = [], packets = [];
  let select = 0;
  const words = new Map([[0xe000edf0,0x01010000],[0xe0002000,0x10000080],[0xe0001000,0x40000000]]);
  const toWord = v => [v & 255, v >>> 8 & 255, v >>> 16 & 255, v >>> 24];
  const transfer = bytes => ({ status:'ok', data:new DataView(Uint8Array.from(bytes).buffer) });
  function access(request, value){
    const read = request & 2;
    let register = request & 12;
    if (!(request & 1)){
      if (!read && register === 8) select = value;
      if (read) return register === 0 ? 0x6ba02477 : register === 4 ? 0xf0000000 : 0;
      return;
    }
    const ap = select >>> 24;
    const port = ports.get(ap);
    assert.ok(port, `AP ${ap}`);
    register |= select & 0xf0;
    if (register === 0){
      if (read) return port.csw | 0x40;
      port.csw = value;
    } else if (register === 4){
      if (read) return port.tar;
      port.tar = value;
    } else if (register === 12 || register >= 0x10 && register <= 0x1c){
      const address = (register === 12 ? port.tar : (port.tar & ~15) + (register & 12)) >>> 0;
      if (register === 12 && (port.csw & 0x30) === 0x10) port.tar += 1 << (port.csw & 7);
      if (!read){
        writes.push([ap,address,value]);
        if (ap === 2 && address === 0xe00e1004) control = value;
        else if (address === 0xe000edf0) words.set(address,0x01010000 | (value & 15) | ((value & 2) << 16));
        else words.set(address,value);
        return;
      }
      reads.push([ap,address]);
      if (ap === 2 && address === 0xe00e1000) return ap2Id;
      if (ap === 2 && address === 0xe00e1004) return control;
      if (ap === 0 && address === 0x5c001000) return ap0Id;
      if (ap === 0 && address === 0x1ff1e880) return flashKb;
      if (ap === 0 && address === 0xe000ed00) return (ap2Id & 0xfff) !== 0x450 || (control & 0x00600007) === 0x00600007 ? 0x411fc271 : 0;
      return words.get(address) ?? 0;
    } else if (register === 0xfc && read) return ap === 0 ? 0x84770004 : 0x54770002;
    return 0;
  }
  return {
    reads, writes, get debugControl(){ return control; },
    vendorId:0x0d28, productId:0x0204, productName:'CMSIS-DAP', serialNumber:'H743-model',
    async open(){ this.configuration = { configurationValue:1 }; },
    async close(){},
    async claimInterface(number){ assert.equal(number,0); },
    async releaseInterface(){},
    async controlTransferIn({value}, length){
      const descriptors = {
        0x100:[18,1,16,2,0xef,2,1,64,40,13,4,2,0,1,0,2,3,1],
        0x200:[9,2,32,0,1,1,0,128,50,9,4,0,0,2,255,0,0,0,7,5,2,2,0,2,0,7,5,0x81,2,0,2,0],
        0x300:[4,3,9,4],
      };
      const name = value === 0x302 ? 'CMSIS-DAP' : this.serialNumber;
      return transfer((descriptors[value] || [2 + name.length * 2, 3, ...Array.from(name,c=>[c.charCodeAt(0),0]).flat()]).slice(0,length));
    },
    async transferOut(endpoint, bytes){
      assert.equal(endpoint,2);
      const [op,arg] = bytes;
      const view = new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength);
      if (op === 0x05 || op === 0x06){
        const block = op === 0x06;
        const count = block ? view.getUint16(2,true) : bytes[2];
        const reply = block ? [op,bytes[2],bytes[3],1] : [op,count,1];
        let offset = block ? 5 : 3;
        for (let n=0;n<count;n++){
          const request = block ? bytes[4] : bytes[offset++];
          assert.equal(request & ~15,0);
          if (request & 2) reply.push(...toWord(access(request)));
          else {
            access(request, view.getUint32(offset,true));
            offset += 4;
          }
        }
        packets.push(reply);
      } else if (op === 0){
        const data = { 0xf0:[1], 0xfe:[8], 0xff:[0,2] }[arg] || [];
        packets.push([0,data.length,...data]);
      } else packets.push([op, op === 2 ? 1 : 0]);
      return { status:'ok',bytesWritten:bytes.length };
    },
    async transferIn(endpoint){
      assert.equal(endpoint,1);
      assert.ok(packets.length);
      return transfer(packets.shift());
    },
  };
}

test('real Wasm H743 Flash probes AP2 when the AP0 DBGMCU alias returns zero', async t => {
  const wasmBinary = await readFile(new URL('../../app/core/openocd-runtime/openocd-core.wasm', import.meta.url));
  let selected, device;
  const old = Object.getOwnPropertyDescriptor(globalThis,'navigator');
  Object.defineProperty(globalThis,'navigator',{ configurable:true,value:{ usb:{ async getDevices(){ return selected ? [device] : []; } } } });
  t.after(() => Object.defineProperty(globalThis,'navigator',old));
  t.mock.method(OpenOcdTargetSession,'createWorker',() => {
    const worker = { terminate(){} };
    worker.postMessage = createOpenOcdDispatcher({ wasmBinary,select:value => { selected = value; },
      post:data => queueMicrotask(() => worker.onmessage({ data })) });
    return worker;
  });
  const cfg = makeOpenOcdProfile({ family:'stm32h7',clockKhz:10000,flash:true });
  for (const [ap0Id, ap2Id, legacy, flashKb] of [[0,0x20036450,true],[0,0x20036450,false],
    [0x20036450,0,false],[0,0,false],[0,0x20036450,false,0]]){
    device = h743FlashUsb({ ap0Id,ap2Id,flashKb });
    const profile = legacy ? { ...cfg,script:cfg.script.replaceAll(' -dbgmcu-target workbench.h7dbg','') } : cfg;
    const probe = new OpenOcdH7FlashProbe(device,profile);
    try {
      await probe.connect();
      if (legacy || !(ap0Id || ap2Id)){
        await assert.rejects(probe.command('flash probe 0'),/Cannot identify target/);
      } else if (flashKb === 0){
        await assert.rejects(probe.command('flash probe 0'),/valid STM32H74x\/75x Flash size/);
      } else {
        for (const bank of [0,1]) await probe.command(`flash probe ${bank}`);
        const banks = await probe.command('capture {flash banks}');
        assert.match(banks,/at 0x08000000, size 0x00100000/);
        assert.match(banks,/at 0x08100000, size 0x00100000/);
      }
      assert.equal(device.reads.some(([ap,address]) => ap === 0 && address === 0x5c001000),legacy || !ap2Id);
    } finally { await probe.disconnect(); }
    assert.equal(device.debugControl,0x00100000);
    assert.deepEqual(device.writes.filter(([ap]) => ap === 2), ap2Id ?
      [[2,0xe00e1004,0x00700007],[2,0xe00e1004,0x00100000]] : []);
    assert.ok(!device.writes.some(([,address]) => address >= 0x52002000 && address < 0x52002200));
  }
});

test('H7 reset checks physical DHCSR after transient failures without resetting again', async t => {
  const probe = new OpenOcdH7FlashProbe(null, {arch:'arm',clockKhz:10000});
  const command = t.mock.method(probe,'command',async () => '');
  const samples = [new Error('AP0 unavailable'),0,0x90001,0x1010001];
  t.mock.method(probe,'_readWord',async address => {
    assert.equal(address,0xe000edf0);
    const sample = samples.shift();
    if (sample instanceof Error) throw sample;
    return sample;
  });
  assert.match(await probe.reset(),/复位/);
  assert.deepEqual(command.mock.calls.map(call => call.arguments),[['reset run']]);
  assert.deepEqual(samples,[]);
});
