import assert from 'node:assert/strict';
import test from 'node:test';
import { importOpenOcdCfg as importCfg, readCfgDirectory } from '../../app/dbg/openocd-cfg.js';

const board = 'source [find target/stm32f1x.cfg]';
const target = [
  'source [find target/swj-dp.tcl]',
  'source [find mem_helper.tcl]',
  'if {[info exists CHIPNAME]} {',
  '  set _CHIPNAME $CHIPNAME',
  '} else { set _CHIPNAME stm32f1x }',
  'if { [using_jtag] } {',
  '  jtag newtap stm32f1x cpu -irlen 4',
  '} {',
  '  swj_newdap $_CHIPNAME cpu -irlen 4 \\',
  '    -expected-id 0x1ba01477',
  '}',
  'dap create $_CHIPNAME.dap -tap $_CHIPNAME.cpu',
  'set _TARGETNAME $_CHIPNAME.cpu',
  'target create $_TARGETNAME cortex_m -dap $_CHIPNAME.dap -endian little',
  '$_TARGETNAME configure -work-area-phys 0x20000000 -work-area-size 0x1000 -work-area-backup 0',
  'flash bank f1.flash stm32f1x 0x08000000 0 0 0 $_TARGETNAME',
  'adapter speed 1400',
  'reset_config srst_nogate',
  'if {![using_hla]} { cortex_m reset_config sysresetreq }',
].join('\r\n');

test('reads STM32F1 target declarations, SWD arm of conditionals and line continuations', () => {
  assert.deepEqual(importCfg('\ufeff# target\r\n' + target), {
    kind: 'target', arch: 'arm', family: 'stm32f1', target: 'stm32f1x.cpu', dpidr: 0x1ba01477, clockKhz: 1400,
    workAreaBase: 0x20000000, workAreaSize: 4096, ignored: ['Flash bank'],
  });
});

test('a board resolves the built-in F1 target and applies explicit overrides in order', () => {
  const cfg = importCfg([
    'source [find interface/cmsis-dap.cfg]',
    'adapter driver cmsis-dap; transport select swd',
    'set CHIPNAME my_board',
    'set WORKAREASIZE 0x8000',
    'set CPUTAPID 0x2ba01477',
    'adapter speed 500',
    board,
    'adapter speed 2400',
    'proc stm32_enable_fsmc {} { mww 0x40021014 0x114 }',
    '$_TARGETNAME configure -event reset-init { stm32_enable_fsmc }',
  ].join('\n'));
  assert.equal(cfg.kind, 'board');
  assert.equal(cfg.target, 'my_board.cpu');
  assert.equal(cfg.dpidr, 0x2ba01477);
  assert.equal(cfg.clockKhz, 2400);
  assert.equal(cfg.workAreaSize, 32768);
  assert.ok(cfg.ignored.includes('板卡外部 FSMC 初始化'));
  assert.equal(importCfg('adapter speed 500\n' + board).clockKhz, 1000);
});

test('rejects executable commands, unsupported transports and board dependencies', () => {
  for (const unsafe of [
    'exec openocd',
    'source [find board/unrecognized.cfg]',
    'source [find ../../target/stm32f1x.cfg]',
    'transport select jtag',
    'adapter driver st-link',
    'reset_config srst_only connect_assert_srst',
    'flash write_image erase data.hex',
    'mww 0x40022004 0x45670123',
    'set THING [exec openocd]',
    'set THING {$CPUTAPID}',
    'if {[expr 1]} { adapter speed 2000 }',
    '$_TARGETNAME configure -event reset-init { mww 0x40021014 0x114 }',
    'adapter speed 61000',
    'if {[using_swd]} {',
  ]) assert.throws(() => importCfg(board + '\n' + unsafe), /CFG/);
  assert.throws(() => importCfg('set CPUTAPID 0\n' + board), /DPIDR/);
  assert.throws(() => importCfg('# empty'), /Cortex-M/);
});

test('non-STM32 Cortex-M targets keep their own DPIDR and advisory work area', () => {
  const cfg = importCfg(target.replaceAll('stm32f1x','vendor_m4').replace('0x20000000','0x10000000'));
  assert.equal(cfg.family, 'cortex-m');
  assert.equal(cfg.workAreaBase, 0x10000000);
  assert.equal(importCfg('source [find target/nrf52.cfg]').dpidr, 0x2ba01477);
});

test('RISC-V CFG imports a single JTAG TAP, hart and sysbus access', () => {
  const script = [
    'transport select jtag',
    'jtag newtap test cpu -irlen 6 -expected-id 0x12345679',
    'target create test.cpu riscv -chain-position test.cpu -coreid 2',
    'riscv set_mem_access sysbus progbuf',
    'adapter speed 1500',
  ].join('\n');
  const cfg = importCfg(script);
  assert.deepEqual([cfg.arch, cfg.tapIdcode, cfg.irLength, cfg.hart, cfg.clockKhz], ['riscv', 0x12345679, 6, 2, 1500]);
  assert.throws(() => importCfg(script.replace('irlen 6', 'irlen 4')), /JTAG IR/);
  assert.throws(() => importCfg(script.replace('coreid 2', 'coreid 1024')), /hart/);
  assert.throws(() => importCfg(script.replace('sysbus progbuf', 'progbuf')), /set_mem_access/);
  assert.throws(() => importCfg(script + '\njtag newtap other cpu -irlen 5 -expected-id 0x12345679'), /单个/);
});

