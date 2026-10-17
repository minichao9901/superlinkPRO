/**
 * 「USB→I2C」页的**真机冒烟**（真探针 + 真 I2C 器件）：
 *   node tools/selftest/i2c-hw.mjs                          # 默认 0x50，只读（安全）
 *   node tools/selftest/i2c-hw.mjs --write                  # 额外做"页写 → 回读 → 还原"（会动 EEPROM）
 *   node tools/selftest/i2c-hw.mjs --dev=0x68               # 换器件地址（写测试只对 EEPROM 有意义）
 *   node tools/selftest/i2c-hw.mjs --addr=0x20 --pattern=A5,5A,DE,AD,BE,EF,12,34
 * 等价：make i2c-hw [ARGS="--write"]
 *
 * 前置（page-prep 会做）：8899 静态服务 + 9333 CDP 浏览器，且**探针已授权给这个 profile 的 WebHID**。
 *
 * ⚠️ 三条纪律（都踩过）：
 *   ① 一个探针同时只能被一个页签占着 —— 连之前先请别的页签让出（probeBus.requestRelease），
 *      探针被别处占着时报 "Unable to claim interface" 要**如实报错**，绝不假装成功；
 *   ② EEPROM 写要等 tWR（≤5 ms）—— 写完立刻读会拿到 NACK，那不是"桥坏了"；
 *   ③ **写测试默认不做**：先把原内容读出来存好，写完必须还原并回读对账，任何一步失败都要说清。
 */
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');
const CDP = process.env.CDP || 'http://127.0.0.1:9333';
const APP = process.env.APP || 'http://127.0.0.1:8899/index.html';

const argv = process.argv.slice(2);
const argOf = (name, def) => {
  const hit = argv.find(a => a === `--${name}` || a.startsWith(`--${name}=`));
  if (!hit) return def;
  const eq = hit.indexOf('=');
  return eq < 0 ? true : hit.slice(eq + 1);
};
const DEV = parseInt(String(argOf('dev', '0x50')).replace(/^0x/i, ''), 16);
const ADDR = parseInt(String(argOf('addr', '0x00')).replace(/^0x/i, ''), 16);
const DO_WRITE = !!argOf('write', false);
const PATTERN = String(argOf('pattern', 'A5,5A,DE,AD,BE,EF,12,34'))
  .split(/[\s,]+/).filter(Boolean).map(x => parseInt(x.replace(/^0x/i, ''), 16) & 0xff);

setTimeout(() => { console.error('[WATCHDOG] 总超时'); process.exit(9); }, 180000);
const sleep = ms => new Promise(r => setTimeout(r, ms));

