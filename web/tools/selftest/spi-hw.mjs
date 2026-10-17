/**
 * 「SPI/QSPI 屏」真机验收（CDP 驱动真页面 + 真探针/真屏）：
 *   node tools/selftest/spi-hw.mjs                      # 默认按 AXS15352（档 1）验收
 *   node tools/selftest/spi-hw.mjs --panel=st77916      # 换成 ST77916（档 2）
 *   node tools/selftest/spi-hw.mjs --sclk=20,40,60,75   # 逐档 SCLK 刷一遍对比吞吐
 *   node tools/selftest/spi-hw.mjs --loop               # 先跑回环自检（需要 J3[19]↔J3[21] 跳线）
 *
 * 前置：静态服务 8899 + CDP 浏览器 9333（`make page-prep` 会自动起），探针与屏已接好。
 * 设备授权：已授权就直连；没授权会自动应答浏览器的设备选择框（Runtime.evaluate 带 userGesture）。
 *
 * 这个脚本**只报客观数字**（frames_ok/err、耗时、吞吐、实际 SCLK），"屏上有没有出现彩条"要人看 ——
 * 所以跑完会明确提示去看屏。
 */
const CDP = process.env.CDP || 'http://127.0.0.1:9333';
const APP = process.env.APP || 'http://127.0.0.1:8899/index.html';

const argv = process.argv.slice(2);
const arg = (name, dflt) => {
  const m = argv.find(a => a.startsWith(`--${name}=`));
  return m ? m.split('=')[1] : dflt;
};
const PANEL = arg('panel', 'axs15352');
const SCLKS = arg('sclk', '40').split(',').map(Number).filter(n => n > 0);
const DO_LOOP = argv.includes('--loop');

const PANELS = {
  axs15352: { label: '天马 2P01 / AXS15352（档 1，4 线 SPI + DC）', profile: 1, geom: 'axs15352', sclk: 40, preset: 'axs15352' },
  st77916: { label: 'ST77916（档 2，QSPI）', profile: 2, geom: 'st77916', sclk: 40, preset: 'st77916' },
};
const PANEL_CFG = PANELS[PANEL];
if (!PANEL_CFG) throw new Error(`未知 --panel=${PANEL}（可选 ${Object.keys(PANELS).join(' / ')}）`);

let pass = 0, fail = 0;
const ok = (c, name, extra = '') => { if (c){ pass++; console.log(`  PASS  ${name}`); } else { fail++; console.log(`  FAIL  ${name} ${extra}`); } };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const step = s => console.log(`\n== ${s} ==`);

