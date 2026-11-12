import { OpenOcdTargetSession } from '../core/openocd-session.js';
import { cfgPath } from './openocd-cfg.js';

const targetName = /^[A-Za-z0-9_.-]{1,100}$/;

/** Ask OpenOCD to interpret the uploaded files with USB disconnected. */
export async function inspectOpenOcdCfg(text, entry, directory, connection){
  entry = cfgPath(entry);
  const scripts = directory || new Map([[entry,text]]);
  if (!scripts.has(entry)) throw Error('CFG 目录缺少入口：' + entry);
  const files = [...scripts].map(([path, body]) => [cfgPath(path),body]);
  const hpmSdk = /^hpm\w+_all_in_one\.cfg$/i.test(entry) && scripts.has('probes/cmsis_dap.cfg');
  const raw = { entry, files, hpmSdk, ...(connection && {connection}) };
  const session = new OpenOcdTargetSession();
  try {
    await session.open({raw});
    if (await session.command('command mode') !== 'config')
      throw Error('CFG 在加载时调用了 init；请在工作台连接目标时初始化');
    if (await session.command('capture {adapter name}') !== 'cmsis-dap')
      throw Error('工作台仅支持 CMSIS-DAP / WebUSB 适配器');
    const transport = await session.command('transport select');
    const names = (await session.command('target names')).split(/\s+/).filter(Boolean);
    if (!names.length || names.length > 64 || names.some(name => !targetName.test(name)))
      throw Error('CFG 未定义可选的 OpenOCD 核');
    const cores = [];
    for (const target of names){
      const type = await session.command(`capture {${target} cget -type}`);
      if (transport === 'swd' && type === 'cortex_m') cores.push({target,arch:'arm'});
      if (transport === 'jtag' && type === 'riscv') cores.push({target,arch:'riscv'});
    }
    if (!cores.length) throw Error('CFG 没有 Cortex-M / SWD 或 RISC-V / JTAG 核');
    const clock = await session.command('capture {adapter speed}');
    const clockKhz = Number(clock.match(/(\d+)\s*kHz/i)?.[1]);
    if (!(clockKhz >= 1 && clockKhz <= (hpmSdk ? 25000 : 60000))) throw Error(`CFG 时钟须在 1–${hpmSdk ? 25000 : 60000} kHz`);
    const bankText = await session.command('capture {flash banks}');
    const banks = [...bankText.matchAll(/^#\d+\s*:\s*[^\n]+?\(([^)]+)\)\s+at\s+(0x[\da-f]+),/gim)];
    const [, driver, address] = banks[0] ?? [];
    // The STM32 drivers infer the main bank location when probing. For BIN
    // display use its conventional address, and validate it live at write time.
    const flashBase = Number(address) === 0 && driver.startsWith('stm32') ? '0x08000000' : address;
    const digest = new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(raw))));
    const hash = Array.from(digest,b=>b.toString(16).padStart(2,'0')).join('');
    return { raw, hash, name:entry, kind:'原版', transport, clockKhz, cores,
      hasFlash:!!banks.length, flashBase, ignored:[], ...cores[0] };
  } finally { await session.close(); }
}
