/**
 * 「修正项」：对模板产物做**定点修补**。
 *
 * 背景：产物是与 Python 工具（uvprojx2cmake.py）逐字节一致的移植版，但那份模板里有几个
 * 用起来会硌人的地方。2026-09-27 用户逐条过了一遍，采纳下面 4 条（其余明确不采纳，
 * 见 docs/gen-page.md 的「已知取舍」）。
 *
 * 设计原则：
 *   1. **逐行定点替换**，不做全局替换；匹配不到期望内容就抛错 —— 绝不静默产出半成品。
 *   2. 用户自己填过的参数优先：例如 RTT 范围若被改成别的值，就不再套用修正。
 *   3. 全部不勾 = 与 Python 工具逐字节一致（tools/selftest/gen-parity.mjs 就是这么对账的）。
 *
 * 输入/输出都用 **LF** 换行（model.js 先 normalizeNewlines 再调这里，最后才按用户选的换行落盘）。
 */

/** 界面上的勾选项元数据（key 与 docs 保持一致） */
export const FIX_ITEMS = [
  {
    key: 'rttSize',
    label: 'RTT 搜索范围收进 RAM（0x5000 → 0x2000）',
    why: 'Python 模板的 0x5000 在 20KB RAM 的 F103 上会越过 RAM 顶（0x20002000+0x5000 > 0x20005000）',
    file: 'Makefile.jlink',
  },
  {
    key: 'cleanLogs',
    label: 'clean-jlink 不再删 *.log',
    why: '原来 `del *.log` 会把 J-Link 自己写的 JLinkLog.txt 一起删掉',
    file: 'Makefile.jlink',
  },
  {
    key: 'openocdQuotes',
    label: 'openocd-rtt 改用双引号（cmd.exe 也能跑）',
    why: "原来用 -c '...'，cmd.exe 里单引号不是引号 → 直接报错；改成双引号 + 内部转义",
    file: 'Makefile.openocd',
  },
  {
    key: 'dropSwo',
    label: '去掉 jlink-swo（SWO）目标',
    why: '硬编码 72MHz 只对 F103 成立，且固件里没开 PB3/TRACESWO，跑出来是空日志；用户表示基本不用',
    file: 'Makefile.jlink',
  },
];

export const FIX_KEYS = FIX_ITEMS.map(i => i.key);

const splitLines = text => String(text).split('\n');
const joinLines = lines => lines.join('\n');
const isBlank = l => l.trim() === '';

/** 变量默认行：`RTT_SIZE ?= 0x5000`（找不到就抛；被用户改过就跳过） */
function retuneDefault(lines, name, want, key){
  const re = new RegExp('^' + name + ' \\?= ');
  const idx = lines.findIndex(l => re.test(l));
  if (idx < 0) throw new Error(`[fixes:${key}] 产物里找不到 ${name} ?= 那一行（模板变了？）`);
  const cur = lines[idx].slice(lines[idx].indexOf('?=') + 2).trim();
  if (cur !== '0x5000') return false;          // 用户自己填过别的值 → 尊重用户
  lines[idx] = lines[idx].replace(/\?\=.*$/, '?= ' + want);
  return true;
}

/** 删掉一行（必须命中，否则抛） */
function dropLine(lines, test, key, what){
  const idx = lines.findIndex(test);
  if (idx < 0) throw new Error(`[fixes:${key}] 找不到要删的行：${what}`);
  lines.splice(idx, 1);
}

/** 删掉一个 make target：目标行 + 紧随的菜谱，以及紧挨着的注释行 */
function dropTarget(lines, target, key){
  const idx = lines.findIndex(l => l.startsWith(target + ':'));
  if (idx < 0) throw new Error(`[fixes:${key}] 找不到目标 ${target}`);
  let end = idx + 1;
  while (end < lines.length && !isBlank(lines[end])) end++;      // 菜谱行（以 Tab 开头）
  let start = idx;
  if (start > 0 && lines[start - 1].trim().startsWith('#')) start--;   // 连带它上面的注释
  lines.splice(start, end - start);
}

function fixJlink(lines, fixes){
  if (fixes.rttSize) retuneDefault(lines, 'RTT_SIZE', '0x2000', 'rttSize');

  if (fixes.cleanLogs){
    // 只删「删日志」那一行（注意菜谱行前面有 Tab），别的清理动作保留
    dropLine(lines, l => /^\s*@del \*\.log 2>nul \|\| rm -f \*\.log\s*$/.test(l), 'cleanLogs', '@del *.log …');
  }

  if (fixes.dropSwo){
    dropTarget(lines, 'jlink-swo', 'dropSwo');
    // 用法注释 + .PHONY + help 里的条目也要一起清掉，否则列着一个不存在的目标
    const drop = [
      l => /^#\s+make jlink-swo\b/.test(l),
      l => /jlink-swo\s+- Start SWO viewer/.test(l),
      l => /^\s*@echo "  jlink-swo\b/.test(l),
    ];
    for (const t of drop){
      const i = lines.findIndex(t);
      if (i >= 0) lines.splice(i, 1);
    }
    const ph = lines.findIndex(l => l.startsWith('.PHONY:'));
    if (ph < 0) throw new Error('[fixes:dropSwo] 找不到 .PHONY 行');
    lines[ph] = lines[ph].replace(/\bjlink-swo\s+/, '');
    const left = lines.filter(l => l.includes('jlink-swo'));
    if (left.length) throw new Error(`[fixes:dropSwo] 还有残留：${left[0]}`);
  }
}

function fixOpenocd(lines, fixes){
  if (!fixes.openocdQuotes) return;
  let n = 0;
  for (let i = 0; i < lines.length; i++){
    if (!/-c '/.test(lines[i])) continue;
    lines[i] = lines[i].replace(/-c '(.*)'\s*$/, (m, inner) => '-c "' + inner.replace(/"/g, '\\"') + '"');
    if (/-c '/.test(lines[i])) throw new Error('[fixes:openocdQuotes] 单引号没换干净：' + lines[i]);
    n++;
  }
  if (!n) throw new Error('[fixes:openocdQuotes] 一条 -c \'…\' 都没找到（模板变了？）');
}

/**
 * @param {string} name  文件名（Makefile.jlink / Makefile.openocd / …）
 * @param {string} text  渲染出来的文本（LF 换行）
 * @param {object} fixes {rttSize,cleanLogs,openocdQuotes,dropSwo} —— 缺省全部按"不修正"
 * @returns {string}
 */
export function applyFixes(name, text, fixes){
  if (!fixes) return text;
  const on = FIX_KEYS.some(k => fixes[k]);
  if (!on) return text;

  const lines = splitLines(text);
  if (name === 'Makefile.jlink') fixJlink(lines, fixes);
  else if (name === 'Makefile.openocd') fixOpenocd(lines, fixes);
  else return text;
  return joinLines(lines);
}
