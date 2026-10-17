/**
 * 把 `bridge/rtt-bridge.mjs` 嵌成 `app/gen/bridge-src.js`（页面生成「桥包」时用它拼 zip）。
 *
 * 为什么要嵌一份（而不是页面运行时 fetch）：
 *   · 页面用 file:// 打开时 fetch 会被 CORS 拦掉，生成就废了；
 *   · 嵌入后生成是**确定性**的（同一版页面永远产出同一份桥），便于对账；
 *   · 代价是仓库里多 ~70 KB 文本 —— 换来的是一条 `make test-gen` 里的哈希对账（防漂移）。
 *
 * 用法：node tools/dev/embed-bridge.mjs      （改完 bridge/rtt-bridge.mjs 就重跑一次）
 * 校核：node tools/selftest/bridge-kit.test.mjs（断言 BRIDGE_SRC 与仓库那份**逐字节**一致）
 *
 * 换行统一成 LF：仓库工作区在 Windows 上可能是 CRLF（autocrlf），嵌进去会让"逐字节对账"
 * 变成看平台脸色 —— 统一 LF 后，包里的 .mjs 在任何平台都一样，Node 也都跑得动。
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const srcPath = join(root, 'bridge', 'rtt-bridge.mjs');
const outPath = join(root, 'app', 'gen', 'bridge-src.js');

const src = readFileSync(srcPath, 'utf8').replace(/\r\n/g, '\n');
const sha = createHash('sha256').update(src, 'utf8').digest('hex');

const out = `/**
 * ⚠️ **自动生成，别手改** —— 重跑： \`node tools/dev/embed-bridge.mjs\`
 *
 * 内容是 \`bridge/rtt-bridge.mjs\` 的逐字节副本（换行统一成 LF），给「工程生成」页拼桥包用。
 * 漂移由 \`tools/selftest/bridge-kit.test.mjs\` 拦（比对 sha256），所以改完桥**必须**重跑本脚本。
 */
export const BRIDGE_SRC = ${JSON.stringify(src)};

/** BRIDGE_SRC 的 sha256（桥包里 README 也印它，便于人工核对） */
export const BRIDGE_SHA256 = ${JSON.stringify(sha)};
`;
writeFileSync(outPath, out, 'utf8');
console.log(`已嵌入 ${srcPath}\n  → ${outPath}`);
console.log(`  字节 ${src.length} · sha256 ${sha}`);
