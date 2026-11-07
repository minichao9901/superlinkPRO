import assert from 'node:assert/strict';
import test from 'node:test';
import { OpenOcdDebugSession } from '../../app/dbg/openocd-session.js';

test('Wasm restores a temporarily removed hardware breakpoint even when stepping fails', async () => {
  const session = new OpenOcdDebugSession();
  const first = 0x8000578c, second = 0x80006094;
  const calls = [];
  session.link = { command: async line => { calls.push(line); return ''; } };
  await session.bpAdd(first, 'engine_linear');
  await session.bpAdd(second);
  await assert.rejects(session._withBpCleared(first, async () => {
    assert.deepEqual(session.bps, [second]);
    throw new Error('step failed');
  }), /step failed/);
  assert.deepEqual(calls.slice(2), [`rbp ${first}`, `bp ${first} 2 hw`]);
  assert.deepEqual(session.bps, [first, second]);
  assert.equal(session.bpNotes.get(first), 'engine_linear');
});

test('Wasm XIP view only uses fully covered ELF bytes and keeps the target transport untouched', async () => {
  const session = new OpenOcdDebugSession();
  const bytes = Uint8Array.of(0x12, 0x34);
  session.sym = { codeBytes: (addr, len) => addr === 0x80003000 && len === 2 ? bytes : null };
  session.link = { command: async () => assert.fail('XIP must not read from the target bus') };
  assert.deepEqual(await session.memRead(0x80003000, 2), bytes);
  await assert.rejects(session.memRead(0x80003000, 3), /未完整覆盖/);
  await assert.rejects(session.memRead(0x7fffffff, 2), /未完整覆盖/);
});
