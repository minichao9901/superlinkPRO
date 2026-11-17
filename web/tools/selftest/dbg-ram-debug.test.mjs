import assert from 'node:assert/strict';
import test from 'node:test';
import { OpenOcdProbe } from '../../app/core/openocd-probe.js';
import { ramDebugPlan, RAM_BASE as F103_RAM, readRamDebugSize } from '../../app/dbg/ram-debug.js';
import { DebugSession } from '../../app/dbg/session.js';
import { DbgView } from '../../app/dbg/view.js';

const main = F103_RAM + 0x104;
const symbols = {
  find: name => name === 'main' ? { kind: 'func', addr: main } : null,
  lines: { at: addr => addr === main ? { line: 10 } : null },
};

function ramElf(){
  const bytes = new Uint8Array(176);
  bytes.set([0x7f, 0x45, 0x4c, 0x46, 1, 1, 1]);
  const v = new DataView(bytes.buffer);
  v.setUint16(16, 2, true);
  v.setUint16(18, 40, true);
  v.setUint32(28, 52, true);
  v.setUint16(42, 32, true);
  v.setUint16(44, 3, true);
  const segments = [
    [1, 148, F103_RAM, F103_RAM, 8, 8, 4],
    [1, 156, F103_RAM + 0x100, F103_RAM + 0x100, 8, 8, 5],
    [1, 164, F103_RAM + 0x108, F103_RAM + 0x108, 4, 8, 6],
  ];
  segments.forEach((fields, i) => fields.forEach((value, j) => v.setUint32(52 + i * 32 + j * 4, value, true)));
  v.setUint32(148, F103_RAM + 65536, true);
  v.setUint32(152, F103_RAM + 0x101, true);
  v.setUint32(164, 42, true);
  return v;
}

test('RAM launch plan loads file bytes and zeroes BSS', () => {
  const plan = ramDebugPlan(ramElf().buffer, symbols);
  assert.equal(plan.sp, F103_RAM + 65536);
  assert.equal(plan.entry, F103_RAM + 0x100);
  assert.equal(plan.main, main);
  assert.deepEqual(plan.code, [{ start: F103_RAM + 0x100, end: F103_RAM + 0x108 }]);
  assert.deepEqual([...plan.segments[2].data], [42, 0, 0, 0, 0, 0, 0, 0]);
});

test('RAM launch rejects a Flash load, mismatched run address, overlap, BSS overflow and invalid vectors', () => {
  for (const [offset, value, error] of [
    [64, 0x08000000, /PT_LOAD/],
    [92, F103_RAM + 0x200, /PT_LOAD/],
    [72, 0x104, /PT_LOAD/],
    [136, 65536, /PT_LOAD/],
    [148, F103_RAM + 0x100, /MSP/],
    [152, F103_RAM + 0x100, /Thumb/],
    [152, 0x08000001, /Thumb/],
  ]){
    const elf = ramElf();
    elf.setUint32(offset, value, true);
    assert.throws(() => ramDebugPlan(elf.buffer, symbols), error);
  }
  assert.throws(() => ramDebugPlan(ramElf().buffer, { ...symbols, lines: null }), /DWARF/);
});

test('a temporary RAM breakpoint restores the original instruction after a failed resume', async () => {
  const s = new DebugSession();
  const original = Uint8Array.of(0x40, 0xf2);
  let contents = original.slice();
  s.ensureHalted = async () => { s.halted = true; };
  s.memRead = async () => contents.slice();
  s.memWrite = async (addr, bytes) => {
    assert.equal(addr, main);
    assert.ok(s.halted);
    contents = bytes.slice();
  };
  await assert.rejects(s._withRamBreakpoint(main, async () => {
    assert.deepEqual([...contents], [0x00, 0xbe]);
    s.halted = false;
    throw new Error('resume failed');
  }), /resume failed/);
  assert.deepEqual(contents, original);
  assert.ok(s.halted);
});

test('Nordic RAM profile requires read-only silicon identity and uses actual SRAM size', async () => {
  const words = new Map([[0xe000ed00, 0x410fc241], [0x10000100, 0x52832], [0x1000010c, 32]]);
  const size = await readRamDebugSize(addr => words.get(addr), 'nrf52');
  assert.equal(size, 32768);
  assert.throws(() => ramDebugPlan(ramElf().buffer, symbols, size), /MSP/);
  const elf = ramElf();
  elf.setUint32(148, F103_RAM + size, true);
  assert.equal(ramDebugPlan(elf.buffer, symbols, size).sp, F103_RAM + size);
  words.set(0x10000100, 0x52840);
  await assert.rejects(readRamDebugSize(addr => words.get(addr), 'nrf52'), /nRF52832/);
});

test('Cortex-M0+ CFG enables RAM launch after declaration and checks image bounds before reset', async () => {
  const view = new DbgView();
  const session = view.session;
  const cfg = { raw: {}, family:'cortex-m' };
  view._activeCfg = () => cfg;
  const probe = session.probe = new OpenOcdProbe(null, cfg);
  probe.armPart = 0xc20;
  assert.throws(() => session.configureRamWindow(F103_RAM, 65536), /Cortex-M0\+/);

  probe.armPart = 0xc60;
  assert.equal(view._ramDebugFamily(), null);
  assert.throws(() => session.configureRamWindow(0x80000000, 65536), /0x20000000/);
  assert.throws(() => session.configureRamWindow(F103_RAM, 0.5), /长度/);
  await assert.rejects(session._startRamDebug(ramElf().buffer, symbols, 'cortex-m0plus'), /先声明/);
  session.configureRamWindow(F103_RAM, 0x104);
  assert.equal(view._ramDebugFamily(), 'cortex-m0plus');
  session.resetHalt = async () => assert.fail('out-of-window ELF reached reset');
  await assert.rejects(session._startRamDebug(ramElf().buffer, symbols, 'cortex-m0plus'), /PT_LOAD/);
  await session.disconnect();
  assert.equal(session.ramWindow, null);
});

test('Cortex-M0+ rejects a missing VTOR before writing the ELF to SRAM', async () => {
  const session = new DebugSession();
  const probe = session.probe = new OpenOcdProbe(null, {raw:{}});
  probe.armPart = 0xc60;
  probe._readWord = async () => 0;
  session.configureRamWindow(F103_RAM, 65536);
  session.resetHalt = async () => { session.halted = true; };
  session.readReg = async () => 0x01000000;
  session.memWrite = async address => assert.equal(address, 0xe000ed08);
  await assert.rejects(session._startRamDebug(ramElf().buffer, symbols, 'cortex-m0plus'), /向量表地址回读失败/);
});
