#!/usr/bin/env node
/**
 * tools/dev/panel-json-to-c.mjs —— 把屏初始化资料转成能直接用的 **C 数组**。
 *
 * 两种输入：
 *  ① dump JSON（默认）：`E:\esp-idf-wsh\资料\panel_init\dumps\<id>.json`（44 份，从 SiFli-SDK
 *     `customer/peripherals/*\/*.c` 提取；行格式 `[cmd, [data...]]`）。
 *     ⚠️ **这份 dump 会把"零参数命令"整行丢掉**（2026-10 拿 GC9A01 与厂商源码逐条对出来的）：
 *     `{0xEF, 0}` / `{0xFE, 0}` / `{0x35, 0}` / `{0x21, 0}` 这类**没有数据字节**的行全没了 ——
 *     而它们恰恰是解锁寄存器、开反显的关键命令。所以 dump 模式会**主动告警**，关键屏请用下面 ②。
 *  ② 厂商 C 源码（`--from-c=<file>`）：直接吃 SDK 那张
 *     `static const uint8_t lcd_init_cmds[][MAX_CMD_LEN] = { {cmd, len, p0..}, ... }`，
 *     **零参数行不会丢**，且能发现"len 声明与实际给的字节数不符"的问题（会按声明长度补 0，与 SDK 行为一致）。
 *
 * 出：带来源注释的 `static const` 表，格式与网页「SPI/QSPI 屏」页『面板初始化』大框吃的
 *     C 数组完全一致（`{cmd, (uint8_t[]){...}, len, delay}`）—— **整段贴进页面就能解析**。
 *
 * 用法：
 *   node tools/dev/panel-json-to-c.mjs gc9a01                       # dump 模式
 *   node tools/dev/panel-json-to-c.mjs --from-c=vendor.c --id=gc9a01 \
 *        --before="0x01:120,0x11:120" --after="0x29:0" --last-delay=120
 *   node tools/dev/panel-json-to-c.mjs --all                        # 44 份 dump 全导
 *
 * 选项：--dumps=<目录> · --out=<文件> · --symbol=<数组名> · --type=<元素类型名> · --note=<补充说明，可重复>
 *       --from-c=<文件>（厂商源码模式）· --id=<面板 id>（配合 --from-c）· --max-len=<N>
 *       --before="cmd[:delay],…" / --after="cmd[:delay],…"（表外补命令，来自驱动的 LCD_Init）
 *       --last-delay=<ms>（把"表后延时"挂到原表最后一条命令上）· --all
 */
import fs from 'node:fs';
import path from 'node:path';

const DUMPS_DEFAULT = 'E:\\esp-idf-wsh\\资料\\panel_init\\dumps';
const OUT_DIR_DEFAULT = path.join('samples', 'panel-init');
const argv = process.argv.slice(2);
const opt = (k, d = null) => { const a = argv.find(x => x.startsWith(`--${k}=`)); return a ? a.split('=').slice(1).join('=') : d; };
const notes = argv.filter(x => x.startsWith('--note=')).map(x => x.split('=').slice(1).join('='));
const ALL = argv.includes('--all');
const dumpsDir = opt('dumps', DUMPS_DEFAULT);
const FROM_C = opt('from-c');

const hex2 = v => '0x' + (v & 0xff).toString(16).toUpperCase().padStart(2, '0');
const cfgText = j => (j.configs || []).map(c =>
  `${c.name || '?'}: ${c.itf || '?'}${c.freq ? ` · ${(c.freq / 1e6).toFixed(c.freq % 1e6 ? 1 : 0)} MHz` : ''}${c.color_mode ? ` · ${c.color_mode}` : ''}`
).join('\n *           ');

/** "0x11:120,0x29:0" → [{cmd:0x11,delay:120},{cmd:0x29,delay:0}] */
function parseSpec(s){
  if (!s) return [];
  return s.split(',').map(x => x.trim()).filter(Boolean).map(x => {
    const [c, d] = x.split(':');
    const cmd = c.trim().toLowerCase().startsWith('0x') ? parseInt(c, 16) : Number(c);
    if (!Number.isFinite(cmd)) throw new Error(`--before/--after 里的命令看不懂：${x}`);
    return { cmd: cmd & 0xff, data: [], delay: d ? Number(d) | 0 : 0 };
  });
}

/**
 * 厂商 C 源码模式：抠出 `static const uint8_t <name>[][MAX] = { {cmd, len, p…}, … }`
 * 语义完全按 SDK 的消费代码来：`LCD_WriteReg(hlcdc, row[0], &row[2], row[1])`
 *   ⇒ 命令 = row[0]，长度 = row[1]（**声明值**），数据 = row[2..2+len-1]（不够的按 0 补，因为数组是零初始化的）。
 */
