/**
 * ETM 线上页副本 + 跨源桥接的自测（纯离线，不需要硬件/浏览器）：
 *   node tools/selftest/etm-page-copy.test.mjs     （已并入 make test）
 *
 * 咬五件事：
 *   ① `web/etm/**` 与 `etm/web/**` 的生成规则一致（= `sync-etm-page.mjs --check`）；
 *   ② 副本里**没有绝对路径**（否则挂在 /superlinkPRO/etm/ 下就 404），且带 bootstrap；
 *   ③ 子页 API base 的三级来源（data-api-base / ?api= / localStorage）+ github.io 默认值都在；
 *   ④ 父页探测桥、放宽 attach 白名单的代码在位（跨源拓扑能成立）；
 *   ⑤ 两个 server.py 都带 CORS（白名单 + 预检 + 可配置），且都是"额外允许"不是 `*`。
 */
import { readFileSync, existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { buildPageFiles, checkCopy, SOURCE_DIR, DEST_DIR } from '../dev/sync-etm-page.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const WEB = resolve(here, '..', '..');
const REPO = resolve(WEB, '..');
let pass = 0, fail = 0;
const ok = (condition, name, extra = '') => { if (condition){ pass++; console.log(`  PASS  ${name}`); } else { fail++; console.log(`  FAIL  ${name}${extra ? '  → ' + extra : ''}`); } };
const read = (...parts) => readFileSync(join(...parts), 'utf8');

// ==================================================================== 1
console.log('== 1. 副本与源一致（生成规则可复现） ==');
{
  const { problems, count, actual } = checkCopy();
  ok(problems.length === 0, `sync-etm-page 副本一致（${count} 个文件 / 磁盘 ${actual} 个）`, problems.join(' · '));
  ok(count === actual, '期望文件数 == 磁盘文件数');
  const expected = buildPageFiles();
  ok(expected.has('index.html') && expected.has('app/view.js') && expected.has('app/shared/rtt/dap-webusb.js'),
    '副本覆盖 index.html + app/view.js + shared 驱动');
}

// ==================================================================== 2
console.log('== 2. 副本 index.html：无绝对路径 + 有 bootstrap/内嵌样式 ==');
{
  const html = read(DEST_DIR, 'index.html');
  const absolute = [...html.matchAll(/\s(?:href|src)="(\/[^/][^"]*)"/g)].map(m => m[1]);
  ok(absolute.length === 0, '没有 href="/…" / src="/…" 绝对路径', absolute.join(' · '));
  ok(html.includes('href="./style.css"') && html.includes('src="./app/view.js"') && html.includes('href="./app/main-tool-theme.css"'),
    '三条资源路径都改成了相对路径');
  ok(html.includes('id="etm-online-bootstrap"'), '含静态副本 bootstrap');
  ok(html.includes('id="etm-embedded-style"'), '含内嵌样式（与 serve-etm 一致）');
  ok(html.includes("get('api')") && html.includes("localStorage.getItem('etm.apiBase')"),
    'bootstrap 支持 ?api= 与 localStorage');
  ok(html.includes("'http://127.0.0.1:8899/etm/api/'"), 'github.io 默认指向本机桥 8899');
  ok(html.includes('../bridge/etm/downloads/etm-bridge-win64.zip') && html.includes('../docs/etm-online-bridge.md'),
    '无桥时可下载便携包/查排障文档（不白屏）');
  ok(html.includes("dataset.embedded='1'"), '?embedded=1 时打内嵌标记');
}

// ==================================================================== 3
console.log('== 3. 子页 API base 三级来源 + 默认值（页面源码） ==');
{
  const view = read(SOURCE_DIR, 'app', 'view.js');
  ok(view.includes('document.body?.dataset?.apiBase'), '来源①：data-api-base 属性（serve-etm 注入/副本 bootstrap）');
  ok(view.includes("new URLSearchParams(location.search).get('api')"), '来源②：?api= 查询参数');
  ok(view.includes("localStorage.getItem('etm.apiBase')"), '来源③：localStorage 记忆值');
  ok(/github\.io/.test(view) && view.includes('http://127.0.0.1:8899/etm/api/'), '默认值：github.io → 本机桥 8899/etm/api/');
  ok(view.includes('function apiUrl('), '把服务端返回的 /etm/api/… 根相对下载地址绑到 API 源');
  const embedding = read(SOURCE_DIR, 'app', 'embedding.js');
  ok(embedding.includes("get('embedded')==='1'"), '内嵌判定兼容 ?embedded=1（副本用）与 data-embedded（serve-etm 用）');
}

// ==================================================================== 4
console.log('== 4. 父页：多端口探测 + 白名单 attach ==');
{
  const host = read(WEB, 'app', 'etm', 'view.js');
  ok(host.includes('BRIDGE_PORTS = [8878, 8890, 8899]'), '候选端口 8878/8890/8899');
  ok(host.includes('PROBE_TIMEOUT_MS = 3000'), '单候选 3 s 超时');
  ok(host.includes('Promise.any'), '并发探测、命中即用');
  ok(host.includes("PAGES_ORIGIN = 'https://minichao9901.github.io'"), '父页白名单含 Pages origin');
  ok(/originAllowedChild[\s\S]{0,200}127\\\.0\\\.0\\\.1/.test(host), '子页白名单：http://127.0.0.1:* / localhost:*');
  ok(host.includes("if(this.bridge&&this.probe)"), '保留「旧子页仍连着探针」保护');
  ok(host.includes('./etm/index.html?embedded=1&api='), '跨源时 iframe 指向同源副本（子页与父页同源才能 attach）');
  ok(host.includes("'/etm/?embedded=1'"), '同源命中时仍用本地工作台页面（不回归）');
  ok(host.includes('id="etm-bridge"') || read(WEB, 'index.html').includes('id="etm-bridge"'), '页面有桥地址输入框');
  ok(read(WEB, 'index.html').includes('id="etm-bridge"') && read(WEB, 'index.html').includes('id="etm-retry"'),
    '页面有「桥地址 + 重新检测」控件');
}

// ==================================================================== 5
console.log('== 5. 两个 server.py 都有 CORS（额外允许，不是 *） ==');
for (const file of [join(REPO, 'etm', 'server.py'), join(WEB, 'bridge', 'etm', 'backend', 'server.py')]){
  const rel = file.startsWith(REPO + '\\') ? file.slice(REPO.length + 1).replace(/\\/g, '/') : file;
  const source = readFileSync(file, 'utf8');
  ok(source.includes("CORS_PAGE_HOSTS = {'minichao9901.github.io'}"), `${rel}: Pages origin 白名单`);
  ok(source.includes('CORS_LOCAL_RE') && source.includes('ETM_CORS_ORIGINS'), `${rel}: 本机回环匹配 + 可配置追加`);
  ok(source.includes('Access-Control-Allow-Origin') && source.includes('Access-Control-Allow-Methods'), `${rel}: 发 CORS 头`);
  ok(source.includes('def cors_preflight') && source.includes("status=204"), `${rel}: OPTIONS 预检 204`);
  ok(source.includes('def cors_path') && source.includes("'/api/' in path"), `${rel}: 只对 API/integration.json 加 CORS`);
  ok(!source.includes("'Access-Control-Allow-Origin'] = '*'") && !source.includes('"Access-Control-Allow-Origin" = "*"'),
    `${rel}: 不是通配符 *`);
  ok(source.includes("request.method not in ('GET', 'OPTIONS')"), `${rel}: 跨源 POST 不再被 local_only 误杀`);
}

// ==================================================================== 6
console.log('== 6. 副本里的共享驱动与真源一致（8 个） ==');
{
  const drivers = ['rtt/dap-webusb.js', 'core/usb-device.js', 'core/probe-bus.js', 'core/pace.js', 'core/bin.js', 'core/operation-budget.js', 'dbg/bp.js', 'dbg/source.js'];
  let same = 0;
  for (const rel of drivers){
    const truth = readFileSync(join(WEB, 'app', rel));
    const copy = readFileSync(join(DEST_DIR, 'app', 'shared', rel));
    if (truth.equals(copy)) same++; else console.log(`        差异：${rel}`);
  }
  ok(same === drivers.length, `8/8 副本与 web/app 真源逐字节一致（实际 ${same}/8）`);
}

console.log(`\n${fail ? '❌' : '✅'} etm-page-copy.test: ${pass} 通过 / ${fail} 失败`);
process.exitCode = fail ? 1 : 0;