/* ---------------- 极简 CDP 客户端（设备选择框要浏览器级会话） ---------------- */
class Cdp {
  constructor(){ this.seq = 0; this.pending = new Map(); this.handlers = new Map(); this.prompts = []; }
  _open(url, onMsg){
    const ws = new WebSocket(url);
    return new Promise((res, rej) => {
      ws.onopen = () => res(ws);
      ws.onerror = () => rej(new Error('CDP WebSocket 连不上：' + url));
      ws.onmessage = e => onMsg(ws, JSON.parse(e.data));
    });
  }
  _dispatch(m){
    if (m.id && this.pending.has(m.id)){
      const p = this.pending.get(m.id); this.pending.delete(m.id);
      m.error ? p.rej(new Error(m.error.message)) : p.res(m.result);
      return;
    }
    if (m.method === 'DeviceAccess.deviceRequestPrompted') this.prompts.push(m.params);
    const hs = this.handlers.get(m.method);
    if (hs) for (const h of hs) h(m.params);
  }
  async connect(){
    const ver = await (await fetch(CDP + '/json/version')).json();
    this.browserWs = await this._open(ver.webSocketDebuggerUrl, (ws, m) => this._dispatch(m));
    const list = await (await fetch(CDP + '/json/list')).json();
    const page = list.find(t => t.type === 'page');
    if (!page) throw new Error('没有可用的页面目标');
    this.ws = await this._open(page.webSocketDebuggerUrl, (ws, m) => this._dispatch(m));
    await this.send('Page.enable');
    await this.send('Runtime.enable');
    // 🚨 关缓存：python http.server 不带 Cache-Control，改完代码可能还在跑旧模块
    try { await this.send('Network.enable'); await this.send('Network.setCacheDisabled', { cacheDisabled: true }); } catch {}
    for (const fn of [() => this.sendBrowser('DeviceAccess.enable'), () => this.send('DeviceAccess.enable')]){
      try { await fn(); this.deviceAccessOn = true; break; } catch {}
    }
    if (!this.deviceAccessOn) console.log('  [warn] DeviceAccess 不可用（已授权设备仍可直连）');
  }
  send(method, params = {}){ return this._call(this.ws, method, params); }
  sendBrowser(method, params = {}){ return this._call(this.browserWs, method, params); }
  _call(ws, method, params){
    const id = ++this.seq;
    return new Promise((res, rej) => {
      this.pending.set(id, { res, rej });
      ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => { if (this.pending.delete(id)) rej(new Error(`CDP ${method} 超时`)); }, 120000);
    });
  }
  async eval(expr, userGesture = false){
    const r = await this.send('Runtime.evaluate', { expression: `(async()=>{ ${expr} })()`, userGesture, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error('页面异常：' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
    return r.result.value;
  }
  async pickDevice(match, timeout = 20000){
    const t0 = Date.now();
    while (!this.prompts.length){
      if (Date.now() - t0 > timeout) throw new Error('等设备选择框超时');
      await sleep(100);
    }
    const p = this.prompts.shift();
    const dev = p.devices.find(d => match.test(d.name)) || p.devices[0];
    if (!dev) throw new Error('选择框里没有设备：' + JSON.stringify(p.devices));
    await this.sendBrowser('DeviceAccess.selectPrompt', { id: p.id, deviceId: dev.id });
    return dev;
  }
  /** 等"授权这件事"落地：可能弹框（自动选），也可能已授权直接连上 */
  async settle(match, readyExpr, timeout = 20000){
    const t0 = Date.now();
    for (;;){
      if (this.prompts.length) return await this.pickDevice(match);
      let ready = false;
      try { ready = await this.eval(`return !!(${readyExpr});`); } catch {}
      if (ready) return null;
      if (Date.now() - t0 > timeout) throw new Error(`既没弹选择框也没就绪：${readyExpr}`);
      await sleep(200);
    }
  }
  async waitIdle(timeout = 180000){
    const t0 = Date.now();
    for (;;){
      if (!(await this.eval('return window.__tools.spiSession.busy;'))) return true;
      if (Date.now() - t0 > timeout) return false;
      await sleep(200);
    }
  }
}

const cdp = new Cdp();
await cdp.connect();
await cdp.send('Page.navigate', { url: APP + '?demo=serial&t=' + Date.now() });
for (let i = 0; i < 60; i++){
  await sleep(400);
  try { if (await cdp.eval('return !!window.__tools?.spiSession;')) break; } catch {}
}
console.log(`真机验收：${PANEL_CFG.label}　SCLK ${SCLKS.join('/')} MHz${DO_LOOP ? '　（含回环自检）' : ''}`);

// ==================================================================== 1 连接
step('1. 连接探针（HID）与数据端点（WebUSB）');
{
  await cdp.eval(`document.querySelector('#tabs .tab[data-tab="spi"]').click();`);
  await sleep(300);
  // 先走"已授权"这条路（浏览器记得就不用弹框）；没有授权才弹框 + 自动应答
  await cdp.eval(`document.getElementById('sp-reconnect').click();`);
  await sleep(900);
  let hid = await cdp.eval(`return { connected: window.__tools.spiSession.connected, label: window.__tools.spiSession.hid?.label || '' };`);
  if (!hid.connected){
    console.log('  （没有已授权的 HID，触发选择框…）');
    await cdp.eval(`document.getElementById('sp-connect').click();`, true);
    await cdp.settle(/akaLinkPro|CMSIS|HID/i, 'window.__tools.spiSession.connected');
    await sleep(600);
    hid = await cdp.eval(`return { connected: window.__tools.spiSession.connected, label: window.__tools.spiSession.hid?.label || '' };`);
  }
  ok(hid.connected, `HID 已连接（${hid.label || 'akaLinkPro'}）`);

  await cdp.eval(`document.getElementById('sp-usb').click();`);      // 智能：已授权直连、否则弹框
  await cdp.settle(/akaLinkPro|CMSIS|WinUSB|Composite/i, 'window.__tools.spiSession.dataReady');
  await sleep(600);
  const usb = await cdp.eval(`return { ready: window.__tools.spiSession.dataReady, label: window.__tools.spiSession.transport?.label || '', iface: window.__tools.spiSession.transport?.iface };`);
  ok(usb.ready, `数据端点已连接（接口 ${usb.iface}）：${usb.label}`);
}

// ==================================================================== 2 配置
step('2. 读配置 + 套用该屏的推荐值');
{
  await cdp.eval(`document.getElementById('sp-get').click();`);
  await sleep(400);
  const cfg0 = await cdp.eval('return window.__tools.spiSession.cfg;');
  console.log(`  固件默认：SCLK=${cfg0.sclkHz}（0=板级 20M）mode=${cfg0.mode} cs_policy=${cfg0.csPolicy} 阈值=${cfg0.txDmaThreshold}`);
  console.log(`  辅助脚：DC=${cfg0.padDc} RST=${cfg0.padRst} CS_AUX=${cfg0.padCsAux} BL=${cfg0.padBl} TE=${cfg0.padTe} active_low=0x${cfg0.padActiveLow.toString(16)}`);
  ok(cfg0.maxFrameBytes === 504, `固件报告的单帧上限 = ${cfg0.maxFrameBytes}（协议要求 504）`);

  await cdp.eval(`document.querySelector('#tabs .tab[data-tab="panel"]').click();`);
  await sleep(300);
  await cdp.eval(`
    document.getElementById('pn-preset').value = '${PANEL_CFG.preset}';
    document.getElementById('pn-preset').dispatchEvent(new Event('change'));
    document.getElementById('pn-preset-apply').click();`);
  await sleep(1500);
  const st = await cdp.eval(`return { cfg: window.__tools.spiSession.cfg, prof: window.__tools.spiSession.profile, geom: document.getElementById('pn-geom').value };`);
  ok(st.prof?.profile === PANEL_CFG.profile, `面板档 = ${st.prof?.profile}（期望 ${PANEL_CFG.profile}）`);
  ok(st.geom === PANEL_CFG.geom, `屏幕几何 = ${st.geom}`);
  console.log(`  套用后：SCLK=${st.cfg.sclkHz} DC=${st.cfg.padDc} RST=${st.cfg.padRst} BL=${st.cfg.padBl}`);
}

// ==================================================================== 3 回环
if (DO_LOOP){
  step('3. 回环自检（J3[19]↔J3[21] 跳线）');
  await cdp.eval(`document.querySelector('#tabs .tab[data-tab="spi"]').click();`);
  await sleep(200);
  await cdp.eval(`document.getElementById('sp-enable').click();`);
  await sleep(600);
  await cdp.eval(`
    document.getElementById('sp-lb-lens').value = '1,2,32,99,100,101,256,492';
    document.getElementById('sp-lb-lines').value = '1';
    document.getElementById('sp-lb-dma').checked = true;
    document.getElementById('sp-lb-run').click();`);
  await cdp.waitIdle(120000);
  await sleep(500);
  const lb = await cdp.eval(`return { sum: document.getElementById('sp-lb-sum').textContent, rows: document.querySelectorAll('#sp-lb-body tr').length };`);
  ok(/^16\/16/.test(lb.sum), `回环 1 线 × 8 长度 × 2 路径：${lb.sum}`, `${lb.rows} 行`);
  await cdp.eval(`document.querySelector('#tabs .tab[data-tab="panel"]').click();`);
  await sleep(200);
}

// ==================================================================== 4 面板初始化
step('4. 面板初始化：内置表 + 自动补 MADCTL/COLMOD → 下发');
{
  const init = await cdp.eval(`
    document.getElementById('pn-code-preset').value = '${PANEL === 'axs15352' ? 'axs15352' : 'st77916'}';
    document.getElementById('pn-code-load').click();
    await new Promise(r => setTimeout(r, 600));
    return { rows: window.__tools.panel.summary().rows,
             table: document.querySelectorAll('#pn-code-body tr').length,
             sum: document.getElementById('pn-code-sum').textContent };`);
  ok(init.rows > 0, `解析出 ${init.rows} 条`, init.sum);
  // 面板不再替用户的表补 MADCTL/COLMOD（2026-09-30 去掉）：表格行数应等于解析条数
  ok(init.table === init.rows, `表格行数 == 解析条数（${init.table}/${init.rows}，不再自动补前缀）`);

  await cdp.eval(`document.getElementById('pn-enable').click();`);
  await sleep(700);
  const en = await cdp.eval(`return { enabled: window.__tools.spiSession.enabled, sclk: window.__tools.spiSession.counters.actualSclkHz };`);
  ok(en.enabled, `桥已使能，实际 SCLK = ${(en.sclk / 1e6).toFixed(1)} MHz`);

  const t0 = Date.now();
  await cdp.eval(`document.getElementById('pn-code-play').click();`);
  await cdp.waitIdle(120000);
  await sleep(500);
  const play = await cdp.eval(`
    const p = window.__tools.spiSession.mockProbe; void p;
    return { prog: document.getElementById('pn-code-prog').textContent,
             counters: window.__tools.spiSession.counters,
             log: document.getElementById('pn-log').textContent.split('\\n').filter(l => /解析完成|完成/.test(l)).slice(-2) };`);
  ok(play.counters.framesErr === 0, `整表下发零错误（frames_ok=${play.counters.framesOk} err=${play.counters.framesErr}）`, play.prog);
  console.log(`  ${play.log.join(' | ')}`);
  console.log(`  （整表含 0x11 + 100ms 延时，耗时 ${Date.now() - t0} ms 属正常）`);
}

// ==================================================================== 5 屏参数
step('5. 屏参数（窗口 + 切片 + 字节序）');
{
  const info = await cdp.eval(`
    const btns = [...document.querySelectorAll('#pn-patterns button')];
    btns.find(b => b.textContent === '色条 8').click();
    await new Promise(r => setTimeout(r, 400));
    return { sum: document.getElementById('pn-img-sum').textContent,
             order: document.getElementById('pn-byteorder').value,
             swap: document.getElementById('pn-swap').checked,
             geom: document.getElementById('pn-geom').value };`);
  console.log('  ' + info.sum.replace(/\n/g, '\n  '));
  ok(info.order === 'be' && info.swap === false, '默认「高字节在前 + 不交换 R/B」（与 MADCTL=0x00 是正确组合）');
  const expectPx = PANEL === 'axs15352' ? 240 * 296 * 2 : 360 * 360 * 2;
  ok(info.sum.includes(String(expectPx)), `整屏字节数 = ${expectPx}（${PANEL}）`);
}

// ==================================================================== 6 刷图
step('6. 整屏刷图（逐档 SCLK）');
const results = [];
for (const mhz of SCLKS){
  // 每一档都显式设一遍（第一档也要 —— 否则会沿用上一个会话遗留的频率）
  await cdp.eval(`
    document.querySelector('#tabs .tab[data-tab="spi"]').click();
    await new Promise(r => setTimeout(r, 200));
    document.getElementById('sp-sclk').value = '${mhz * 1e6}';
    document.getElementById('sp-set').click();
    await new Promise(r => setTimeout(r, 800));
    document.querySelector('#tabs .tab[data-tab="panel"]').click();
    await new Promise(r => setTimeout(r, 200));
    return true;`);
  const meta = await cdp.eval(`
    const s = window.__tools.spiSession;
    const before = { ok: s.counters.framesOk, err: s.counters.framesErr };
    document.getElementById('pn-img-send').click();
    return { before };`);
  await cdp.waitIdle(180000);
  await sleep(600);
  const r = await cdp.eval(`
    const s = window.__tools.spiSession;
    await s.pollStatus(true);
    return { run: window.__tools.panel.summary().lastRun,
             actual: s.counters.actualSclkHz, ok: s.counters.framesOk, err: s.counters.framesErr,
             lastUs: s.counters.lastUs };`);
  const okFrames = r.ok - meta.before.ok;
  const errFrames = r.err - meta.before.err;
  const bytes = PANEL === 'axs15352' ? 240 * 296 * 2 : 360 * 360 * 2;
  const secs = (r.run?.ms ?? 0) / 1000;
  const mbps = secs > 0 ? (bytes / 1024 / 1024 / secs) : 0;
  results.push({ req: mhz, actual: r.actual / 1e6, frames: okFrames, err: errFrames, ms: r.run?.ms ?? 0, mbps, lastUs: r.lastUs });
  ok(errFrames === 0, `${mhz} MHz：刷图零错误（${okFrames} 帧，实际 ${(r.actual / 1e6).toFixed(1)} MHz · ${(r.run?.ms ?? 0).toFixed(0)} ms）`);
  if (secs > 0) console.log(`  吞吐 ${mbps.toFixed(2)} MB/s　单笔事务 ${r.lastUs ? r.lastUs.toFixed(1) : '?'} µs　` +
    `（SPI 理论 ${(bytes * 8 / (r.actual) * 1000).toFixed(1)} ms，实测 ${(r.run.ms).toFixed(0)} ms）`);
}

// ==================================================================== 7 收尾
step('7. 收尾');
{
  await cdp.eval(`document.querySelector('#tabs .tab[data-tab="spi"]').click();`);
  await sleep(200);
  await cdp.eval(`document.getElementById('sp-disable').click();`);
  await sleep(500);
  const off = await cdp.eval(`return { enabled: window.__tools.spiSession.enabled, counters: window.__tools.spiSession.counters };`);
  ok(!off.enabled, 'ENABLE 0 已下发（注意：引脚保持现状，PA26 要探针重启才回到 UART break）');
  const err = await cdp.eval('return window.__tools.summary().errors;');
  ok(err.length === 0, '整场跑完页面无未捕获错误', JSON.stringify(err.slice(0, 3)));
}

console.log('\n===== 真机验收小结 =====');
console.log(`屏：${PANEL_CFG.label}　配置：档 ${PANEL_CFG.profile}`);
for (const r of results){
  console.log(`  SCLK 请求 ${String(r.req).padStart(2)} MHz → 实际 ${r.actual.toFixed(1)} MHz　` +
    `${r.frames} 帧 / ${r.err} 错　${r.ms.toFixed(0)} ms　${r.mbps.toFixed(2)} MB/s`);
}
console.log('\n👀 现在看屏：应该出现**8 条彩条**（白/黄/青/绿/品红/红/蓝/黑，从左到右）。');
console.log('   颜色不对：只翻一个开关（先试「R/B 交换」，再试「低字节在前」）—— 别两个一起翻。');
console.log(`\n${fail ? '❌' : '✅'} spi-hw: ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
