import { webClockLimitKhz } from '../core/target-clock.js';
import { hpmBoard } from '../targets/hpm/porting.js';

const F1_DP = 0x1ba01477;
const TARGETS = new Map([
  ['target/stm32f1x.cfg', { family:'stm32f1', chip:'stm32f1x', dpidr:F1_DP, workAreaSize:0x1000,
    ignored:['Flash bank', 'SRST 引脚延时', 'STM32F1 调试冻结事件', 'TPIU / trace 设置'] }],
  ['target/nrf52.cfg', { family:'nrf52', chip:'nrf52', dpidr:0x2ba01477, workAreaSize:0x4000,
    ignored:['Flash bank', 'nRF52 AP 锁检测及擦除解锁命令'] }],
  ['target/gd32vf103.cfg', { family:'riscv', chip:'gd32vf103', tapIdcode:0x1000563d, irLength:5, workAreaSize:0x1800,
    ignored:['Flash bank', 'GD32VF103 调试冻结事件'] }],
]);
const BUILTIN_HELPERS = new Set(['target/swj-dp.tcl', 'mem_helper.tcl', 'interface/cmsis-dap.cfg']);

export function cfgPath(path){
  if (/[\u0000-\u001f\\:\[\]{}$;'"<>|?*]/.test(path) || path.split('/').some(part => !part || part === '..'))
    throw new Error(`CFG 路径必须位于所选脚本目录内（${path}）`);
  const normalized = path.split('/').filter(part => part !== '.').join('/');
  if (!normalized) throw new Error('CFG 路径缺少文件名');
  return normalized;
}

export async function readCfgDirectory(files){
  const scripts = new Map();
  let root, bytes = 0;
  for (const file of files){
    if (!/\.(?:cfg|tcl)$/i.test(file.name)) continue;
    const relative = file.webkitRelativePath;
    const split = relative?.indexOf('/') ?? -1;
    if (split <= 0) throw new Error('请选择包含 CFG 的目录');
    root ??= relative.slice(0, split);
    if (relative.slice(0, split) !== root) throw new Error('CFG 文件必须来自同一目录');
    const path = cfgPath(relative.slice(split + 1));
    if (scripts.has(path)) throw new Error(`CFG 路径重复（${path}）`);
    scripts.set(path, file);
    bytes += file.size;
    if (scripts.size > 4096 || bytes > 8 * 1024 * 1024)
      throw new Error('CFG 目录最多支持 4096 个脚本和 8 MiB 文件内容');
  }
  if (![...scripts.keys()].some(path => /\.cfg$/i.test(path))) throw new Error('所选目录没有入口 CFG');
  return new Map(await Promise.all([...scripts].map(async ([path, file]) => [path, await file.text()])));
}

function normalizeSource(text){
  if (text.length > 65536) throw new Error('单个 CFG/Tcl 文件最大支持 64 KiB');
  return text.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n').replace(/\\\n[ \t]*/g, ' ');
}

// Split the limited Tcl syntax used by the STM32F1 profiles. Group bodies stay
// inert unless they are the selected arm of a supported configuration condition.
function* commands(source){
  const stack = [];
  let words = [], word = '';
  const closing = { '{': '}', '[': ']', '"': '"' };
  for (let i = 0; i <= source.length; i++){
    const c = source[i] ?? '\n';
    const top = stack.at(-1);
    if (!top && !word && !words.length && c === '#'){
      const end = source.indexOf('\n', i);
      if (end < 0) break;
      i = end;
      continue;
    }
    if (!top && c === '\\') throw new Error('CFG 仅支持行续接反斜杠');
    if (!top && /\s|;/.test(c)){
      if (word) words.push(word);
      word = '';
      if ((c === '\n' || c === ';') && words.length){
        yield words;
        words = [];
      }
      continue;
    }
    if (c === top) stack.pop();
    else if (top !== '"' && closing[c]) stack.push(closing[c]);
    else if (top !== '"' && /[}\]]/.test(c)) throw new Error('CFG 分组符号不匹配');
    word += c;
    if (stack.length > 16) throw new Error('CFG 分组嵌套过深');
  }
  if (stack.length) throw new Error('CFG 存在未闭合的分组');
}

