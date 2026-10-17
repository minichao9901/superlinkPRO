/**
 * 「载入 ELF 就推荐源码目录」（`sourceRootSuggestions`）的纯函数回归。
 *
 * 为什么值得钉：这份 HPM6800EVK 的 ELF 里，188 条 DWARF 路径**全是绝对路径**，且分三簇 ——
 * `E:/sdk_env_v1.11.0/hpm_sdk` 146 条、`E:/sdk_env_v1.11.0/toolchains` 18 条、
 * `/home/builder/...` 24 条（编译机路径，本机覆盖不到）。推荐逻辑要：
 *   ① 沿最大分支往下钻到一个"够用又不至于太大"的目录；② 把跨机器的簇标出来，别让用户白选。
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { sourceRootSuggestions, SourceStore, relUnderRoot } from '../../app/dbg/source.js';
import { SymTab } from '../../app/dbg/symbols.js';

// ---------- ① 合成：主簇里没有绝对多数分支 → 就停在簇根
{
  const gen = (n, sub) => Array.from({ length: n }, (_, i) => `E:/proj/sdk/${sub}/f${i}.c`);
  const paths = [...gen(40, 'core'), ...gen(30, 'drivers'), ...gen(20, 'middleware'),
                 ...Array.from({ length: 10 }, (_, i) => `/home/builder/work/g${i}.c`)];
  const s = sourceRootSuggestions(paths);
  assert.equal(s.total, 100, '总数');
  assert.equal(s.best.dir, 'E:/proj/sdk', '没有绝对多数分支时就停在簇根');
  assert.equal(s.best.files, 90, '覆盖数 = 主簇文件数');
  assert.equal(s.others.length, 1, '另一簇要列出来');
  assert.equal(s.others[0].dir, '/home/builder/work');
  assert.equal(s.others[0].foreign, true, '跨机器（win vs posix）要标 foreign');
}

// ---------- ② 合成：某个孩子占绝对多数 → 往下钻（推荐更具体的目录）
{
  const gen = (n, sub) => Array.from({ length: n }, (_, i) => `E:/proj2/app/${sub}/f${i}.c`);
  const paths = [...gen(60, 'src/core'), ...gen(20, 'src/drv'), ...gen(10, 'inc')];
  const s = sourceRootSuggestions(paths);
  assert.equal(s.best.dir, 'E:/proj2/app/src', '80/90 超过 60%，应当从 app 钻到 src');
  assert.equal(s.best.files, 80);
}

// ---------- ③ 大小写不敏感去重（Windows 路径）
{
  const s = sourceRootSuggestions(['E:/Proj/src/a.c', 'e:/proj/src/A.C', 'E:/proj/src/b.c']);
  assert.equal(s.total, 2, '同一文件的大小写变体只算一次');
}

// ---------- ④ 空 / 无绝对路径
{
  assert.equal(sourceRootSuggestions([]).total, 0);
  assert.equal(sourceRootSuggestions(['a.c', './b.c']).total, 0, '相对路径不算');
}

// ---------- ⑤ 真 ELF（仓库里的 fixture）：主簇取到最多文件，别的簇列出来
{
  const st = SymTab.fromBuffer(readFileSync('tools/fixtures/dwarf/stm32f103_rtt_speed.elf'));
  const s = sourceRootSuggestions(st.lines?.paths || []);
  assert.ok(s.total > 0 && s.best, 'fixture ELF 应当能给出建议');
  assert.ok(s.best.files >= s.total / 2, `推荐目录要覆盖多数文件（实到 ${s.best.files}/${s.total}）`);
  assert.equal(s.best.dir.split('/').slice(0, 2).join('/').toLowerCase(),
               s.root.dir.toLowerCase(), '推荐目录必须落在主簇里');
}

// ---------- ⑥ 「ELF 路径 → 所选目录下的相对路径」
{
  const p = 'E:/sdk_env_v1.11.0/hpm_sdk/samples/lwip/lwip_tcpecho/src/lwip.c';
  assert.equal(relUnderRoot(p, 'hpm_sdk'), 'samples/lwip/lwip_tcpecho/src/lwip.c', '选 hpm_sdk');
  assert.equal(relUnderRoot(p, 'sdk_env_v1.11.0'), 'hpm_sdk/samples/lwip/lwip_tcpecho/src/lwip.c', '选它的父目录');
  assert.equal(relUnderRoot(p, 'Samples'), 'lwip/lwip_tcpecho/src/lwip.c', '目录名大小写无关（选 samples 也对）');
  assert.equal(relUnderRoot(p, 'NoSuchDir'), null, '名字完全对不上 → null');
  assert.equal(relUnderRoot(p, ''), null);
}

// ---------- ⑦ 按需索引：只开 ELF 点名的文件（不遍历目录树）
{
  const files = new Map([['samples/lwip/src/lwip.c', 'int main(void){return 0;}']]);
  const makeDir = (prefix) => ({
    async getDirectoryHandle(seg){ return makeDir(prefix + seg + '/'); },
    async getFileHandle(name){
      const rel = prefix + name;
      if (!files.has(rel)) throw new Error('NotFound: ' + rel);
      return { getFile: async () => ({ text: async () => files.get(rel) }) };
    },
  });
  const s = new SourceStore();
  s.root = makeDir(''); s.rootName = 'hpm_sdk';
  await s.setExpectedPaths(['E:/x/hpm_sdk/samples/lwip/src/lwip.c', '/home/builder/gcc/soft-fp/x.c']);
  assert.equal(s.count, 1, '只索引"在这个目录里且被 ELF 引用"的文件');
  assert.ok(/按需索引：1\/2/.test(s.summary()), '摘要要给覆盖数：' + s.summary());
  assert.ok(s.resolve('E:/x/hpm_sdk/samples/lwip/src/lwip.c'), 'resolve 仍然命中');
  assert.equal((await s.read('E:/x/hpm_sdk/samples/lwip/src/lwip.c')).slice(0, 7), 'int mai', 'read 取到内容');
}

// ---------- ⑧ FileList 兜底：按 ELF 的路径筛（光文件名的也要放行，自测脚本就这么造）
{
  const mk = (rel, text) => { const f = { name: rel.split('/').pop(), text: async () => text }; Object.defineProperty(f, 'webkitRelativePath', { value: rel }); return f; };
  const s = new SourceStore();
  s.expected = ['E:/x/samples/lwip/src/lwip.c'];
  s.indexFileList([mk('samples/lwip/src/lwip.c', 'A'), mk('samples/other/junk.c', 'B')]);
  assert.equal(s.count, 1, '无关文件被筛掉：' + s.summary());
  const s2 = new SourceStore();
  s2.expected = ['E:/x/samples/lwip/src/lwip.c'];
  s2.indexFileList([{ name: 'lwip.c', text: async () => 'A' }]);        // 无 webkitRelativePath（自测脚本）
  assert.equal(s2.count, 1, '光文件名（basename 命中）也要放行');
}

// ---------- ⑦ `SymTab.covers()`：自动刷新不许碰"不属于当前目标"的陈旧地址
/**
 * 真机现场（2026-10，HPM6800EVK + tcpecho）：localStorage 里留着上一块 **ARM** 板的内存页地址
 * `0x0800_0300`；在 RISC-V 那颗芯片上没映射，自动刷新一读它 → SBA 报错 → 触发自愈（复位 DM）
 * → 用户紧接着按「继续」就撞上 `抽象命令失败（读寄存器 0x7b0）：cmderr=4`。
 * `covers()` 就是"这个地址还属于当前目标吗"的判据（只约束自动刷新，手动输入照读）。
 */
{
  const st = SymTab.fromBuffer(readFileSync('tools/fixtures/dwarf/riscv_dwarf5.elf'));
  const alloc = st.elf.sections().filter(s => (s.flags & 2) && s.size);
  assert.ok(alloc.length, 'fixture ELF 应当有已分配段');
  for (const s of alloc){
    const start = s.addr >>> 0;
    assert.equal(st.covers(start), true, `段首 ${s.name}@0x${start.toString(16)} 必须在覆盖内`);
    assert.equal(st.covers((start + s.size - 1) >>> 0), true, `段尾前最后一个字节 ${s.name} 必须在覆盖内`);
  }
  assert.equal(st.covers(0x08000300), false, '上一块 ARM 板留下的 0x0800_0300 不在 RISC-V 目标里');
  assert.equal(st.covers(0x00000000), false, '空地址不算覆盖（别把 addr 忘填当合法）');
  const gap = (alloc[0].addr >>> 0) - 0x10;
  assert.equal(st.covers(gap), false, `段外空隙 0x${gap.toString(16)} 不算覆盖`);
}

