export const ocdHex = n => '0x' + n.toString(16);

// Only numeric fields and known drivers enter generated Tcl. Imported target
// settings use the existing restricted CFG parser before reaching here.
export function makeOpenOcdProfile({ arch = 'arm', clockKhz = 1000, dpidr, tapIdcode, irLength = 5,
  hart = 0, port, family, name } = {}){
  const riscv = arch === 'riscv';
  const rp2350 = !riscv && family === 'rp2350';
  const target = 'workbench.cpu';
  const integer = (value, min, max) => {
    if (!Number.isInteger(value) || value < min || value > max) throw Error('OpenOCD 目标配置参数超出范围');
    return value;
  };
  const id = riscv ? (tapIdcode ?? port?.debug.tapIdcode) : dpidr;
  const expected = id == null ? '' : ' -expected-id ' + ocdHex(integer(id, 0, 0xffffffff));
  const clock = integer(clockKhz, 1, riscv && port ? 25000 : 60000);
  const lines = ['adapter driver cmsis-dap', `transport select ${riscv ? 'jtag' : 'swd'}`,
    `adapter speed ${clock}`, 'reset_config none'];
  if (riscv){
    const ir = integer(port?.debug.irLength ?? irLength, 2, 32);
    lines.push(`jtag newtap workbench cpu -irlen ${ir}${expected}`,
      `target create ${target} riscv -chain-position workbench.cpu -coreid ${integer(port?.debug.hart ?? hart, 0, 1023)}`);
  } else if (rp2350){
    // RP2350's Cortex-M33 core 0 is on the ADIv6 AP at 0x2000.
    lines.push('swd newdap workbench cpu -expected-id 0x00040927',
      'dap create workbench.dap -adiv6 -tap workbench.cpu',
      `target create ${target} cortex_m -dap workbench.dap -ap-num 0x2000`,
      `${target} cortex_m reset_config sysresetreq`);
  } else {
    lines.push(`swd newdap workbench cpu${expected}`, 'dap create workbench.dap -chain-position workbench.cpu',
      `target create ${target} cortex_m -dap workbench.dap`,
      'cortex_m reset_config sysresetreq');
  }
  return { name:name || family || port?.id || arch, arch, target, clockKhz:clock, dpidr,
    tapIdcode:riscv ? id : undefined, family, boardId:port?.id,
    sbaFence:port?.memory.sbaReadForbidden, script:lines.join('\n') + '\n' };
}