let pass = 0, fail = 0;
const ok = (cond, name, extra = '') => {
  if (cond){ pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name} ${extra}`); }
};
const hex = a => [...a].map(x => x.toString(16).padStart(2, '0').toUpperCase()).join(' ');

async function ensureBrowser(){
  try { await fetch(CDP + '/json/version', { signal: AbortSignal.timeout(2500) }); return; } catch {}
  console.log('  （CDP 浏览器没在跑，自己拉一个…）');
  const ps = spawn('pwsh', ['-NoProfile', '-File', join(root, 'tools', 'selftest', 'launch-browser.ps1'), '-Port', '9333', '-Url', APP],
    { stdio: 'ignore', detached: true });
  ps.unref();
  for (let i = 0; i < 90; i++){
    await sleep(500);
    try { await fetch(CDP + '/json/version', { signal: AbortSignal.timeout(2000) }); return; } catch {}
  }
  throw new Error('等 CDP 浏览器超时');
}

await ensureBrowser();
const list = await (await fetch(CDP + '/json/list', { signal: AbortSignal.timeout(5000) })).json();
const page = list.find(t => t.type === 'page');
if (!page) throw new Error('CDP 里没有页面目标');

const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('CDP 连不上')); });
let seq = 0; const pend = new Map();
ws.onmessage = e => {
  const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)){ const p = pend.get(m.id); pend.delete(m.id); m.error ? p.rej(new Error(m.error.message)) : p.res(m.result); }
};
const send = (method, params = {}, t = 40000) => new Promise((res, rej) => {
  const id = ++seq; pend.set(id, { res, rej });
  ws.send(JSON.stringify({ id, method, params }));
  setTimeout(() => { if (pend.delete(id)) rej(new Error(method + ' 超时')); }, t);
});
async function ev(expr){
  const r = await send('Runtime.evaluate', { expression: `(async()=>{ ${expr} })()`, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error('页面里报错：' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
  return r.result.value;
}

await send('Page.enable');
await send('Runtime.enable');
try { await send('Network.enable'); await send('Network.setCacheDisabled', { cacheDisabled: true }); } catch {}
await send('Page.navigate', { url: APP + '?t=' + Date.now() + '#i2c' });
console.log(`目标: ${APP}#i2c  器件=0x${DEV.toString(16).toUpperCase()}  写测试=${DO_WRITE ? '开' : '关'}\n`);

let ready = false;
for (let i = 0; i < 60; i++){
  await sleep(500);
  try { if (await ev('return !!window.__tools?.i2c;')) { ready = true; break; } } catch {}
}
if (!ready) throw new Error('页面没起来（__tools.i2c 不存在）');

// ==================================================================== 1
console.log('== 1. 连真探针（非交互：用浏览器已授权的 WebHID 设备）==');
{
  const r = await ev(`
    const t = window.__tools.i2c;
    if (t.runner.running) t.runner.stop();
    await t.bus?.requestRelease?.({ why: '真机冒烟要占用探针' });
    await new Promise(r => setTimeout(r, 400));
    document.getElementById('i2-mock').checked = false;
    const ok = await t.session.connect(false, { mock: false });
    await new Promise(r => setTimeout(r, 200));
    if (ok && !t.session.enabled) await t.session.setEnabled(true);
    // 使能之后再读一次配置：未使能时固件回 actual_scl_hz = 0（真机实测），那不代表读失败
    const cfg = await t.session.loadCfg({ quiet: true });
    return { ok, connected: t.session.connected, enabled: t.session.enabled,
             label: t.session.hid?.label || '', lost: t.session.lost, cfg };`);
  if (!r.connected){
    console.error('\n❌ 连不上真探针。最常见的原因：**这个 profile 还没把探针授权给 WebHID**。');
    console.error('   请在这个浏览器窗口里打开「USB→I2C」页，手点一次「连接探针（授权）」并在弹框里选 akaLinkPro，然后重跑。');
    console.error('   （另一个常见原因：别的程序/页签占着探针的 HID —— 关掉再试。）');
    process.exit(2);
  }
  ok(r.connected, `真探针已连接：${r.label}`);
  ok(r.enabled, '桥已使能');
  ok(r.cfg && r.cfg.actualSclHz > 0, `读到桥配置（实际 SCL ${(r.cfg?.actualSclHz || 0) / 1000} kHz）`);
  if (r.cfg && r.cfg.actualSclHz !== 100000){
    const c = await ev(`return (await window.__tools.i2c.session.applyCfg({ sclHz: 100000, pullup: 1, retries: 0 }));`);
    ok(c && c.actualSclHz === 100000, '收敛到 100 kHz + 内部上拉（真机冒烟固定这一档，结果可比）');
  } else {
    const c = await ev(`return (await window.__tools.i2c.session.applyCfg({ sclHz: 100000, pullup: 1, retries: 0 }));`);
    ok(c && c.pullup === 1, '写了一次配置：100 kHz + 内部上拉');
  }
}

// ==================================================================== 2
console.log('== 2. 接线自检（PINTEST）：先证明"桥这一侧没问题" ==');
{
  const pt = await ev(`return (await window.__tools.i2c.session.pinTest());`);
  if (!pt){ ok(false, 'PINTEST 被拒（总线忙？稍后重试）'); }
  else {
    console.log(`    空闲 SDA=${pt.idleSda} SCL=${pt.idleScl} · 开内部上拉后 SDA=${pt.pullupSda} SCL=${pt.pullupScl} · 事务中曾拉低 SCL=${pt.droveScl} SDA=${pt.droveSda}`);
    ok(pt.droveScl === 1, '🚨 硬证据：事务中我们的 SCL 确实在驱动总线（桥侧 OK）');
    ok(pt.idleSda === 1 && pt.idleScl === 1, '空闲时 SDA/SCL 都被上拉拉高了（没被拽死）');
    if (pt.problems.length) console.log('    ⚠️ 问题位图：' + pt.problems.join('；'));
    ok(pt.bridgeOk, 'bit16=1 且问题位图=0 ⇒ 桥侧正常，没 ACK 就该往器件侧查');
  }
}

// ==================================================================== 3
console.log('== 3. 扫描总线 ==');
{
  const s = await ev(`return (await window.__tools.i2c.session.scan());`);
  const list = s.addrs.map(a => '0x' + a.toString(16).toUpperCase().padStart(2, '0'));
  console.log(`    扫到：${list.join(' ') || '（空）'}  （${s.ms.toFixed(0)} ms）`);
  ok(s.addrs.includes(DEV), `目标器件 0x${DEV.toString(16).toUpperCase()} 在扫描结果里`);
  ok(s.addrs.length >= 1, `总线上至少有 1 个器件（${s.addrs.length} 个）`);
}

// ==================================================================== 4
console.log('== 4. 只读事务 ==');
let baseline = null;
let BASE = {};
{
  // 计数器是探针侧累计的，先记个基线（收尾时按"本次新增"判）
  BASE = await ev(`
    await window.__tools.i2c.session.readStatus({ quiet: true });
    const c = window.__tools.i2c.session.counters;
    return { framesOk: c.framesOk, framesErr: c.framesErr };`);
  const r = await ev(`
    const t = window.__tools.i2c;
    const ping = await t.session.transaction({ dev:${DEV}, addr:[], wr:[], rd:0 }, { quiet:true });
    const rd = await t.session.transaction({ dev:${DEV}, addr:[${ADDR}], wr:[], rd:8 }, { quiet:true });
    return { pingErr: ping.err, rdErr: rd.err, data: [...rd.data], ms: rd.ms };`);
  ok(r.pingErr === 0, '地址探测（只问 ACK）成功');
  ok(r.rdErr === 0, `读 0x${ADDR.toString(16).toUpperCase()} 起 8 B 成功`);
  ok(r.data.length === 8, `确实读回 8 B：${hex(r.data)}（${r.ms.toFixed(1)} ms）`);
  baseline = r.data;

  // 分片读（地址指针自增法）：先零长度写推指针，再连读
  const frag = await ev(`
    const t = window.__tools.i2c;
    const setp = await t.session.transaction({ dev:${DEV}, addr:[${ADDR}], wr:[], rd:0 }, { quiet:true });
    const a = await t.session.transaction({ dev:${DEV}, addr:[], wr:[], rd:16 }, { quiet:true });
    return { setpErr: setp.err, err: a.err, data: [...a.data] };`);
  ok(frag.setpErr === 0, '零长度写（设地址指针）成功 —— EEPROM 分片读就靠这一笔');
  ok(frag.err === 0 && frag.data.length === 16, `接着续读 16 B 成功：${hex(frag.data)}`);
  ok(frag.data.slice(0, 8).join(',') === baseline.join(','), '首 8 B 与带子地址读到的**逐字节一致**（分片法没读错位）');

  // 🚨 长读自动分片：填 256，外部只看到一条命令、一行日志（分片是实现细节）
  const longRead = await ev(`
    const t = window.__tools.i2c;
    const proto = Object.getPrototypeOf(t.session);
    const realLog = proto.log.bind(t.session);      // 前面几节可能打桩静音过，这里要数日志
    t.session.log = realLog;
    t.session.ring.length = 0;
    const r = await t.session.readLong({ dev:${DEV}, addr:[${ADDR}], rd:256 });
    t.session.log = () => {};
    return { err: r.err, n: r.data.length, chunks: r.chunks, mode: r.mode, ms: r.ms,
             ok1: r.data[0], ok2: r.data[1],
             logs: t.session.ring.filter(e => e.kind === 'ok').map(e => e.text) };`);
  ok(longRead.err === 0, `256 B 长读成功（分 ${longRead.chunks} 笔，${longRead.ms?.toFixed?.(0)} ms）`);
  ok(longRead.n === 256 && longRead.chunks === 5, '自动分成 5 笔、拼回 256 B');
  ok(longRead.mode === 'reset', '默认走「每片重发子地址」（对 EEPROM / 寄存器型都成立）');
  ok(longRead.ok1 === baseline[0] && longRead.ok2 === baseline[1], '长读首两字节与短读一致（拼接没串位）');
  ok(longRead.logs.length === 1, `🚨 真机上日志也只出一行（实际 ${longRead.logs.length} 行）`, JSON.stringify(longRead.logs));
  ok(/256 B/.test(longRead.logs[0] || '') && /分 5 笔/.test(longRead.logs[0] || ''), '……那一行说清总长与笔数', longRead.logs[0]);

  // 不存在的地址必须报 NACK（证明错误路径是活的，不是"什么都回成功"）
  const bad = await ev(`return (await window.__tools.i2c.session.transaction({ dev:0x21, addr:[0], wr:[], rd:1 }, { quiet:true })).err;`);
  ok(bad === 3, '探测不存在的 0x21 → E_NO_ADDR(3)（错误路径是活的）', String(bad));
}

// ==================================================================== 5
console.log(`== 5. 写 + 回读对账（${DO_WRITE ? '带还原' : '默认跳过'}）==`);
if (!DO_WRITE){
  console.log('    （跳过：写会改动 EEPROM 内容。要跑加 --write —— 脚本会先读原值、写完还原并回读对账）');
} else {
  const w = await ev(`
    const t = window.__tools.i2c;
    const orig = await t.session.transaction({ dev:${DEV}, addr:[${ADDR}], wr:[], rd:${PATTERN.length} }, { quiet:true });
    if (orig.err !== 0) return { step:'read-orig', err: orig.err };
    const wr = await t.session.transaction({ dev:${DEV}, addr:[${ADDR}], wr:[${PATTERN.join(',')}], rd:0 }, { quiet:true });
    if (wr.err !== 0) return { step:'write', err: wr.err };
    // 🚨 tWR（≤5 ms）期间器件连地址都不 ACK —— 必须等，不是桥坏了
    const quick = await t.session.transaction({ dev:${DEV}, addr:[${ADDR}], wr:[], rd:${PATTERN.length} }, { quiet:true });
    await new Promise(r => setTimeout(r, 12));
    const back = await t.session.transaction({ dev:${DEV}, addr:[${ADDR}], wr:[], rd:${PATTERN.length} }, { quiet:true });
    // 还原
    const re = await t.session.transaction({ dev:${DEV}, addr:[${ADDR}], wr:[...orig.data], rd:0 }, { quiet:true });
    await new Promise(r => setTimeout(r, 12));
    const check = await t.session.transaction({ dev:${DEV}, addr:[${ADDR}], wr:[], rd:${PATTERN.length} }, { quiet:true });
    return { step:'done', orig: [...orig.data], quickErr: quick.err, quick: [...quick.data],
             backErr: back.err, back: [...back.data],
             reErr: re.err, checkErr: check.err, check: [...check.data] };`);
  if (w.step !== 'done'){
    ok(false, `写测试在「${w.step}」这一步失败（err=${w.err}）`);
  } else {
    console.log(`    原值：${hex(w.orig)}`);
    // ⚠️ 真机上**不能**假定"写完立刻读一定 NACK"：本机这块 AT24C02 模块实测
    //    事务往返（~1 ms）之内写周期就结束了（或者它压根不等 tWR），立刻读回的是
    //    ACK + 旧内容。所以这里只如实记录，不拿它当判据 —— 判据是"等够时间后回读对不对"。
    console.log(w.quickErr === 3
      ? '    （写完立刻读被 NACK —— 这块片子等 tWR，符合数据手册）'
      : `    （写完立刻读**没有** NACK（err=${w.quickErr}，读到 ${hex(w.quick || [])}）—— 本机这块模块不体现 tWR，别把"必须 NACK"写进代码）`);
    ok(w.backErr === 0 && hex(w.back) === hex(PATTERN), `写完等 12 ms 回读 → 逐字节对得上（${hex(w.back)}）`);
    ok(w.reErr === 0 && w.checkErr === 0 && hex(w.check) === hex(w.orig), `还原成功（读回 ${hex(w.check)}）`);
  }
}

// ==================================================================== 5b
/**
 * 5b) **跨页写**真机回归（2026-10 代码审查 #1 的现场钉子）。
 *
 * 上面那条 8 B 写是从 `--addr`（默认 0）起的、**正好页对齐**，所以打不到这个 bug。
 * 真正的坑：EEPROM 的页写在器件内部**按页回卷** —— 起始地址不是页倍数时，
 * 越过页界的那几个字节绕回**本页页首**，盖掉刚写的，而器件全程老实 ACK、页面还报"写成功"。
 * 修法是在 `planWrite` 里给 `pageSize`：每片收窄到不跨页（AT24C02 = 8 B）。
 *
 * 🚨 这段的"反证"**会真的写花**（2026-10 本机实测：不给 pageSize 时 `A5 5A DE` 写在 0x05..0x07、
 *    剩下的 `AD BE EF 12 34` 绕回写进 **0x00..0x04**）—— 所以必须先**快照整页**、结束时还原整页，
 *    只按"0x05 起的 8 B"读回是看不出来的（那一截是对的）。第一版就是这么把自己坑了：
 *    反证写花 0x00..0x04 却没还原，后面 §6 解出来的 b0 从 0xA5 变成 0xAD。
 */
console.log('== 5b. 跨页写（起始地址不是页倍数）==');
if (!DO_WRITE){
  console.log('    （跳过：需要写 EEPROM，加 --write）');
} else {
  const PAGE = Number(String(argOf('page') ?? '8'));       // AT24C02 = 8 B
  const cross = await ev(`
    const t = window.__tools.i2c, addr = ${ADDR} + 5, n = ${PATTERN.length}, page = ${PAGE};
    const pageStart = addr - (addr % page);
    const rdPage = () => t.session.transaction({ dev:${DEV}, addr:[pageStart], wr:[], rd:page }, { quiet:true });
    const rdOne = a => t.session.transaction({ dev:${DEV}, addr:[a], wr:[], rd:${PATTERN.length} }, { quiet:true });
    const snapshot = await rdPage();
    if (snapshot.err !== 0) return { step:'read-page', err: snapshot.err };
    const orig = await rdOne(addr);
    if (orig.err !== 0) return { step:'read-orig', err: orig.err };
    // ① 按页对齐写（正式路径）：每片不许跨页
    const good = await t.session.writeLong({ dev:${DEV}, addr:[addr],
        data: Uint8Array.from([${PATTERN.join(',')}]), chunkMax: ${PATTERN.length}, pageSize: page, gapMs: 12 }, { quiet:true });
    await new Promise(r => setTimeout(r, 12));
    const back = await rdOne(addr);
    // ② 反证（只为观察，不判）：不给 pageSize —— 会绕回写花本页页首
    const bad = await t.session.writeLong({ dev:${DEV}, addr:[addr],
        data: Uint8Array.from([${PATTERN.join(',')}]), chunkMax: ${PATTERN.length}, gapMs: 12 }, { quiet:true });
    await new Promise(r => setTimeout(r, 12));
    const badBack = await rdOne(addr);
    const afterBad = await rdPage();
    // ③ 还原**整页**（只还原 0x05 起那段会把绕回写花的部分留下 —— 见上面 🚨）
    await t.session.writeLong({ dev:${DEV}, addr:[pageStart], data: Uint8Array.from(snapshot.data),
        chunkMax: page, pageSize: page, gapMs: 12 }, { quiet:true });
    await new Promise(r => setTimeout(r, 12));
    const restored = await rdPage();
    return { step:'done', addr, pageStart,
             snap:[...snapshot.data], orig:[...orig.data], goodErr: good.err, chunks: good.chunks,
             backErr: back.err, back:[...back.data], badErr: bad.err, badBack:[...badBack.data],
             afterBad:[...afterBad.data], restoredErr: restored.err, restored:[...restored.data] };`);
  if (cross.step !== 'done'){
    ok(false, `跨页写在「${cross.step}」这一步失败（err=${cross.err}）`);
  } else {
    const want = PATTERN.join(',');
    const ps = cross.pageStart;
    /** 取前 PAGE 个字节（page 侧回的是数组；这里做一次归一，免得形状不对时报个看不懂的错） */
    const head = (arr) => Array.from(arr ?? []).slice(0, PAGE).map(Number);
    const slice = (arr) => head(arr).join(',');
    console.log(`    起始 0x${cross.addr.toString(16)}（页 0x${ps.toString(16)} 起，页大小 ${PAGE}）→ 实得 ${cross.chunks} 片`);
    console.log(`    整页快照 ${hex(head(cross.snap))}  类型 ${Array.isArray(cross.snap) ? 'array' : typeof cross.snap}`);
    ok(cross.goodErr === 0 && cross.chunks >= 2, `页对齐写被切成多片（${cross.chunks} 片）`);
    ok(cross.backErr === 0 && cross.back.join(',') === want,
      `🚨 跨页写完回读逐字节对得上（${hex(cross.back)}）`, `期望 ${want}`);
    const wrapped = slice(cross.afterBad) !== slice(cross.snap);
    console.log(wrapped
      ? `    ✅ 反证（不给页大小）：整页确实被写花 → ${hex(head(cross.afterBad))}（页首被绕回的数据盖掉了）`
      : '    （反证：这块模块不给页大小也没花 —— 有些模块内部按字节写；离线假器件仍会花）');
    ok(cross.restoredErr === 0 && slice(cross.restored) === slice(cross.snap),
      `还原整页成功（读回 ${hex(head(cross.restored))}）`);
  }
}

// ==================================================================== 6
console.log('== 6. 页面上跑一段 while(1) 定时读（真机）==');
{
  // 脚本内容在 Node 侧拼好再送进去：直接在页面表达式里做字符串拼接极易踩到
  // `80.toString(16)` 这种"数字后跟点"的语法坑（实测报 Invalid or unexpected token）
  const devHex = '0x' + DEV.toString(16).toUpperCase();
  const addrHex = '0x' + ADDR.toString(16).toUpperCase().padStart(2, '0');
  const script = `# 真机冒烟：每 100 ms 读一次 ${devHex} 的 ${addrHex} 起 8 B\nloop 100ms\n  rd ${devHex} ${addrHex} 8 as b0=u8(0), b1=u8(1)\nend\n`;
  const r = await ev(`
    const t = window.__tools.i2c;
    t.session.log = () => {};
    const hid = t.session.hid, trace = [], events = [];
    const xfer = hid.xfer.bind(hid);
    hid.xfer = async (cmd, data, ...rest) => {
      const item = { cmd, action: data?.[0] };
      try { const out = await xfer(cmd, data, ...rest); item.res = [...out].slice(0, 24); return out; }
      catch (e){ item.error = String(e?.message || e); throw e; }
      finally { trace.push(item); }
    };
    const emit = t.runner._emit.bind(t.runner);
    t.runner._emit = e => { if (['error','tick','end','phase'].includes(e.type)) events.push({ type:e.type, error:e.error, why:e.why, n:e.n }); emit(e); };
    document.getElementById('i2-dsl-text').value = ${JSON.stringify(script)};
    document.getElementById('i2-dsl-parse').click();
    await new Promise(r => setTimeout(r, 200));
    const sum = document.getElementById('i2-dsl-sum').textContent;
    const errRows = document.querySelectorAll('#i2-dsl-err tr').length;
    document.getElementById('i2-dsl-run').click();
    await new Promise(r => setTimeout(r, 2200));
    const running = t.runner.running;
    const pill = document.getElementById('i2-run-pill').textContent;
    const e = t.live.get('b0');
    const ticks = t.runner.stats?.ticks || 0;
    const errors = t.runner.stats?.errors || 0;
    // 切到「实时值」tab 再停 —— 顺便验证 tab 化之后"切走不打断、胶囊还在"
    document.querySelector('#i2-dock-tabs button[data-dock="live"]').click();
    await new Promise(r => setTimeout(r, 400));
    const stillRunning = t.runner.running;
    const pillAfterSwitch = document.getElementById('i2-run-pill').textContent;
    document.getElementById('i2-run-stop').click();
    await new Promise(r => setTimeout(r, 500));
    return { sum, errRows, running, pill, stillRunning, pillAfterSwitch, n: e ? e.n : 0,
             last: e ? e.last : null, ticks, errors, afterStop: t.runner.running,
             trace: trace.slice(-24), events, lost:t.session.lost,
             periodic: !!t.session._periodic, stopError:t.session._periodic?.client.stopError?.message || null,
             pending: !!t.session.hid._pending };`);
  if (r.n < 12 || r.trace.some(x => x.error) || r.events.some(x => x.type === 'error'))
    console.log(`    诊断：${JSON.stringify({ trace:r.trace, events:r.events, lost:r.lost,
      periodic:r.periodic, stopError:r.stopError, pending:r.pending })}`);
  ok(r.errRows === 0, '生成的脚本零语法错');
  ok(/1 个循环任务/.test(r.sum), '脚本解析出 1 个循环任务', r.sum);
  ok(r.running === true, '真机上 while(1) 跑起来了');
  ok(r.n >= 12, `2.2 s 内按 100 ms 采了 ${r.n} 次（应当 ≥12）`);
  ok(r.stillRunning === true && /运行中/.test(r.pillAfterSwitch), '切到别的 tab 不打断、运行胶囊照样看得到', r.pillAfterSwitch);
  ok(r.errors === 0, `循环期间零失败（errors=${r.errors}）`);
  ok(r.last === (baseline ? baseline[0] : r.last), `解出来的 b0 = ${r.last}，与第 4 步读到的首字节一致`);
  ok(r.afterStop === false, '点 tab 栏的「停止」后真的停了');
}

// ==================================================================== 7
console.log('== 7. 收尾 ==');
{
  const s = await ev(`
    const t = window.__tools.i2c;
    let error = null;
    try { await t.session.readStatus({ quiet: true }); } catch(e){ error = e.message; }
    return { counters: t.session.counters, status: t.session.status, error };`);
  if (s.error) console.log(`    收尾读状态失败：${s.error}`);
  const c = s.counters;
  console.log(`    计数器：ok=${c.framesOk} err=${c.framesErr} tx=${c.bytesTx}B rx=${c.bytesRx}B ` +
    `nack地址=${c.nackAddr} 超时=${c.timeouts} 单笔=${(c.lastTicks / 24).toFixed(0)}µs`);
  // ⚠️ 计数器是**探针侧累计**的（只有 RESET 才清零），所以要按"本次新增"判，
  //    不能按绝对值 —— 上一轮跑过的错误会留下来（踩过：以为这轮多了 2 笔失败）。
  const dErr = c.framesErr - (BASE.framesErr || 0);
  const dOk = c.framesOk - (BASE.framesOk || 0);
  ok(dErr <= 1, `本次新增失败事务 ≤1 笔（实际 +${dErr}，就是故意探测的那个 0x21）`);
  ok(dOk >= 10, `本次新增成功事务 ${dOk} 笔（扫描 + 读写 + 循环采样）`);
  const err = await ev('return window.__tools.summary().errors;');
  ok(err.length === 0, '整场跑完页面无未捕获错误', JSON.stringify(err));
  // 把桥恢复成"没使能、100 kHz、上拉关"的默认样子（别给用户留个奇怪的现场），
  // 右列 tab 也拨回「扫描总线」（它是持久化设置，留着会让下一个套件的前置变脏）
  await ev(`
    const t = window.__tools.i2c;
    document.querySelector('#i2-dock-tabs button[data-dock="scan"]')?.click();
    await t.session.applyCfg({ sclHz: 100000, pullup: 0, retries: 0 });
    await t.session.setEnabled(false);
    await t.session.disconnect();
    return true;`);
  console.log('    （已把桥恢复成未使能 + 100 kHz + 上拉关，右列拨回「扫描总线」，并放掉 HID）');
}

console.log(`\n${fail ? '❌' : '✅'} i2c-hw: ${pass} 通过 / ${fail} 失败`);
ws.close();
process.exit(fail ? 1 : 0);
