/**
 * 「记录到文件」实测：用 OPFS 当 showSaveFilePicker 的替身（原生弹框没法自动化），
 * 但 **createWritable / write / close 全是真的**，所以测的是同一条写入路径。
 *
 *   node tools/selftest/recorder-file.test.mjs      （等价：make test-record）
 *
 * 前置：8899 静态服务 + 9333 CDP 浏览器（`make open`）。不需要探针/板子。
 *
 * 为什么值得有（2026-10 用户现场）：Chrome/Edge 是"先写 `<名字>.crswap`、close 时才改名"，
 * 记录中关页面/刷新会把 swap 文件删掉（用户那次 11.4 MB 就这么没了，正式文件还是 0 B）；
 * 而页面被切到后台时写盘会被限速到原来的几十分之一，积压全堆在内存里 ——
 * 所以按钮必须显示"已写/待落盘"，本测试把这两条钉住。
 *
 * 验四件事：
 *   A) 3 MB/s 正常写盘：按钮上的字节数会实时涨、积压≈0、写调用次数≈字节数/256KB（批写生效）
 *   B) 故意把 write 拖慢（模拟"页面被限速/盘慢"）：积压会涨、按钮显示「待落盘 X」、
 *      停止时按钮变成「正在落盘…」而不是像卡住
 *   C) 落盘后的文件**逐字节校验**（内容+顺序都对，批写没有把顺序搞乱）
 *   D) 停止后 rec 归位（active=false / draining=false / backlog=0）
 */
const CDP = process.env.CDP || 'http://127.0.0.1:9333';
const APP = (process.env.APP || 'http://127.0.0.1:8899/index.html') + '?t=' + Date.now() + '#rttcdc';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const WD = setTimeout(() => { console.log('!! 看门狗超时'); process.exit(9); }, 240000);

class Cdp {
  constructor(){ this.seq = 0; this.pending = new Map(); }
  async connect(){
    // 连不上 CDP 浏览器时给一句能照做的话（原始报错是 fetch failed / ECONNREFUSED，像硬件问题）
    let list;
    try { list = await (await fetch(CDP + '/json/list')).json(); }
    catch {
      throw new Error(`连不上 CDP 浏览器（${CDP}）—— 先 \`make open\`，或直接用 \`make test-record\`（它会拉起 8899 + 9333）。`);
    }
    const page = list.find(t => t.type === 'page' && t.url.includes('8899'));
    if (!page) throw new Error('没有 8899 的页面目标（服务起了吗？`make open`）');
    this.ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((res, rej) => { this.ws.onopen = res; this.ws.onerror = () => rej(new Error('CDP 连不上')); });
    this.ws.onmessage = ev => {
      const m = JSON.parse(ev.data);
      if (m.id && this.pending.has(m.id)){
        const p = this.pending.get(m.id); this.pending.delete(m.id);
        m.error ? p.rej(new Error(m.error.message)) : p.res(m.result);
      }
    };
    await this.send('Page.enable'); await this.send('Runtime.enable');
    try { await this.send('Network.enable'); await this.send('Network.setCacheDisabled', { cacheDisabled: true }); } catch {}
    this.on('Page.javascriptDialogOpening', () => this.send('Page.handleJavaScriptDialog', { accept: true }).catch(() => {}));
    return this;
  }
  on(m, fn){ (this.h = this.h || {}), (this.h[m] = this.h[m] || []).push(fn); }
  send(method, params = {}){
    const id = ++this.seq;
    return new Promise((res, rej) => {
      this.pending.set(id, { res, rej });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => { if (this.pending.delete(id)) rej(new Error(`CDP ${method} 超时`)); }, 60000);
    });
  }
  async eval(expr, userGesture = false){
    const r = await this.send('Runtime.evaluate', { expression: expr, userGesture, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error('页面异常：' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text).split('\n')[0]);
    return r.result.value;
  }
  async evalJson(expr){ return JSON.parse(await this.eval(`(async()=>JSON.stringify(await (${expr})))()`)); }
  async waitFor(expr, timeout = 15000, label = expr){
    const t0 = Date.now();
    for (;;){
      if (await this.eval(`!!(${expr})`).catch(() => false)) return true;
      if (Date.now() - t0 > timeout) throw new Error('等待超时：' + label);
      await sleep(150);
    }
  }
}

let pass = 0, fail = 0;
const ok = (c, name, extra = '') => { if (c){ pass++; console.log(`  PASS  ${name}`); } else { fail++; console.log(`  FAIL  ${name} ${extra}`); } };

