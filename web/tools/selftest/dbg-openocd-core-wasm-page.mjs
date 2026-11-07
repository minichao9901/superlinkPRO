import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { Cdp, sleep } from './cdp-lib.mjs';

const [url, upstream, hpmSdk, output, adapter = 'cmsis-dap', flashProfile, hpmFlash] = process.argv.slice(2);
assert.ok(url && upstream && hpmSdk && output && ['cmsis-dap', 'dummy'].includes(adapter),
  'Usage: node dbg-openocd-core-wasm-page.mjs URL OPENOCD_TCL HPM_SDK OUTPUT_JSON [cmsis-dap|dummy] [full|stm32-hpm-flash] [hpm-flash]');
assert.ok(!flashProfile || ['full', 'stm32-hpm-flash'].includes(flashProfile));
assert.ok(!hpmFlash || hpmFlash === 'hpm-flash');
const page = new URL(url);
assert.equal(page.hostname, '127.0.0.1');
const cdp = new Cdp();
const fixtures = (directory, names, prefix = '') =>
  Promise.all(names.map(async name => [prefix + name, await readFile(join(directory, name), 'utf8')]));
const run = input => cdp.eval(`
  return await new Promise((resolve, reject) => {
    const worker = new Worker('./openocd-core-wasm-worker.mjs', { type:'module' });
    const finish = (fn, value) => {
      clearTimeout(timer);
      worker.terminate();
      fn(value);
    };
    const timer = setTimeout(() => finish(reject, Error('OpenOCD Worker timed out')), 15000);
    worker.onerror = event => finish(reject, Error(event.message || 'OpenOCD Worker failed'));
    worker.onmessage = ({ data }) => {
      if (data.ready) worker.postMessage(${JSON.stringify(input)});
      else finish(resolve, data);
    };
  });
`);
// RAM-only policy: vendor Flash declarations may be present even when the
// corresponding driver is absent. Do not register their banks or accept writes.
const ramOnly = `rename flash {};
  proc flash {operation args} {
    if {$operation eq "bank"} {
      echo "OCD_SKIPPED_FLASH=[lindex $args 1]"
      return
    }
    error "Flash operation is disabled in RAM configuration verification: $operation"
  }`;
const offline = ['-c', 'noinit', '-c', 'gdb_port disabled', '-c', 'tcl_port disabled', '-c', 'telnet_port disabled'];
const config = [...offline, '-c', ramOnly];
const query = (key, command) => ['-c', `echo "OCD_${key}=[string map [list "\\n" " "] [string trim [${command}]]]"`];
const report = { adapter, flashProfile };

function check(run, expected){
  const log = run.lines?.join('\n') || run.error;
  assert.equal(run.code, 0, log);
  assert.deepEqual(run.attempted, []);
  for (const item of expected) assert.ok(log.includes(item), log);
}

