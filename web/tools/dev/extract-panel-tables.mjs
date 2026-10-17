/**
 * 从 ESP-IDF 工程里把两块屏的**原始 C 初始化数组**提取成 `app/spi/panels-data.js`。
 *   node tools/dev/extract-panel-tables.mjs
 *
 * 为什么要内置原始 C 文本而不是解析后的 JSON：
 *   页面上那个"大大的初始化面板"主打"**把 C 代码贴进去**"，所以内置表示例也应当是同一形态 ——
 *   载入示例 == 往文本框里填一段真实的 C 数组，用户看到的、能改的、能再复制走的都是同一份东西。
 *
 * 源文件（外部工程，只读）：
 *   E:\esp-idf-wsh\projects\spi_lcd_axs15352\main\axs15352_init_cmds.h
 *   E:\esp-idf-wsh\projects\qspi_lcd_st77916\main\st77916_init_cmds_ch32.h
 * 可用 ESP_PANEL_DIR 覆盖根目录（默认 E:\esp-idf-wsh）。
 *
 * 产物里的 `expect` 是源文件注释里自报的数字（条数/参数字节/累计延时），
 * 自测拿它跟"解析出来的"对账 —— 提取失真会当场暴露。
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');
const SRC_ROOT = process.env.ESP_PANEL_DIR || 'E:\\esp-idf-wsh';

const SPECS = [
  {
    key: 'axs15352',
    label: '天马 2P01 / AXS15352（240×296 · 4 线 SPI + DC）',
    file: join(SRC_ROOT, 'projects', 'spi_lcd_axs15352', 'main', 'axs15352_init_cmds.h'),
    rel: 'spi_lcd_axs15352/main/axs15352_init_cmds.h',
    expect: { rows: 30, params: 28, delays: 1 },
  },
  {
    key: 'st77916',
    label: 'ST77916（圆屏 360×360 · QSPI 四线）',
    file: join(SRC_ROOT, 'projects', 'qspi_lcd_st77916', 'main', 'st77916_init_cmds_ch32.h'),
    rel: 'qspi_lcd_st77916/main/st77916_init_cmds_ch32.h',
    expect: { rows: 192, paramsBytes: 215, delayMs: 120 },
  },
];

/** 抠出"能直接贴进页面"的那段：注释头 + 数组定义（去掉 #pragma/#include 这类编译指令）*/
function extract(text){
  const m = /static\s+const\s+[\w\s]+\w+\[\]\s*=\s*\{[\s\S]*?\n\};/m.exec(text);
  if (!m) throw new Error('没找到 `static const ...[] = { ... };` 数组');
  const head = text.slice(0, m.index)
    .split(/\r?\n/)
    .filter(l => !/^\s*#\s*(pragma|include)\b/.test(l))
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
  return (head ? head + '\n' : '') + m[0];
}

const out = {};
for (const s of SPECS){
  let text;
  try { text = readFileSync(s.file, 'utf8'); }
  catch (e){ throw new Error(`读不到源文件：${s.file}\n（可以用 ESP_PANEL_DIR 指定根目录）\n${e.message}`); }
  const body = extract(text).replace(/\r\n/g, '\n');
  out[s.key] = { label: s.label, file: s.rel, expect: s.expect, text: body };
  console.log(`${s.key.padEnd(10)} ${body.split('\n').length} 行 / ${body.length} 字符  ← ${s.rel}`);
}

const js = `/**
 * 内置的面板初始化数组（**原始 C 文本**）—— 由 tools/dev/extract-panel-tables.mjs 生成，别手改。
 *   node tools/dev/extract-panel-tables.mjs        # 重新提取（源工程在 E:\\esp-idf-wsh）
 *
 * 页面上"载入内置示例"就是把这些文本填进那个大文本框，走的是和"你自己粘贴"**完全相同**的解析路径 ——
 * 所以"内置示例能解析"与"你贴的能解析"是同一件事，不存在两套逻辑。
 *
 * \`expect\` 是源文件注释里自报的数字，自测拿它跟解析结果对账（见 tools/selftest/spi-panel-code.test.mjs）。
 */
export const PANEL_DATA = ${JSON.stringify(out, null, 2)};

/** 下拉里用的顺序（也是"载入内置示例"的顺序）*/
export const PANEL_KEYS = ${JSON.stringify(SPECS.map(s => s.key))};
`;

const dest = join(root, 'app', 'spi', 'panels-data.js');
writeFileSync(dest, js, 'utf8');
console.log(`\n已写出 ${dest}（${js.length} 字符）`);
