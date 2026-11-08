/** 同步 ETM 页面仍使用的共享模块。目标访问由 OpenOCD Wasm 提供。 */
import { readFileSync, writeFileSync, mkdirSync, existsSync, readdirSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');   // = web/

export const SHARED_DRIVERS = [
  'core/usb-device.js',
  'core/probe-bus.js',
  'core/pace.js',
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

  const extras = (existsSync(dstDir) ? readdirSync(dstDir, { recursive: true }) : [])
    .map(p => String(p).replace(/\\/g, '/'))
    .filter(p => p.endsWith('.js') && !SHARED_DRIVERS.includes(p));

  if (args.check){
    for (const path of extras) console.warn("多余副本：" + path);
    for (const r of rows){
      const state = !r.dstBytes ? 'MISSING' : r.same ? 'ok' : 'DRIFT';
      console.log(`  ${state.padEnd(8)} ${r.rel.padEnd(22)} src=${r.srcSha.slice(0, 12)} dst=${(r.dstSha || '-').slice(0, 12)}`);
    }
    const ok = missing.length === 0 && drifted.length === 0 && extras.length === 0;
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
  for (const p of extras) console.warn(`  ⚠ 副本目录里有多余的 .js：${p}（不该手写在这里）`);

  console.log(`已生成：写入 ${written} 个、内容已相同 ${unchanged} 个${extras.length ? `、多余 ${extras.length} 个` : ''}`);
  return extras.length ? 1 : 0;
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url))){
  try { process.exitCode = main(); }
  catch (e){ console.error(String(e?.stack || e)); process.exitCode = 1; }
}