function parseVendorC(src){
  const m = src.match(/static\s+const\s+uint8_t\s+(\w+)\s*\[\s*\]\s*\[\s*([A-Za-z_]\w*|\d+)\s*\]\s*=\s*\{([\s\S]*?)\n\};/);
  if (!m) throw new Error('源码里没找到 `static const uint8_t <表名>[][N] = { … };` 形态的初始化表');
  const rows = [];
  const quirks = [];
  for (const raw of m[3].split('\n')){
    /* ⚠️ 先去掉行尾的 \r：`.` 在 JS 里不匹配 \r，`//…$` 这种剥注释会**静默失败**，
     *    于是 `{0xFE, 0},  // critical` 这类带行尾注释的行整行被漏掉（本会话踩过：GC9A01 少了 3 条）。 */
    const line = raw.replace(/\r/g, '').replace(/\/\/.*$/, '').trim();
    const g = line.match(/^\{([^}]*)\},?$/);
    if (!g) continue;
    const nums = [];
    for (const tok of g[1].split(',')){
      const t = tok.trim();
      if (!t) continue;
      const v = /^0x/i.test(t) ? parseInt(t, 16) : Number(t);
      if (Number.isFinite(v)) nums.push(v & 0xff);
    }
    if (!nums.length) continue;
    const cmd = nums[0];
    const len = nums.length > 1 ? nums[1] : 0;
    const given = nums.slice(2);
    const data = [];
    for (let i = 0; i < len; i++) data.push(i < given.length ? given[i] : 0x00);
    if (given.length && given.length !== len) quirks.push({ cmd, len, given: given.length });
    rows.push({ cmd, data, delay: 0 });
  }
  return { rows, quirks, tableName: m[1], maxLen: m[2] };
}

/** dump JSON 模式 */
function rowsFromDump(j){
  const rows = [];
  for (const t of j.tables || []) for (const r of t.rows || []){
    const cmd = (Array.isArray(r) ? r[0] : r) | 0;
    const data = Array.isArray(r?.[1]) ? r[1].map(x => x & 0xff) : [];
    rows.push({ cmd: cmd & 0xff, data, delay: typeof r?.[2] === 'number' ? r[2] | 0 : 0 });
  }
  return rows;
}

function render({ id, rows, header, quirks = [], extraNotes = [], symbol, type }){
  const lines = [];
  const params = rows.reduce((s, r) => s + r.data.length, 0);
  const delays = rows.filter(r => r.delay).length;
  lines.push('/*');
  lines.push(` * ${String(id).toUpperCase()} 初始化序列 —— **由脚本生成，别手改**`);
  lines.push(' *');
  lines.push(' *   生成器: tools/dev/panel-json-to-c.mjs');
  for (const h of header) lines.push(' *   ' + h);
  lines.push(` *   规模  : 命令 ${rows.length} 条 · 参数 ${params} B · 带延时 ${delays} 条` +
             `（延时合计 ${rows.reduce((s, r) => s + (r.delay || 0), 0)} ms）`);
  lines.push(' *');
  if (quirks.length){
    lines.push(' * ⚠️ 源码里"长度声明与实际给的字节数不符"的行（本表**按声明长度补 0**，与 SDK 行为一致）：');
    for (const q of quirks) lines.push(` *      ${hex2(q.cmd)}: 声明 ${q.len} B，源码只写了 ${q.given} B`);
  }
  lines.push(' * ⚠️ 表内**没有延时项**，也**不含** 0x11（sleep out）/ 0x29（display on）——');
  lines.push(' *    SDK 驱动把它们写在初始化表**外面**（本文件已按 LCD_Init 的次序把表外命令补进来，见下面的分隔注释）。');
  lines.push(' *    上屏前还要自己补：复位（RST 低 → 20 ms → 高 → ≥120 ms）；IPS 圆屏（GC9A01 那类）需要 0x21 开反显。');
  for (const n of extraNotes) lines.push(' * ⚠️ ' + n);
  lines.push(' *');
  lines.push(' * 格式: {cmd, data, data_bytes, delay_ms} —— delay 是**该命令之后**的延时。');
  lines.push(' * 用法: 整段贴进网页「SPI/QSPI 屏」页的『面板初始化』大框即可解析（本来就是 C 数组）。');
  lines.push(' */');
  lines.push(`static const ${type} ${symbol}[] = {`);
  for (const r of rows){
    if (r.mark) lines.push(`    // ${r.mark}`);
    lines.push(`    {${hex2(r.cmd)}, ${r.data.length ? `(uint8_t[]){${r.data.map(hex2).join(', ')}}` : 'NULL'}, ${r.data.length}, ${r.delay || 0}},`);
  }
  lines.push('};');
  lines.push('');
  return { text: lines.join('\n'), rows, params, delays };
}

const mkdirp = d => fs.mkdirSync(d, { recursive: true });
const write = (out, r) => {
  mkdirp(path.dirname(out));
  fs.writeFileSync(out, r.text, 'utf8');
  console.log(`✓ ${out}`);
  console.log(`   ${r.rows.length} 条命令 · 参数 ${r.params} B · 带延时 ${r.delays} 条`);
};

