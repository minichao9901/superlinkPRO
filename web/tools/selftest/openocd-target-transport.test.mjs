import assert from 'node:assert/strict';
import test from 'node:test';
import { installNativeOpenOcd } from '../../../etm/tools/node-transport/openocd-worker.mjs';
import { CHIPS } from '../../app/core/chips.js';
import { OpenOcdProbe } from '../../app/core/openocd-probe.js';
import { hasOpenOcdFlash, makeOpenOcdProfile } from '../../app/core/openocd-profile.js';
import { OpenOcdTargetSession } from '../../app/core/openocd-session.js';
import { inspectOpenOcdCfg } from '../../app/dbg/openocd-raw-cfg.js';
import { programOpenOcdFlash } from '../../app/flash/openocd-program.js';
import { HPM_BOARDS } from '../../app/targets/hpm/porting.js';

test('production Wasm accepts SWD and JTAG configurations without opening hardware', async () => {
  const device = { vendorId:0x0d28, productId:0x0204, serialNumber:'offline' };
  await installNativeOpenOcd({ getDevices:async () => assert.fail('configuration must never open USB') });
  const profiles = [makeOpenOcdProfile(), makeOpenOcdProfile({ arch:'riscv' }),
    ...CHIPS.filter(c => hasOpenOcdFlash(c.v)).map(c => makeOpenOcdProfile({ family:c.v, flash:true })),
    ...HPM_BOARDS.map(port => makeOpenOcdProfile({ arch:'riscv', port, flash:true }))];
  for (const cfg of profiles){
    const session = new OpenOcdTargetSession();
    try {
      await session.open(cfg, device);
      const expectedTargets = cfg.family === 'stm32h7' ? [cfg.target, 'workbench.h7dbg'] : [cfg.target];
      assert.deepEqual((await session.command('target names')).split(/\s+/), expectedTargets, cfg.name);
      if (expectedTargets.length === 2){
        assert.equal(await session.command('workbench.h7dbg cget -ap-num'), '2');
      }
      assert.equal(await session.command('transport select'), cfg.arch === 'riscv' ? 'jtag' : 'swd', cfg.name);
    } finally { await session.close(); }
  }
});

test('browser Wasm uses an advancing monotonic clock for OpenOCD waits', async () => {
  await installNativeOpenOcd({ getDevices:async () => assert.fail('clock test must never open USB') });
  const session = new OpenOcdTargetSession();
  try {
    await session.open(makeOpenOcdProfile());
    const first = Number(await session.command('ms'));
    await new Promise(resolve => setTimeout(resolve, 40));
    const second = Number(await session.command('ms'));
    assert(first >= 0 && second - first >= 25, `OpenOCD clock: ${first} to ${second}`);
  } finally { await session.close(); }
});

test('original CFG directory exposes multiple cores and a flash bank without USB', async () => {
  let usbCalls=0;
  await installNativeOpenOcd({ getDevices:async () => { usbCalls++; return []; } });
  const entry='board/example.cfg';
  const directory=new Map([[entry,'source [find target/example.cfg]'],['target/example.cfg',`
    transport select swd
    adapter speed 8000
    swd newdap example cpu -expected-id 0x00040927
    dap create example.dap -adiv6 -tap example.cpu
    foreach {core ap} {a 0x2000 b 0x4000} {
      target create example.$core cortex_m -dap example.dap -ap-num $ap
    }
    flash bank example.flash rp2xxx 0x10000000 0 0 0 example.a
  `]]);
  const cfg=await inspectOpenOcdCfg(directory.get(entry),entry,directory);
  assert.deepEqual(cfg.cores,[{target:'example.a',arch:'arm'},{target:'example.b',arch:'arm'}]);
  assert.equal(cfg.flashBase,'0x10000000');
  assert.equal(cfg.clockKhz,8000);
  await assert.rejects(inspectOpenOcdCfg(directory.get('target/example.cfg')+'\ninit\n','unsafe.cfg'));
  assert.equal(usbCalls,0);
});

test('running JTAG memory uses physical words and preserves adjacent bytes', async () => {
  const cfg = makeOpenOcdProfile({ arch:'riscv', port:HPM_BOARDS.find(b => b.id === 'hpm6800evk') });
  const probe = new OpenOcdProbe({}, cfg, null, true);
  const commands = [];
  probe.targetCommand = async command => {
    commands.push(command);
    if (command.startsWith('read_memory')) return '0x44332211 0x88776655';
    return '';
  };
  assert.deepEqual(await probe.readMem(0x1240001, 5), Uint8Array.of(0x22, 0x33, 0x44, 0x55, 0x66));
  await assert.rejects(probe.readMem(0x80000000, 4), /禁止通过 SBA/);
  probe.targetCommand = async command => { commands.push(command); return '0x44332211'; };
  await probe.writeMem(0x1240001, Uint8Array.of(0x77, 0x88));
  assert.deepEqual(commands, ['read_memory 0x1240000 32 2 phys', 'read_memory 0x1240000 32 1 phys',
    'write_memory 0x1240000 32 {1149794065} phys']);
});

