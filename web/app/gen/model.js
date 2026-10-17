/**
 * 「工程生成」页的数据模型：器件映射表、.uvprojx 解析、参数 → 文件内容。
 *
 * 这一层**不碰 DOM**（除了没有），所以 Node 自测（tools/selftest/gen-parity.mjs）能直接 import
 * 来跟 Python 工具的真实产物做逐字节对账。
 *
 * 兼容性基线：E:\VibeCoding\my_trace_tools\uvprojx2cmake.py（用户自己的工具）。
 * 默认参数下，本页产物必须与它**逐字节相同**（含 CRLF 换行）。
 */

import {
  renderJlinkMakefile, renderGdbScript, renderPyocdMakefile,
  renderOpenocdMakefile, renderRttLogger, makeTestSramBin,
} from './templates.js';
import { applyFixes, FIX_KEYS } from './fixes.js';

/**
 * 页面**默认**就在模板产物上套这 4 项修正（用户 2026-09-27 逐条采纳，见 app/gen/fixes.js）。
 * 想做逐字节对账（与 Python 工具完全一致）就显式传 `fixes: null`。
 */
export const ALL_FIXES = Object.fromEntries(FIX_KEYS.map(k => [k, true]));

// ============================================================================
// 与 Python 工具一致的默认值
// 来源：JLINK_MAKEFILE_TEMPLATE / PYOCD_MAKEFILE_TEMPLATE / OPENOCD_MAKEFILE_TEMPLATE
//       里 `XXX ?= 值` 那些行；改成别的值就会写进产物（不填 = 保持原样）。
// ============================================================================
export const PARAM_DEFAULTS = {
  projectName: 'project',
  jlinkDevice: 'STM32F103RB',
  flashStart: '0x08000000',
  rttAddress: '0x20002000',
  jlinkIf: 'SWD',
  jlinkSpeed: '25000',
  rttSize: '0x5000',
  gdbPort: '3333',
  buildDir: 'build',
  cmakeGenerator: 'Ninja',
  buildType: 'Debug',
  pyocdTarget: 'stm32f103rb',
  pyocdFreq: '10000000',
  openocdRoot: '',
  openocdInterface: 'interface/cmsis-dap.cfg',
  openocdTarget: 'target/stm32f1x.cfg',
  openocdFreq: '10000',
  rttPort: '9090',
  testBinSize: '20480',
};

/** 产物清单（顺序 = 界面里的顺序） */
export const OUTPUTS = [
  { key: 'jlink', name: 'Makefile.jlink', label: 'Makefile.jlink' },
  { key: 'gdb', name: 'jlink_gdb.script', label: 'jlink_gdb.script' },
  { key: 'pyocd', name: 'Makefile.pyocd', label: 'Makefile.pyocd' },
  { key: 'openocd', name: 'Makefile.openocd', label: 'Makefile.openocd' },
  { key: 'rttlogger', name: 'rtt_logger.py', label: 'rtt_logger.py', note: '勾 OpenOCD 时一起生成' },
  { key: 'testBin', name: 'test_sram.bin', label: 'test_sram.bin', bin: true },
];

// ============================================================================
// 器件映射表（照抄 uvprojx2cmake.py，改的时候两边一起改）
// ============================================================================

export const JLINK_DEVICE_MAP = {
  CH32F207VC: 'CH32F207VC',
  STM32F103RB: 'STM32F103RB',
  STM32F103ZE: 'STM32F103ZE',
  STM32H750XB: 'STM32H750XB',
  STM32H743ZE: 'STM32H743ZE',
  STM32U5A5ZJ: 'STM32U5A5ZJ',
  STM32H7R7: 'STM32H7R7',
  STM32H7B3VI: 'STM32H7B3VI',
};

export const PYOCD_TARGET_MAP = {
  CH32F207VC: 'ch32f207vc',
  STM32F103RB: 'stm32f103rb',
  STM32F103ZE: 'stm32f103ze',
  STM32H750XB: 'stm32h750xb',
  STM32H743ZE: 'stm32h743zi',
  STM32U5A5ZJ: 'stm32u5a5zj',
  STM32H7R7: 'stm32h7r7',
  STM32H7B3VI: 'stm32h7b3vi',
};

export const OPENOCD_TARGET_MAP = {
  CH32F20x: 'target/ch32f2x.cfg',
  CH32F207VC: 'target/ch32f2x.cfg',
  STM32F10x: 'target/stm32f1x.cfg',
  STM32F103RB: 'target/stm32f1x.cfg',
  STM32F103ZE: 'target/stm32f1x.cfg',
  STM32F4xx: 'target/stm32f4x.cfg',
  STM32H750XB: 'target/stm32h7x.cfg',
  STM32H743ZE: 'target/stm32h7x.cfg',
  STM32U5A5ZJ: 'target/stm32u5x.cfg',
  STM32H7R7: 'target/stm32h7x.cfg',
  STM32H7B3VI: 'target/stm32h7x.cfg',
  GD32F30x: 'target/gd32f3x0.cfg',
  'Generic-CM3': 'target/cortex_m.cfg',
  'Generic-CM4': 'target/cortex_m.cfg',
  'Generic-CM7': 'target/cortex_m.cfg',
  'Generic-CM33': 'target/cortex_m.cfg',
};

