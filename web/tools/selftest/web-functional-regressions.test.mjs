import assert from 'node:assert/strict';
import test from 'node:test';
import * as nor from '../../app/spi/flash.js';
import { SpiBusView } from '../../app/spi/bus-view.js';
import { ScopeView } from '../../app/scope/view.js';
import { DebugSession } from '../../app/dbg/session.js';
import { FPB, compAddr } from '../../app/dbg/bp.js';
import { OpenOcdProbe } from '../../app/core/openocd-probe.js';
import { FlashView } from '../../app/flash/view.js';
import { Rtt } from '../../app/rtt/protocol.js';
import { MockProbe } from '../../app/rtt/mock.js';
import { RttView } from '../../app/rtt/view.js';

test('NOR validates the complete range before emitting any write or erase', async () => {
  assert.equal(nor.programPages(0xfffff0, new Uint8Array(16)).length, 1);
  assert.ok(nor.readItems(0xffffff, 1).length);
  assert.throws(() => nor.programItems(0xfffff0, new Uint8Array(32)), /三字节地址/);
  assert.throws(() => nor.readItems(0xffffff, 2), /三字节地址/);
  assert.throws(() => nor.eraseItems(0x1000000), /三字节地址/);
  const view = Object.create(SpiBusView.prototype);
  view.flArmed = () => true;
  view.flAddr = () => 0xffff00;
  view.session = { sendFrames(){ assert.fail('越界测速不应发送帧'); } };
  globalThis.document = { getElementById: () => ({ value:'1' }) };
  await assert.rejects(view.flWriteBench(), /三字节地址/);
});

test('Scope CSV preserves small float values including the exponent', async t => {
  let blob;
  t.mock.method(URL, 'createObjectURL', value => { blob = value; return 'blob:test'; });
  globalThis.document = {
    createElement: () => ({ click(){}, remove(){} }),
    body: { appendChild(){} },
  };
  const samples = [1e-10, -1e-20, 1.25, 0];
  const view = Object.create(ScopeView.prototype);
  view.setStatusText = () => {};
  view.store = {
    count: samples.length, vars:[{name:'voltage'}], timeAt:i => i,
    channels:[{value:i => samples[i]}],
  };
  view.exportCsv();
  const rows = (await blob.text()).trim().split('\n').slice(1);
  assert.deepEqual(rows.map(row => Number(row.split(',')[1])), samples);
});

test('ARM disconnect retries unconfirmed FPB cleanup and restores running state', async () => {
  const session = new DebugSession();
  const probe = Object.create(OpenOcdProbe.prototype);
  session.probe = probe;
  session.caps = { numCode:2, rev:1 };
  session.bps = [0x08001000, 0x08002000];
  session.bpNotes.set(0x08002000, 'keep until confirmed');
  const registers = new Map([[compAddr(0), 1], [compAddr(1), 1], [FPB.CTRL, 1]]);
  let halted = false;
  let fail = true;
  let closed = false;
  probe.isHalted = async () => halted;
  probe.halt = async () => { halted = true; };
  probe.run = async () => { halted = false; };
  probe.disconnect = async () => { closed = true; };
  probe._readWord = async addr => registers.get(addr);
  probe.writeMem = async (addr, bytes) => {
    if (addr === compAddr(1) && fail) throw Error('injected FPB write failure');
    registers.set(addr, new DataView(bytes.buffer, bytes.byteOffset, 4).getUint32(0, true));
  };
  await assert.rejects(session.disconnect(), /injected/);
  assert.equal(registers.get(compAddr(1)), 1);
  assert.equal(session.bps.length, 2);
  assert.equal(session.bpNotes.size, 1);
  assert.equal(closed, false);
  fail = false;
  await session.disconnect();
  assert.equal(registers.get(compAddr(1)), 0);
  assert.equal(registers.get(FPB.CTRL) & 1, 0);
  assert.equal(session.probe, null);
  assert.equal(halted, false);
  assert.equal(closed, true);
});

test('queued Flash operation uses its original target, image and options', async () => {
  const nodes = {
    'f-chip':{value:'stm32f103'}, 'f-speed':{value:'1000',disabled:true},
    'f-base':{value:'0x08000000'}, 'f-verify':{checked:true}, 'f-reset':{checked:false},
    'f-result':{},
  };
  globalThis.document = { getElementById:id => nodes[id] };
  const view = new FlashView();
  view.file = {name:'fixture.bin',dataB64:'AQIDBA=='};
  view._status = view._bar = () => {};
  let handoff;
  view._clearProbeUsers = () => new Promise(resolve => { handoff = resolve; });
  view._openTarget = async (_flash, cfg) => {
    assert.equal(cfg.builtin, 'stm32f103');
    assert.equal(cfg.clockKhz, 1000);
    return {disconnect:async () => {}};
  };
  view._program = async (_probe, regions, verify) => {
    assert.equal(verify, true);
    assert.equal(regions[0].addr, 0x08000000);
    assert.deepEqual([...regions[0].data], [1,2,3,4]);
    return {bytes:4};
  };
  const operation = view.flash();
  assert.equal(nodes['f-chip'].disabled, true);
  nodes['f-chip'].value = 'stm32h7';
  nodes['f-speed'].value = '20000';
  nodes['f-base'].value = '0x08100000';
  nodes['f-verify'].checked = false;
  nodes['f-reset'].checked = true;
  handoff(true);
  assert.deepEqual(await operation, {bytes:4,reset:false});
  assert.equal(nodes['f-speed'].disabled, true);
  assert.equal(view.busy, false);
});