const groupBody = word => {
  if (!word?.startsWith('{') || !word.endsWith('}')) throw new Error('预期 CFG 命令块');
  return word.slice(1, -1).trim();
};

function integer(word, label){
  if (!/^(?:0|[1-9]\d*|0x[\da-f]+)$/i.test(word) || Number(word) > 0xffffffff)
    throw new Error(`CFG 的 ${label} 需要无符号整数`);
  return Number(word);
}

function options(words, allowed){
  const pairs = new Map();
  for (let i = 0; i < words.length; i += 2){
    const key = words[i];
    if (!allowed.includes(key) || pairs.has(key) || words[i + 1] == null)
      throw new Error(`不支持的 CFG 参数 ${key}`);
    pairs.set(key, words[i + 1]);
  }
  return pairs;
}

/** Import single-target Cortex-M/SWD or RISC-V/JTAG configuration as data. */
export function importOpenOcdCfg(text, name = '', files = null){
  const vars = new Map();
  const ignored = new Set();
  const boardEntry = name.startsWith('board/');
  const hpm = name.split('/').at(-1).toLowerCase() === 'hpm6800evk.cfg' ? hpmBoard('hpm6800evk') : null;
  const stack = [], sourced = new Set();
  let expandedLength = 0;
  let hpmBoardDeclaration = false;
  let chip = '', tap = '', dap = '', target = '', family, transport, tapType;
  let dpidr, tapIdcode, irLength, hart = 0, clockKhz, workAreaBase, workAreaSize, board = false;
  let hasFsmcProcedure = false;

  const scalar = (word, substitute = true) => {
    const value = substitute ? word.replace(/\$([a-z_]\w*)/gi, (_, name) => {
      if (!vars.has(name)) throw new Error(`CFG 变量 ${name} 尚未定义`);
      return vars.get(name);
    }) : word;
    if (!/^[\w.:-]+$/.test(value)) throw new Error(`不支持的 CFG 值 ${word}`);
    return value;
  };
  const literal = word => {
    if (/^\{[^{}]*\}$/.test(word)) return scalar(word.slice(1, -1), false);
    return scalar(/^"[^"]*"$/.test(word) ? word.slice(1, -1) : word);
  };

  function supportedCondition(word){
    const condition = word.startsWith('{') ? groupBody(word) : word;
    const exists = /^(!?)\s*\[\s*info\s+exists\s+([a-z_]\w*)\s*\]$/i.exec(condition);
    if (exists) return vars.has(exists[2]) !== (exists[1] === '!');
    if (/^!?\[using_(?:jtag|hla|swd)\]$/.test(condition))
      return (condition.replace(/^!?\[using_|\]$/g, '') === (transport ?? tapType ?? 'swd')) !== condition.startsWith('!');
    throw new Error(`不支持的 CFG 条件 ${word}`);
  }

  function builtinTarget(profile){
    if (tap || target) throw new Error('CFG 只支持一个目标');
    board = true;
    family = profile.family;
    chip = vars.get('CHIPNAME') ?? profile.chip;
    target = `${chip}.${profile.cpu ?? 'cpu'}`;
    tapType = family === 'riscv' ? 'jtag' : 'swd';
    if (tapType === 'jtag') ({ tapIdcode, irLength } = profile);
    else dpidr = integer(vars.get('CPUTAPID') ?? profile.dpidr, 'CPUTAPID');
    workAreaBase = profile.workAreaBase ?? 0x20000000;
    workAreaSize = integer(vars.get('WORKAREASIZE') ?? profile.workAreaSize, 'WORKAREASIZE');
    clockKhz = profile.clockKhz ?? 1000;
    vars.set('_CHIPNAME', chip);
    vars.set('_TARGETNAME', target);
    for (const item of profile.ignored) ignored.add(item);
  }

  function readFile(path, body){
    if (stack.includes(path)) throw new Error(`CFG 循环引用：${[...stack, path].join(' → ')}`);
    if (stack.length >= 16) throw new Error('CFG 文件引用超过 16 层');
    expandedLength += body.length;
    if (expandedLength > 1024 * 1024) throw new Error('CFG 展开后超过 1 MiB');
    stack.push(path);
    try { read(normalizeSource(body)); }
    catch (e){
      if (e.cfgPath) throw e;
      const error = new Error(`${path || '入口 CFG'}：${e.message}`, { cause:e });
      error.cfgPath = path || '入口 CFG';
      throw error;
    } finally { stack.pop(); }
  }

  function sourceFile(word){
    const find = /^\[find\s+(.+?)\s*\]$/.exec(word);
    const raw = find?.[1] ?? word;
    const quoted = /^"[^"]*"$|^\{[^{}]*\}$/.test(raw);
    if (!quoted && /\s/.test(raw)) throw new Error('CFG 引用必须指定单个文件');
    const path = cfgPath(quoted ? raw.slice(1, -1) : raw);
    const relative = !find && stack.at(-1)?.includes('/')
      ? stack.at(-1).slice(0, stack.at(-1).lastIndexOf('/') + 1) + path : path;
    if (path === 'interface/jlink.cfg'){
      ignored.add('板载 J-Link 接口（使用 WebUSB CMSIS-DAP）');
      return;
    }
    if (BUILTIN_HELPERS.has(path)) return;
    const resolved = files?.has(relative) ? relative : path;
    if (files?.has(resolved)){
      sourced.add(resolved);
      readFile(resolved, files.get(resolved));
    } else if (!files && TARGETS.has(path)) builtinTarget(TARGETS.get(path));
    else throw new Error(`缺少 CFG 引用 ${path}，请选择包含依赖的脚本目录`);
  }

  function read(script){
    for (const [op, ...args] of commands(script)){
      const [a, b, c, d] = args;
      const shape = args.join(' ');
      if (op === 'if'){
        if (args.length !== 2 && args.length !== 3 && !(args.length === 4 && c === 'else'))
          throw new Error('CFG 仅支持有限的 if/else 语法');
        const yes = groupBody(b), no = c ? groupBody(c === 'else' ? d : c) : '';
        read(supportedCondition(a) ? yes : no);
      } else if (op === 'set' && args.length === 2 && /^[a-z_]\w*$/i.test(a)){
        vars.set(a, literal(b));
      } else if (op === 'source' && args.length === 1){
        sourceFile(a);
      } else if (op === 'transport' && ['select swd', 'select jtag'].includes(shape)){
        if (transport && transport !== b) throw new Error('CFG 混用了 SWD 和 JTAG');
        transport = b;
      } else if (op === 'adapter' && shape === 'driver cmsis-dap'){
        // The WebUSB session uses CMSIS-DAP.
      } else if (op === 'adapter' && a === 'speed' && args.length === 2){
        clockKhz = integer(scalar(b), 'adapter speed');
      } else if (op === 'swj_newdap' || (op === 'swd' && a === 'newdap') || (op === 'jtag' && a === 'newtap')){
        if (tap || board) throw new Error('CFG 只支持单个 DAP 或 JTAG TAP');
        tapType = op === 'jtag' ? 'jtag' : 'swd';
        const [device, tag, ...rest] = op === 'swj_newdap' ? args : args.slice(1);
        const opts = options(rest, ['-expected-id', '-irlen', '-ircapture', '-irmask']);
        chip = scalar(device ?? '');
        tap = `${chip}.${scalar(tag ?? '')}`;
        if (tapType === 'jtag'){
          tapIdcode = integer(scalar(opts.get('-expected-id') ?? ''), 'TAP -expected-id');
          irLength = integer(scalar(opts.get('-irlen') ?? ''), 'TAP -irlen');
        } else if (opts.has('-expected-id')) dpidr = integer(scalar(opts.get('-expected-id')), '-expected-id');
      } else if (op === 'dap' && a === 'create' && args.length === 4 &&
        ['-chain-position', '-tap'].includes(c) && tapType === 'swd' && scalar(d) === tap && !dap){
        dap = scalar(b);
      } else if (op === 'target' && a === 'create' && c === 'cortex_m' && dap && !target){
        const opts = options(args.slice(3), ['-dap', '-endian']);
        if (scalar(opts.get('-dap') ?? '') !== dap || scalar(opts.get('-endian') ?? 'little') !== 'little')
          throw new Error('CFG 目标必须是已声明 DAP 上的小端 Cortex-M');
        target = scalar(b);
      } else if (op === 'target' && a === 'create' && c === 'riscv' && tapType === 'jtag' && !target){
        const opts = options(args.slice(3), ['-chain-position', '-coreid']);
        if (scalar(opts.get('-chain-position') ?? '') !== tap) throw new Error('CFG 的 RISC-V 核必须绑定已声明的单个 TAP');
        hart = integer(scalar(opts.get('-coreid') ?? '0'), 'hart');
        family = 'riscv';
        target = scalar(b);
      } else if (op === 'riscv' && a === 'set_mem_access' && b === 'sysbus'){
        if (args.length > 2) ignored.add('RISC-V 备用内存访问方式（使用 SBA）');
      } else if ((op === 'reset_config' && ['srst_nogate', 'none'].includes(shape)) ||
        (op === 'cortex_m' && shape === 'reset_config sysresetreq')){
        // Reset is provided by the existing Cortex-M SYSRESETREQ path.
      } else if (op === 'adapter' && a === 'srst' && b === 'delay' && args.length === 3){
        integer(scalar(c), 'SRST 延时');
        ignored.add('SRST 引脚延时');
      } else if (op === 'flash' && a === 'bank' && args.length >= 7){
        if (tapType === 'swd' && c === 'stm32f1x' && integer(scalar(d), 'Flash 基址') === 0x08000000) family ??= 'stm32f1';
        if (hpm && b === 'xpi0' && c === 'hpm_xpi' &&
          integer(d, 'Flash 基址') === hpm.flashBase && integer(args[4], 'Flash 大小') === hpm.flashSize &&
          args[7] === '$_TARGET0' && integer(args[8], 'XPI 基址') === hpm.xpiBase &&
          integer(args[9], 'XPI option0') === hpm.option0) hpmBoardDeclaration = true;
        ignored.add('Flash bank');
      } else if ((op === 'tpiu' && a === 'create') || (op === 'lappend' && a === '_telnet_autocomplete_skip') ||
        (op === 'proc' && a?.startsWith('_proc_pre_enable_')) ||
        (op.endsWith('.tpiu') && a === 'configure' && b === '-event' && c === 'pre-enable')){
        ignored.add('TPIU / trace 设置');
      } else if (op === 'proc' && a === 'stm32_enable_fsmc' && b === '{}' && args.length === 3){
        groupBody(c);
        hasFsmcProcedure = true;
      } else if (op === 'proc' && ['nrf52_check_ap_lock', 'nrf52_recover'].includes(a) && b === '{}' && args.length === 3){
        groupBody(c);
        ignored.add('nRF52 AP 锁检测及擦除解锁命令');
      } else if (op === 'add_help_text' && a === 'nrf52_recover' && args.length === 2){
        ignored.add('nRF52 AP 锁检测及擦除解锁命令');
      } else if (hpm && op === 'proc' && ['init_clock', 'init_ddr3', 'init_dram'].includes(a) && b === '{}' && args.length === 3){
        groupBody(c);
        ignored.add('HPM6800EVK 时钟和 DDR 初始化');
      } else if (a === 'configure' && target && scalar(op) === target){
        if (b === '-event'){
          if (args.length !== 4) throw new Error('无效的 CFG 目标事件');
          if (/^gdb-flash-(?:erase|write)-(?:start|end)$/.test(c)){
            ignored.add('GDB Flash 操作事件');
          } else if (c === 'examine-fail' && d === 'nrf52_check_ap_lock'){
            family ??= 'nrf52';
            ignored.add('nRF52 AP 锁检测及擦除解锁命令');
          } else if (c === 'examine-end' && [...commands(groupBody(d))].map(w => w.join(' ').toLowerCase()).join(';') ===
            'mmw 0xe0042004 0x00000307 0') ignored.add('STM32F1 调试冻结事件');
          else if (c === 'reset-init' && /^gd32vf103/i.test(chip) &&
            [...commands(groupBody(d))].map(w => w.join(' ').toLowerCase()).join(';') === 'mmw 0xe0042004 0x00000300 0')
            ignored.add('GD32VF103 调试冻结事件');
          else if (c === 'reset-init' && family === 'stm32f1' && (board || boardEntry) &&
            hasFsmcProcedure && groupBody(d) === 'stm32_enable_fsmc')
            ignored.add('板卡外部 FSMC 初始化');
          else if (hpm && c === 'reset-init' && /^init_clock\s+init_ddr3$/.test(groupBody(d)))
            ignored.add('HPM6800EVK 时钟和 DDR 初始化');
          else if (hpm && c === 'gdb-attach' && /^reset\s+halt$/.test(groupBody(d)))
            ignored.add('HPM6800EVK GDB 连接复位事件');
          else throw new Error(`不支持的 CFG 目标事件 ${c}`);
        } else {
          const opts = options(args.slice(1), ['-work-area-phys', '-work-area-size', '-work-area-backup']);
          workAreaBase = integer(scalar(opts.get('-work-area-phys') ?? ''), '工作区基址');
          workAreaSize = integer(scalar(opts.get('-work-area-size') ?? ''), '工作区大小');
          if (workAreaBase + workAreaSize > 0x100000000) throw new Error('CFG 工作区超出 32 位地址空间');
        }
      } else {
        throw new Error(`不支持的 CFG 指令 ${op} ${args.slice(0, 2).join(' ')}`.trimEnd());
      }
    }
  }
  if (hpm){
    // The SDK's boards/hpm6800evk.cfg is a board fragment. Its launcher sources
    // soc/hpm6880.cfg and probes/cmsis_dap.cfg before the board file.
    builtinTarget({ family:'riscv', chip:hpm.chipId, cpu:'cpu0', clockKhz:8000,
      tapIdcode:hpm.debug.tapIdcode, irLength:hpm.debug.irLength,
      workAreaBase:hpm.memory.workArea.addr, workAreaSize:hpm.memory.workArea.size, ignored:[] });
    vars.set('_TARGET0', target);
  }
  readFile(files ? cfgPath(name) : name, text);
  if (hpm && !hpmBoardDeclaration) throw new Error('CFG 缺少 HPM6800EVK SDK 板卡声明');
  if (!target) throw new Error('CFG 未定义受支持的 Cortex-M 或 RISC-V 目标');
  if (transport && transport !== tapType) throw new Error('CFG 的传输方式与目标声明不匹配');
  if (!family) family = /^stm32f1/i.test(chip) ? 'stm32f1' : /^nrf52/i.test(chip) ? 'nrf52' : 'cortex-m';
  clockKhz ??= 1000;
  if (family === 'stm32f1') dpidr ??= F1_DP;
  if (!clockKhz || clockKhz > webClockLimitKhz('arm')) throw new Error('CFG 的调试时钟必须在 1–60000 kHz');
  const common = { kind: board || boardEntry ? 'board' : 'target',
    family, target, clockKhz, workAreaBase, workAreaSize, ignored: [...ignored],
    ...(sourced.size ? { sourced:[...sourced] } : {}) };
  if (family === 'riscv'){
    if (!tapIdcode || tapIdcode === 0xffffffff || irLength < 5 || irLength > 8 || hart > 0x3ff)
      throw new Error('CFG 需要有效 TAP IDCODE、5–8 位 JTAG IR 和 0–1023 的 hart');
    return { ...common, arch:'riscv', tapIdcode, irLength, hart, ...(hpm ? { boardId:hpm.id } : {}) };
  }
  if (!dpidr) throw new Error('CFG 需要非零的 SWD DPIDR');
  return { ...common, arch:'arm', dpidr };
}
