/**
 * 烧录耗时体检（真机）：把「烧录很慢」变成一行行数字 —— 慢在哪一步、是不是探针的锅。
 *
 *   node tools/selftest/flash-timing.mjs                     # 跑一轮，打印时间线
 *   node tools/selftest/flash-timing.mjs --runs=2            # 连跑两轮
 *   node tools/selftest/flash-timing.mjs --clamp             # 模拟"浏览器给后台页限速定时器"
 *   node tools/selftest/flash-timing.mjs --minimize-after=1  # 第 2 轮前把窗口最小化（真节流）
 *   node tools/selftest/flash-timing.mjs --ghost             # 先开一个同源页签再关掉（跨页签协调）
 *
 * 前置（同其他真机脚本）：
 *   python -m http.server 8899 --bind 127.0.0.1
 *   pwsh -File tools/selftest/launch-browser.ps1 -Url http://127.0.0.1:8899/index.html#flash
 *
 * 背景（2026-10 真机定因，用户报「烧录非常慢，每一步都要好几秒钟」）：
 *   · 页面可见时 3.3 KB 固件 **1.4 s** 烧完，其中 USB 往返总共只花 0.25 s；
 *   · 把窗口最小化（页面不可见）后同样的固件要 **12~13 s**，把每个 <1 s 的等待都按 1 s
 *     执行则要 **13.9 s** —— 全部来自浏览器对后台页的定时器限速（短延时被钳到 ≥1 s），
 *     而烧录流程里有几十处 2~60 ms 的轮询间隔。修法见 app/core/pace.js（改用 MessageChannel 让路）。
 *   所以这个脚本把「页面可见性 / 定时器被钳次数 / 各步耗时 / USB 往返总量」一起打出来，
 *   免得下次再把"等待方式慢"误判成"探针慢"。
 */
const CDP = process.env.CDP || 'http://127.0.0.1:9333';
/** 每次跑都带一个变化的查询参数：只改 hash 的同 URL 导航不会重新加载文档，插桩会被旧版挡住 */
const APP = (process.env.APP || 'http://127.0.0.1:8899/index.html') + '?t=' + Date.now() + '#flash';
const FW = process.env.FW || '/tools/target-firmware/stm32f103_scope/build/fw.elf';
const arg = k => process.argv.find(a => a.startsWith(`--${k}=`));
const GHOST = process.argv.includes('--ghost');
const CLAMP = process.argv.includes('--clamp');
const RUNS = Number(arg('runs')?.split('=')[1]) || 1;
const HIDE_AFTER = Number(arg('hide-after')?.split('=')[1]) || 0;
const MIN_AFTER = Number(arg('minimize-after')?.split('=')[1]) || 0;
const MAX_S = Number(arg('max')?.split('=')[1]) || 8;          // 超过这么多秒就判"偏慢"（退出码 1）
const sleep = ms => new Promise(r => setTimeout(r, ms));

// 硬看门狗：整脚本最多 5 分钟，绝不挂住（本机纪律：不许出现无超时的等待）
const WD = setTimeout(() => { console.log('!! 看门狗超时，脚本退出'); process.exit(9); }, 300000);

