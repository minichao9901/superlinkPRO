import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';
import { join, relative, resolve } from 'node:path';
import { importOpenOcdCfg } from '../../app/dbg/openocd-cfg.js';

const root = process.argv[2];
assert.ok(root, 'Usage: node web/tools/selftest/dbg-cfg-corpus.mjs OPENOCD_TCL_DIRECTORY');
const scripts = new Map();
for (const item of await readdir(root, { recursive:true, withFileTypes:true })){
  if (!item.isFile() || !/\.(cfg|tcl)$/i.test(item.name)) continue;
  const path = join(item.parentPath, item.name);
  scripts.set(relative(resolve(root), resolve(path)).replaceAll('\\', '/'), await readFile(path, 'utf8'));
}

const report = { scripts:scripts.size, groups:{} };
for (const kind of ['board', 'target']){
  const entries = [...scripts.keys()].filter(path => path.startsWith(kind + '/') && path.endsWith('.cfg')).sort();
  const stats = report.groups[kind] = { entries:entries.length, single:0, directory:0,
    directoryByArch:{arm:0,riscv:0}, directoryOnly:[], singleOnly:[], failures:{} };
  for (const path of entries){
    const body = scripts.get(path);
    let single = false;
    try { importOpenOcdCfg(body, path); single = true; stats.single++; } catch { /* Compare the directory path below. */ }
    try {
      const cfg = importOpenOcdCfg(body, path, scripts);
      stats.directory++;
      stats.directoryByArch[cfg.arch]++;
      if (!single) stats.directoryOnly.push({ path, arch:cfg.arch, target:cfg.target, sourced:cfg.sourced });
    } catch (error){
      if (single) stats.singleOnly.push({ path, message:error.message });
      const reason = error.cause?.message || error.message;
      const category = reason.split(/[\s（]/).slice(0, 3).join(' ');
      const failures = stats.failures[category] ??= { count:0, example:{ path, message:error.message } };
      failures.count++;
    }
  }
}
console.log(JSON.stringify(report, null, 2));
