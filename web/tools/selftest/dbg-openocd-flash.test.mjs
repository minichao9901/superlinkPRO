import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { hpmFlashPlan } from '../../app/dbg/openocd-flash.js';
import { OpenOcdDebugSession } from '../../app/dbg/openocd-session.js';

const fixture = Uint8Array.from(await readFile(new URL('../target-firmware/hpm6800evk_scope/fw.elf', import.meta.url))).buffer;

test('HPM XIP ELF includes both ROM headers and excludes untouched sector 2', () => {
  const { entry, main, ranges } = hpmFlashPlan(fixture);
  assert.equal(entry, 0x80003000);
  assert.equal(main, 0x80005b7a);
  assert.deepEqual(ranges.map(r => [r.first, r.last]), [[0, 1], [3, 13]]);
  const bad = fixture.slice(0);
  new DataView(bad).setUint32(24, 0x18000, true);
  assert.throws(() => hpmFlashPlan(bad), /启动头或可从 Flash 执行的入口/);
});

test('failed Flash readback cannot authorize boot, and omitted sector bytes are preserved', async () => {
  const session = new OpenOcdDebugSession();
  session.probe = {};
  session.hpmSdk = true;
  session.ensureHalted = async () => {};
  const info = '#0 : hpm_xpi at 0x80000000, size 0x01000000, buswidth 1, chipwidth 1\n' +
    Array.from({ length:14 }, (_, i) => `\t#${i}: 0x${(0x1000 * i).toString(16)} (0x1000 4kB)`).join('\n');
  const original = new Map([[0, new Uint8Array(8192).fill(0x5a)], [12288, new Uint8Array(45056).fill(0xa5)]]);
  const written = new Map();
  const erase = [];
  let input;
  session.link = { async request(_, payload){ if (payload?.input) input = payload.input; } };
  session._command = async command => {
    if (command.includes('flash info 0 sectors')) return info;
    if (command.startsWith('flash erase_sector')) erase.push(command);
    if (command.startsWith('flash write_bank')) written.set(Number(command.split(' ').at(-1)), input);
  };
  session._readFlashRange = async ({ offset }) => {
    const data = written.get(offset);
    if (!data) return original.get(offset).slice();
    const corrupted = data.slice();
    corrupted[0] ^= 1;
    return corrupted;
  };
  await assert.rejects(session.programFlashElf(fixture), /回读不一致，地址 0x80000000/);
  assert.deepEqual(erase, ['flash erase_sector 0 0 1', 'flash erase_sector 0 3 13']);
  assert.equal(written.get(0)[0], 0x5a);
  assert.equal(written.get(0)[0x400], 1);
  await assert.rejects(session.bootVerifiedFlash(), /先在当前会话校验/);
});
