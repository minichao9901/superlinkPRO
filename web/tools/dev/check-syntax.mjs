/**
 * 语法体检：把 `app/**\/*.js` 与 `bridge/*.mjs` **全部**过一遍 `node --check`。
 *
 * 为什么要有这个脚本（2026-10 代码审查）：
 *   `make check` 原来是一份**手写的白名单**（62 条 `node --check app/xxx.js`）。手写的清单
 *   一定会漂移 —— 实测仓库里 108 个项目模块只覆盖了 61 个，`core/probe-bus.js`、`core/store.js`、
 *   整个 `flash/hpm/`、`serial/*`、`rtt/protocol.js`、`spi/anim.js`、`i2c/registers.js`
 *   这些"改了最容易出事"的模块**一条都不在里面**，而 `make check` 照样绿。
 *   现在改成遍历目录：**加了新模块自动被覆盖**，不用记得回来加一行。
 *
 * 用法：node tools/dev/check-syntax.mjs        （退出码 0 = 全过）
 *
 * 说明：
 *   · 只做**语法**检查（`node --check` 不执行模块，所以浏览器专属的 `document`/`navigator` 不会报错）；
 *   · 与 `make check` 里原来那条一样，逐文件起一个 node 进程 —— 这一点没有更快的替代
 *     （`import()` 会把页面代码跑起来）。
 *   · vendor 目录（`app/vendor/**`，xterm 那两个）也一起查：它们同样是页面会加载的模块，
 *     "全都过一遍"比"除了 vendor"更好解释。
 */
import { readdirSync, statSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join, relative, sep } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');

/** 递归收集某个目录下的 .js（app/ 用）*/
function collectJs(dir, out = []){
  for (const name of readdirSync(dir)){
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) collectJs(p, out);
    else if (name.endsWith('.js')) out.push(p);
  }
  return out;
}

/** bridge/ 与 tools/ 下的 .mjs（递归；跳过 node_modules）*/
function collectMjs(dir, out = []){
  if (!statSync(dir, { throwIfNoEntry: false })?.isDirectory()) return out;
  for (const name of readdirSync(dir)){
    const p = join(dir, name);
    if (statSync(p).isDirectory()){
      if (name !== 'node_modules') collectMjs(p, out);
    } else if (name.endsWith('.mjs')) out.push(p);
  }
  return out;
}

const files = [
  ...collectJs(join(root, 'app')),
  ...collectMjs(join(root, 'bridge')),
  /* `tools/**` 也一起查：真机套件与校验脚本同样会被"改一行就语法错"咬到，
     而它们以前完全不在 `make check` 的视野里（2026-10）。 */
  ...collectMjs(join(root, 'tools')),
].sort();

if (!files.length){
  console.error('❌ 一个 JS 模块都没找到 —— 目录结构变了？');
  process.exit(1);
}

const failed = [];
for (const f of files){
  const r = spawnSync(process.execPath, ['--check', f], { encoding: 'utf8' });
  if (r.status !== 0){
    const msg = String(r.stderr || r.stdout || '').trim().split('\n').slice(0, 4).join('\n      ');
    failed.push({ f, msg });
  }
}

const rel = p => relative(root, p).split(sep).join('/');
if (failed.length){
  console.error(`❌ 语法检查失败 ${failed.length} / ${files.length}：`);
  for (const { f, msg } of failed) console.error(`  · ${rel(f)}\n      ${msg}`);
  process.exit(1);
}
console.log(`✅ 语法检查通过：${files.length} 个模块（app/ + bridge/ + tools/ 全部，自动遍历，不用维护白名单）`);
