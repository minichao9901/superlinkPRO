/**
 * 把 ETM 页面从 `etm/web/**` 生成一份**静态副本**到 `web/etm/**`，供 GitHub Pages 使用。
 *
 * 为什么是「生成副本」而不是「把源码搬进 web/」（D-7 的三条真实依赖）：
 *   1. `etm/start-web.ps1` 要能独立启动 ETM 页（合并后仍必须能单跑）；
 *   2. `web/bridge/etm/backend/**` 是便携包快照，必须自洽、不能引用跨子树路径；
 *   3. `web/tools/dev/serve-etm.py` 只改写 HTML 的 href/src，**不改写 JS import**。
 * 所以：源码仍在 `etm/web/`，`web/etm/` 是逐字节副本 + 一处只对副本做的 HTML 改写。
 *
 * 只对 `index.html` 做三件事（其余文件逐字节相同）：
 *   ① 绝对路径改相对（`/style.css` → `./style.css`、`/app/view.js` → `./app/view.js`），
 *      这样把副本挂在 `/superlinkPRO/etm/` 下也能找到资源；
 *   ② 在 `</body>` 的模块脚本前插入一段 bootstrap：解析 API base（?api= > localStorage > 默认，
 *      线上默认 `http://127.0.0.1:8899/etm/api/`）、按 `?embedded=1` 打上内嵌标记、注入内嵌样式；
 *   ③ 无本地桥时在页面顶部给出可操作提示（+ 便携包下载 / 排障文档链接），不留白屏。
 *
 * 用法（在 web/ 下）：
 *   node tools/dev/sync-etm-page.mjs            # 生成/更新副本（含删除过期文件）
 *   node tools/dev/sync-etm-page.mjs --check    # 只校验（漂移/过期 → 退出码 1）；make check 里有
 *
 * 守护：`tools/selftest/etm-page-copy.test.mjs`（make test）。
 */
