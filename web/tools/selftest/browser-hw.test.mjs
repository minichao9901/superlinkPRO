/**
 * 浏览器真机端到端测试（用 CDP 驱动 Edge/Chrome，自动应答 WebUSB / Web Serial 的设备选择框）。
 *
 * 前置：
 *   1) 先起一个静态服务：  python -m http.server 8899 --bind 127.0.0.1     (在仓库根)
 *   2) 再起一个带调试端口的浏览器（headless 也行）：
 *      msedge --headless=new --remote-debugging-port=9333 --user-data-dir=%TEMP%\edge-cdp about:blank
 *   3) 跑：
 *      node tools/selftest/browser-hw.test.mjs webusb     # 零安装 RTT（需先关掉 OpenOCD/桥）
 *      node tools/selftest/browser-hw.test.mjs bridge     # 桥 + OpenOCD 后端
 *      node tools/selftest/browser-hw.test.mjs serial     # 串口助手（COM66 = DAPLink 桥到 PA9/PA10）
 *
 * 关键点：Runtime.evaluate 带 userGesture:true，点击才算"用户手势"，否则 requestDevice 直接被拒。
 */
const CDP = process.env.CDP || 'http://127.0.0.1:9333';
const APP = process.env.APP || 'http://127.0.0.1:8899/index.html';
const STAGE = (process.argv[2] || 'webusb').toLowerCase();

let pass = 0, fail = 0;
const ok = (c, name, extra = '') => { if (c){ pass++; console.log(`  PASS  ${name}`); } else { fail++; console.log(`  FAIL  ${name} ${extra}`); } };
const sleep = ms => new Promise(r => setTimeout(r, ms));

/* ---------------- 极简 CDP 客户端 ----------------
 * 🚨 关键：DeviceAccess（设备选择框）是**浏览器级**域，事件只发给 /json/version 那条
 *    WebSocket；页面级会话能调 enable 但收不到 deviceRequestPrompted。
 *    所以这里开两条连接：browser（选择框）+ page（执行 JS）。
 */