test('DHCSR retirement and reset flags do not cause a clock downgrade', async () => {
  const session = new DebugSession();
  const values = [0x03030003, 0x01030003, 0x00030003];
  session.clockHz = 10000000;
  session.probe = { _readWord:async () => values.shift(), setClock:async () => assert.fail('unexpected downgrade') };
  assert.equal((await session.verifyClock()).ok, true);
});

test('F103 high-speed tuning verifies intermediate SWD clocks and stops on a failed stage', async () => {
  for (const failAt of [null, 36000000]){
    const session = new DebugSession();
    const probe = Object.create(OpenOcdProbe.prototype);
    probe.cfg = { family:'stm32f103' };
    session.probe = probe;
    session.clockHz = 1000000;
    const clocks = [];
    const checks = [];
    probe.setClock = async hz => { clocks.push(hz); probe.clockHz = hz; };
    probe._readWord = async () => {
      checks.push(probe.clockHz);
      return probe.clockHz === failAt ? 0 : 0x00030003;
    };
    const result = await session.setAdapterClock(60000);
    const expected = failAt ? [10000000, 20000000, failAt] : [10000000, 20000000, 36000000, 45000000, 60000000];
    assert.deepEqual(checks, expected.flatMap(hz => [hz, hz, hz]));
    assert.deepEqual(clocks, failAt ? [...expected, 1000000] : expected);
    assert.equal(result.hz, failAt ? 1000000 : 60000000);
    assert.equal(result.ok, failAt === null);
  }
});

test('invalid SPI read benchmark input releases the shared busy flag', async () => {
  globalThis.document = { getElementById:() => ({value:'bad!'}) };
  const view = Object.create(SpiBusView.prototype);
  view.refreshButtons = view.flOut = () => {};
  view.flMode = () => ({});
  view.session = {
    setBusy(busy){ this.busy = busy; }, log(){}, async pollStatus(){},
    sendFrames(){ assert.fail('invalid address must not issue a transfer'); },
  };
  await view.flBench();
  assert.equal(view.session.busy, false);
});

test('concurrent RTT sends survive a cursor wrap and do not overwrite each other', async () => {
  const probe = new MockProbe();
  const rtt = new Rtt(probe, {addr:probe.cbAddr});
  await rtt.init();
  const initial = probe.downSize - 2;
  const cursor = probe.cbAddr + 60;
  probe._setU32(cursor, initial);
  probe._setU32(cursor + 4, initial);
  const encoder = new TextEncoder();
  assert.deepEqual(await Promise.all([
    rtt.writeDown(0, encoder.encode('ABC')),
    rtt.writeDown(0, encoder.encode('DEF')),
  ]), [3, 3]);
  assert.equal(probe._getU32(cursor), 4);
  const tail = await probe.readMem(probe.downBuf + initial, 2);
  const head = await probe.readMem(probe.downBuf, 4);
  assert.equal(new TextDecoder().decode(Uint8Array.of(...tail, ...head)), 'ABCDEF');
});

test('RTT recovers the Wasm debug port after repeated transport errors', async () => {
  const memory = new MockProbe();
  const probe = Object.create(OpenOcdProbe.prototype);
  let recoveries = 0;
  probe.disconnect = async () => {};
  probe.connect = async () => { recoveries++; };
  probe.readMem = async (...args) => {
    if (!recoveries) throw Error('FAULT injected until recovery');
    return memory.readMem(...args);
  };
  assert.equal((await Rtt.validate(probe, memory.cbAddr)).ok, true);
  assert.equal(recoveries, 1);
});

test('RTT disconnect waits for an in-flight downlink cursor commit', async () => {
  globalThis.document = { getElementById:() => ({classList:{remove(){},add(){}}}) };
  const probe = new MockProbe();
  const rtt = new Rtt(probe, {addr:probe.cbAddr});
  await rtt.init();
  let release;
  let entered;
  const writeStarted = new Promise(resolve => { entered = resolve; });
  const write = probe.writeMem.bind(probe);
  probe.writeMem = async (addr, bytes) => {
    if (addr === probe.downBuf){
      await new Promise(resolve => { release = resolve; entered(); });
    }
    return write(addr, bytes);
  };
  let closed = false;
  probe.disconnect = async () => {
    assert.equal(probe._getU32(probe.cbAddr + 60), 1);
    closed = true;
  };
  const view = Object.create(RttView.prototype);
  Object.assign(view, {rtt,probe,rec:{},_uiConnected(){}});
  const sent = view._sendBytes(Uint8Array.of(42));
  await writeStarted;
  const closing = view.disconnect();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(closed, false);
  release();
  await Promise.all([sent, closing]);
  assert.equal(closed, true);
});
