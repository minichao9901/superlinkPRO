const ARM_BANKS = {
  stm32f0:'stm32f1x', stm32f1:'stm32f1x', stm32f103:'stm32f1x', stm32f3:'stm32f1x',
  stm32f2:'stm32f2x', stm32f4:'stm32f2x', stm32f7:'stm32f2x',
  stm32l0:'stm32lx', stm32l1:'stm32lx', stm32h7:'stm32h7x', stm32h7b0:'stm32h7x',
  stm32c0:'stm32l4x', stm32g0:'stm32l4x', stm32g4:'stm32l4x', stm32l4:'stm32l4x',
  stm32l5:'stm32l4x', stm32u5:'stm32l4x', stm32wb:'stm32l4x', stm32wl:'stm32l4x',
  nrf52:'nrf5', rp2350:'rp2xxx',
};

export const ocdHex = n => '0x' + n.toString(16);
export const hasOpenOcdFlash = family => Object.hasOwn(ARM_BANKS, family);

// Only numeric fields and known drivers enter generated Tcl. Imported target
// settings use the existing restricted CFG parser before reaching here.
export function makeOpenOcdProfile({ arch = 'arm', clockKhz = 1000, dpidr, tapIdcode, irLength = 5,
  hart = 0, port, family, name, flash = false } = {}){
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
      `target create ${target} cortex_m -dap workbench.dap`, 'cortex_m reset_config sysresetreq');
  }
  if (flash && port){
    const { flashBase, flashSize, xpiBase, option0, option1 } = port.flash;
    const args = [xpiBase, option0, option1].filter(n => n != null).map(n => ocdHex(integer(n, 0, 0xffffffff)));
    lines.push(`${target} configure -work-area-phys ${ocdHex(port.memory.workArea.addr)} -work-area-size ${ocdHex(port.memory.workArea.size)} -work-area-backup 1`,
      `flash bank xpi0 hpm_xpi ${ocdHex(flashBase)} ${ocdHex(flashSize)} 1 1 ${target} ${args.join(' ')}`);
  } else if (flash){
    const driver = hasOpenOcdFlash(family) ? ARM_BANKS[family] : null;
    if (!driver) throw Error('这个目标没有内置 OpenOCD Flash 配置');
    const base = rp2350 ? '0x10000000' : driver === 'nrf5' ? '0x00000000' : '0x08000000';
    if (rp2350) lines.push(`${target} configure -work-area-phys 0x20010000 -work-area-size 0x10000 -work-area-backup 1`,
      `${target} configure -event reset-init { rp2xxx rom_api_call FC }`);
    lines.push(`flash bank internal ${driver} ${base} 0 0 0 ${target}`);
    if (family === 'stm32h7') lines.push(`flash bank internal2 stm32h7x 0x08100000 0 0 0 ${target}`);
  }
  return { name:name || family || port?.id || arch, arch, target, clockKhz:clock, dpidr,
    tapIdcode:riscv ? id : undefined, family, boardId:port?.id,
    sbaFence:port?.memory.sbaReadForbidden, script:lines.join('\n') + '\n' };
}
