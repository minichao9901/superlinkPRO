/**
 * 「工程生成」页的真页面验收（CDP 驱动真浏览器）：
 *   node tools/selftest/gen-page.test.mjs        （等价：make test-gen-page）
 * 前置：静态服务跑在 8899（make open / make serve），浏览器带 CDP 跑在 9333（make browser）。
 *
 * 验的是一条完整链路：页面渲染 → 产物字节 vs Python 工具基线 → 勾选/换行符开关生效
 * → .uvprojx 自动填参 → 「打包 ZIP」真的下下来 → 「写入文件夹」的写入逻辑。
 * 真·写入文件夹需要真人手势 + 目录授权，自动跑不了；那一节用假的 showDirectoryPicker 驱动
 * 页面里真实的 saveToFolder()，覆盖同名冲突检测 / 覆盖开关 / 逐个文件的字节。
 */
import { readFileSync, readdirSync, rmSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const CDP = process.env.CDP || 'http://127.0.0.1:9333';
const APP = process.env.APP || 'http://127.0.0.1:8899/index.html';
const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..').replace(/\\/g, '/');
const DL = join(ROOT, 'tmp', 'gen-dl');
const FIXTURES = join(ROOT, 'tools', 'fixtures', 'gen', 'mdk-arm');
const UVPROJX = join(ROOT, 'tools', 'fixtures', 'gen', 'uvprojx-sample', 'CubeMX_Config.uvprojx');

// 页面**固定**套 4 项修正（app/gen/fixes.js），所以"期望字节" = 对账基线再走一遍同一套修正。
// 修正逻辑本身在 gen-parity.mjs 里单独断言，这里只负责"页面确实按它产出了"。
const { applyFixes } = await import('file://' + join(ROOT, 'app', 'gen', 'fixes.js'));
const { ALL_FIXES, normalizeNewlines, toBytes } = await import('file://' + join(ROOT, 'app', 'gen', 'model.js'));
const expectedBytes = name => {
  const p = join(FIXTURES, name);
  if (name.endsWith('.bin')) return readFileSync(p);
  return Buffer.from(toBytes(applyFixes(name, normalizeNewlines(readFileSync(p, 'utf8')), ALL_FIXES), 'crlf'));
};

setTimeout(() => { console.error('[WATCHDOG] 总超时，强制退出'); process.exit(9); }, 120000);

let pass = 0, fail = 0;
const ok = (cond, name, extra = '') => {
  if (cond){ pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${extra ? '  → ' + extra : ''}`); }
};
const sleep = ms => new Promise(r => setTimeout(r, ms));

// ---------------------------------------------------------------- CDP 小客户端
const list = await (await fetch(CDP + '/json/list', { signal: AbortSignal.timeout(5000) })).json();
const page = list.find(t => t.type === 'page');
if (!page) throw new Error('CDP 里没有页面目标');
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('CDP 连不上')); });

let seq = 0;
const pending = new Map();
ws.onmessage = e => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)){
    const { res, rej } = pending.get(m.id);
    pending.delete(m.id);
    m.error ? rej(new Error(m.error.message)) : res(m.result);
  }
};
const send = (method, params = {}, timeout = 8000) => new Promise((res, rej) => {
  const id = ++seq;
  pending.set(id, { res, rej });
  ws.send(JSON.stringify({ id, method, params }));
  setTimeout(() => { if (pending.delete(id)) rej(new Error(method + ' 超时')); }, timeout);
});
const evaluate = async (expression, timeout = 8000) => {
  const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeout);
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result?.value;
};
const until = async (expr, what, tries = 80, gap = 150) => {
  for (let i = 0; i < tries; i++){
    try { if (await evaluate(expr)) return true; } catch {}
    await sleep(gap);
  }
  throw new Error('等待超时：' + what);
};

// ---------------------------------------------------------------- 打开页面
await send('Page.enable');
await send('Runtime.enable');
await send('DOM.enable');
// 关掉 HTTP 缓存：否则刚改完 JS 就跑测试，可能拿到上一次的模块（跑出"假失败"）
try { await send('Network.enable'); await send('Network.setCacheDisabled', { cacheDisabled: true }); } catch {}
await send('Page.navigate', { url: APP + '?t=' + Date.now() + '#gen' });   // 带 cache-bust：hash-only 导航不会重新执行模块
await until('!!(window.__tools && window.__tools.gen && document.readyState === "complete")', '页面与 gen 模块就绪', 120, 200);

console.log('== 1. 页面接好了没 ==');
{
  ok(await evaluate(`document.querySelector('#tabs .tab[data-tab=gen]') !== null`), '顶栏有「工程生成」标签');
  ok(await evaluate(`document.querySelector('#tab-gen').classList.contains('active')`), '#gen 直达链接生效（面板已激活）');
  const tabs = await evaluate(`JSON.stringify(window.__tools.summary().tabs)`);
  ok(tabs.includes('gen') && tabs.includes('flash'), `自检摘要里的标签列表 = ${tabs}`);
  ok(await evaluate(`window.__tools.errors.length === 0`), '页面没有 JS 报错', await evaluate(`JSON.stringify(window.__tools.errors)`));
}

console.log('== 1b. 复位成已知状态（页面选项存在 localStorage 里，上一轮跑完会影响下一轮）==');
{
  await evaluate(`(()=>{
    const fire = (el, ev) => el.dispatchEvent(new Event(ev, { bubbles:true }));
    const f = document.getElementById('g-force'); f.checked = false; fire(f, 'change');
    for (const id of ['g-c-jlink','g-c-gdb','g-c-pyocd','g-c-openocd','g-c-testbin']){
      const e = document.getElementById(id); e.checked = true; fire(e, 'change');
    }
    const n = document.getElementById('g-newline'); n.value = 'crlf'; fire(n, 'change');
    const s = document.getElementById('g-speed'); s.value = '25000'; fire(s, 'input');
    return true;
  })()`);
  await sleep(300);
  ok(!(await evaluate(`document.getElementById('g-force').checked`)), '「直接覆盖」已复位为未勾选');
  const after = await evaluate(`JSON.stringify(window.__tools.gen.summary())`);
  const sm = JSON.parse(after);
  ok(sm.files.length === 13 && sm.bridgeKit.files.length === 7,
     `产物重新全勾上：6 个模板文件 + 7 个桥包文件（实际 ${sm.files.length} / 桥包 ${sm.bridgeKit.files.length}）`);
}

console.log('== 2. 产物字节 = Python 工具基线 + 4 项固定修正 ==');
{
  // 项目名改成基线那个（其余默认值本就一致），然后逐字节比。
  // 注意：页面**固定**套 4 项修正，所以期望值 = 基线文件再走一遍同一套修正逻辑。
  await evaluate(`(()=>{const e=document.getElementById('g-project');e.value='MDK-ARM';e.dispatchEvent(new Event('input',{bubbles:true}));return true})()`);
  await sleep(300);
  const all = await evaluate(`(async()=>{
    const b64 = u8 => { let s=''; for(let i=0;i<u8.length;i+=4096) s += String.fromCharCode.apply(null, u8.subarray(i,i+4096)); return btoa(s); };
    return window.__tools.gen.files.map(f => ({ name:f.name, len:f.data.length, b64:b64(f.data) }));
  })()`);
  const files = all.filter(f => !f.name.startsWith('rtt-bridge-kit/'));
  ok(files.length === 6 && all.length === 13, `模板产物 6 个（总 ${all.length}，另 7 个是桥包）`, all.map(f => f.name).join(','));
  for (const f of files){
    const fixed = expectedBytes(f.name);
    const got = Buffer.from(f.b64, 'base64');
    if (Buffer.compare(Buffer.from(fixed), got) === 0) ok(true, `${f.name} = 基线 + 修正 后逐字节相同（${got.length} B）`);
    else {
      let at = -1;
      const w = Buffer.from(fixed);
      for (let i = 0; i < Math.min(w.length, got.length); i++) if (w[i] !== got[i]){ at = i; break; }
      ok(false, `${f.name} 逐字节相同`, `长度 ${w.length} vs ${got.length}，首个差异 @${at}`);
    }
  }
}

console.log('== 2b. 4 项修正真的生效（页面产物里逐条查）==');
{
  const text = async name => (await evaluate(`window.__tools.gen.files.find(f=>f.name===${JSON.stringify(name)}).text`)) || '';
  const jl = await text('Makefile.jlink');
  const oc = await text('Makefile.openocd');
  ok(/RTT_SIZE \?= 0x2000/.test(jl) && !/RTT_SIZE \?= 0x5000/.test(jl), '① Makefile.jlink 的 RTT_SIZE = 0x2000');
  ok(!/del \*\.log/.test(jl), '② clean-jlink 里没有删 *.log 那行');
  ok(/-c "init;.*rtt setup .*\\"SEGGER RTT\\";.*rtt server start/.test(oc), '③ openocd-rtt 用双引号 + 转义内层引号');
  ok(!/jlink-swo/.test(jl), '④ Makefile.jlink 里再没有 jlink-swo（目标/注释/help 都清了）');
  ok(!/JLinkSWOViewerCL/.test(jl), '④ 顺带：SWO viewer 那行也没了');
  const status = await evaluate(`document.getElementById('g-status').textContent`);
  ok(/修正 4 项/.test(status), `状态栏标明修正项数：${status}`);
}

console.log('== 3. 预览区显示的就是要写出去的内容 ==');
{
  const shown = await evaluate(`document.getElementById('g-preview').textContent.length`);
  ok(shown > 3000, `预览非空（${shown} 字符）`);
  const head = await evaluate(`document.getElementById('g-preview').textContent.slice(0,60)`);
  ok(head.startsWith('# JLink Makefile for MDK-ARM'), `预览开头正确：${JSON.stringify(head.slice(0, 40))}`);
  const status = await evaluate(`document.getElementById('g-status').textContent`);
  ok(/13 个文件.*CRLF.*含桥包/.test(status), `状态行：${status}`);
  // 切到 test_sram.bin 看二进制预览
  await evaluate(`[...document.querySelectorAll('#g-files button')].find(b=>b.textContent==='test_sram.bin').click(), true`);
  await sleep(120);
  const bin = await evaluate(`document.getElementById('g-preview').textContent.slice(0,80)`);
  ok(/^00000000  00 01 02 03/.test(bin), `二进制走 hex 预览：${JSON.stringify(bin.slice(0, 40))}`);
}

console.log('== 4. 勾选与参数开关真的生效 ==');
{
  const before = await evaluate(`window.__tools.gen.files.length`);
  await evaluate(`(()=>{const e=document.getElementById('g-c-pyocd');e.checked=false;e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  await sleep(300);
  const after = await evaluate(`JSON.stringify(window.__tools.gen.files.map(f=>f.name))`);
  ok(before === 13 && !after.includes('Makefile.pyocd'), `取消勾选后不再生成 Makefile.pyocd（${before} → ${JSON.parse(after).length} 个）`);
  await evaluate(`(()=>{const e=document.getElementById('g-c-pyocd');e.checked=true;e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);

  // 参数改动 → 只该动那一行（拿页面自己的两个状态比，避免把修正项掺进来）
  const baseJl = await evaluate(`window.__tools.gen.files.find(f=>f.name==='Makefile.jlink').text`);
  await evaluate(`(()=>{const e=document.getElementById('g-speed');e.value='50000';e.dispatchEvent(new Event('input',{bubbles:true}));return true})()`);
  await sleep(300);
  const tuned = await evaluate(`window.__tools.gen.files.find(f=>f.name==='Makefile.jlink').text`);
  const la = baseJl.split('\n'), lb = tuned.split('\n');
  const diffLines = la.map((l, i) => l === lb[i] ? null : i).filter(i => i !== null);
  ok(la.length === lb.length && diffLines.length === 1
     && /JLINK_SPEED \?= 25000/.test(la[diffLines[0]]) && /JLINK_SPEED \?= 50000/.test(lb[diffLines[0]]),
     `改成 50000 后只动了 JLINK_SPEED 那一行（实际 ${diffLines.length} 行）`,
     diffLines.map(i => `${la[i]} → ${lb[i]}`).join(' | '));
  await evaluate(`(()=>{const e=document.getElementById('g-speed');e.value='25000';e.dispatchEvent(new Event('input',{bubbles:true}));return true})()`);
  await sleep(250);

  const b64 = expr => evaluate(`(()=>{const u8=${expr}; let s=''; for(let i=0;i<u8.length;i+=4096) s += String.fromCharCode.apply(null, u8.subarray(i,i+4096)); return btoa(s)})()`);
  const crlfBytes = Buffer.from(await b64(`window.__tools.gen.files.find(f=>f.name==='Makefile.jlink').data`), 'base64');
  await evaluate(`(()=>{const e=document.getElementById('g-newline');e.value='lf';e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  await sleep(300);
  const lfBytes = Buffer.from(await b64(`window.__tools.gen.files.find(f=>f.name==='Makefile.jlink').data`), 'base64');
  const crlfText = crlfBytes.toString('utf8');
  ok(lfBytes.toString('utf8') === crlfText.replace(/\r\n/g, '\n'), 'LF 开关：内容除换行外一致');
  ok(lfBytes.length === crlfBytes.length - (crlfText.match(/\r\n/g) || []).length, 'LF 模式字节数 = 去掉 CR 后的长度');
  await evaluate(`(()=>{const e=document.getElementById('g-newline');e.value='crlf';e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  await sleep(250);
}

console.log('== 5. 拖入 .uvprojx → 自动填参 ==');
{
  const { root } = await send('DOM.getDocument', { depth: -1 });
  const { nodeId } = await send('DOM.querySelector', { nodeId: root.nodeId, selector: '#g-uvfile' });
  ok(nodeId > 0, '找得到文件输入框');
  await send('DOM.setFileInputFiles', { files: [UVPROJX.replace(/\//g, '\\')], nodeId });
  await evaluate(`document.getElementById('g-uvfile').dispatchEvent(new Event('change')), true`);
  await sleep(600);
  const v = await evaluate(`JSON.stringify({
    project: document.getElementById('g-project').value,
    device: document.getElementById('g-device').value,
    flash: document.getElementById('g-flash-start').value,
    pyocd: document.getElementById('g-pyocd-target').value,
    ocd: document.getElementById('g-openocd-target').value,
    hint: document.getElementById('g-detect').textContent,
    detect: window.__tools.gen.summary().detect,
  })`);
  const r = JSON.parse(v);
  ok(r.device === 'STM32F103RB', `器件按族映射：${r.device}`);
  ok(r.flash === '0x08000000', `Flash 起址：${r.flash}`);
  ok(r.pyocd === 'stm32f103rb' && r.ocd === 'target/stm32f1x.cfg', `PyOCD/OpenOCD 目标：${r.pyocd} / ${r.ocd}`);
  ok(r.project === 'CubeMX_Config', `项目名退回 <TargetName>：${r.project}`);
  ok(/STM32F103C8/.test(r.hint) && /RAM 0x20000000\+0x5000/.test(r.hint), `识别提示：${r.hint}`);
  ok(r.detect && r.detect.device === 'STM32F103C8', '解析结果里有原始器件名');
}

console.log('== 6. 「打包 ZIP」真的下下来 ==');
{
  // 把项目名改回基线那个，这样解出来的每个文件都能和 fixtures 逐字节对
  await evaluate(`(()=>{const e=document.getElementById('g-project');e.value='MDK-ARM';e.dispatchEvent(new Event('input',{bubbles:true}));return true})()`);
  await sleep(300);
  rmSync(DL, { recursive: true, force: true });
  mkdirSync(DL, { recursive: true });
  await send('Browser.setDownloadBehavior', { behavior: 'allow', downloadPath: DL.replace(/\//g, '\\'), eventsEnabled: true });
  await evaluate(`document.getElementById('g-zip').click(), true`);
  let zip = null;
  for (let i = 0; i < 40; i++){
    await sleep(250);
    const files = readdirSync(DL).filter(f => f.endsWith('.zip'));
    if (files.length){ zip = join(DL, files[0]); break; }
  }
  ok(!!zip, '下载目录里出现了 zip', zip || readdirSync(DL).join(','));
  if (zip){
    const size = readFileSync(zip).length;
    ok(size > 40000, `zip 大小 ${size} B（6 个模板文件 42509 B + 桥包约 68 KB）`);
    ok(/MDK-ARM-gen\.zip$/.test(zip), '文件名按项目名生成：' + zip.split('\\').pop());
    // 桥包那 7 个也在这个 zip 里（名字带 rtt-bridge-kit/ 一层）—— 直接按 ZIP 中央目录里的名字核
    const raw = readFileSync(zip);
    const names = [];
    for (let i = 0; i + 30 < raw.length; i++){
      if (raw.readUInt32LE(i) === 0x02014b50){ const n = raw.readUInt16LE(i + 28); names.push(raw.subarray(i + 46, i + 46 + n).toString('utf8')); }
    }
    ok(names.filter(n => n.startsWith('rtt-bridge-kit/')).length === 7,
       `zip 里带 rtt-bridge-kit/ 那 7 个文件（中央目录读到 ${names.length} 项：${names.slice(0, 3).join(',')}…）`);
  }
  const supported = await evaluate(`window.__tools.gen.summary().supported.directoryPicker`);
  console.log(`  NOTE  showDirectoryPicker 可用 = ${supported}（真·写入文件夹需要真人手势，自动化测不了）`);
}

console.log('== 7. 「写入文件夹」的写入逻辑（拿假目录句柄跑一遍真代码路径）==');
{
  // 真·showDirectoryPicker 需要真人手势，自动化点不了；这里把它换成假的，
  // 但走的是 page 里真实的 saveToFolder()（含冲突检测 / 覆盖 / 逐个写入）。
  await evaluate(`(()=>{
    window.__mock = { written:{}, asked:0 };
    window.confirm = () => { window.__mock.asked++; return true; };
    // 目录句柄：带 getDirectoryHandle（桥包的文件名里有 rtt-bridge-kit/ 这一层）
    const mk = label => ({
      name: label,
      async getFileHandle(name, opts){
        if (opts && opts.create === false){
          if (name === 'Makefile.jlink' || name === 'rtt_logger.py') return { name };   // 假装这两个已存在
          const e = new Error('NotFound'); e.name = 'NotFoundError'; throw e;
        }
        return { name, async createWritable(){ return {
          async write(d){
            const u8 = d instanceof Uint8Array ? d : new Uint8Array(d);
            let s = ''; for (let i = 0; i < u8.length; i += 4096) s += String.fromCharCode.apply(null, u8.subarray(i, i + 4096));
            window.__mock.written[name] = btoa(s);
          },
          async close(){},
        }; } };
      },
      async getDirectoryHandle(name){ return mk(name); },
    });
    window.showDirectoryPicker = async () => mk('FAKE-PROJ');
    return true;
  })()`);
  await evaluate(`document.getElementById('g-save').click(), true`);
  await sleep(900);
  const raw = await evaluate(`JSON.stringify({ names:Object.keys(window.__mock.written), asked:window.__mock.asked, status:document.getElementById('g-status').textContent, force:document.getElementById('g-force').checked })`);
  const r = JSON.parse(raw);
  ok(r.names.length === 13 && r.names.includes('start-bridge.bat') && r.names.includes('rtt-bridge.mjs'),
     `13 个文件都写进去了（6 模板 + 7 桥包，实际 ${r.names.length}：${r.names.join(',')}）`);
  ok(r.asked === 1, `同名文件触发了一次覆盖确认（asked=${r.asked}）`);
  ok(/FAKE-PROJ/.test(r.status), `状态行报告写入目录：${r.status}`);
  // 模板那 6 个逐字节对基线（桥包那 7 个由 tools/selftest/bridge-kit.test.mjs 管内容）
  for (const f of ['Makefile.jlink', 'jlink_gdb.script', 'Makefile.pyocd', 'Makefile.openocd', 'rtt_logger.py', 'test_sram.bin']){
    const b64 = await evaluate(`window.__mock.written[${JSON.stringify(f)}]`);
    const want = expectedBytes(f);
    const got = Buffer.from(b64, 'base64');
    ok(want.equals(got), `${f} 写出去的字节 = 基线 + 修正（${got.length} B）`);
  }
  // 勾上「已存在就直接覆盖」→ 不该再问
  await evaluate(`(()=>{const e=document.getElementById('g-force');e.checked=true;e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  await evaluate(`document.getElementById('g-save').click(), true`);
  await sleep(700);
  const asked2 = await evaluate(`window.__mock.asked`);
  ok(asked2 === 1, `勾了「直接覆盖」后不再弹确认（asked 仍为 ${asked2}）`);
}

console.log('== 7b. 桥包（本地桥安装包）：内容与「下载桥包」按钮 ==');
{
  const kit = await evaluate(`(async()=>{
    const g = window.__tools.gen;
    const f = n => g.files.find(x => x.name.endsWith('/' + n));
    return {
      names: g.files.filter(x => x.name.startsWith('rtt-bridge-kit/')).map(x => x.name.split('/').pop()),
      bat: f('start-bridge.bat').text,
      cfg: f('bridge.config.json').text,
      mjs: f('rtt-bridge.mjs').data.length,
      sh: f('start-bridge.sh').text,
      ps: f('get-node.ps1').text,
    };
  })()`);
  ok(kit.names.length === 7, `桥包 7 个文件：${kit.names.join(' · ')}`);
  ok(/--doctor/.test(kit.bat) && /get-node\.ps1/.test(kit.bat) && /Ctrl\+C/.test(kit.bat),
     '启动器里有：环境预检 + 便携 Node 兜底 + Ctrl+C 说明');
  ok(JSON.parse(kit.cfg).targets.stm32f103 && JSON.parse(kit.cfg).jlink.speed === 50000,
     '配置里带目标与 J-Link 默认值（真机标定过的 50 MHz）');
  ok(kit.mjs > 50000, `桥本体嵌进来了（${kit.mjs} B，与仓库那份逐字节一致由 make test-gen 对）`);
  ok(/^#!\/bin\/sh/.test(kit.sh) && !/\r/.test(kit.sh), 'sh 版是 LF 的（CRLF 会让 bash 报 bad interpreter）');
  ok(/npmmirror/.test(kit.ps) && /SHASUMS256/.test(kit.ps), '便携 Node 下载器带国内镜像与 sha256 校验');

  // 改一个参数 → 立刻反映到生成物（页面是"边填边生成"）
  await evaluate(`(()=>{const e=document.getElementById('g-bk-port');e.value='17999';e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  await sleep(300);
  const port = await evaluate(`window.__tools.gen.files.find(x=>x.name.endsWith('start-bridge.bat')).text.includes('set "PORT=17999"')`);
  ok(port === true, '页面里改端口 → 桥包的启动器立刻跟着改');
  await evaluate(`(()=>{const e=document.getElementById('g-bk-port');e.value='17321';e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  await sleep(250);

  // 关掉开关 → 桥包那 7 个消失
  await evaluate(`(()=>{const e=document.getElementById('g-bk-on');e.checked=false;e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  await sleep(300);
  const off = await evaluate(`JSON.stringify(window.__tools.gen.summary().bridgeKit)`);
  const offObj = JSON.parse(off);
  ok(offObj.on === false && offObj.files.length === 0, `关掉「生成桥包」→ 只剩 6 个模板文件（桥包 ${offObj.files.length} 个）`);
  await evaluate(`(()=>{const e=document.getElementById('g-bk-on');e.checked=true;e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  await sleep(250);
  ok(await evaluate(`!!document.getElementById('g-zip-bridge')`), '「下载桥包（ZIP）」按钮在页面上');
}

// ==================================================================== 8
console.log('== 8. 右列 tab 化 + 左栏联动（2026-10 重构）==');
{
  /* 起手自带前置：`gen.dock` 是持久化设置，上一轮留在别的 tab 会让这一节整片红
     （和 #i2c / #spi 的页面套件同一个纪律）。 */
  await evaluate(`(()=>{
    const K = 'serial-rtt-tools:v1';
    const st = JSON.parse(localStorage.getItem(K) || '{}');
    delete st['gen.dock'];
    localStorage.setItem(K, JSON.stringify(st));
    return true;})()`);
  await send('Page.reload', { ignoreCache: true });
  for (let i = 0; i < 40; i++){
    await sleep(400);
    try { if (await evaluate('!!window.__tools?.gen')) break; } catch {}
  }
  await evaluate(`(()=>{document.querySelector('#tabs .tab[data-tab="gen"]').click(); return true;})()`);
  await sleep(300);

  const shape = await evaluate(`(()=>{
    const ids = [...document.querySelectorAll('#g-dock-tabs button[data-dock]')].map(b => b.dataset.dock);
    const pages = [...document.querySelectorAll('#g-box-dock .dockpage')].map(p => p.dataset.dock);
    const on = [...document.querySelectorAll('#g-box-dock .dockpage.on')].map(p => p.dataset.dock);
    const left = [...document.querySelectorAll('#tab-gen .side fieldset')].filter(f => !f.hidden)
      .map(f => f.querySelector('legend').textContent.trim());
    const leftAll = [...document.querySelectorAll('#tab-gen .side fieldset')].map(f => f.dataset.dock);
    return { ids, pages, on, left, leftAll };})()`);
  ok(shape.ids.join(',') === 'files,params,bridge', `三个 tab：工程文件 / 调试参数 / 本地桥（${shape.ids.join(',')}）`);
  ok(shape.pages.join(',') === shape.ids.join(','), '每个 tab 都有对应的内容块（顺序一致）');
  ok(shape.on.length === 1 && shape.on[0] === 'files', '🚨 同时**只有一个**内容块可见，默认「工程文件」');
  ok(shape.left.join(',') === '工程,生成哪些,落地方式', `左栏默认只显示本 tab 的三张卡（${shape.left.join(' / ')}）`);
  ok(shape.leftAll.join(',') === 'files,files,params,files,bridge', '五张卡都带 data-dock 归属（不是靠删节点）');

  // 切 tab → 左栏跟着换
  const switched = await evaluate(`(async()=>{
    const click = d => document.querySelector('#g-dock-tabs button[data-dock="'+d+'"]').click();
    const snap = () => ({
      on: [...document.querySelectorAll('#g-box-dock .dockpage.on')].map(p => p.dataset.dock),
      btnOn: [...document.querySelectorAll('#g-dock-tabs button.on')].map(b => b.dataset.dock),
      left: [...document.querySelectorAll('#tab-gen .side fieldset')].filter(f => !f.hidden)
        .map(f => f.querySelector('legend').textContent.trim()),
      saved: (JSON.parse(localStorage.getItem('serial-rtt-tools:v1') || '{}') || {})['gen.dock'],
      dock: window.__tools.gen.dock,
      pill: document.getElementById('g-dock-pill').textContent,
    });
    const out = {};
    for (const d of ['params', 'bridge', 'files']){
      click(d); await new Promise(r => setTimeout(r, 150));
      out[d] = snap();
    }
    return out;})()`);
  ok(switched.params.on.join(',') === 'params' && switched.params.left.join(',') === '参数',
     `切「调试参数」→ 左边只剩「参数」卡（${switched.params.left.join('/')}）`);
  ok(switched.bridge.on.join(',') === 'bridge' && switched.bridge.left.length === 1 && /本地桥/.test(switched.bridge.left[0]),
     `切「本地桥」→ 左边只剩「本地桥」卡（${switched.bridge.left.join('/')}）`);
  ok(switched.files.left.length === 3 && switched.files.left.includes('工程'),
     `切回「工程文件」→ 三张卡都回来（${switched.files.left.join('/')}）`);
  ok(['params', 'bridge', 'files'].every(d => switched[d].saved === d && switched[d].dock === d),
     '每次切换都落 localStorage 且 view.dock 同步（刷新/切页回来还在）');
  ok(/项参数/.test(switched.params.pill) && /个文件/.test(switched.bridge.pill) && /个文件/.test(switched.files.pill),
     `tab 栏的胶囊跟着 tab 走（参数「${switched.params.pill}」/ 桥「${switched.bridge.pill}」/ 工程「${switched.files.pill}」）`);

  // 「调试参数」tab：参数一览表（长值看得全、点值复制）
  const params = await evaluate(`(async()=>{
    document.querySelector('#g-dock-tabs button[data-dock="params"]').click();
    await new Promise(r => setTimeout(r, 250));
    const rows = [...document.querySelectorAll('#g-params-body tr')];
    const heads = rows.filter(r => r.classList.contains('gparamhead')).map(r => r.textContent.trim());
    const find = k => {
      const tr = rows.find(r => r.querySelector('.gpk')?.textContent.trim() === k);
      return tr ? { v: tr.querySelector('.gpv code').textContent, title: tr.querySelector('.gpv code').title } : null;
    };
    return { count: rows.length, heads, project: find('项目名'), openocdRoot: find('OpenOCD 根'),
             device: find('器件'), projSame: find('项目名')?.v === document.getElementById('g-project').value,
             text: window.__tools.gen.paramsText().slice(0, 80) };})()`);
  ok(params.count > 20, `参数表列了 ${params.count} 行（含分组标题）`);
  /**
   * 参数表**故意列全五张卡的值**（包括当前 tab 藏起来的那些）—— 它的用途就是"一屏复核我到底填了什么"，
   * 分组标题写着每个值属于哪张卡（隐藏 ≠ 没填）。
   */
  ok(['工程', '参数', '本地桥'].every(k => params.heads.some(h => h.includes(k))),
     `按左栏卡片分组（五张卡都列）：${params.heads.join(' / ')}`);
  ok(params.projSame === true, `「项目名」的值与左栏输入框一致（${params.project?.v}）`);
  ok(/点击复制/.test(params.openocdRoot?.title || ''), '长路径那一行带"点击复制"提示（值不被侧栏宽度截断）');
  ok(/^\[工程\]/.test(params.text), `「复制全部」的文本以分组标题开头（${JSON.stringify(params.text.slice(0, 24))}）`);

  /**
   * 🚨 参数表要**真的能往下滚**（用户 2026-10 现场："下面显示不全，还没有滑条可以往下拖"）：
   * 表格比容器高时必须 `overflow-y:auto/scroll` 且 `scrollHeight > clientHeight`，
   * 并且容器高度不能超过它所在的 dockpage（否则会被 `.gendock{overflow:hidden}` 裁掉）。
   */
  const scroll = await evaluate(`(()=>{
    const w = document.getElementById('g-params-wrap');
    const page = w.closest('.dockpage');
    const cs = getComputedStyle(w);
    return { overflowY: cs.overflowY, scrollH: w.scrollHeight, clientH: w.clientHeight,
             wrapBottom: Math.round(w.getBoundingClientRect().bottom),
             pageBottom: Math.round(page.getBoundingClientRect().bottom) };})()`);
  ok(['auto', 'scroll'].includes(scroll.overflowY), `参数表的滚动区是 auto/scroll（实际 ${scroll.overflowY}）`);
  ok(scroll.scrollH > scroll.clientH, `内容比容器高（${scroll.scrollH} > ${scroll.clientH}）—— 所以必须有滚动条`);
  ok(scroll.wrapBottom <= scroll.pageBottom + 2,
     `滚动区没有溢出 dockpage（底 ${scroll.wrapBottom} ≤ ${scroll.pageBottom}）—— 溢出的部分会被裁掉且拖不到`);

  // 「本地桥」tab：7 个文件各有预览；关掉开关就只剩提示
  const kit = await evaluate(`(async()=>{
    document.querySelector('#g-dock-tabs button[data-dock="bridge"]').click();
    await new Promise(r => setTimeout(r, 200));
    const btns = [...document.querySelectorAll('#g-kit-files button')];
    const names = btns.map(b => b.textContent);
    btns.find(b => b.textContent === 'start-bridge.bat')?.click();
    await new Promise(r => setTimeout(r, 150));
    const prev = document.getElementById('g-kit-preview').textContent;
    const projPrevKeeps = document.getElementById('g-preview').textContent.length;   // 工程预览不该被清掉
    return { names, prevHead: prev.slice(0, 40), hasDoctor: /--doctor/.test(prev),
             projPrevKeeps, projBtns: [...document.querySelectorAll('#g-files button')].length };})()`);
  ok(kit.names.length === 7, `桥 tab 的文件栏就是那 7 个（${kit.names.join(' · ')}）`);
  ok(/^@echo off/.test(kit.prevHead) && kit.hasDoctor, `点 start-bridge.bat → 右侧预览的是它（${JSON.stringify(kit.prevHead)}）`);
  ok(kit.projBtns === 6 && kit.projPrevKeeps > 3000,
     `桥包不再挤进「工程文件」那条栏（那边 ${kit.projBtns} 个），工程预览也没被清掉`);

  const kitOff = await evaluate(`(async()=>{
    const e = document.getElementById('g-bk-on');
    e.checked = false; e.dispatchEvent(new Event('change', { bubbles: true }));
    await new Promise(r => setTimeout(r, 350));
    const btns = [...document.querySelectorAll('#g-kit-files button')].length;
    const prev = document.getElementById('g-kit-preview').textContent;
    e.checked = true; e.dispatchEvent(new Event('change', { bubbles: true }));
    await new Promise(r => setTimeout(r, 300));
    return { btns, prev, back: [...document.querySelectorAll('#g-kit-files button')].length };})()`);
  ok(kitOff.btns === 0 && /勾上/.test(kitOff.prev), '关掉「生成桥包」→ 桥 tab 给明确提示（不是空白）');
  ok(kitOff.back === 7, '再勾上 → 7 个文件回来');

  // 收尾：把 tab 拨回默认，别给下一次测试留个"停在本地桥"
  await evaluate(`(()=>{
    document.querySelector('#g-dock-tabs button[data-dock="files"]').click();
    const K = 'serial-rtt-tools:v1';
    const st = JSON.parse(localStorage.getItem(K) || '{}');
    st['gen.dock'] = 'files'; localStorage.setItem(K, JSON.stringify(st));
    return true;})()`);
  const err = await evaluate('window.__tools.summary().errors');
  ok(err.length === 0, '这一节跑完页面无未捕获错误', JSON.stringify(err));
}

console.log(`\n${fail ? 'FAIL' : 'OK'}  ${pass} 通过 / ${fail} 失败`);
ws.close();
process.exit(fail ? 1 : 0);
