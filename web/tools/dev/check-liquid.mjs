/**
 * GitHub Pages 的隐形地雷检查：**Markdown 里的 `{{` / `{%`**。
 *
 * 为什么需要（2026-10-03 现场）：本仓的 Pages 走的是 GitHub 的
 * `pages build and deployment`（分支发布 + Jekyll，主题 primer）。Jekyll 会
 * 把**每一个 .md 都送进 Liquid**（有 `jekyll-optional-front-matter`，没有
 * front matter 的 md 也照样渲染），于是任何一句 C 代码里的 `{{cmd, len, ...}}`
 * 都会被当成 Liquid 变量 → `Liquid syntax error` → **整个部署失败**。
 *
 * 代价实测：`0a258bb`（10-02 16:43）起 Pages 连续三次构建失败（0a258bb / c2c4583 /
 * 6dbc165），线上一直停在旧版，而 `git push` 本身是成功的 —— 只看 push 结果看不出来。
 *
 * 判据：逐行扫 .md，出现 `{{` 或 `{%` 且**不在 `{% raw %}` / `{% endraw %}` 区间内**
 * 就报错并给出文件:行号（`{ {` 这种加了空格的写法天然安全，Liquid 不认）。
 *
 *   node tools/dev/check-liquid.mjs          # make check 里会跑（扫仓库里所有 .md）
 *   node tools/dev/check-liquid.mjs a.md b.md # 也可以只查指定文件（便于自测/排查）
 */
import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';

const files = process.argv.length > 2
  ? process.argv.slice(2)
  // 🚨 必须 quotepath=false + -z：仓库里有中文文件名（docs/交接-….md），
  // 默认 quotepath=true 会回成 "\344\272\244…" 这种带引号的转义，readFileSync 直接 ENOENT
  : execFileSync('git', ['-c', 'core.quotepath=false', 'ls-files', '-z', '*.md'], { encoding: 'utf8' })
      .split('\0').map(s => s.trim()).filter(Boolean);

const bad = [];
for (const f of files){
  const text = readFileSync(f, 'utf8');
  let inRaw = false;
  text.split(/\r?\n/).forEach((line, i) => {
    // raw 区间：{% raw %} … {% endraw %}（同一行、跨行都要认）——
    // 逐个切出 raw / endraw 标签，标签**之间**的非 raw 片段才做检查
    let visible = '';
    let raw = inRaw;
    for (const part of line.split(/(\{%-?\s*(?:end)?raw\s*-?%\})/)){
      if (/^\{%-?\s*raw\s*-?%\}$/.test(part)){ raw = true; continue; }
      if (/^\{%-?\s*endraw\s*-?%\}$/.test(part)){ raw = false; continue; }
      if (!raw) visible += part;
    }
    inRaw = raw;
    if (visible.includes('{{') || visible.includes('{%')) bad.push(`${f}:${i + 1}: ${line.trim().slice(0, 100)}`);
  });
}

if (bad.length){
  console.error('❌ Markdown 里有会被 Jekyll/Liquid 当成变量的写法（Pages 会构建失败）：');
  for (const b of bad) console.error('   ' + b);
  console.error('   改法：用 {% raw %} 包起来，或写成 "{ {"（Liquid 不认带空格的写法）');
  process.exit(1);
}
console.log(`OK  ${files.length} 个 Markdown 都没有 Liquid 地雷（{{ / {%）`);