function one(idOrPath){
  const p = idOrPath.endsWith('.json') ? idOrPath : path.join(dumpsDir, `${idOrPath}.json`);
  if (!fs.existsSync(p)) throw new Error(`找不到 dump：${p}`);
  const j = JSON.parse(fs.readFileSync(p, 'utf8'));
  const id = j.id || path.basename(p, '.json');
  const rows = rowsFromDump(j);
  // 🚨 dump 会丢"零参数命令"——检测不到任何零参数行就告警（GC9A01 就是这么丢了 0xEF/0xFE/0x35/0x21）
  if (!rows.some(r => r.data.length === 0)) {
    console.log(`   ⚠️ 这张表里**一条零参数命令都没有** —— dump 很可能把 {0xEF,0}/{0x11,0}/{0x29,0} 这类行丢掉了；`);
    console.log(`      关键屏请改用厂商源码：--from-c=<该屏的 .c>（GC9A01 实测丢了 5 条，含 2 条 critical）`);
  }
  write(opt('out') || path.join(OUT_DIR_DEFAULT, `${id}_init_cmds.h`), render({
    id,
    rows,
    symbol: opt('symbol', `${id}_init_cmds`),
    type: opt('type', 'lcd_init_cmd_t'),
    extraNotes: notes,
    header: [
      `来源  : ${p}`,
      `          提取自 SiFli-SDK 的 ${j.file || '?'}${j.registered_name ? `（注册名 ${j.registered_name}）` : ''}`,
      ...(j.configs || []).length ? [`接口  : ${cfgText(j)}`] : [],
      `分辨率: ${j.res_w && j.res_h ? `${j.res_w}×${j.res_h}` : 'dump 里为 null（取自板级宏）—— 别当成已知值'}`,
    ],
  }));
}

function oneFromC(srcPath){
  const src = fs.readFileSync(srcPath, 'utf8');
  const parsed = parseVendorC(src);
  const id = opt('id') || path.basename(srcPath).replace(/\.[^.]+$/, '');
  const before = parseSpec(opt('before'));
  const after = parseSpec(opt('after'));
  const lastDelay = Number(opt('last-delay') || 0) | 0;
  const rows = [];
  for (const r of before) rows.push({ ...r, mark: '↓ 表外（驱动 LCD_Init 里、初始化表之前）' });
  const body = parsed.rows.map(r => ({ ...r }));
  if (lastDelay && body.length) body[body.length - 1].delay = lastDelay;
  rows.push(...body);
  for (const r of after) rows.push({ ...r, mark: '↓ 表外（驱动 LCD_Init 里、初始化表之后）' });
  const cfgLine = (src.match(/\.lcd_itf\s*=\s*([A-Z_0-9]+)/) || [])[1];
  const freq = (src.match(/\.freq\s*=\s*(\d+)/) || [])[1];
  const color = (src.match(/\.color_mode\s*=\s*([A-Z_0-9]+)/) || [])[1];
  const exportName = (src.match(/LCD_DRIVER_EXPORT2\(\s*(\w+)/) || [])[1];
  write(opt('out') || path.join(OUT_DIR_DEFAULT, `${id}_init_cmds.h`), render({
    id,
    rows,
    symbol: opt('symbol', `${id}_init_cmds`),
    type: opt('type', 'lcd_init_cmd_t'),
    quirks: parsed.quirks,
    extraNotes: notes,
    header: [
      `来源  : ${srcPath}`,
      `          厂商源码（SiFli-SDK 同款）：表 ${parsed.tableName}[][${parsed.maxLen}]` +
        `${exportName ? ` · LCD_DRIVER_EXPORT2(${exportName})` : ''}`,
      ...(cfgLine || freq || color ? [`接口  : ${cfgLine || '?'}${freq ? ` · ${(freq / 1e6).toFixed(freq % 1e6 ? 1 : 0)} MHz` : ''}${color ? ` · ${color}` : ''}`] : []),
      `说明  : 表内 ${parsed.rows.length} 条来自源码；表外 ${before.length + after.length} 条按 LCD_Init 的次序补入（行前有注释标出）`,
    ],
  }));
}

if (ALL){
  const ids = fs.readdirSync(dumpsDir).filter(f => f.endsWith('.json')).map(f => path.basename(f, '.json'));
  console.log(`全量导出 ${ids.length} 份 → ${OUT_DIR_DEFAULT}\\`);
  let ok = 0, bad = 0;
  for (const id of ids){ try { one(id); ok++; } catch (e){ console.log(`✗ ${id}: ${e.message}`); bad++; } }
  console.log(`\n完成：成功 ${ok} · 失败 ${bad}`);
} else if (FROM_C){
  oneFromC(FROM_C);
} else {
  const target = argv.find(a => !a.startsWith('--'));
  if (!target){
    console.log('用法：node tools/dev/panel-json-to-c.mjs <id|dump.json> [--out=…] [--symbol=…] [--type=…] [--note=…]');
    console.log('      node tools/dev/panel-json-to-c.mjs --from-c=<厂商.c> --id=<id> [--before="0x01:120,0x11:120"] [--after="0x29:0"] [--last-delay=120]');
    console.log('      node tools/dev/panel-json-to-c.mjs --all');
    process.exit(1);
  }
  one(target);
}