import { readFileSync, writeFileSync, mkdirSync, existsSync, readdirSync, rmSync, statSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const WEB = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');   // = web/
const REPO = resolve(WEB, '..');
export const SOURCE_DIR = resolve(REPO, 'etm', 'web');
export const DEST_DIR = resolve(WEB, 'etm');
const TRANSFORMED = 'index.html';

/** 内嵌样式：与 serve-etm.py 的 EMBEDDED_STYLE 保持一致（本地工作台与线上副本同一套观感）。 */
export const EMBEDDED_STYLE = '<style id="etm-embedded-style">body#etm-app[data-embedded]{min-height:0}body#etm-app[data-embedded]>header{display:none}body#etm-app[data-embedded]>.workspace{height:100vh}body#etm-app .embedded-actions{display:flex;gap:4px}</style>';

/** 静态副本专用 bootstrap（无模块、同步执行；classic script 在模块脚本之前跑完）。 */
export function bootstrapScript(newline = '\n'){
  const lines = [
    '<script id="etm-online-bootstrap">',
    '/* 自动生成，别手改：web/tools/dev/sync-etm-page.mjs（见 web/docs/etm-online-bridge.md） */',
    '(function(){',
    "  var query=new URLSearchParams(location.search);",
    "  var embedded=query.get('embedded')==='1';",
    "  var api=query.get('api');",
    "  if(!api){try{api=localStorage.getItem('etm.apiBase');}catch(e){}}",
    "  if(!api){api=/\\.github\\.io$/i.test(location.hostname)?'http://127.0.0.1:8899/etm/api/':'/api/';}",
    "  if(!/\\/$/.test(api))api+='/';",
    "  var body=document.body;body.dataset.apiBase=api;if(embedded)body.dataset.embedded='1';",
    "  window.__etmApiBase=api;",
    "  var notice=null;",
    "  function showNotice(message){",
    "    if(notice)return;",
    "    notice=document.createElement('div');notice.id='etm-online-notice';notice.setAttribute('role','alert');",
    "    notice.style.cssText='position:fixed;z-index:99;left:50%;top:12px;transform:translateX(-50%);max-width:min(720px,92vw);background:#3b1d22;color:#fff;border:1px solid #a45;border-radius:8px;padding:10px 14px;font:13px/1.6 system-ui,Segoe UI,sans-serif;box-shadow:0 6px 24px #0007';",
    "    var strong=document.createElement('strong');strong.textContent='需要本地 ETM 服务（桥）';",
    "    notice.appendChild(strong);notice.appendChild(document.createElement('br'));",
    "    notice.appendChild(document.createTextNode(message+' '));",
    "    function link(href,text){var a=document.createElement('a');a.href=href;a.textContent=text;a.style.color='#9fd';return a;}",
    "    notice.appendChild(link('../bridge/etm/downloads/etm-bridge-win64.zip','下载完整 ETM 桥包'));",
    "    notice.appendChild(document.createTextNode(' · '));",
    "    notice.appendChild(link('../docs/etm-online-bridge.md','启动与排障'));",
    "    body.appendChild(notice);",
    "  }",
    "  function diagnose(message){var state=document.getElementById('job-state');if(state){state.textContent='页面发生错误：'+message;state.classList.add('bad');}showNotice('页面运行出错：'+message);}",
    "  addEventListener('error',function(e){diagnose(e.message||'未知错误');});",
    "  addEventListener('unhandledrejection',function(e){diagnose((e.reason&&e.reason.message)||String(e.reason));});",
    "  if(!embedded){",
    "    addEventListener('load',function(){",
    "      var controller=new AbortController();var timer=setTimeout(function(){controller.abort();},3000);",
    "      fetch(api+'status',{cache:'no-store',signal:controller.signal}).then(function(response){clearTimeout(timer);if(!response.ok)throw Error('HTTP '+response.status);}).catch(function(){clearTimeout(timer);showNotice('没有检测到本机桥（'+api.replace(/\\/api\\/$/,'')+'）。请先启动桥，或在主工具的 ETM 标签里填写桥地址后点「重新检测」。');});",
    "    });",
    "  }",
    "})();",
    '</script>',
  ];
  return lines.join(newline);
}

/** 对 index.html 做副本专用的三处改写（输入/输出都是字符串，便于 --check 复现）。 */
export function transformIndexHtml(text){
  const newline = text.includes('\r\n') ? '\r\n' : '\n';
  let out = text.replace(/(\s(?:href|src)=")\/(?!\/)/g, '$1./');
  const marker = '<script type="module" src="./app/view.js"></script>';
  if (!out.includes(marker)) throw new Error('ETM index.html 里找不到 ./app/view.js 的模块脚本（页面结构变了？）');
  out = out.replace(marker, `${EMBEDDED_STYLE}${newline}${bootstrapScript(newline)}${newline}${marker}`);
  return out;
}

function walk(dir, base = dir, acc = []){
  for (const entry of readdirSync(dir, { withFileTypes: true })){
    const full = join(dir, entry.name);
    if (entry.isDirectory()) walk(full, base, acc);
    else if (entry.isFile()) acc.push(relative(base, full).replace(/\\/g, '/'));
  }
  return acc.sort();
}

/** 生成「期望的副本内容」：Map<相对路径, Buffer>。--check 与生成共用这一份。 */
export function buildPageFiles(){
  const files = new Map();
  for (const rel of walk(SOURCE_DIR)){
    const bytes = readFileSync(join(SOURCE_DIR, rel));
    if (rel === TRANSFORMED) files.set(rel, Buffer.from(transformIndexHtml(bytes.toString('utf8')), 'utf8'));
    else files.set(rel, bytes);
  }
  return files;
}

export function checkCopy(){
  const problems = [];
  const expected = buildPageFiles();
  for (const [rel, bytes] of expected){
    const path = join(DEST_DIR, rel);
    if (!existsSync(path)){ problems.push(`MISSING  ${rel}`); continue; }
    if (!readFileSync(path).equals(bytes)) problems.push(`DRIFT    ${rel}`);
  }
  const actual = existsSync(DEST_DIR) ? walk(DEST_DIR, DEST_DIR) : [];
  for (const rel of actual) if (!expected.has(rel)) problems.push(`STALE    ${rel}`);
  return { problems, count: expected.size, actual: actual.length };
}

function main(){
  const check = process.argv.includes('--check');
  if (check){
    const { problems, count, actual } = checkCopy();
    for (const problem of problems) console.log(`  ${problem}`);
    console.log(problems.length
      ? `ETM 线上副本已漂移：${problems.length} 处（期望 ${count} 个文件 / 实际 ${actual} 个）—— 运行 node tools/dev/sync-etm-page.mjs 重新生成`
      : `ETM 线上副本一致（${count} 个文件，index.html 已按副本规则改写）`);
    return problems.length ? 1 : 0;
  }
  const expected = buildPageFiles();
  mkdirSync(DEST_DIR, { recursive: true });
  let written = 0, unchanged = 0;
  for (const [rel, bytes] of expected){
    const path = join(DEST_DIR, rel);
    mkdirSync(dirname(path), { recursive: true });
    if (existsSync(path) && readFileSync(path).equals(bytes)){ unchanged++; continue; }
    writeFileSync(path, bytes); written++;
  }
  // 删掉源里已经不存在的文件（页面文件改名/删除后不留旧副本）。
  let removed = 0;
  for (const rel of existsSync(DEST_DIR) ? walk(DEST_DIR, DEST_DIR) : []){
    if (expected.has(rel)) continue;
    const path = join(DEST_DIR, rel); rmSync(path, { force: true }); removed++;
    // 顺手清掉空目录（best-effort）
    try { let dir = dirname(path); while (dir.startsWith(DEST_DIR) && readdirSync(dir).length === 0){ rmSync(dir, { recursive: true, force: true }); dir = dirname(dir); } } catch {}
  }
  const indexBytes = expected.get(TRANSFORMED);
  console.log(`已生成 ETM 线上副本：写入 ${written} 个 / 已相同 ${unchanged} 个 / 删除过期 ${removed} 个`);
  console.log(`  index.html 改写后 ${indexBytes.length} B（sha256 ${createHash('sha256').update(indexBytes).digest('hex').slice(0, 16)}…）`);
  console.log(`  目标：${DEST_DIR}`);
  return 0;
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url))){
  try { process.exitCode = main(); }
  catch (e){ console.error(String(e?.stack || e)); process.exitCode = 1; }
}