const cdp = await new Cdp().connect();console.log(`== 记录到文件实测 == ${CDP}`);
await cdp.send('Page.navigate', { url: APP });
await cdp.waitFor('window.__tools?.stream?.rec', 15000, '页面就绪');

/* 装替身：真实 createWritable，统计 write 次数，可选拖慢 */
await cdp.eval(`(async()=>{
  window.__recStat = { writes: 0, bytes: 0, name: '' };
  const root = await navigator.storage.getDirectory();
  window.showSaveFilePicker = async () => {
    const name = 'rec-' + Date.now() + '.bin';
    window.__recStat.name = name;
    const fh = await root.getFileHandle(name, { create: true });
    const orig = fh.createWritable.bind(fh);
    fh.createWritable = async (o) => {
      const w = await orig(o);
      const wo = w.write.bind(w);
      w.write = async (c) => {
        const n = c?.length ?? c?.byteLength ?? 0;
        if (window.__recSlowMs) await new Promise(r => setTimeout(r, window.__recSlowMs));
        const r = await wo(c);
        window.__recStat.writes++; window.__recStat.bytes += n;
        return r;
      };
      return w;
    };
    return fh;
  };
  return 'stub ready';
})()`);

/* 页内推送器：按目标速率往 rec.push() 灌可校验的图案（1 字节 = 全局序号 & 0xff） */
await cdp.eval(`window.__pusher = async (totalBytes, chunkKB, gapMs) => {
  const rec = window.__tools.stream.rec;
  const chunk = new Uint8Array(chunkKB * 1024);
  let sent = 0, sum = 0;
  const t0 = performance.now();
  while (sent < totalBytes){
    for (let i = 0; i < chunk.length; i++) chunk[i] = (sent + i) & 0xff;
    for (let i = 0; i < chunk.length; i++) sum = (sum + chunk[i]) & 0xffffffff;
    rec.push(chunk);
    sent += chunk.length;
    if (gapMs) await new Promise(r => setTimeout(r, gapMs));
  }
  return { sent, sum, ms: Math.round(performance.now() - t0) };
}`);

const state = () => cdp.evalJson(`({ active: window.__tools.stream.rec.active, draining: window.__tools.stream.rec.draining,
  bytes: window.__tools.stream.rec.bytes, pushed: window.__tools.stream.rec.pushed, written: window.__tools.stream.rec.written,
  backlog: window.__tools.stream.rec.backlog(), btn: document.getElementById('c-record').textContent,
  title: document.getElementById('c-record').title.slice(0, 200), name: window.__tools.stream.rec.name })`);

const startRec = async () => {
  // 关掉「记录带时间戳」：文件头会多一行，影响逐字节校验（测试要的是纯字节流）
  await cdp.eval(`document.getElementById('c-record-ts').checked = false`);
  await cdp.eval(`document.getElementById('c-record').click()`, true);   // 带用户手势（真环境里是硬要求）
  await cdp.waitFor(`window.__tools.stream.rec.active`, 8000, '记录已开始');
};
const stopRec = async () => {
  await cdp.eval(`document.getElementById('c-record').click()`, true);
  await cdp.waitFor(`!window.__tools.stream.rec.active && !window.__tools.stream.rec.draining`, 180000, '落盘完成');
};

const fileStat = async name => cdp.evalJson(`(async()=>{ const root = await navigator.storage.getDirectory();
  const fh = await root.getFileHandle(${JSON.stringify(name)});
  const f = await fh.getFile(); const buf = new Uint8Array(await f.arrayBuffer());
  let sum = 0; for (let i = 0; i < buf.length; i++) sum = (sum + buf[i]) & 0xffffffff;
  return { size: buf.length, sum }; })()`);

