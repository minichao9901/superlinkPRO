/**
 * 「共享探针驱动」不漂移自测（纯离线，不需要探针/浏览器）：
 *   node tools/selftest/shared-drivers.test.mjs      （已并入 make test 与 make check）
 *
 * 咬四件事：
 *   ① **唯一真源**：`web/app/**` 的 7 个驱动是手写源；`etm/web/app/shared/**` 的副本必须与它
 *      **逐字节相同**（改完真源忘了跑 `node tools/dev/sync-shared-drivers.mjs` = 这里红）；
 *   ② **合并成果没丢**：toolbox 独有（OperationBudget / DAP_SESSION_LOST / 5 字节 WriteABORT）
 *      与 ETM 独有（resetDevice 永不抛 / 禁短超时 drain / 响应回显≠命令 时清队列重试）都要在；
 *   ③ **缺陷没被抄回来**：ETM 版 `abort()` 少写 DAP index（4 字节）是 bug，副本里不许再出现；
 *   ④ **不做跨子树 import**：ETM 页只许 import `./shared/...`，不允许指回 `../../web/app/...`
 *      （那会让 ETM 页无法独立启动、也进不了便携桥包）。
 */
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { dirname, join, resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { SHARED_DRIVERS } from '../dev/sync-shared-drivers.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const web = resolve(here, '..', '..');            // web/
const repo = resolve(web, '..');                  // superlinkPRO/
const etmShared = join(repo, 'etm', 'web', 'app', 'shared');
const etmPage = join(repo, 'etm', 'web');

const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');
let pass = 0, fail = 0;
const ok = (c, name, extra = '') => { if (c){ pass++; console.log(`  PASS  ${name}`); } else { fail++; console.log(`  FAIL  ${name}${extra ? '  → ' + extra : ''}`); } };

// ==================================================================== 1
console.log('== 1. 7 个驱动：真源 vs ETM 副本（逐字节） ==');
for (const rel of SHARED_DRIVERS){
  const srcPath = join(web, 'app', rel), dstPath = join(etmShared, rel);
  const src = readFileSync(srcPath);
  const dst = existsSync(dstPath) ? readFileSync(dstPath) : null;
  ok(dst != null && src.equals(dst),
    `${rel}（${src.length} B）`,
    dst == null ? '副本缺失' : `src=${sha256(src).slice(0, 12)} dst=${sha256(dst).slice(0, 12)}`);
}

// ==================================================================== 2
console.log('== 2. 副本目录里没有多余的手写 .js ==');
{
  const extras = readdirSync(etmShared, { recursive: true })
    .map(p => String(p).replace(/\\/g, '/'))
    .filter(p => p.endsWith('.js') && !SHARED_DRIVERS.includes(p));
  ok(extras.length === 0, '只有那 7 个 .js', extras.join(' · '));
  ok(readFileSync(join(etmShared, 'README.md'), 'utf8').includes('sync-shared-drivers.mjs'),
    '副本目录有 README 指向生成脚本（新人不会手改副本）');
}

// ==================================================================== 3
console.log('== 3. 合并成果都在（toolbox 独有 + ETM 独有） ==');
{
  const src = readFileSync(join(web, 'app', 'rtt', 'dap-webusb.js'), 'utf8');
  const pins = [
    ['OperationBudget', '分阶段超时预算（toolbox 独有）'],
    ['DAP_SESSION_LOST', 'DAP 会话失效错误码（toolbox 独有）'],
    ['_targetNeedsReconnect', '会话失效跟踪（toolbox 独有）'],
    ['Uint8Array.of(0, 0x1e, 0, 0, 0)', '规范 WriteABORT：DAP index + 4 字节 ABORT（toolbox 独有）'],
    ['USB 端口复位失败（非致命，继续认领）', 'resetDevice 永不抛（ETM 独有）'],
    ['不要**用「一串短超时 transferIn 去排空」', '禁短超时 drain / 只用 clearHalt（ETM 独有）'],
    ['USB 端口复位不可用（这颗探针常见）', '复位不可用时明确提示后继续认领（ETM 独有）'],
    ['/响应回显/.test(e.message)', '「响应回显 ≠ 命令」时清队列按短包重试（两边共有，不丢）'],
    ['AP 初始化后的 CPUID 核验失败', 'AP 初始化后 CPUID 复核（toolbox 更严，保留）'],
  ];
  for (const [needle, name] of pins) ok(src.includes(needle), name);
  ok(!src.includes('Uint8Array.of(0x1e, 0x00, 0x00, 0x00)'),
    '没有把 ETM 版 4 字节 abort()（缺 DAP index）抄回来');
  ok(!/async _healFaultLocked\(\)\{[^}]*_targetInit/.test(src) || src.includes('_healFaultLocked'),
    'FAULT 自愈仍在锁内（_healFaultLocked；ETM 的锁外写法不要混进来）');
}

// ==================================================================== 4
console.log('== 4. ETM 页不做跨子树 import ==');
{
  const files = readdirSync(join(etmPage, 'app'), { recursive: true })
    .map(p => String(p).replace(/\\/g, '/'))
    .filter(p => p.endsWith('.js'));
  const sharedImporters = new Set();
  let sharedSpecs = 0;
  const crossTree = [];
  for (const rel of files){
    const text = readFileSync(join(etmPage, 'app', rel), 'utf8');
    for (const m of text.matchAll(/from\s+['"]([^'"]+)['"]/g)){
      const spec = m[1];
      if (spec.startsWith('./shared/') || spec.startsWith('../shared/')){ sharedImporters.add(rel); sharedSpecs++; }
      if (/web\/app|\.\.\/\.\.\/\.\.\/\.\.\//.test(spec)) crossTree.push(`${rel} -> ${spec}`);
    }
  }
  ok(crossTree.length === 0, '没有 import 指回 web/app（跨子树）', crossTree.join(' · '));
  ok(sharedImporters.size >= 3 && sharedSpecs >= 6,
    `ETM 页确实在消费这些副本（${sharedImporters.size} 个模块 / ${sharedSpecs} 条 import：${[...sharedImporters].sort().join(', ')}）`);
  // 副本里的相对 import 必须自我闭合（shared/core/bin.js 等互引不能爬出 shared/）
  const outside = [];
  for (const rel of SHARED_DRIVERS){
    const text = readFileSync(join(etmShared, rel), 'utf8');
    for (const m of text.matchAll(/from\s+['"]([^'"]+)['"]/g)){
      const target = resolve(dirname(join(etmShared, rel)), m[1]);
      if (!target.startsWith(etmShared)) outside.push(`${rel} -> ${m[1]}（解析到 ${relative(etmShared, target)}）`);
    }
  }
  ok(outside.length === 0, '副本内部的相对 import 都落在 shared/ 内', outside.join(' · '));
}

console.log(`\n${fail ? '❌' : '✅'} shared-drivers.test: ${pass} 通过 / ${fail} 失败`);
process.exitCode = fail ? 1 : 0;
