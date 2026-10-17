/**
 * 真机验收：SPI/QSPI 屏的**局部刷新**（默认 AXS15352 / 档 1，SPI+DC）。
 *   make spi-partial-hw                            # = node tools/selftest/spi-partial-hw.mjs
 *   make spi-partial-hw ARGS="--panel=st77916"     # 换 ST77916（档 2，QSPI）
 *   make spi-partial-hw ARGS="--sclk=60"
 *
 * 验三件事，都要**客观数字**，不靠"看屏"：
 *   ① 同内容重刷 → 整帧跳过：探针侧 framesOk 一个不涨、线上一个包都不发；
 *   ② 小范围变化 → 只发那个包围盒：抓**真发出去的包**解出 CASET/RASET 坐标与像素字节数，
 *      并与"同一张图整帧里该子矩形"逐字节比（不用 MISO 回读也能证明屏上是对的）；
 *   ③ 动画：同一个 GIF，局部刷新开/关各播一段，比 fps 与每帧像素量。
 *
 * 前置：8899 静态服务 + 9333 CDP 浏览器 + 探针 + 屏（make page-prep 会自动起前两个）。
 */
const CDP = process.env.CDP || 'http://127.0.0.1:9333';
const APP = process.env.APP || 'http://127.0.0.1:8899/index.html';
const argv = process.argv.slice(2);
const arg = (n, d) => { const m = argv.find(a => a.startsWith(`--${n}=`)); return m ? m.split('=')[1] : d; };
const PANEL = arg('panel', 'axs15352');
const SCLK = Number(arg('sclk', '40')) * 1e6;
const PANELS = {
  axs15352: { preset: 'axs15352', geom: 'axs15352', profile: 1, w: 240, h: 296, frames: 292 },
  st77916: { preset: 'st77916', geom: 'st77916', profile: 2, w: 360, h: 360, frames: 529 },
};
const PC = PANELS[PANEL];
if (!PC) throw new Error('未知 --panel=' + PANEL);
const GIF = `samples/anim/ball-grid-${PC.w}x${PC.h}.gif`;

setTimeout(() => { console.error('[WATCHDOG] 超时'); process.exit(9); }, 300000);
const sleep = ms => new Promise(r => setTimeout(r, ms));
let pass = 0, fail = 0;
const ok = (c, name, extra = '') => { if (c){ pass++; console.log(`  PASS  ${name}`); } else { fail++; console.log(`  FAIL  ${name} ${extra}`); } };
const step = s => console.log(`\n== ${s} ==`);