test('HPM6800EVK SDK board fragment supplies safe debug settings without invoking startup hooks', () => {
  const text = [
    'flash bank xpi0 hpm_xpi 0x80000000 0x2000000 1 1 $_TARGET0 0xF3000000 0x7',
    'proc init_clock {} { mww 0xF4000800 0xffffffff }',
    'proc init_ddr3 {} { mww 0xF3010000 0x1040001 }',
    '$_TARGET0 configure -event reset-init { init_clock\n init_ddr3 }',
    '$_TARGET0 configure -event gdb-attach { reset halt }',
  ].join('\n');
  const cfg = importCfg(text, 'hpm6800evk.cfg');
  assert.deepEqual(
    [cfg.boardId, cfg.target, cfg.tapIdcode, cfg.irLength, cfg.hart, cfg.clockKhz, cfg.workAreaSize],
    ['hpm6800evk', 'hpm6880.cpu0', 0x1000563d, 5, 0, 8000, 0x20000],
  );
  assert.deepEqual(cfg.ignored, ['Flash bank', 'HPM6800EVK 时钟和 DDR 初始化', 'HPM6800EVK GDB 连接复位事件']);
  assert.throws(() => importCfg(text, 'unknown.cfg'), /CFG/);
  assert.throws(() => importCfg(text.replace('0x2000000', '0x1000000'), 'hpm6800evk.cfg'), /缺少 HPM6800EVK/);
  assert.throws(() => importCfg(text + '\nprogram firmware.elf', 'hpm6800evk.cfg'), /CFG/);
});

test('directory entry evaluates supplied target and nested dependencies in order', () => {
  const files = new Map([
    ['board/custom.cfg', '# C:\\sdk\\example\nif { ![info exists CHIPNAME ] } { set CHIPNAME custom }\n' +
      'source [find target/stm32f1x.cfg]\nadapter speed 2400\n' +
      'proc stm32_enable_fsmc {} { mww 0x40021014 0x114 }\n' +
      '$_TARGETNAME configure -event reset-init { stm32_enable_fsmc }'],
    ['target/stm32f1x.cfg', 'source "连接 参数.tcl"\n' + target],
    ['target/连接 参数.tcl', 'set CPUTAPID 0x2ba01477'],
  ]);
  files.set('target/stm32f1x.cfg', files.get('target/stm32f1x.cfg').replace('-expected-id 0x1ba01477', '-expected-id $CPUTAPID'));
  const cfg = importCfg(files.get('board/custom.cfg'), 'board/custom.cfg', files);
  assert.deepEqual([cfg.kind, cfg.target, cfg.clockKhz, cfg.dpidr], ['board', 'custom.cpu', 2400, 0x2ba01477]);
  assert.deepEqual(cfg.sourced, ['target/stm32f1x.cfg', 'target/连接 参数.tcl']);
  assert.ok(cfg.ignored.includes('板卡外部 FSMC 初始化'));
  assert.throws(() => importCfg(board, 'board/custom.cfg', new Map()), /缺少 CFG 引用 target\/stm32f1x.cfg/);
});

test('GDB Flash events stay inert for RAM debugging', () => {
  const cfg = importCfg(target + '\n$_TARGETNAME configure -event gdb-flash-erase-start { reset init; flash erase_sector 0 0 last }');
  assert.ok(cfg.ignored.includes('GDB Flash 操作事件'));
});

test('directory imports reject cycles, traversal, unsupported sourced commands and duplicate paths', async () => {
  const files = new Map([['board/cycle.cfg', 'source [find target/next.cfg]'], ['target/next.cfg', 'source [find board/cycle.cfg]']]);
  const entry = files.get('board/cycle.cfg');
  assert.throws(() => importCfg(entry, 'board/cycle.cfg', files), /循环引用.*board\/cycle.cfg/);
  files.set('target/next.cfg', 'source ../outside.tcl');
  assert.throws(() => importCfg(entry, 'board/cycle.cfg', files), /target\/next.cfg.*所选脚本目录/);
  files.set('target/next.cfg', 'exec openocd');
  assert.throws(() => importCfg(entry, 'board/cycle.cfg', files), /target\/next.cfg.*exec openocd/);
  const file = (path, body) => ({name:path.split('/').at(-1), webkitRelativePath:path, size:body.length, text:async()=>body});
  const map = await readCfgDirectory([file('tcl/board/cycle.cfg', entry), file('tcl/target/сpu.cfg', 'set TEST 1')]);
  assert.deepEqual([...map.keys()], ['board/cycle.cfg', 'target/сpu.cfg']);
  await assert.rejects(readCfgDirectory([file('tcl/board/cycle.cfg', entry), file('tcl/board/./cycle.cfg', entry)]), /路径重复/);
});