class Cdp {
  constructor(){ this.seq = 0; this.pending = new Map(); this.handlers = new Map(); this.prompts = []; }
  _open(url, onMsg){
    const ws = new WebSocket(url);
    return new Promise((res, rej) => {
      ws.onopen = () => res(ws);
      ws.onerror = () => rej(new Error('CDP WebSocket 连不上：' + url));
      ws.onmessage = ev => onMsg(ws, JSON.parse(ev.data));
    });
  }
  _dispatch(ws, m){
    if (m.id && this.pending.has(m.id)){
      const p = this.pending.get(m.id);
      this.pending.delete(m.id);
      m.error ? p.rej(new Error(m.error.message)) : p.res(m.result);
      return;
    }
    if (!m.method) return;
    if (m.method === 'DeviceAccess.deviceRequestPrompted'){
      this.prompts.push(m.params);
      this.onPrompt?.(m.params);
    }
    const hs = this.handlers.get(m.method);
    if (hs) for (const h of hs) h(m.params);
  }
  async connect(){
    const ver = await (await fetch(CDP + '/json/version')).json();
    this.browserWs = await this._open(ver.webSocketDebuggerUrl, (ws, m) => this._dispatch(ws, m));
    const list = await (await fetch(CDP + '/json/list')).json();
    const page = list.find(t => t.type === 'page');
    if (!page) throw new Error('没有可用的页面目标');
    this.ws = await this._open(page.webSocketDebuggerUrl, (ws, m) => this._dispatch(ws, m));
    await this.send('Page.enable');
    await this.send('Runtime.enable');
    // 🚨 必须关缓存：python -m http.server 不带 Cache-Control，浏览器会按"启发式"缓存 JS 模块，
    //    改完代码再跑测试可能仍在跑**旧模块**（排查时会被误导很久）。
    try { await this.send('Network.enable'); await this.send('Network.setCacheDisabled', { cacheDisabled: true }); } catch {}
    // DeviceAccess 的 enable 只在**页面级**会话上存在（浏览器级会报 wasn't found）；
    // 而选择框事件在无头模式下不投递。两边都试，失败就算了 ——
    // 因为设备已授权/被策略预授权时**根本不弹框**，压根用不到它。
    for (const [where, fn] of [['browser', () => this.sendBrowser('DeviceAccess.enable')],
                               ['page', () => this.send('DeviceAccess.enable')]]){
      try { await fn(); this.deviceAccessOn = where; break; } catch {}
    }
    if (!this.deviceAccessOn) console.log('  [warn] DeviceAccess 不可用（不影响已授权设备直连）');
  }
  send(method, params = {}){
    return this._call(this.ws, method, params);
  }
  sendBrowser(method, params = {}){
    return this._call(this.browserWs, method, params);
  }
  _call(ws, method, params){
    const id = ++this.seq;
    return new Promise((res, rej) => {
      this.pending.set(id, { res, rej });
      ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => { if (this.pending.delete(id)) rej(new Error(`CDP ${method} 超时`)); }, 60000);
    });
  }
  on(method, fn){
    if (!this.handlers.has(method)) this.handlers.set(method, []);
    this.handlers.get(method).push(fn);
  }
  /** 在页面里执行表达式；userGesture=true 才算用户手势（requestDevice/requestPort 必须要） */
  async eval(expr, userGesture = false){
    const r = await this.send('Runtime.evaluate', { expression: expr, userGesture, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error('页面异常：' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
    return r.result.value;
  }
  /** 页面里求值并 JSON 化（表达式可以是 Promise —— 早期版本直接 JSON.stringify(promise) 得到 {}，
   *  于是"模块新鲜度检查"永远打印 undefined，白跑了好几轮） */
  async evalJson(expr){
    const v = await this.eval(`(async()=>JSON.stringify(await (${expr})))()`);
    return JSON.parse(v);
  }
  /** 等设备选择框，选第一个匹配的设备 */
  async pickDevice(match, timeout = 15000){
    const t0 = Date.now();
    while (!this.prompts.length){
      if (Date.now() - t0 > timeout) throw new Error('等设备选择框超时（选择框没弹出来？）');
      await sleep(100);
    }
    const p = this.prompts.shift();
    const dev = p.devices.find(d => match.test(d.name)) || p.devices[0];
    if (!dev) throw new Error('选择框里没有设备：' + JSON.stringify(p.devices));
    await this.sendBrowser('DeviceAccess.selectPrompt', { id: p.id, deviceId: dev.id });
    return dev;
  }
  async waitFor(expr, timeout = 10000, label = expr){
    const t0 = Date.now();
    for (;;){
      let v = false;
      try { v = await this.eval(`!!(${expr})`); } catch {}
      if (v) return true;
      if (Date.now() - t0 > timeout) throw new Error(`等待超时：${label}`);
      await sleep(200);
    }
  }
  async navigate(url){
    this.prompts.length = 0;
    await this.send('Page.navigate', { url });
    await sleep(300);
  }
}

/* ---------------- 页面操作小工具 ---------------- */
const setBackend = b => `(()=>{const s=document.getElementById('r-backend');s.value='${b}';s.dispatchEvent(new Event('change'));return s.value})()`;
const click = id => `document.getElementById('${id}').click()`;
const rttRecords = `window.__tools.rtt.records.map(r=>new TextDecoder().decode(r.b)).join('')`;
const rxText = `document.getElementById('s-rx').textContent`;

/**
 * 等"设备授权"这件事落地：可能弹选择框（自动选第一个匹配的），
 * 也可能因为已授权/策略预授权而**根本没弹框**直接连上（两条路都要认）。
 */
async function settle(cdp, match, readyExpr, timeout = 10000){
  const t0 = Date.now();
  for (;;){
    if (cdp.prompts.length) return await cdp.pickDevice(match);
    let ready = false;
    try { ready = await cdp.eval(`!!(${readyExpr})`); } catch {}
    if (ready) return null;
    if (Date.now() - t0 > timeout) throw new Error(`既没弹选择框也没就绪：${readyExpr}`);
    await sleep(200);
  }
}

/* ---------------- 主流程 ---------------- */
const cdp = new Cdp();
await cdp.connect();
console.log(`== 浏览器真机测试：${STAGE} ==`);
await cdp.navigate(APP);
await cdp.waitFor('window.__tools', 8000, '页面加载');
ok(true, `页面加载完成（${APP}）`);
// 确认跑的是磁盘上最新的模块（否则后面所有结论都不可信）
const modInfo = await cdp.evalJson(`fetch('/app/rtt/dap-webusb.js',{cache:'reload'}).then(r=>r.text()).then(t=>({len:t.length, hasActivation:t.includes('SWD 激活序列'), hasPad:t.includes('补齐到整包再发')}))`);
console.log(`        dap-webusb.js ${modInfo.len} 字节 · 含激活序列修复: ${modInfo.hasActivation} · 含整包发送: ${modInfo.hasPad}`);
ok(modInfo.hasActivation && modInfo.hasPad, '页面加载到的是最新模块（不是缓存里的旧版）');

if (STAGE === 'webusb'){
  await cdp.eval(setBackend('webusb'));
  // 扫描范围限定在这块 F103 的 20KB SRAM 内（默认范围会读进未映射区，白等）
  await cdp.eval(`document.getElementById('r-range').value='0x20000000-0x20005000'`);

  await cdp.eval(click('r-usb-connect'), true);
  const dev = await settle(cdp, /MicroLink|DAP|CMSIS/i, `window.__tools.rtt.probe`);
  ok(true, dev ? `选择框里选中探针：${dev.name}` : '用已授权设备直连（没弹选择框）');
  await cdp.waitFor('window.__tools.rtt.probe', 8000, '探针对象');
  const probeName = await cdp.evalJson(`window.__tools.rtt.probe.name`);
  console.log(`        探针：${probeName}`);

  await cdp.waitFor('window.__tools.rtt.rtt', 12000, 'RTT 控制块定位+初始化');
  const st = await cdp.evalJson(`({cb:document.getElementById('r-cb').textContent,
      up:document.getElementById('r-up').textContent, down:document.getElementById('r-down').textContent,
      probe:window.__tools.rtt.probe && window.__tools.rtt.probe.name})`);
  ok(/0x2000/.test(st.cb), `控制块定位 ${st.cb}（探针：${st.probe}）`);
  ok(st.up === '2' && st.down === '1', `通道数 up=${st.up} down=${st.down}`);

  // 等固件的周期性日志经 WebUSB 流进来
  await cdp.waitFor(`${rttRecords}.length > 40`, 8000, 'ch0 数据');
  const txt = await cdp.eval(rttRecords);
  ok(txt.includes('RTT ch0') || txt.includes('tick='), `ch0 收到固件日志 ${txt.length} 字符`);
  ok(txt.includes('\x1b['), 'CH0 带 ANSI 转义（终端模式能用）');

  // 下行命令 → 固件回包
  await cdp.eval(`window.__tools.rtt._sendBytes(new TextEncoder().encode('help\\r'))`);
  await cdp.waitFor(`${rttRecords}.includes('测试固件命令')`, 8000, 'help 回包');
  const txt2 = await cdp.eval(rttRecords);
  ok(txt2.includes('测试固件命令') && txt2.includes('reboot'), '下行 help 收到完整命令列表');

  const stats = await cdp.evalJson(`({rate:document.getElementById('r-rate').textContent,
      hz:document.getElementById('r-hz').textContent, level:document.getElementById('r-full').textContent})`);
  console.log(`        读取 ${stats.rate} · 轮询 ${stats.hz} Hz · 缓冲水位 ${stats.level}`);
  ok(parseFloat(stats.hz) > 20, `轮询频率 ${stats.hz} Hz（WebUSB 实测单命令往返 0.34ms，应按百 Hz 级别）`);

  // 测试复位按钮（会重新扫描控制块，而且**目标必须重新跑起来**——否则固件不打印）
  const beforeReset = await cdp.evalJson(`window.__tools.rtt.records.length`);
  await cdp.eval(`window.__tools.rtt.resetTarget()`, true);
  await sleep(900);
  const st2 = await cdp.evalJson(`({cb:document.getElementById('r-cb').textContent, err:document.getElementById('r-err').textContent})`);
  ok(/0x2000/.test(st2.cb), `复位后重新定位控制块 ${st2.cb}`, st2.err);
  await cdp.waitFor(`window.__tools.rtt.records.length > ${beforeReset}`, 8000, '复位后又收到数据');
  const after = await cdp.eval(rttRecords);
  ok(after.includes('STM32F103') || after.includes('tick='), '复位后目标重新运行并打印（没被停在 halt）');
}

if (STAGE === 'bridge'){
  await cdp.eval(setBackend('bridge-openocd'));
  await cdp.eval(`document.getElementById('r-range').value='0x20000000-0x20005000'`);
  await cdp.eval(click('r-bridge-connect'), true);
  await cdp.waitFor('window.__tools.rtt.rtt', 12000, '桥 + OpenOCD 的 RTT 初始化');
  const st = await cdp.evalJson(`({cb:document.getElementById('r-cb').textContent,
      up:document.getElementById('r-up').textContent})`);
  ok(/0x2000/.test(st.cb), `（桥）控制块定位 ${st.cb}，up=${st.up}`);
  await cdp.waitFor(`${rttRecords}.length > 40`, 8000, 'ch0 数据');
  await cdp.eval(`window.__tools.rtt._sendBytes(new TextEncoder().encode('info\\r'))`);
  await cdp.waitFor(`${rttRecords}.includes('USART1')`, 8000, 'info 回包');
  const txt = await cdp.eval(rttRecords);
  ok(txt.includes('USART1') && txt.includes('RTT'), '（桥）下行 info 收到固件信息');
  const errs = await cdp.evalJson(`window.__tools.errors`);
  ok(errs.length === 0, '页面无 JS 错误', JSON.stringify(errs));
}

if (STAGE === 'serial'){
  await cdp.eval(click('s-pick'), true);
  const dev = await settle(cdp, /COM66|MicroLink|DAP/i, `window.__tools.assistant.ports.length > 0`);
  ok(true, dev ? `串口选择框里选中：${dev.name}` : '用已授权串口（没弹选择框）');
  await cdp.eval(`document.getElementById('s-baud').value='115200'`);
  await cdp.eval(click('s-open'), true);
  await cdp.waitFor('window.__tools.session.isOpen', 10000, '串口打开');
  ok(true, '串口已打开（Web Serial）');

  // 固件每秒往串口打一条带 ANSI 的行、每 3 秒一条 UART 专有行
  await cdp.waitFor(`${rxText}.includes('[UART]') || ${rxText}.includes('[RTT ch0]')`, 8000, '设备输出');
  const t1 = await cdp.eval(rxText);
  ok(t1.includes('[RTT ch0]') || t1.includes('[UART]'), `接收区收到设备输出 ${t1.length} 字符`);

  // 发送命令（ASCII + CRLF）
  const txBefore = await cdp.evalJson(`window.__tools.assistant.txc.total`);
  await cdp.eval(`window.__tools.assistant.send('help')`, true);
  await cdp.waitFor(`${rxText}.includes('测试固件命令')`, 10000, 'help 回包');
  const txAfter = await cdp.evalJson(`window.__tools.assistant.txc.total`);
  ok(txAfter - txBefore === 6, `TX 记账 = 6 字节（help + CRLF），实际 ${txAfter - txBefore}`);
  const t2 = await cdp.eval(rxText);
  ok(t2.includes('测试固件命令') && t2.includes('reboot'), '串口收到完整 help 列表');

  // HEX 显示模式 + HEX 发送
  await cdp.eval(`window.__tools.assistant.rx.setMode('hex')`);
  await sleep(300);
  const t3 = await cdp.eval(rxText);
  ok(/[0-9A-F]{2} [0-9A-F]{2}/.test(t3), 'HEX 显示模式生效');
  await cdp.eval(`window.__tools.assistant.rx.setMode('ascii')`);

  await cdp.eval(`window.__tools.assistant.txSeg.set('hex')`);
  const tx0 = await cdp.evalJson(`window.__tools.assistant.txc.total`);
  await cdp.eval(`document.getElementById('s-tx').value='01 03 00 0A'`);
  await cdp.eval(`window.__tools.assistant.send()`, true);
  await sleep(500);
  const tx1 = await cdp.evalJson(`window.__tools.assistant.txc.total`);
  ok(tx1 - tx0 === 6, `HEX 发送记账 = 6 字节（4 + CRLF），实际 ${tx1 - tx0}`);

  // 定时发送
  await cdp.eval(`window.__tools.assistant.txSeg.set('ascii');document.getElementById('s-tx').value='uptime';`);
  await cdp.evalJson(`(function(){const b=window.__tools.assistant;document.getElementById('s-timer-ms').value='300';
      document.getElementById('s-timer').checked=true;b._armTimer();return 1})()`);
  await sleep(1200);
  const frames = await cdp.evalJson(`window.__tools.assistant.txc.frames`);
  await cdp.eval(`document.getElementById('s-timer').checked=false;window.__tools.assistant._armTimer()`);
  ok(frames >= 3, `定时发送在跑（${frames} 帧）`);

  const errs = await cdp.evalJson(`window.__tools.errors`);
  ok(errs.length === 0, '页面无 JS 错误', JSON.stringify(errs));
}

const errors = await cdp.evalJson(`window.__tools.errors`);
if (errors.length) console.log('  页面错误：' + JSON.stringify(errors));

// 收尾：把探针/串口让出去，否则下次 OpenOCD（或别的工具）认不到设备
try { await cdp.eval(`window.__tools.rtt.disconnect()`); } catch {}
try { await cdp.eval(`window.__tools.session.close()`); } catch {}
console.log('（已断开探针与串口，方便别的工具接管）');

console.log(`\n结果：${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
