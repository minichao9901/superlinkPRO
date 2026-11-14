import { inspectOpenOcdCfg } from '../dbg/openocd-raw-cfg.js';
import { upstreamFlashConfigs, hpmSdkFlashConfigs } from './openocd-cfgs/catalog.js';

export const hasBuiltinFlashCfg = family => Object.hasOwn(upstreamFlashConfigs, family) ||
  Object.hasOwn(hpmSdkFlashConfigs, family);

const scripts = new Map();

async function loadScript(path){
  if (!scripts.has(path)){
    const response = await fetch(new URL(`./openocd-cfgs/${path}`, import.meta.url));
    if (!response.ok) throw Error(`无法加载 OpenOCD 脚本 ${path}（HTTP ${response.status}）`);
    scripts.set(path, await response.text());
  }
  return scripts.get(path);
}

export async function loadBuiltinFlashCfg(family, clockKhz, transport = 'swd'){
  if (!hasBuiltinFlashCfg(family)) throw Error('没有可用的官方 OpenOCD Flash CFG');
  const hpm = hpmSdkFlashConfigs[family];
  const paths = hpm ? [
    `hpm${hpm[0]}_all_in_one.cfg`, `boards/${family}.cfg`, `soc/hpm${hpm[1]}.cfg`, 'probes/cmsis_dap.cfg',
    'soc/hpm_common.cfg', 'soc/hpm_reset.cfg', 'soc/hpm_common_csr.cfg', 'soc/hpm_common_csr_lite.cfg',
  ] : upstreamFlashConfigs[family];
  const [entry] = paths;
  const files = new Map(await Promise.all(paths.map(async path => [path, await loadScript(hpm ? `hpm-sdk/${path}` : path)])));
  if (hpm) files.set('interface/cmsis-dap.cfg', await loadScript('interface/cmsis-dap.cfg'));
  const cfg = await inspectOpenOcdCfg(files.get(entry), entry, files, {
    transport:hpm ? 'jtag' : transport, clockKhz, ...(hpm && {board:family}),
  });
  if (!cfg.hasFlash) throw Error('OpenOCD 官方 CFG 没有定义 Flash bank：' + entry);
  return cfg;
}
