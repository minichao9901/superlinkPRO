/**
 * 纯 Node 自测（不需要浏览器、不需要硬件、不需要网络）：
 *   node tools/selftest/scenery-samples.test.mjs
 *
 * 守的是 `samples/test_images/scenery/` 这批**风景照片素材**的三件事：
 *   A. **文件都在、几何对**：16 个 BMP = 4 类 × 2 张 × 2 种屏（AXS15352 240×296 / ST77916 360×360）
 *   B. **页面真的能吃**：拿 `app/spi/image.js` 的 parseBMP 真解一遍，尺寸/像素数对得上，
 *      并且是 24bpp BI_RGB（页面只支持 16/24bpp + 不压缩；换成别的格式页面上会直接报错）
 *   C. **没被偷改**：逐文件 sha256 与 `manifest.json` 对账 —— 谁把图悄悄换了一张，这里会红
 *
 * 为什么不比对"原图"：素材是从 Wikimedia Commons 下载后裁剪缩放的（见 README 的来源表），
 * 原图不随仓库走；能钉住的确定性来自 manifest 的 sha256 —— 同一套输入、同一份脚本，
 * 重跑出来的字节是稳定的（实测连跑两遍一致）。
 */
import { readFileSync, existsSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, '..', '..');
const url = p => 'file://' + join(repo, p).replace(/\\/g, '/');
const I = await import(url('app/spi/image.js'));

const ROOT = join(repo, 'samples', 'test_images', 'scenery');
const PANELS = { axs15352: { w: 240, h: 296 }, st77916: { w: 360, h: 360 } };
const GROUPS = ['花朵绿树', '美女', '蓝天白云', '大海'];
const PER_GROUP = 2;

let pass = 0, fail = 0;
const ok = (cond, name, extra = '') => {
  if (cond){ pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name} ${extra}`); }
};

// ==================================================================== A
console.log('== A. manifest 与目录结构 ==');
const manifestPath = join(ROOT, 'manifest.json');
ok(existsSync(manifestPath), 'manifest.json 在');
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));

for (const panel of Object.keys(PANELS)){
  const rows = manifest.filter(m => m.panel === panel);
  ok(rows.length === GROUPS.length * PER_GROUP,
    `${panel} 有 ${GROUPS.length} 类 × ${PER_GROUP} 张 = ${GROUPS.length * PER_GROUP} 条记录`,
    `实际 ${rows.length}`);
}
{
  const byGroup = new Map();
  for (const m of manifest) byGroup.set(m.group, (byGroup.get(m.group) || 0) + 1);
  ok(GROUPS.every(g => byGroup.get(g) === PER_GROUP * 2),
    `四类齐全且每类 ${PER_GROUP} 张（两屏各一份）`,
    JSON.stringify([...byGroup]));
  const keys = new Set(manifest.map(m => m.key));
  ok(keys.size === GROUPS.length * PER_GROUP, `素材 key 唯一：${keys.size} 个`);
  ok(manifest.every(m => m.license), '每条都记了 license');
  ok(manifest.every(m => m.title), '每条都记了原始文件名（可回溯来源）');
  const pd = manifest.filter(m => /^(CC0|Public domain|CC BY)/.test(m.license));
  ok(pd.length === manifest.length, '许可都是 CC0 / 公有领域 / CC BY / CC BY-SA', 
    manifest.filter(m => !/^(CC0|Public domain|CC BY)/.test(m.license)).map(m => m.license).join(','));
}

// ==================================================================== B / C
console.log('== B. 页面解码器吃得下（parseBMP 真解一遍）+ 文件头 ==  ');
for (const panel of Object.keys(PANELS)){
  const g = PANELS[panel];
  const rows = manifest.filter(m => m.panel === panel);
  let dimOk = 0, hdrOk = 0, pxOk = 0;
  for (const m of rows){
    const p = join(ROOT, panel, `${m.key}.bmp`);
    if (!existsSync(p)){ ok(false, `${panel}/${m.key}.bmp 存在`); continue; }
    const bytes = readFileSync(p);
    // 文件头：24bpp / 不压缩 / 与几何一致
    const bpp = bytes.readUInt16LE(28), comp = bytes.readUInt32LE(30);
    const dibW = bytes.readInt32LE(18), dibH = Math.abs(bytes.readInt32LE(22));
    if (bpp === 24 && comp === 0 && dibW === g.w && dibH === g.h
        && bytes.readUInt32LE(14) >= 40) hdrOk++;
    // 页面自己的解码器
    const img = I.parseBMP(bytes);
    if (img.w === g.w && img.h === g.h && img.rgba.length === g.w * g.h * 4) dimOk++;
    // 顺带确认不是"全黑/全白"这种坏图：抽查若干点，颜色要有变化
    const seen = new Set();
    for (let i = 0; i < 32; i++){
      const x = Math.floor(g.w * ((i * 37) % 100) / 100), y = Math.floor(g.h * ((i * 61) % 100) / 100);
      const o = (y * g.w + x) * 4;
      seen.add(`${img.rgba[o]},${img.rgba[o + 1]},${img.rgba[o + 2]}`);
    }
    if (seen.size > 4) pxOk++;
  }
  ok(hdrOk === rows.length, `${panel}: ${rows.length} 个文件都是 24bpp BI_RGB 且几何正确`, `${hdrOk}/${rows.length}`);
  ok(dimOk === rows.length, `${panel}: parseBMP 解出的尺寸/像素数全对`, `${dimOk}/${rows.length}`);
  ok(pxOk === rows.length, `${panel}: 抽查像素有颜色变化（不是纯色坏图）`, `${pxOk}/${rows.length}`);
}

console.log('== C. sha256 与 manifest 对账（防偷改）==');
{
  let same = 0, bad = [];
  for (const m of manifest){
    const p = join(ROOT, m.panel, `${m.key}.bmp`);
    if (!existsSync(p)) continue;
    const h = createHash('sha256').update(readFileSync(p)).digest('hex');
    if (h === m.sha256) same++; else bad.push(`${m.panel}/${m.key}`);
  }
  ok(same === manifest.length, `${manifest.length} 个文件 sha256 全对`, bad.join(', '));
}

console.log(`\n${fail ? 'FAIL' : 'OK'}  ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