/** OpenOCD 接口（Python 工具里 OPENOCD_INTERFACE_MAP） */
export const OPENOCD_INTERFACES = [
  { key: 'daplink', label: 'CMSIS-DAP / DAPLink（默认）', cfg: 'interface/cmsis-dap.cfg' },
  { key: 'jlink', label: 'J-Link', cfg: 'interface/jlink.cfg' },
  { key: 'stlink', label: 'ST-Link', cfg: 'interface/stlink.cfg' },
  { key: 'ftdi', label: 'FTDI JTAGKey', cfg: 'interface/ftdi/jtagkey.cfg' },
];

/** 下拉里能直接选的 J-Link 器件（J-Link 也认其它型号，可直接手输） */
export const JLINK_DEVICES = [
  'STM32F103RB', 'STM32F103ZE', 'STM32F103C8', 'STM32F407VG', 'STM32F411RE',
  'STM32F746NG', 'STM32H743ZE', 'STM32H750XB', 'STM32H7B3VI', 'STM32H7R7',
  'STM32U5A5ZJ', 'CH32F207VC', 'CH32V307VC',
];

/** 常见 OpenOCD target cfg（也可手输） */
export const OPENOCD_TARGETS = [
  'target/stm32f1x.cfg', 'target/stm32f4x.cfg', 'target/stm32f7x.cfg',
  'target/stm32h7x.cfg', 'target/stm32u5x.cfg', 'target/stm32f0x.cfg',
  'target/stm32l4x.cfg', 'target/ch32f2x.cfg', 'target/gd32f3x0.cfg',
  'target/cortex_m.cfg',
];

// ============================================================================
// .uvprojx 解析（纯正则，浏览器 / Node 都能跑，不依赖 DOMParser）
// ============================================================================

const pick = (text, re) => {
  const m = text.match(re);
  return m ? m[1].trim() : '';
};

/** `IRAM(0x20000000,0x5000)` 和 `IRAM(0x20000000-0x20004FFF)` 两种写法都要认 */
function parseRegion(cpuText, tag){
  const comma = cpuText.match(new RegExp(tag + '\\((0x[0-9a-fA-F]+)\\s*,\\s*(0x[0-9a-fA-F]+)\\)'));
  if (comma) return { start: comma[1], size: comma[2] };
  const dash = cpuText.match(new RegExp(tag + '\\((0x[0-9a-fA-F]+)\\s*-\\s*(0x[0-9a-fA-F]+)\\)'));
  if (dash){
    const s = parseInt(dash[1], 16), e = parseInt(dash[2], 16);
    return { start: dash[1], size: '0x' + (e - s + 1).toString(16) };
  }
  return null;
}

const normAddr = a => {
  if (!a) return '';
  const n = parseInt(a, 16);
  return isNaN(n) ? a : '0x' + n.toString(16).padStart(8, '0');
};

/** 大小不补零（0x5000 比 0x00005000 好读） */
const normSize = a => {
  if (!a) return '';
  const n = parseInt(a, 16);
  return isNaN(n) ? a : '0x' + n.toString(16);
};

/**
 * 从 .uvprojx 文本里抠出能自动填的东西。
 * @returns {{device,cpu,clock,flashStart,flashSize,ramStart,ramSize,targetName,packages}}
 */
export function parseUvprojx(xml){
  const text = String(xml || '');
  const cpuText = pick(text, /<Cpu>([\s\S]*?)<\/Cpu>/);
  const irom = parseRegion(cpuText, 'IROM');
  const iram = parseRegion(cpuText, 'IRAM');
  return {
    device: pick(text, /<Device>([^<]*)<\/Device>/),
    cpu: pick(cpuText, /CPUTYPE\("([^"]+)"\)/),
    clock: pick(cpuText, /CLOCK\((\d+)\)/),
    flashStart: irom ? normAddr(irom.start) : '',
    flashSize: irom ? normSize(irom.size) : '',
    ramStart: iram ? normAddr(iram.start) : '',
    ramSize: iram ? normSize(iram.size) : '',
    targetName: pick(text, /<TargetName>([^<]*)<\/TargetName>/),
  };
}

/** 器件名 → 映射表里的 key（精确命中优先，其次按前缀归族） */
export function matchFamily(device){
  const up = String(device || '').trim().toUpperCase();
  if (!up) return '';
  if (JLINK_DEVICE_MAP[up] || PYOCD_TARGET_MAP[up] || OPENOCD_TARGET_MAP[up]) return up;

  const prefix = [
    [/^STM32F10/, 'STM32F103RB'],   // Python 的 legacy 别名 STM32F10x → STM32F103RB
    [/^CH32F20/, 'CH32F207VC'],
  ];
  for (const [re, key] of prefix) if (re.test(up)) return key;
  if (/^STM32F4/.test(up)) return 'STM32F4xx';
  if (/^GD32F30/.test(up)) return 'GD32F30x';
  return up;   // 表外的器件原样透传：J-Link 本身就认具体型号
}