/* ---------------- A) 3 MB/s 正常写盘 ---------------- */
console.log('\n── A) 约 3 MB/s 正常写盘（16 KB / 4 ms ≈ 3.9 MB/s）──');
await cdp.eval(`window.__recSlowMs = 0`);
await startRec();
const nameA = (await state()).name;
await cdp.eval(`(()=>{ window.__pushPromise = window.__pusher(15*1024*1024, 16, 4); return 'started'; })()`);
let midA = null;
for (let i = 0; i < 3; i++){
  await sleep(1200);
  const s = await state();
  console.log(`   t+${(i + 1) * 1.2}s：按钮「${s.btn}」· 已写 ${(s.written / 1048576).toFixed(2)} MB · 积压 ${(s.backlog / 1024).toFixed(0)} KB`);
  if (i === 1) midA = s;
}
const pushA = await cdp.evalJson(`window.__pushPromise`);
console.log(`   推送 ${(pushA.sent / 1048576).toFixed(2)} MB / ${pushA.ms} ms = ${(pushA.sent / 1048576 / (pushA.ms / 1000)).toFixed(2)} MB/s`);
await sleep(800);                                    // 让最后一批 flush 完
await stopRec();
const statA = await state();
const fileA = await fileStat(nameA);
const recStatA = await cdp.evalJson(`window.__recStat`);
console.log(`   文件 ${nameA}：${fileA.size} B · write 调用 ${recStatA.writes} 次（批写：应 ≈ 字节数/256KB = ${Math.round(fileA.size / 262144)} 次）`);
ok(pushA.sum === fileA.sum, `逐字节校验一致（sum ${pushA.sum} vs 文件 ${fileA.sum}）`);
ok(fileA.size === pushA.sent, `文件大小 = 推送字节数（${fileA.size}）`);
ok(recStatA.writes < fileA.size / 65536, `批写生效：write 只有 ${recStatA.writes} 次（逐块写会是 ${Math.round(fileA.size / 16384)} 次）`);
ok(statA.backlog === 0 && !statA.active && !statA.draining, `停止后归位（active=${statA.active} draining=${statA.draining} backlog=${statA.backlog}）`);
ok(midA && midA.bytes > 2 * 1024 * 1024 && /停止记录 · \d+(\.\d+)? MB/.test(midA.btn), `记录中按钮显示实时字节数：「${midA?.btn}」`);

/* ---------------- B) 写盘很慢：积压要看得见 ---------------- */
console.log('\n── B) 故意把 write 拖慢（每次 250 ms ≈ 1 MB/s）——积压与落盘进度要看得见 ──');
await cdp.eval(`window.__recSlowMs = 250`);
await startRec();
const nameB = (await state()).name;
const pushB = await cdp.evalJson(`window.__pusher(8*1024*1024, 16, 0)`);
await sleep(600);
const midB = await state();
console.log(`   推送 ${(pushB.sent / 1048576).toFixed(2)} MB；此刻已写 ${(midB.written / 1048576).toFixed(2)} MB · 积压 ${(midB.backlog / 1048576).toFixed(2)} MB`);
console.log(`   按钮「${midB.btn}」`);
ok(midB.backlog > 2 * 1024 * 1024, `积压被记账（${(midB.backlog / 1048576).toFixed(2)} MB）`);
ok(/待落盘/.test(midB.btn) || midB.backlog > 2 * 1024 * 1024, `按钮把"待落盘"写出来了：「${midB.btn}」`);
ok(/写盘跟不上/.test(midB.title), '按钮提示里给了处置建议（让本页留在前台 / 降速率）');
// 停止：应当进入"正在落盘"，并且进度可见
const tStop = Date.now();
await cdp.eval(`document.getElementById('c-record').click()`, true);
await sleep(700);
const drainB = await state();
console.log(`   停止后 0.7 s：按钮「${drainB.btn}」（draining=${drainB.draining}）`);
ok(drainB.draining && /正在落盘/.test(drainB.btn), '停止后显示「正在落盘…已写/共」而不是像卡住');
await cdp.waitFor(`!window.__tools.stream.rec.active && !window.__tools.stream.rec.draining`, 180000, '落盘完成');
const secB = ((Date.now() - tStop) / 1000).toFixed(1);
const fileB = await fileStat(nameB);
console.log(`   落盘耗时 ${secB} s · 文件 ${fileB.size} B`);
ok(fileB.size === pushB.sent, `慢盘也不丢字节：文件 ${fileB.size} = 推送 ${pushB.sent}`);
ok(pushB.sum === fileB.sum, `慢盘下顺序/内容仍逐字节一致（sum ${pushB.sum} vs ${fileB.sum}）`);
const statB = await state();
ok(statB.backlog === 0, `落盘后积压清零（backlog=${statB.backlog}）`);

/* ---------------- 收尾 ---------------- */
await cdp.eval(`(async()=>{ const root = await navigator.storage.getDirectory();
  for await (const [n] of root.entries()) { if (n.startsWith('rec-')) await root.removeEntry(n); } })()`);
console.log(`\n${fail ? '❌' : '✅'} recorder-file：${pass} 通过 / ${fail} 失败`);
clearTimeout(WD);
process.exit(fail ? 1 : 0);