console.log('dbg-src-suggest: 首选目录（沿最大分支钻）/ 跨机器簇标记 / 大小写去重 / 真 ELF /'
  + ' 陈旧地址判据 covers() /'
  + ' 按需索引（不遍历目录）/ FileList 筛选 PASS');

// Latest ELF/directory wins even if an old native filesystem request finishes later.
{
 const s=new SourceStore();s.root={};s.rootName='proj';let release;
 const oldHandle=new Promise(resolve=>release=resolve);
 s._openRel=rel=>rel==='old.c'?oldHandle:Promise.resolve({getFile:async()=>({size:1,text:async()=>rel})});
 const old=s.setExpectedPaths(['/proj/old.c']);
 await s.setExpectedPaths(['/proj/new.c']);
 release({getFile:async()=>({size:1,text:async()=> 'old'})});await old;
 assert.deepEqual(s.entries.map(e=>e.rel),['new.c']);
 assert.equal(s.resolve('/proj/old.c'),null);
}
// Switching to FileList retires the old directory handle and preserves files for another ELF.
{
 const s=new SourceStore();s.root={};s.rootName='old';
 s.indexFileList([{name:'a.c',webkitRelativePath:'proj/a.c',size:1,text:async()=> 'a'},
                  {name:'b.c',webkitRelativePath:'proj/b.c',size:1,text:async()=> 'b'}]);
 assert.equal(s.root,null);
 await s.setExpectedPaths(['/proj/a.c']);assert.deepEqual(s.entries.map(e=>e.rel),['a.c']);
 await s.setExpectedPaths(['/proj/b.c']);assert.deepEqual(s.entries.map(e=>e.rel),['b.c']);
}
// A cancelled fallback directory walk cannot inject files into the new index.
{
 const s=new SourceStore();let release;
 s.root={async *entries(){await new Promise(resolve=>release=resolve);yield ['old.c',{kind:'file'}];}};
 s.rootName='proj';s._openRel=async()=>null;
 const old=s.setExpectedPaths(['/proj/missing.c']);
 await new Promise(resolve=>setImmediate(resolve));
 s.clear();await s.setExpectedPaths(['/proj/new.c']);
 s.indexFileList([{name:'new.c',webkitRelativePath:'proj/new.c'}]);
 release();await old;assert.deepEqual(s.entries.map(e=>e.rel),['new.c']);
}
// File text which completes after a directory/ELF change cannot poison the new cache.
{
 const s=new SourceStore();let release;
 s.indexFileList([{name:'a.c',webkitRelativePath:'proj/a.c',size:1,text:()=>new Promise(resolve=>release=resolve)}]);
 const old=s.read('/proj/a.c');await new Promise(resolve=>setImmediate(resolve));
 s.indexFileList([{name:'a.c',webkitRelativePath:'proj/a.c',size:1,text:async()=> 'new'}]);
 release('old');await assert.rejects(old,/已切换/);assert.equal(await s.read('/proj/a.c'),'new');
}
console.log('SourceStore: stale ELF open/walk/text cancelled, FileList retires directory and reindexes across ELF changes PASS');
{
 const s=new SourceStore();s.root={};s.rootName='proj';s._walk=async()=>{throw Error('must not scan without source paths');};
 await s.setExpectedPaths([]);assert.equal(s.count,0);assert.match(s.note,/未扫描/);
}