/** 由器件名推 J-Link / PyOCD / OpenOCD 三项参数 */
export function suggestFromDevice(device){
  const fam = matchFamily(device);
  const dev = String(device || '').trim().toUpperCase();
  return {
    jlinkDevice: JLINK_DEVICE_MAP[fam] || dev || PARAM_DEFAULTS.jlinkDevice,
    pyocdTarget: PYOCD_TARGET_MAP[fam] || dev.toLowerCase() || PARAM_DEFAULTS.pyocdTarget,
    openocdTarget: OPENOCD_TARGET_MAP[fam] || 'target/cortex_m.cfg',
  };
}

// ============================================================================
// 生成
// ============================================================================

const enc = new TextEncoder();

/** 统一成 LF 再按需转 CRLF：模板不管怎么写都不会被转两次 */
export function normalizeNewlines(s){
  return String(s).replace(/\r\n/g, '\n');
}

export function toBytes(text, newline = 'crlf'){
  const lf = normalizeNewlines(text);
  return enc.encode(newline === 'lf' ? lf : lf.replace(/\n/g, '\r\n'));
}

/** 只保留"和模板默认值不同"的覆盖项 —— 保证默认参数下产物与 Python 工具逐字节相同 */
function pruneOverrides(obj){
  const out = {};
  for (const [k, v] of Object.entries(obj)){
    if (v === undefined || v === null || v === '') continue;
    const dflt = PARAM_DEFAULTS[k];
    if (dflt !== undefined && String(dflt) === String(v)) continue;
    out[k] = v;
  }
  return out;
}

/**
 * 参数 → 待写出的文件。
 * @param {object} p 见 PARAM_DEFAULTS，外加 checks:{jlink,gdb,pyocd,openocd,testBin} 与 newline
 * @returns {{name:string,data:Uint8Array,text:string|null,bin:boolean}[]}
 */
export function buildOutputs(p){
  const checks = p.checks || {};
  const newline = p.newline === 'lf' ? 'lf' : 'crlf';
  // 默认套修正；`fixes: null` = 原汁原味（对账基线用）
  const fixes = p.fixes === undefined ? ALL_FIXES : p.fixes;
  const asText = (name, raw) => {
    const lf = applyFixes(name, normalizeNewlines(raw), fixes);
    return { name, data: toBytes(lf, newline), text: lf, bin: false };
  };

  const base = {
    projectName: p.projectName || PARAM_DEFAULTS.projectName,
    flashStart: p.flashStart || PARAM_DEFAULTS.flashStart,
    rttAddress: p.rttAddress || PARAM_DEFAULTS.rttAddress,
  };
  const tune = pruneOverrides({
    jlinkIf: p.jlinkIf, jlinkSpeed: p.jlinkSpeed, rttSize: p.rttSize, gdbPort: p.gdbPort,
    buildDir: p.buildDir, cmakeGenerator: p.cmakeGenerator, buildType: p.buildType,
    pyocdFreq: p.pyocdFreq, openocdFreq: p.openocdFreq, rttPort: p.rttPort,
  });

  const files = [];
  if (checks.jlink){
    files.push(asText('Makefile.jlink', renderJlinkMakefile({
      ...base, ...tune,
      jlinkDevice: p.jlinkDevice || PARAM_DEFAULTS.jlinkDevice,
    })));
  }
  if (checks.gdb){
    files.push(asText('jlink_gdb.script', renderGdbScript({ projectName: base.projectName, gdbPort: tune.gdbPort })));
  }
  if (checks.pyocd){
    files.push(asText('Makefile.pyocd', renderPyocdMakefile({
      ...base, ...tune,
      pyocdTarget: p.pyocdTarget || PARAM_DEFAULTS.pyocdTarget,
    })));
  }
  if (checks.openocd){
    files.push(asText('Makefile.openocd', renderOpenocdMakefile({
      ...base, ...tune,
      openocdRoot: p.openocdRoot || '',
      openocdInterface: p.openocdInterface || PARAM_DEFAULTS.openocdInterface,
      openocdTarget: p.openocdTarget || PARAM_DEFAULTS.openocdTarget,
    })));
    files.push(asText('rtt_logger.py', renderRttLogger()));   // 与 Python 工具一致：勾 OpenOCD 附带
  }
  if (checks.testBin){
    const size = parseInt(p.testBinSize, 10) || parseInt(PARAM_DEFAULTS.testBinSize, 10);
    files.push({ name: 'test_sram.bin', data: makeTestSramBin(size), text: null, bin: true });
  }
  return files;
}

/** 只要文件名（预览列表 / 冲突检查用） */
export function outputNames(p){
  return buildOutputs(p).map(f => f.name);
}

/** 当前参数下实际生效的修正项（状态栏显示用） */
export function appliedFixes(p){
  const fixes = p?.fixes === undefined ? ALL_FIXES : p.fixes;
  return FIX_KEYS.filter(k => fixes && fixes[k]);
}
