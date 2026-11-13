import test from 'node:test';
import assert from 'node:assert/strict';
import { OpenOcdProbe } from '../../app/core/openocd-probe.js';
import { programOpenOcdFlash, resetAfterFlash } from '../../app/flash/openocd-program.js';

test('H7 raw CFG prepares the same DAP AP2 before init and requires a successful examine', async () => {
  const probe = new OpenOcdProbe(null, { arch:'arm', raw:{}, target:'custom.cpu0', stm32h7Flash:true });
  const calls = [];
  const link = { async command(line){ calls.push(line); return line === 'set workbench_h7_clocks_ready' ? '1' : '0'; } };
  await assert.rejects(probe._initTarget(link), /Flash 尚未擦写/);
  assert.match(calls[0], /workbench_h7_dap \[custom\.cpu0 cget -dap\]/);
  assert.match(calls[0], /cget -ap-num\] == 2/);
  assert.match(calls[0], /configure -event examine-start \[list .*\[custom\.cpu0 cget -event examine-start\]\]/);
  assert.match(calls[0], /DBGMCU debug clocks not confirmed/);
  assert.deepEqual(calls.slice(1), ['init', 'set workbench_h7_clocks_ready', 'custom.cpu0 was_examined']);
});

test('H7 reset checks physical DHCSR after transient failures without resetting again', async t => {
  const probe = new OpenOcdProbe(null, {arch:'arm',clockKhz:10000});
  const command = t.mock.method(probe,'command',async () => '');
  const samples = [new Error('AP0 unavailable'),0,0x90001,0x1010001];
  t.mock.method(probe,'_readWord',async address => {
    assert.equal(address,0xe000edf0);
    const sample = samples.shift();
    if (sample instanceof Error) throw sample;
    return sample;
  });
  await resetAfterFlash(probe);
  assert.deepEqual(command.mock.calls.map(call => call.arguments),[['reset run']]);
  assert.deepEqual(samples,[]);
});

test('H7 work area is restored after block writes and skipped when the MPU is enabled', async () => {
  for (const scenario of ['success','write-failure','short-backup','mpu-enabled','short-mpu','disable-rejected']){
    const failWrite = scenario === 'write-failure';
    const mpuEnabled = scenario === 'mpu-enabled' || scenario === 'disable-rejected';
    const original = Uint8Array.of(1,2,3,4,5,6,7,8);
    let ram = original.slice(), image, flash;
    let erased = false, workAreaSize = 8;
    const probe = {cfg:{target:'h743.cpu0'},halt:async()=>{},
      readMem:async address => {
        if (address === 0xe000ed94)
          return scenario === 'short-mpu' ? new Uint8Array() : Uint8Array.of(Number(mpuEnabled),0,0,0);
        assert.equal(mpuEnabled,false,'MPU fallback must avoid the RAM work area');
        return scenario === 'short-backup' ? ram.slice(0,4) : ram.slice();
      },
      writeMem:async(_a,b)=>{ram=b.slice();},
      command:async line => {
        if (line === 'capture {flash banks}') return '#0 : internal (stm32h7x) at 0x08000000, size 0x00000004';
        if (line.includes('flash info')) return '#0 : internal (stm32h7x) at 0x08000000, size 0x00000004\n # 0: 0x00000000 (0x00000004 0 KiB)';
        if (line.endsWith('cget -work-area-phys')) return '0x24060000';
        if (line.endsWith('cget -work-area-size')) return String(workAreaSize);
        if (line.endsWith('configure -work-area-size 0') && scenario !== 'disable-rejected') workAreaSize = 0;
        if (line.startsWith('flash erase_sector')) erased = true;
        if (line.startsWith('flash write_bank')){
          assert.equal(workAreaSize,mpuEnabled ? 0 : 8);
          if (workAreaSize) ram.fill(0xee);
          if (failWrite) throw Error('write failed');
          flash = image.slice();
        }
        return '';
      },link:{request:async(_t,p)=>{
        if(p?.input)image=p.input;
        return p?.read?{bytes:flash.slice()}:{};
      }},
    };
    const run=()=>programOpenOcdFlash(probe,[{addr:0x08000000,data:Uint8Array.of(9,8,7,6)}],{verify:true,progress(){}});
    const rejectedBeforeErase = {'short-backup':/备份不完整/,'short-mpu':/MPU 状态回读不完整/,'disable-rejected':/禁用未确认/};
    if(rejectedBeforeErase[scenario]){
      await assert.rejects(run(),rejectedBeforeErase[scenario]);
      assert.equal(erased,false);
    }else if(failWrite)await assert.rejects(run(),/write failed/);else await run();
    assert.deepEqual(ram,original);
  }
});
