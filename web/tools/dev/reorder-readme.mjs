/**
 * 一次性整理 README 顺序：把「功能一览（11 个标签页）」从 40pin 之后挪到它前面
 * （卖点 → 故事 → 功能表 → 接线图 → 更新 → 截图），并删掉与开篇重复的那段"为什么 RTT 分三种后端"。
 *   node tools/dev/reorder-readme.mjs --dry   # 只看会怎么动
 *   node tools/dev/reorder-readme.mjs         # 真改
 */
import { readFileSync, writeFileSync } from 'node:fs';
const dry = process.argv.includes('--dry');
const p = 'README.md';
const lines = readFileSync(p, 'utf8').split(/\r?\n/);

const idx = s => lines.findIndex(l => l.trim() === s);
const iFunc = idx('## 功能一览（11 个标签页）');
const iFace = idx('## 界面');
const iPin = idx('## 40pin 引脚定义（HPM5301EVKLite / J3）');
if (iFunc < 0 || iFace < 0 || iPin < 0) throw new Error(`锚点没找齐：功能=${iFunc} 界面=${iFace} 40pin=${iPin}`);
if (!(iFunc < iFace && iFunc > iPin)) throw new Error('位置不符合预期，先人工看一眼再改');

/* 取出 [功能一览 .. 界面) 这一段，去掉尾部的空行 */
let block = lines.slice(iFunc, iFace);
while (block.length && !block[block.length - 1].trim()) block.pop();
/* 删掉与开篇重复的"为什么 RTT 要分三种后端"两行（新开篇里已经写过同一件事） */
const dup = block.findIndex(l => l.startsWith('> 为什么 RTT 要分三种后端'));
if (dup >= 0) block.splice(dup, 3);

const rest = [...lines.slice(0, iFunc), ...lines.slice(iFace)];
const at = rest.findIndex(l => l.trim() === '## 40pin 引脚定义（HPM5301EVKLite / J3）');
const out = [...rest.slice(0, at), ...block, '', ...rest.slice(at)];

if (dry){
  console.log(`功能一览：原 ${iFunc + 1}..${iFace} 行 → 插到 40pin（原 ${iPin + 1} 行）之前；块内 ${block.length} 行`);
  console.log(out.slice(at - 2, at + 6).join('\n'));
} else {
  writeFileSync(p, out.join('\n'));
  console.log(`已重排：功能一览移到 40pin 之前（${lines.length} → ${out.length} 行）`);
}