test('bulk HPM6800 SRAM uses binary images without changing edge or peripheral access', async () => {
  const cfg = makeOpenOcdProfile({ arch:'riscv', port:HPM_BOARDS.find(b => b.id === 'hpm6800evk') });
  const probe = new OpenOcdProbe({}, cfg, null, true);
  assert.equal(probe.rttPollLimit, 8192);
  const requests = [];
  probe.link = { request:async (type, args) => {
    requests.push({ type, ...args });
    return { bytes:Uint8Array.from({ length:args.length }, (_, i) => i % 256) };
  } };
  const data = await probe.readMem(0x01240001, 4095);
  assert.equal(data.length, 4095);
  assert.equal(data[0], 1);
  assert.equal(data.at(-1), 255);
  await probe.writeMem(0x18000, new Uint8Array(4096));
  assert.deepEqual(requests.map(({type,address,length,input}) => [type,address,length,input?.length]),
    [['memoryImage',0x01240000,4096,undefined],['memoryImage',0x18000,4096,4096]]);
  await assert.rejects(probe.readMem(0x80000000, 4096), /禁止通过 SBA/);
  assert.equal(requests.length, 2);
});

test('binary ARM images are limited to aligned SRAM on an identified high-density F103', async () => {
  const cfg = makeOpenOcdProfile({ family:'stm32f103' });
  const probe = new OpenOcdProbe({}, cfg);
  let identifications = 0;
  probe._readWord = async address => {
    assert.equal(address, 0xe0042000);
    identifications++;
    return 0x414;
  };
  const calls = [];
  const input = new Uint8Array(4096);
  probe.link = { request:async (type, args) => {
    calls.push([type,args]);
    return { bytes:input };
  } };
  assert.deepEqual(await probe.readMem(0x20000000, input.length), input);
  await probe.writeMem(0x2000f000, input);
  assert.deepEqual(calls, [
    ['memoryImage',{address:0x20000000,length:4096,physical:false}],
    ['memoryImage',{address:0x2000f000,length:4096,input,physical:false}],
  ]);
  assert.equal(identifications, 1);
  for (const address of [0x20000001, 0x2000f004, 0xe0000000])
    assert.equal(await probe._usesF103SramImage(address, 4096), false);
  const other = new OpenOcdProbe({}, cfg);
  other._readWord = async () => 0x410;
  assert.equal(await other._usesF103SramImage(0x20000000, 4096), false);
});

function flashTarget(){
  const data = Uint8Array.from({ length:16 }, (_, i) => i);
  const commands = [];
  let input, readback;
  const probe = {
    async command(line){
      commands.push(line);
      if (line === 'capture {flash banks}') return '#0 : internal (stm32f1x) at 0x08000000, size 0x0, buswidth 0, chipwidth 0\n#1 : alias (virtual) at 0x08000000, size 0x0, buswidth 0, chipwidth 0';
      if (line.startsWith('if {')) return '#0 : stm32f1x at 0x08000000, size 0x10, buswidth 0, chipwidth 0\n  #  0: 0x00000000 (0x8 0kB)\n  #  1: 0x00000008 (0x8 0kB)';
      if (line.startsWith('flash read_bank')){
        const [, offset, size] = line.match(/ (\d+) (\d+)$/);
        readback = data.slice(Number(offset), Number(offset) + Number(size));
      }
      if (line.startsWith('flash erase_sector')){
        const sector = Number(line.split(' ').at(-1));
        data.fill(0xff, sector * 8, (sector + 1) * 8);
      }
      if (line.startsWith('flash write_bank')) data.set(input, Number(line.split(' ').at(-1)));
    },
    link:{ async request(type, options){
      assert.equal(type, 'flashFile');
      if (options?.read) return { bytes:readback };
      input = options?.input;
    } },
  };
  return { probe, data, commands };
}

test('flash preserves uncovered bytes and validates every region before erasing', async () => {
  const { probe, data, commands } = flashTarget();
  const options = { verify:true, progress:() => {} };
  const part = { addr:0x08000007, data:Uint8Array.of(99, 100) };
  await assert.rejects(programOpenOcdFlash(probe, [part, { addr:0x08000010, data:Uint8Array.of(1) }], options), /超出实测/);
  assert.ok(!commands.some(command => command.includes('erase_sector')));
  assert.deepEqual(await programOpenOcdFlash(probe, [part], options), { bytes:2, sectors:2, verified:true });
  assert.ok(commands.every(command => !command.startsWith('flash probe 1')));
  assert.deepEqual([...data], [0, 1, 2, 3, 4, 5, 6, 99, 100, 9, 10, 11, 12, 13, 14, 15]);
});