try {
  await cdp.connect();
  await cdp.send('Page.navigate', { url:page.href });
  for (let i = 0; i < 100; i++){
    if (await cdp.eval(`return location.href === ${JSON.stringify(page.href)};`).catch(() => false)) break;
    await sleep(40);
  }
  const transport = adapter === 'dummy' ? 'jtag' : 'swd';
  const armFiles = await fixtures(upstream, ['target/stm32h7x.cfg', 'target/swj-dp.tcl', 'mem_helper.tcl']);
  const armArgs = ['-c', `adapter driver ${adapter}`, '-c', `transport select ${transport}`,
    '-c', 'set DUAL_CORE 1; set DUAL_BANK 1; set USE_CTI 1', '-f', 'target/stm32h7x.cfg'];
  report.stm32h7 = await run({
    files:armFiles,
    args:['-s', '/cfg', ...config, ...armArgs,
      ...query('MODE', 'command mode'), ...query('TRANSPORT', 'transport select'),
      ...(transport === 'jtag' ? query('TAPS', 'jtag names') : []), ...query('DAPS', 'dap names'),
      ...query('TARGETS', 'target names'), ...query('CTIS', 'cti names'),
      ...query('CPU0_DAP', 'stm32h7x.cpu0 cget -dap'),
      ...query('AP0', 'stm32h7x.cpu0 cget -ap-num'), ...query('AP3', 'stm32h7x.cpu1 cget -ap-num'),
      '-c', 'shutdown'],
  });
  check(report.stm32h7, [
    'OCD_MODE=config', `OCD_TRANSPORT=${transport}`, 'OCD_DAPS=stm32h7x.dap',
    'OCD_TARGETS=stm32h7x.ap2 stm32h7x.cpu0 stm32h7x.cpu1',
    'OCD_CPU0_DAP=stm32h7x.dap', 'OCD_AP0=0', 'OCD_AP3=3', 'OCD_SKIPPED_FLASH=stm32h7x',
  ]);
  if (transport === 'jtag') assert.ok(report.stm32h7.lines.includes('OCD_TAPS=stm32h7x.cpu stm32h7x.bs'));
  assert.ok(report.stm32h7.lines.some(line =>
    line.includes('OCD_CTIS=') && line.includes('stm32h7x.cti0') && line.includes('stm32h7x.cti1')));

  const prefix = 'hpm/boards/openocd/';
  const hpm = await fixtures(join(hpmSdk, 'boards/openocd'), [
    'hpm6800_all_in_one.cfg', 'probes/cmsis_dap.cfg', 'soc/hpm6880.cfg', 'soc/hpm_common.cfg',
    'soc/hpm_reset.cfg', 'soc/hpm_common_csr.cfg', 'boards/hpm6800evk.cfg',
  ], prefix);
  hpm.push(...await fixtures(upstream, ['interface/cmsis-dap.cfg']));
  if (adapter === 'dummy'){
    hpm.push([prefix + 'probes/dummy.cfg', 'adapter driver dummy\ntransport select jtag\nadapter speed 8000\n']);
  }
  const hpmArgs = ['-c', `set HPM_SDK_BASE /cfg/hpm; set PROBE ${adapter === 'dummy' ? 'dummy' : 'cmsis_dap'}`,
    '-f', 'hpm6800_all_in_one.cfg'];
  report.hpm6800 = await run({
    files:hpm,
    args:['-s', '/cfg', '-s', '/cfg/' + prefix, ...config, ...hpmArgs, ...query('MODE', 'command mode'),
      ...query('TAPS', 'jtag names'), ...query('TARGETS', 'target names'),
      ...query('HART', 'hpm6880.cpu0 cget -coreid'), '-c', 'shutdown'],
  });
  check(report.hpm6800, [
    'OCD_MODE=config', 'OCD_TAPS=hpm6880.cpu', 'OCD_TARGETS=hpm6880.cpu0',
    'OCD_HART=0', 'OCD_SKIPPED_FLASH=hpm_xpi',
  ]);

  report.invalidTarget = await run({
    files:[],
    args:[...config, '-c', 'target create bad.cpu cortex_fake', '-c', 'shutdown'],
  });
  assert.notEqual(report.invalidTarget.code, 0, 'upstream must reject an unknown target type');
  assert.deepEqual(report.invalidTarget.attempted, []);
  assert.match(report.invalidTarget.lines.join('\n'), /cortex_fake/);
  report.flashWrite = await run({
    files:[],
    args:[...config, '-c', 'flash write_image erase sample.elf', '-c', 'shutdown'],
  });
  assert.notEqual(report.flashWrite.code, 0);
  assert.deepEqual(report.flashWrite.attempted, []);
  assert.match(report.flashWrite.lines.join('\n'), /Flash operation is disabled.*write_image/);
  report.resetBeforeInit = await run({
    files:[],
    args:[...config, '-c', 'reset halt', '-c', 'shutdown'],
  });
  assert.notEqual(report.resetBeforeInit.code, 0);
  assert.deepEqual(report.resetBeforeInit.attempted, []);
  assert.match(report.resetBeforeInit.lines.join('\n'), /invalid command name "reset"|'reset' command must be used after 'init'/);
  if (flashProfile){
    const targets = await run({
      files:[],
      args:[...offline, ...query('TYPES', 'target types'), '-c', 'shutdown'],
    });
    check(targets, ['OCD_TYPES=']);
    report.targetTypes = targets.lines.find(line => line.startsWith('OCD_TYPES=')).slice('OCD_TYPES='.length).trim().split(/\s+/);
    for (const type of ['cortex_m', 'cortex_a', 'riscv']) assert.ok(report.targetTypes.includes(type));

    report.stm32Flash = await run({
      files:armFiles,
      args:['-s', '/cfg', ...offline, ...armArgs, '-c', 'flash banks', '-c', 'shutdown'],
    });
    check(report.stm32Flash, ['(stm32h7x) at']);
    if (hpmFlash){
      report.hpmFlash = await run({
        files:hpm,
        args:['-s', '/cfg', '-s', '/cfg/' + prefix, ...offline, ...hpmArgs,
          '-c', 'flash banks', '-c', 'shutdown'],
      });
      check(report.hpmFlash, ['(hpm_xpi) at']);
    }

    if (adapter === 'cmsis-dap'){
      const nrfFiles = await fixtures(upstream, ['target/nordic/nrf52.cfg']).catch(error => {
        if (error.code !== 'ENOENT') throw error;
        return fixtures(upstream, ['target/nrf52.cfg']);
      });
      report.nordicRamConfig = await run({
        files:[...armFiles, ...nrfFiles],
        args:['-s', '/cfg', ...config, '-c', 'adapter driver cmsis-dap', '-c', 'transport select swd',
          '-f', nrfFiles[0][0], ...query('TARGETS', 'target names'), '-c', 'shutdown'],
      });
      check(report.nordicRamConfig, ['OCD_TARGETS=nrf52.cpu', 'OCD_SKIPPED_FLASH=nrf5']);
    }
    report.nordicFlash = await run({
      files:armFiles,
      args:['-s', '/cfg', ...offline, ...armArgs,
        '-c', 'flash bank nrf52_size_check nrf5 0 0 1 1 stm32h7x.cpu0',
        '-c', 'flash banks', '-c', 'shutdown'],
    });
    if (flashProfile === 'full') check(report.nordicFlash, ['nrf52_size_check (nrf5) at']);
    else {
      assert.notEqual(report.nordicFlash.code, 0);
      assert.deepEqual(report.nordicFlash.attempted, []);
      assert.match(report.nordicFlash.lines.join('\n'), /flash driver 'nrf5' not found/);
    }
  }
  if (adapter === 'cmsis-dap'){
    report.emptyWebUsb = await run({
      files:[],
      simulateEmptyUsb:true,
      args:[...config, '-c', 'adapter driver cmsis-dap', '-c', 'transport select swd', '-c', 'init'],
    });
    assert.notEqual(report.emptyWebUsb.code, 0);
    assert.deepEqual(report.emptyWebUsb.attempted, ['simulated WebUSB enumeration']);
    assert.match(report.emptyWebUsb.lines.join('\n'), /unable to find a matching CMSIS-DAP device/);
  }
  report.passed = true;
} catch (error){
  report.error = error.message;
  report.passed = false;
  process.exitCode = 1;
} finally {
  cdp.close();
  await writeFile(output, JSON.stringify(report, null, 2) + '\n', { flag:'wx' });
  console.log(JSON.stringify({
    passed:report.passed, error:report.error, adapter:report.adapter,
    stm32h7:report.stm32h7?.lines.filter(line => line.includes('OCD_')),
    hpm6800:report.hpm6800?.lines.filter(line => line.includes('OCD_')),
    emptyWebUsb:report.emptyWebUsb?.attempted,
    targetTypeCount:report.targetTypes?.length,
  }, null, 2));
}