class Cdp {
  constructor(){ this.seq = 0; this.pending = new Map(); this.handlers = new Map(); this.prompts = []; }
  _open(url, onMsg){
    const ws = new WebSocket(url);
    return new Promise((res, rej) => {
      ws.onopen = () => res(ws);
      ws.onerror = () => rej(new Error('CDP 连不上：' + url));
      ws.onmessage = ev => onMsg(ws, JSON.parse(ev.data));
    });
  }
  _dispatch(ws, m){
    if (m.id && this.pending.has(m.id)){
      const p = this.pending.get(m.id); this.pending.delete(m.id);
      m.error ? p.rej(new Error(m.error.message)) : p.res(m.result);
      return;
    }
    if (!m.method) return;
    if (m.method === 'DeviceAccess.deviceRequestPrompted') this.prompts.push(m.params);
    const hs = this.handlers.get(m.method);
    if (hs) for (const h of hs) h(m.params);
  }
  async connect(){
    // 连不上 CDP 浏览器时别只抛 fetch failed —— 那句话看着像硬件问题（见 Makefile 的 page-prep）
    let ver;
    try { ver = await (await fetch(CDP + '/json/version')).json(); }
    catch {
      throw new Error(`连不上 CDP 浏览器（${CDP}）—— 真页面测试需要它。先 \`make open\`，` +
        '或者直接用 `make flash-timing`（它会把 8899 服务与 9333 浏览器拉起来）。');
    }
    this.browserWs = await this._open(ver.webSocketDebuggerUrl, (ws, m) => this._dispatch(ws, m));
    const list = await (await fetch(CDP + '/json/list')).json();
    const page = list.find(t => t.type === 'page' && t.url.includes('8899'));
    if (!page) throw new Error('没有 8899 的页面目标：' + JSON.stringify(list.map(t => t.url)));
    this.ws = await this._open(page.webSocketDebuggerUrl, (ws, m) => this._dispatch(ws, m));
    await this.send('Page.enable');
    await this.send('Runtime.enable');
    // 关缓存：改完代码再跑，必须确保页面加载的是磁盘上的新模块
    try { await this.send('Network.enable'); await this.send('Network.setCacheDisabled', { cacheDisabled: true }); } catch {}
    this.on('Page.javascriptDialogOpening', () => { this.send('Page.handleJavaScriptDialog', { accept: true }).catch(() => {}); });
    try { await this.sendBrowser('DeviceAccess.enable'); }
    catch { try { await this.send('DeviceAccess.enable'); } catch {} }
    return this;
  }
  send(method, params = {}){ return this._call(this.ws, method, params); }
  sendBrowser(method, params = {}){ return this._call(this.browserWs, method, params); }
  _call(ws, method, params){
    const id = ++this.seq;
    return new Promise((res, rej) => {
      this.pending.set(id, { res, rej });
      ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => { if (this.pending.delete(id)) rej(new Error(`CDP ${method} 超时`)); }, 60000);
    });
  }
  on(m, fn){ if (!this.handlers.has(m)) this.handlers.set(m, []); this.handlers.get(m).push(fn); }
  async eval(expr, userGesture = false){
    const r = await this.send('Runtime.evaluate', { expression: expr, userGesture, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error('页面异常：' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
    return r.result.value;
  }
  async evalJson(expr){ return JSON.parse(await this.eval(`(async()=>JSON.stringify(await (${expr})))()`)); }
  async waitFor(expr, timeout = 20000, label = expr){
    const t0 = Date.now();
    for (;;){
      let v = false;
      try { v = await this.eval(`!!(${expr})`); } catch {}
      if (v) return true;
      if (Date.now() - t0 > timeout) throw new Error('等待超时：' + label);
      await sleep(150);
    }
  }
  async navigate(url){ this.prompts.length = 0; await this.send('Page.navigate', { url }); await sleep(400); }
  async pickDevice(match, timeout = 20000){
    const t0 = Date.now();
    while (!this.prompts.length){
      if (Date.now() - t0 > timeout) throw new Error('等设备选择框超时');
      await sleep(100);
    }
    const p = this.prompts.shift();
    const dev = p.devices.find(d => match.test(d.name)) || p.devices[0];
    await this.sendBrowser('DeviceAccess.selectPrompt', { id: p.id, deviceId: dev.id });
    return dev;
  }
}

/* 页面内插桩：FlashView 顶层方法 + 探针层每次调用 + 定时器被钳情况 */
const INSTRUMENT = `(() => {
  if (window.__trace) return 'already';
  window.__trace = []; window.__stats = {}; window.__logline = [];
  const t0page = performance.now();
  const rec = (m, ms, err, kind) => {
    const st = (window.__stats[m] ||= { calls: 0, total: 0, max: 0, err: 0, kind });
    st.calls++; st.total += ms; st.max = Math.max(st.max, ms); if (err) st.err++;
  };
  const wrap = (obj, m, kind, keepAll) => {
    const orig = obj[m]; if (typeof orig !== 'function') return;
    obj[m] = function(...a){
      const t = performance.now();
      const fin = (err) => {
        const ms = performance.now() - t;
        rec(m, ms, err, kind);
        if (keepAll || ms > 40 || err) window.__trace.push({ m, ms: +ms.toFixed(1), t: +(t - t0page).toFixed(0),
          err: err ? String(err.message || err).slice(0, 140) : '' });
      };
      let r; try { r = orig.apply(this, a); } catch (e){ fin(e); throw e; }
      if (r && typeof r.then === 'function') return r.then(v => { fin(); return v; }, e => { fin(e); throw e; });
      fin(); return r;
    };
  };
  const f = window.__tools.flash;
  const P = Object.getPrototypeOf(f);
  for (const m of ['flash', '_clearProbeUsers', '_flashWebusb', '_readStable', '_bootCheck']) wrap(P, m, 'view', true);
  /**
   * 定时器节流探针：记下每个 setTimeout 的"要多久/实际多久"。
   * 页面不可见时浏览器会把 <1 s 的延时钳到 ≥1 s —— 这正是"每一步都要好几秒"的来源。
   * __clampMode 把这件事做成确定性模拟（等价于"每个短等待都按 1 s 执行"）。
   */
  window.__timers = { calls: 0, overshoot: 0, worst: 0, clamped: 0 };
  const _st = window.setTimeout.bind(window);
  window.setTimeout = function(fn, ms, ...rest){
    const want = ms | 0, t0 = performance.now();
    const real = (window.__clampMode && want > 0 && want < 1000) ? 1000 : ms;
    if (typeof fn === 'function' && want > 0 && want <= 500){
      window.__timers.calls++;
      const wrapped = (...a) => {
        const act = performance.now() - t0, over = act - want;
        window.__timers.overshoot += Math.max(0, over);
        if (over > want * 2 + 50) window.__timers.clamped++;
        if (act > window.__timers.worst) window.__timers.worst = act;
        return fn(...a);
      };
      return _st(wrapped, real, ...rest);
    }
    return _st(fn, real, ...rest);
  };
  const logOrig = P._log;
  P._log = function(s){ window.__logline.push({ t: +(performance.now() - t0page).toFixed(0), s: String(s).slice(0, 220) }); return logOrig.apply(this, arguments); };
  window.__meterTimer = setInterval(() => {
    const p = f.probe;
    if (p && !p.__metered){
      p.__metered = true;
      for (const m of ['_setup', '_claim', '_targetInit', '_negotiateClock', '_detectFraming', 'halt', 'run', 'isHalted',
                       'readMem', 'writeMem', 'regRead', 'regWrite', 'maskInterrupts', 'flushWrites', 'sysReset', 'reset',
                       'disconnect', '_transfer', '_transferBlock', '_setTAR', '_flushPosted'])
        wrap(p, m, 'probe');
      window.__probeName = p.name;
    }
  }, 15);
  return 'ok';
})()`;

const cdp = await new Cdp().connect();
console.log(`== 烧录耗时体检 ==  CDP ${CDP}  固件 ${FW}${GHOST ? '  [ghost]' : ''}${CLAMP ? '  [clamp 模拟]' : ''}`);

await cdp.navigate(APP);
await cdp.waitFor('window.__tools', 15000, '页面加载');
await cdp.eval(INSTRUMENT);
if (CLAMP){
  await cdp.eval(`window.__clampMode = true`);
  console.log('   [模拟] 所有 <1s 的短等待按 1s 执行（= 后台/被遮挡时浏览器的定时器节流）');
}
await cdp.eval(`document.getElementById('f-chip').value='stm32f103';document.getElementById('f-chip').dispatchEvent(new Event('change'))`);

// ghost：先开一个同源页签（互相 hello），再关掉 —— 复刻"曾经开过第二个页签"
if (GHOST){
  const t = await cdp.sendBrowser('Target.createTarget', { url: 'http://127.0.0.1:8899/index.html#flash' });
  await sleep(2500);
  const peers = await cdp.evalJson(`Array.from(window.__tools.probeBus.peers)`);
  console.log(`   第二个页签已注册：${JSON.stringify(peers)}；现在关掉它`);
  await cdp.sendBrowser('Target.closeTarget', { targetId: t.targetId });
  await sleep(1200);
}

async function loadFirmware(){
  const info = await cdp.evalJson(`(async () => {
    const r = await fetch(${JSON.stringify(FW)}, { cache: 'reload' });
    if (!r.ok) return { err: 'HTTP ' + r.status };
    const b = await r.arrayBuffer();
    await window.__tools.flash._onFile(new File([b], 'fw.elf'));
    return { name: window.__tools.flash.file?.name, size: window.__tools.flash.file?.size };
  })()`);
  if (info.err) throw new Error('取固件失败：' + info.err);
  return info;
}

async function oneFlash(tag){
  await cdp.eval(`window.__trace.length = 0; window.__logline.length = 0;
                  window.__timers.calls = 0; window.__timers.overshoot = 0; window.__timers.clamped = 0; window.__timers.worst = 0;
                  for (const k of Object.keys(window.__stats)) delete window.__stats[k];`);
  const fw = await loadFirmware();
  console.log(`\n---- ${tag}：${fw.name}（${fw.size} B 文件，3.3 KB 数据）----`);
  const t0 = Date.now();
  await cdp.eval(`document.getElementById('f-flash').click()`, true);      // 必须带用户手势
  for (let i = 0; i < 40 && cdp.prompts.length === 0; i++){
    if (await cdp.eval(`!!window.__tools.flash.busy`)) break;
    await sleep(150);
  }
  if (cdp.prompts.length){
    const dev = await cdp.pickDevice(/MicroLink|DAP|CMSIS/i);
    console.log(`   设备选择框选中：${dev.name}`);
  }
  for (let i = 0; i < 900; i++){
    const st = await cdp.evalJson(`({busy: !!window.__tools.flash.busy, res: document.getElementById('f-result').textContent,
                                     status: document.getElementById('f-status')?.textContent || ''})`);
    if (!st.busy && (st.res.includes('✅') || st.res.includes('❌'))) break;
    if (i === 899) console.log('   !! 等烧录结束超时');
    await sleep(200);
  }
  const wall = Date.now() - t0;
  const out = await cdp.evalJson(`({ log: window.__logline, trace: window.__trace, timers: window.__timers,
      vis: document.visibilityState,
      stats: Object.fromEntries(Object.entries(window.__stats).map(([k, v]) => [k, { calls: v.calls, total: +v.total.toFixed(0), max: +v.max.toFixed(0), err: v.err }])),
      result: document.getElementById('f-result').textContent })`);
  console.log(`   总墙钟：${(wall / 1000).toFixed(1)}s   结果：${out.result}   页面可见性=${out.vis}`);
  console.log(`   定时器：${out.timers.calls} 个短等待，被钳 ${out.timers.clamped} 个（多花 ${(out.timers.overshoot / 1000).toFixed(1)}s）`);
  console.log('   --- 页面日志（相对点按钮）---');
  let prev = 0;
  for (const l of out.log){ console.log(`   +${String(l.t).padStart(6)}ms (+${String(l.t - prev).padStart(5)}ms)  ${l.s}`); prev = l.t; }
  const usb = ['_transfer', '_transferBlock', '_setTAR', 'readMem', 'writeMem', 'regRead', 'regWrite', 'isHalted', 'halt', 'run', '_targetInit', 'sysReset']
    .filter(k => out.stats[k]).map(k => [k, out.stats[k]]).sort((a, b) => b[1].total - a[1].total);
  // 只把两个**底层入口**相加：_transfer / _transferBlock 互不嵌套，其余（readMem 等）会重复计
  const usbTotal = (out.stats._transfer?.total || 0) + (out.stats._transferBlock?.total || 0);
  console.log('   --- 探针层最花时间的调用 ---');
  for (const [k, v] of usb.slice(0, 6)) console.log(`   ${k.padEnd(15)} ${String(v.calls).padStart(5)} 次 共 ${String(v.total).padStart(6)}ms  最慢 ${String(v.max).padStart(5)}ms`);
  console.log(`   （底层 USB 往返 _transfer+_transferBlock 合计 ${usbTotal} ms，占墙钟 ${(usbTotal / wall * 100).toFixed(0)}% ——`);
  console.log('     这个占比高说明"时间花在链路上"（正常：3.3 KB 固件要一千多次往返）；');
  console.log('     占比低而总时长很大，说明时间花在**等待**上：先看下面的"定时器被钳"个数。）');
  const view = Object.entries(out.stats).filter(([k]) => /^_clearProbeUsers$|^_flashWebusb$|^_readStable$|^_bootCheck$/.test(k));
  for (const [k, v] of view) console.log(`   ${k.padEnd(15)} ${String(v.calls).padStart(5)} 次 共 ${String(v.total).padStart(6)}ms`);
  const slow = out.trace.filter(x => x.m !== '_log' && x.ms >= 40).slice(0, 8);
  if (slow.length){
    console.log('   --- 单次 ≥40ms 的调用 ---');
    for (const t of slow) console.log(`   +${String(t.t).padStart(6)}ms  ${t.m.padEnd(15)} ${String(t.ms).padStart(7)}ms ${t.err || ''}`);
  }
  if (wall / 1000 > MAX_S){
    console.log(`   ⚠ 这一轮 ${(wall / 1000).toFixed(1)}s，超过 ${MAX_S}s 的线 —— 看上面「定时器被钳 ${out.timers.clamped} 个」：`);
    console.log('     被钳说明是浏览器在给后台页限速定时器（页面不可见/窗口被盖住），不是探针慢；');
    console.log('     若一个都没被钳、而 USB 往返占了大头，才该去查探针链路（换线/换口/别被 OpenOCD 抢）。');
  }
  return { wall, out };
}

const results = [];
for (let i = 1; i <= RUNS; i++){
  if (HIDE_AFTER && i === HIDE_AFTER + 1){
    const t = await cdp.sendBrowser('Target.createTarget', { url: 'about:blank' });
    await cdp.sendBrowser('Target.activateTarget', { targetId: t.targetId });
    await sleep(1500);
    console.log(`\n>>> 已切到别的页签（visibilityState=${await cdp.evalJson(`document.visibilityState`)}），下一轮模拟"用户切走"`);
  }
  if (MIN_AFTER && i === MIN_AFTER + 1){
    const list = await (await fetch(CDP + '/json/list')).json();
    const page = list.find(t => t.type === 'page' && t.url.includes('8899'));
    const { windowId } = await cdp.sendBrowser('Browser.getWindowForTarget', { targetId: page.id });
    await cdp.sendBrowser('Browser.setWindowBounds', { windowId, bounds: { windowState: 'minimized' } });
    await sleep(2500);
    const v = await cdp.evalJson(`({v: document.visibilityState, h: document.hidden})`);
    console.log(`\n>>> 窗口已最小化（hidden=${v.h}）—— 这一轮是"浏览器被盖住/最小化"的真实情形`);
  }
  results.push(await oneFlash(GHOST ? `ghost 烧录 #${i}` : `烧录 #${i}`));
}

console.log('\n==== 汇总 ====');
let slowCount = 0;
for (const [i, r] of results.entries()){
  const co = (r.out.log.find(l => l.s.includes('跨页签协调')) || {}).s || '(无协调日志)';
  if (r.wall / 1000 > MAX_S) slowCount++;
  console.log(`#${i + 1} ${(r.wall / 1000).toFixed(1)}s · 定时器被钳 ${r.out.timers.clamped} 个 · ${co}`);
}
const errs = await cdp.evalJson(`window.__tools.errors`);
if (errs.length) console.log('页面错误：' + JSON.stringify(errs).slice(0, 400));
clearTimeout(WD);
console.log(slowCount ? `\n❌ ${slowCount}/${results.length} 轮偏慢（> ${MAX_S}s）` : `\n✅ 全部在 ${MAX_S}s 以内`);
process.exit(slowCount ? 1 : 0);
