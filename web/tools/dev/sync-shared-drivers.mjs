/**
 * 生成 / 校验 ETM 页用的「共享探针驱动」副本。
 *
 * 背景（见 docs/merge-map.md §4.1/§4.7 与 web/docs/driver-unification.md）：
 *   合并前，这 7 个探针驱动在 toolbox 与 ETM 两个仓里各有一份、且已经分叉（各修各的 bug）。
 *   合并后**只保留一份手写真源**：`web/app/**` 里的这 7 个文件；ETM 页要用的副本由本脚本生成。
 *
 * 为什么不直接让 ETM 页 import `../../../../web/app/...`：
 *   · ETM 页必须能**独立启动**（`etm/start-web.ps1` 只服务 `etm/` 树）；
 *   · 它还要进**便携桥包**（`bridge/etm/backend/` 是独立快照，包内不应出现跨仓相对路径）；
 *   · `tools/dev/serve-etm.py` 只改写 HTML 的 href/src，**不改写** JS 的 import 说明符。
 *
 * 用法（在 web/ 下）：
 *   node tools/dev/sync-shared-drivers.mjs              # 复制（生成副本）
 *   node tools/dev/sync-shared-drivers.mjs --check      # 只校验；有漂移/缺失则退出码 1
 *   node tools/dev/sync-shared-drivers.mjs --etm-root ../etm   # 换 ETM 子树位置
 *
 * 守护：`tools/selftest/shared-drivers.test.mjs`（make test / make check 都会跑）。
 */
import { readFileSync, writeFileSync, mkdirSync, existsSync, readdirSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');   // = web/

/** 唯一真源 → 生成副本。顺序固定，便于对账输出。
 *
 * 为什么是 8 个而不是 merge-map §4.1 说的 7 个：toolbox 版 `dap-webusb.js` 引入了
 * `core/operation-budget.js`（OperationBudget，分阶段超时预算，toolbox 独有修复）。
 * 它必须跟着驱动一起进副本目录，否则 ETM 页加载 dap-webusb 时会
 * `ERR_MODULE_NOT_FOUND: ../core/operation-budget.js`（合并时用 import 解析检查实测到）。 */
export const SHARED_DRIVERS = [
  'rtt/dap-webusb.js',
  'core/usb-device.js',
  'core/probe-bus.js',
  'core/pace.js',
  'core/bin.js',
  'core/operation-budget.js',   // dap-webusb.js 的依赖（OperationBudget），不是独立驱动
  'dbg/bp.js',
  'dbg/source.js',
];

const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');

function parseArgs(argv){
  const args = { check: false, etmRoot: resolve(ROOT, '..', 'etm') };
  for (let i = 0; i < argv.length; i++){
    const a = argv[i];
    if (a === '--check') args.check = true;
    else if (a === '--etm-root') args.etmRoot = resolve(process.cwd(), argv[++i] ?? '.');
    else if (a === '--help' || a === '-h') args.help = true;
    else throw new Error(`未知参数：${a}（只支持 --check / --etm-root <路径>）`);
  }
  return args;
}

export function paths(etmRoot){
  return {
    srcDir: join(ROOT, 'app'),
    dstDir: join(etmRoot, 'web', 'app', 'shared'),
  };
}

/** 读一对文件；返回 {rel, src, dst, srcBytes, dstBytes, same} */
export function readPair(rel, etmRoot){
  const { srcDir, dstDir } = paths(etmRoot);
  const srcPath = join(srcDir, rel), dstPath = join(dstDir, rel);
  const srcBytes = readFileSync(srcPath);
  const dstBytes = existsSync(dstPath) ? readFileSync(dstPath) : null;
  return {
    rel, srcPath, dstPath, srcBytes, dstBytes,
    srcSha: sha256(srcBytes),
    dstSha: dstBytes ? sha256(dstBytes) : null,
    same: dstBytes != null && srcBytes.equals(dstBytes),
  };
}

function main(){
  const args = parseArgs(process.argv.slice(2));
  if (args.help){
    console.log('用法：node tools/dev/sync-shared-drivers.mjs [--check] [--etm-root <ETM 子树路径>]');
    return 0;
  }
  const { srcDir, dstDir } = paths(args.etmRoot);
  console.log(`真源：${srcDir}`);
  console.log(`副本：${dstDir}`);

  const rows = SHARED_DRIVERS.map(rel => readPair(rel, args.etmRoot));
  const missing = rows.filter(r => !r.dstBytes);
  const drifted = rows.filter(r => r.dstBytes && !r.same);

  if (args.check){
    for (const r of rows){
      const state = !r.dstBytes ? 'MISSING' : r.same ? 'ok' : 'DRIFT';
      console.log(`  ${state.padEnd(8)} ${r.rel.padEnd(22)} src=${r.srcSha.slice(0, 12)} dst=${(r.dstSha || '-').slice(0, 12)}`);
    }
    const ok = missing.length === 0 && drifted.length === 0;
    console.log(ok
      ? `共享驱动副本一致（${rows.length} 个文件）`
      : `共享驱动副本已漂移：缺失 ${missing.length}、内容不同 ${drifted.length} —— 运行 node tools/dev/sync-shared-drivers.mjs 重新生成`);
    return ok ? 0 : 1;
  }

  mkdirSync(dstDir, { recursive: true });
  let written = 0, unchanged = 0;
  for (const r of rows){
    mkdirSync(dirname(r.dstPath), { recursive: true });
    if (r.same){ unchanged++; continue; }
    writeFileSync(r.dstPath, r.srcBytes);
    written++;
  }
  // 副本目录里只允许这 7 个文件 + 说明文件；别的 .js 一律视为手工遗留，报出来（不删，交给人判断）。
  const extras = readdirSync(dstDir, { recursive: true })
    .map(p => String(p).replace(/\\/g, '/'))
    .filter(p => p.endsWith('.js') && !SHARED_DRIVERS.includes(p));
  for (const p of extras) console.warn(`  ⚠ 副本目录里有多余的 .js：${p}（不该手写在这里）`);

  console.log(`已生成：写入 ${written} 个、内容已相同 ${unchanged} 个${extras.length ? `、多余 ${extras.length} 个` : ''}`);
  return extras.length ? 1 : 0;
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url))){
  try { process.exitCode = main(); }
  catch (e){ console.error(String(e?.stack || e)); process.exitCode = 1; }
}