const list = await (await fetch(CDP + '/json/list')).json();
const page = list.find(t => t.type === 'page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('CDP ws')); });
let seq = 0; const pend = new Map();
ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pend.has(m.id)){ const p = pend.get(m.id); pend.delete(m.id); m.error ? p.rej(new Error(m.error.message)) : p.res(m.result); } };
const send = (method, params = {}, t = 90000) => new Promise((res, rej) => { const id = ++seq; pend.set(id, { res, rej }); ws.send(JSON.stringify({ id, method, params })); setTimeout(() => { if (pend.delete(id)) rej(new Error(method + ' 超时')); }, t); });
const ev = async expr => {
  const r = await send('Runtime.evaluate', { expression: `(async()=>{ ${expr} })()`, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error('页面异常：' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
  return r.result.value;
};
await send('Runtime.enable'); await send('Page.enable');
await send('Page.navigate', { url: APP + '?demo=serial&t=' + Date.now() });
for (let i = 0; i < 60; i++){ await sleep(400); if (await ev('return !!window.__tools?.panel;').catch(() => false)) break; }

console.log(`真机局部刷新验收：${PANEL}（档 ${PC.profile}）@ ${SCLK / 1e6} MHz`);

// ==================================================================== 1 连接
step('1. 连接探针 + 配置');
{
  await ev(`document.querySelector('#tabs .tab[data-tab="spi"]').click(); return 1;`);
  await sleep(200);
  await ev(`document.getElementById('sp-reconnect').click(); return 1;`);
  await sleep(900);
  const hid = await ev(`return { connected: window.__tools.spiSession.connected, label: window.__tools.spiSession.hid?.label || '' };`);
  ok(hid.connected, `HID 已连接（${hid.label}）`);
  await ev(`document.getElementById('sp-usb').click(); return 1;`);
  await sleep(1200);
  const usb = await ev(`return { ready: window.__tools.spiSession.dataReady, iface: window.__tools.spiSession.transport?.iface };`);
  ok(usb.ready, `数据端点已连接（接口 ${usb.iface}）`);

  await ev(`document.querySelector('#tabs .tab[data-tab="panel"]').click(); return 1;`);
  await sleep(200);
  await ev(`
    document.getElementById('pn-preset').value = '${PC.preset}';
    document.getElementById('pn-preset').dispatchEvent(new Event('change'));
    document.getElementById('pn-preset-apply').click();
    await new Promise(r => setTimeout(r, 1600));
    document.getElementById('pn-enable').click();
    await new Promise(r => setTimeout(r, 800));
    return 1;`);
  const st = await ev(`const s = window.__tools.spiSession;
    return { prof: s.profile?.profile, enabled: s.enabled, geom: document.getElementById('pn-geom').value,
             cfg: s.cfg, sclk: s.counters.actualSclkHz, err: s.counters.framesErr };`);
  ok(st.prof === PC.profile && st.enabled, `档位 = ${st.prof} · 桥已使能（实际 SCLK ${(st.sclk / 1e6).toFixed(1)} MHz）`);
  ok(st.geom === PC.geom, `屏幕几何 = ${st.geom}`);
  // 面板初始化表（把屏点亮；不需要 = 后续局部刷新照样能看到效果，但先点亮更接近真实用法）
  await ev(`
    document.getElementById('pn-code-preset').value = '${PC.preset}';
    document.getElementById('pn-code-load').click();
    await new Promise(r => setTimeout(r, 500));
    document.getElementById('pn-code-play').click();
    for (let i = 0; i < 100 && window.__tools.panel.summary().activity; i++) await new Promise(r => setTimeout(r, 100));
    return 1;`);
  console.log('  面板初始化表已下发');
}

// ==================================================================== 2 抓线上包
step('2. 挂钩 transport.sendPacks：把**真发出去的字节**留一份');
{
  const hooked = await ev(`
    const t = window.__tools.spiSession.transport;
    if (!t.__origSendPacks){
      t.__origSendPacks = t.sendPacks.bind(t);
      t.__wire = [];
      t.sendPacks = async (packs, opts) => { for (const p of packs) t.__wire.push(p.slice()); return t.__origSendPacks(packs, opts); };
    }
    t.__wire.length = 0;
    return { hasOrig: !!t.__origSendPacks, iface: t.iface };`);
  ok(hooked.hasOrig, `已挂钩（接口 ${hooked.iface}）—— 抓的是**实际交给 USB 的包**`);
}

/** 发送一张图（或重发），返回线上帧的解码摘要 + 探针侧计数差 */
const sendAndGrab = async () => ev(`
  const P = await import('/app/spi/protocol.js');
  const s = window.__tools.spiSession, t = s.transport;
  t.__wire.length = 0;
  const before = { ok: s.counters.framesOk, err: s.counters.framesErr, bytes: t.writeBytes };
  await window.__tools.panel.sendImage();
  await new Promise(r => setTimeout(r, 400));
  const after = { ok: s.counters.framesOk, err: s.counters.framesErr, bytes: t.writeBytes };
  const frames = [];
  let pxBytes = 0;
  for (const pk of t.__wire) for (const f of P.parsePack(pk).frames){
    const o = { type: f.type, flags: f.flags, len: f.len, tx: 0, cmd: null, tcfg: null, addr: null, addrLen: null, params: null };
    if (f.type === P.T.STEP){ o.cmd = f.payload[0]; o.params = [...f.payload.subarray(4, 4 + f.payload[1])]; }
    else if (f.type === P.T.XFER){
      const tcfg = f.payload[1], tx = f.payload[4] | (f.payload[5] << 8);
      o.cmd = f.payload[0]; o.tcfg = tcfg; o.addrLen = f.payload[2]; o.tx = tx;
      o.addr = (f.payload[8] | (f.payload[9] << 8) | (f.payload[10] << 16)) >>> 0;
      /* 两种档的线上形状不一样，判据要一起认（见 image.js windowItems / pixelItems）：
       *   开窗：档 0/1 = STEP 帧；档 2 = XFER（单线 + CMD_EN + ADDR_EN，地址 = 命令字<<8）
       *   RAMWR 命令帧：XFER DC_EN 但 DC_LEVEL=0（只有档 1 有）
       *   像素：其余带数据的 XFER（档 1 = DC_LEVEL；档 2 = 四线） */
      const isWin = (tcfg & P.TC.CMD_EN) && (tcfg & P.TC.ADDR_EN) && (tcfg & P.TC.LINES_MASK) === P.TC.LINES_1;
      const isRamwr = (tcfg & P.TC.DC_EN) && !(tcfg & P.TC.DC_LEVEL);
      if (isWin){
        o.cmd = (o.addr >>> 8) & 0xff;                       // 档 2：命令字在 24 bit 地址的中间字节
        o.params = [...f.payload.subarray(12, 12 + Math.min(tx, 4))];
      } else if (!isRamwr && tx > 0) pxBytes += tx;
    }
    frames.push(o);
  }
  const sum = window.__tools.panel.summary();
  return { frames, pxBytes, dOk: after.ok - before.ok, dErr: after.err - before.err, dBytes: after.bytes - before.bytes,
           rspBad: (sum.lastRun && sum.lastRun.badRsp) ?? null,
           action: sum.partial.lastAction, win: sum.partial.lastWin, reason: sum.partial.lastReason,
           bytes: sum.lastRun ? sum.lastRun.bytes : 0, slices: sum.lastRun ? sum.lastRun.slices : 0,
           ms: sum.lastRun ? +sum.lastRun.ms.toFixed(1) : 0 };`);

// ==================================================================== 3 整帧 → 跳过 → 局部
step('3. 整帧 → 同内容重刷（应跳过）→ 小改动（应只发包围盒）');
let full, again, partial;
{
  await ev(`window.__tools.panel.setPattern('BAR'); return 1;`);
  await sleep(300);
  full = await sendAndGrab();
  ok(full.action === 'full' && full.frames.length === PC.frames,
     `第 1 次（整帧）：${full.frames.length} 帧上线（期望 ${PC.frames}）· 探针侧 framesOk +${full.dOk} · ${full.ms} ms`);
  ok(full.dErr === 0 && (full.rspBad ?? 0) === 0, `整帧刷零错误（frames_err +${full.dErr}，非 OK 应答 ${full.rspBad}）`);

  again = await sendAndGrab();
  ok(again.action === 'skip' && again.frames.length === 0 && again.dOk === 0,
     `第 2 次（图没变）：整帧跳过 —— 线上 ${again.frames.length} 个帧、探针侧 framesOk +${again.dOk}（原因：${again.reason}）`);

  // 小改动：同一个图案，只在 (100,100) 涂 8×8 —— 与假探针自测同一个构造
  partial = await ev(`
    const v = window.__tools.panel;
    const g = v.geometry();
    const base = { w: g.w, h: g.h, rgba: v.src.rgba.slice(), name: '局部测试' };
    for (let y = 100; y < 108; y++) for (let x = 100; x < 108; x++){ const i = (y * g.w + x) * 4; base.rgba[i] = base.rgba[i+1] = base.rgba[i+2] = 255; }
    v.src = base;
    return 1;`);
  void partial;
  const p = await sendAndGrab();
  partial = p;
  const steps = p.frames.filter(f => f.type === 0x07 || f.cmd === 0x2a || f.cmd === 0x2b);
  const caset = steps.find(f => f.cmd === 0x2a), raset = steps.find(f => f.cmd === 0x2b);
  ok(p.action === 'partial', `第 3 次（只有 8×8 变了）：判定为局部（${p.reason}）`);
  ok(!!caset && caset.params.join(',') === '0,100,0,107',
     `线上 CASET = 0x2a 参数 [${caset ? caset.params.join(',') : '—'}]（期望 0,100,0,107 → 列 100..107）`);
  ok(!!raset && raset.params.join(',') === '0,100,0,107',
     `线上 RASET = 0x2b 参数 [${raset ? raset.params.join(',') : '—'}]（期望 0,100,0,107 → 行 100..107）`);
  const expectFrames = PC.profile === 1 ? 4 : 3;        // 档 1 多一条 RAMWR 命令帧
  ok(p.frames.length === expectFrames,
     `线上只有 ${p.frames.length} 个帧（开窗 2 +〔档 1 的 RAMWR〕+ 1 片像素），整帧要 ${PC.frames}`);
  ok(p.pxBytes === 8 * 8 * 2, `像素字节 ${p.pxBytes} B（= 8×8×2）· 整帧要 ${PC.w * PC.h * 2} B → 省 ${(100 - p.pxBytes / (PC.w * PC.h * 2) * 100).toFixed(2)}%`);
  ok(p.dErr === 0 && (p.rspBad ?? 0) === 0, `局部刷零错误（frames_err +${p.dErr}，非 OK 应答 ${p.rspBad}）`);
  ok(p.bytes === 128, `页面自己也记成 128 B（summary.lastRun.bytes=${p.bytes}）`);
  console.log(`  线上字节：整帧 ${full.dBytes} B → 局部 ${p.dBytes} B（含命令帧）`);

  /* ---- ③b 决定性一条：局部帧的像素字节，必须**逐字节等于**同一张图整帧里的那个子矩形 ----
   * 做法：把局部刷新关掉、把**同一张（改过的）图**整帧发一遍，抓下它的像素流，
   *       再按行抠出 100..107 × 100..107，与局部帧的 128 字节逐字节比。
   * 这是"屏上真的会显示对吗"的机器判据 —— 不需要 MISO 回读、也不靠人看屏。 */
  const cmp = await ev(`
    const P = await import('/app/spi/protocol.js');
    const s = window.__tools.spiSession, t = s.transport;
    const pix = wire => {
      const out = [];
      for (const pk of wire) for (const f of P.parsePack(pk).frames){
        if (f.type !== P.T.XFER) continue;
        const tcfg = f.payload[1], tx = f.payload[4] | (f.payload[5] << 8);
        const isWin = (tcfg & P.TC.CMD_EN) && (tcfg & P.TC.ADDR_EN) && (tcfg & P.TC.LINES_MASK) === P.TC.LINES_1;
        const isRamwr = (tcfg & P.TC.DC_EN) && !(tcfg & P.TC.DC_LEVEL);
        if (!isWin && !isRamwr && tx > 0) out.push(...f.payload.subarray(12, 12 + tx));
      }
      return Uint8Array.from(out);
    };
    // 保住"局部帧"的那份线数据（下一步会把 __wire 清掉）
    const partPx = pix(t.__wire);
    // 关掉局部刷新 → 同一张图整帧发一遍
    const pc = document.getElementById('pn-partial');
    pc.checked = false; pc.dispatchEvent(new Event('change', { bubbles: true }));
    await new Promise(r => setTimeout(r, 120));
    t.__wire.length = 0;
    const b0 = s.counters.framesOk;
    await window.__tools.panel.sendImage();
    await new Promise(r => setTimeout(r, 400));
    const fullPx = pix(t.__wire), dOk = s.counters.framesOk - b0;
    // 从整帧里按行抠 100..107 × 100..107（窗口起点 0,0；**行宽 = 当前屏宽**，别写死 240！）
    const W = window.__tools.panel.geometry().w;
    const want = new Uint8Array(8 * 8 * 2);
    for (let y = 0; y < 8; y++){
      const src = ((100 + y) * W + 100) * 2;
      want.set(fullPx.subarray(src, src + 16), y * 16);
    }
    let diff = 0, firstDiff = -1;
    for (let i = 0; i < want.length; i++) if (want[i] !== partPx[i]){ diff++; if (firstDiff < 0) firstDiff = i; }
    // 收尾：局部刷新放回开。⚠️ 开关一变就**丢掉基准帧**（换语义后再拿旧帧比会把整屏算成"变了"），
    // 所以这里先发一次重建基准（整帧），再发一次才是"跳过"。
    pc.checked = true; pc.dispatchEvent(new Event('change', { bubbles: true }));
    await new Promise(r => setTimeout(r, 120));
    t.__wire.length = 0;
    const b1 = s.counters.framesOk;
    await window.__tools.panel.sendImage();
    await new Promise(r => setTimeout(r, 400));
    const rebuild = s.counters.framesOk - b1;
    const a1 = window.__tools.panel.summary().partial.lastAction;
    t.__wire.length = 0;
    const b2 = s.counters.framesOk;
    await window.__tools.panel.sendImage();
    await new Promise(r => setTimeout(r, 300));
    return { part: partPx.length, full: fullPx.length, dOk, diff, firstDiff,
             rebuild, a1, skipOk: s.counters.framesOk - b2,
             act: window.__tools.panel.summary().partial.lastAction };`);
  ok(cmp.part === 128 && cmp.full === PC.w * PC.h * 2,
     `整帧像素流 ${cmp.full} B / 局部帧像素流 ${cmp.part} B（都从**线上包**里解出来的）`);
  ok(cmp.diff === 0,
     `局部帧的 128 字节 == 同一张图整帧里 100..107×100..107 的对应字节（差异 ${cmp.diff} 字节${cmp.firstDiff >= 0 ? '，首个不同 @' + cmp.firstDiff : ''}）`);
  ok(cmp.rebuild === PC.frames && cmp.a1 === 'full',
     `重新勾上局部刷新 → 基准帧作废、先整帧重建（探针侧 +${cmp.rebuild} 帧，判定 ${cmp.a1}）`);
  ok(cmp.skipOk === 0 && cmp.act === 'skip',
     `再点一次（同内容）→ 整帧跳过（探针侧 +${cmp.skipOk} 帧）`);
}

// ==================================================================== 4 动画
step('4. 动画：同一个 GIF，局部刷新开/关各播一段');
{
  const loaded = await ev(`
    const cands = ['${GIF}', 'samples/anim/ball-grid-240x296.gif'];
    let url = null, b = null;
    for (const c of cands){ const r = await fetch(c).catch(() => null); if (r && r.ok){ url = c; b = await r.blob(); break; } }
    if (!b) return { skip: '素材取不到（先跑 make samples-anim）' };
    const input = document.getElementById('pn-anim-input');
    const dt = new DataTransfer(); dt.items.add(new File([b], url.split('/').pop(), { type: 'image/gif' }));
    input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    for (let i = 0; i < 60 && !/ball-grid/.test(window.__tools.panel.summary().anim?.src || ''); i++) await new Promise(r => setTimeout(r, 100));
    return { url, src: window.__tools.panel.summary().anim?.src, frames: window.__tools.panel.summary().anim?.srcFrames };`);
  if (loaded.skip){
    console.log(`  ⚠ 跳过动画部分：${loaded.skip}`);
  } else {
    console.log(`  源：${loaded.src}（${loaded.frames} 帧）`);
    const run = async (on, ms) => ev(`
      const pc = document.getElementById('pn-partial');
      pc.checked = ${on}; pc.dispatchEvent(new Event('change', { bubbles: true }));
      await new Promise(r => setTimeout(r, 120));
      const s = window.__tools.spiSession;
      const b0 = s.counters.framesOk;
      document.getElementById('pn-anim-play').click();
      await new Promise(r => setTimeout(r, ${ms}));
      document.getElementById('pn-anim-stop').click();
      await new Promise(r => setTimeout(r, 600));
      const a = window.__tools.panel.summary().anim;
      return { ...a, dOk: s.counters.framesOk - b0, framesErr: s.counters.framesErr };`);
    // 先关：整帧基线
    const off = await run(false, 2500);
    const on = await run(true, 2500);
    console.log(`  关：${off.frames} 帧 / ${off.fps.toFixed(1)} fps / 每帧 ${(off.dOk / Math.max(1, off.frames)).toFixed(1)} 个协议帧`);
    console.log(`  开：${on.frames} 帧 / ${on.fps.toFixed(1)} fps / 每帧 ${(on.dOk / Math.max(1, on.frames)).toFixed(1)} 个协议帧 / 像素省 ${on.savePct.toFixed(1)}%（局部 ${on.partial} · 跳过 ${on.skipped}）`);
    ok(on.frames > off.frames, `局部刷新把同一个 GIF 播得更快了：${off.frames} → ${on.frames} 帧（${(on.frames / Math.max(1, off.frames) * 100 - 100).toFixed(0)}%）`);
    ok(on.savePct > 10, `像素省了 ${on.savePct.toFixed(1)}%（局部 ${on.partial} 帧 / 跳过 ${on.skipped} 帧）`);
    ok(on.framesErr === 0 && off.framesErr === 0, `两条路都零错误（frames_err ${off.framesErr} / ${on.framesErr}）`);
    ok(on.dropped >= 0 && off.dropped >= 0, `丢帧统计可用（关 ${off.dropped} / 开 ${on.dropped}）`);
    await ev(`const pc = document.getElementById('pn-partial'); pc.checked = true; pc.dispatchEvent(new Event('change', { bubbles: true })); return 1;`);
  }
}

// ==================================================================== 5 收尾
step('5. 收尾');
await ev(`
  try { const t = window.__tools.spiSession.transport; if (t.__origSendPacks) t.sendPacks = t.__origSendPacks; } catch {}
  window.__tools.panel.setPattern('BAR');
  return 1;`);
console.log(`\n${fail === 0 ? '✅' : '❌'} hw-partial: ${pass} 通过 / ${fail} 失败`);
ws.close();
process.exit(fail ? 1 : 0);
