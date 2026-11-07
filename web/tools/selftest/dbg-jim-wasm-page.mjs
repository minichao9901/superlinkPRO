import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { Cdp, sleep } from './cdp-lib.mjs';

const [url, upstream, hpmSdk, output] = process.argv.slice(2);
assert.ok(url && upstream && hpmSdk && output,
  'Usage: node dbg-jim-wasm-page.mjs URL OPENOCD_TCL HPM_SDK OUTPUT_JSON');
const page = new URL(url);
assert.equal(page.hostname, '127.0.0.1');
const cdp = new Cdp();
const files = (directory, names, prefix = '') =>
  Promise.all(names.map(async name => [prefix + name, await readFile(join(directory, name), 'utf8')]));
const capture = (input, options = {}) => cdp.eval(`
  const { captureOpenOcdConfig } = await import('./openocd-jim-wasm-client.mjs');
  return await captureOpenOcdConfig(${JSON.stringify(input)}, ${JSON.stringify(options)});
`);
const report = {};

try {
  await cdp.connect();
  await cdp.send('Page.navigate', { url:page.href });
  for (let i = 0; i < 100; i++){
    if (await cdp.eval(`return location.href === ${JSON.stringify(page.href)};`).catch(() => false)) break;
    await sleep(40);
  }

  const armInput = {
    files:await files(upstream, ['target/stm32h7x.cfg', 'target/swj-dp.tcl', 'mem_helper.tcl']),
    entry:'target/stm32h7x.cfg',
    variables:{ DUAL_CORE:1, DUAL_BANK:1, USE_CTI:1 },
  };
  report.stm32h7 = await capture(armInput);
  assert.equal(report.stm32h7.code, 0, report.stm32h7.result);
  const arm = report.stm32h7.commands;
  assert.deepEqual(arm.filter(row => row[0] === 'source').map(row => row[1]),
    ['/cfg/target/swj-dp.tcl', '/cfg/mem_helper.tcl']);
  assert.deepEqual(arm.filter(row => row[0] === 'target').map(row => [row[2], row[3], row.at(-1)]),
    [['stm32h7x.ap2', 'mem_ap', '2'], ['stm32h7x.cpu0', 'cortex_m', '0'],
      ['stm32h7x.cpu1', 'cortex_m', '3']]);
  assert.equal(arm.filter(row => row[0] === 'cti' && row[1] === 'create').length, 2);
  assert.equal(arm.filter(row => row[0] === 'flash' && row[1] === 'bank').length, 4);
  assert.ok(arm.some(row => row[0] === 'stm32h7x.cpu0' && row[1] === 'configure' &&
    row[2] === '-event' && row[3] === 'examine-end' && row[4].includes('stm32h7x_dbgmcu_mmw')));

  const prefix = 'boards/openocd/';
  const hpm = await files(join(hpmSdk, prefix), [
    'hpm6800_all_in_one.cfg', 'probes/cmsis_dap.cfg', 'soc/hpm6880.cfg', 'soc/hpm_common.cfg',
    'soc/hpm_reset.cfg', 'soc/hpm_common_csr.cfg', 'boards/hpm6800evk.cfg',
  ], prefix);
  hpm.push(...await files(upstream, ['interface/cmsis-dap.cfg'], prefix));
  report.hpm6800 = await capture({
    files:hpm, entry:'hpm6800_all_in_one.cfg', searchRoot:prefix.slice(0, -1),
    transport:'jtag', variables:{ HPM_SDK_BASE:'/cfg', PROBE:'cmsis_dap' },
  });
  assert.equal(report.hpm6800.code, 0, report.hpm6800.result);
  const rv = report.hpm6800.commands;
  assert.ok(rv.some(row => row.join(' ') === 'jtag newtap hpm6880 cpu -irlen 5 -expected-id 0x1000563D'));
  assert.ok(rv.some(row => row.join(' ') === 'target create hpm6880.cpu0 riscv -chain-position hpm6880.cpu -coreid 0'));
  assert.ok(rv.some(row => row.join(' ') === 'adapter speed 8000'));
  assert.deepEqual(rv.filter(row => row[0] === 'source').map(row => row[1].slice(5 + prefix.length)),
    ['probes/cmsis_dap.cfg', 'interface/cmsis-dap.cfg', 'soc/hpm6880.cfg', 'soc/hpm_common.cfg',
      'soc/hpm_reset.cfg', 'soc/hpm_common_csr.cfg', 'boards/hpm6800evk.cfg']);
  assert.ok(rv.some(row => row[1] === 'configure' && row[3] === 'reset-init' &&
    row[4].includes('init_ddr3')));

  report.rejected = {};
  for (const [name, script, expected] of [
    ['hostExec', 'exec any-program', /exec/],
    ['network', 'socket 127.0.0.1 4444', /socket/],
    ['outsideWorkspace', 'source /etc/hosts', /outside workspace/],
    ['sourceCycle', 'source entry.cfg', /source cycle/],
    ['flashWrite', 'flash write_image erase image.elf', /unsupported OpenOCD command/],
    ['hardwareWrite', 'target create t.cpu cortex_m; t.cpu mww 0x40022004 0x45670123', /unsupported target command/],
    ['reset', 'reset halt', /reset/],
  ]){
    const result = await capture({ files:[['entry.cfg', script]], entry:'entry.cfg' });
    assert.notEqual(result.code, 0, name);
    assert.match(result.result, expected, name);
    report.rejected[name] = result.result;
  }
  await assert.rejects(capture({ files:[['entry.cfg', 'while {1} {}']], entry:'entry.cfg' }, { timeoutMs:300 }),
    /evaluation timed out/);
  const afterTimeout = await capture({ files:[['entry.cfg', 'expr {6 * 7}']], entry:'entry.cfg' });
  assert.equal(afterTimeout.result, '42');
  report.workerDeadlineVerified = true;
  report.passed = true;
} catch (error){
  report.error = error.message;
  report.passed = false;
  process.exitCode = 1;
} finally {
  cdp.close();
  await writeFile(output, JSON.stringify(report, null, 2) + '\n', { flag:'wx' });
  console.log(JSON.stringify({
    passed:report.passed, error:report.error,
    armCommands:report.stm32h7?.commands.length, rvCommands:report.hpm6800?.commands.length,
    blocked:Object.keys(report.rejected || {}), workerDeadlineVerified:report.workerDeadlineVerified,
  }, null, 2));
}
