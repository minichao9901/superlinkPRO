/**
 * 「SPI/NOR Flash 测试」卡的**真机回归**（真探针 + 外接 NOR，本机 = W25Q64）：
 *
 *   node tools/selftest/spi-flash-hw.mjs
 *   node tools/selftest/spi-flash-hw.mjs --addr=0x7F0000 --kb=8
 *   node tools/selftest/spi-flash-hw.mjs --keep-going
 * 等价：make spi-flash-hw [ARGS="--addr=… --kb=…"]
 *
 * 为什么要它（2026-10 代码审查 #2）：写测速的擦除序列原来
 *   ① 先单独擦一次 addr、循环里又从 addr+0 擦一遍 → 第一条就让器件忙起来，
 *      后面几条在 BUSY 期间**全被忽略** → 只有扇区 0 真被擦；
 *   ② 每条 SE 之间不等 BUSY。
 * 现象是"回读不一致"，而日志把它归因成「页间等 tPP 太短」，方向完全错。
 *
 * 判据（**故意把区域先弄脏**，否则"本来全是 0xFF"会让旧代码也能蒙过）：
 *   ① 先往第 2 个扇区写 0x00（不是 0xFF）；
 *   ② 跑「写测速」（= 擦 N 个扇区 → 写 → 回读校验）；
 *   ③ 回读必须逐字节等于写测速的图案（`(i*31+7)&0xFF`）—— 第 2 个扇区没被擦就会 AND 出别的值；
 *   ④ 再单独验证"擦除 → 全 0xFF"这条路径。
 *
 * ⚠️ 这是**破坏性**测试：会擦掉 `--addr` 起的 `--kb` KB。默认取 flash 末尾 64 KB
 *    （W25Q64 = 8 MB → 0x7F0000），那块地方不属于任何固件/数据区。
 */
import { Cdp, sleep, DEV_RE } from './cdp-lib.mjs';

const argv = process.argv.slice(2);
const argOf = (n, d) => { const h = argv.find(a => a.startsWith(`--${n}=`)); return h ? h.split('=').slice(1).join('=') : (argv.includes('--' + n) ? true : d); };
const ADDR = String(argOf('addr', '0x7F0000'));
const KB = Number(argOf('kb', 8));
const KEEP = argv.includes('--keep-going');
const APP = process.env.APP || 'http://127.0.0.1:8899/index.html';
const WANT_ID = String(argOf('id', 'EF 40 17'));      // W25Q64 = EF 40 17（W25Q32=EF4016、W25Q128=EF4018）
setTimeout(() => { console.error('[WATCHDOG] 6 分钟'); process.exit(9); }, 360000);

let pass = 0, fail = 0; const failures = [];
const ok = (c, name, extra = '') => {
  if (c){ pass++; console.log('  PASS  ' + name); }
  else { fail++; failures.push(name + (extra ? ' —— ' + extra : '')); console.log('  FAIL  ' + name + (extra ? '  ' + extra : '')); if (!KEEP) throw new Error('判决未通过：' + name); }
};
const hex = a => [...(a || [])].map(x => x.toString(16).padStart(2, '0')).join(' ');

const cdp = new Cdp();
await cdp.connect();
await cdp.send('Page.navigate', { url: APP + '?t=' + Date.now() });
for (let i = 0; i < 100; i++){ await sleep(300); if (await cdp.eval('return !!window.__tools?.spi;').catch(() => false)) break; }
if (!await cdp.eval('return !!window.__tools?.spi;')) throw new Error('页面没起来（8899 + 9333？见 make page-prep）');
await cdp.eval(`document.querySelector('.tab[data-tab="spi"]').click(); await new Promise(r=>setTimeout(r,300)); return true;`);
await cdp.send('Page.bringToFront').catch(() => {});

console.log(`== SPI/NOR Flash 真机回归 ==  写测速区域 ${ADDR} 起 ${KB} KB（破坏性）`);
console.log(`   期望 JEDEC ID：${WANT_ID}`);

/* ---------------- 前置：连接 + 配置（与 spi-hw.mjs 同一套） ---------------- */
await cdp.eval(`(async()=>{ const t = window.__tools;
    try { await t.scope?.releaseProbe?.('flash-hw'); } catch(e){}
    try { await t.rtt?.disconnect?.(); } catch(e){}
    try { await t.hid?.stop?.(); } catch(e){}
    return true; })()`).catch(() => {});
await sleep(400);

await cdp.eval(`(async()=>{ const s = window.__tools.spiSession; if (!s.hid) await s.connectHid(false); })()`);
if (!await cdp.eval(`!!window.__tools.spiSession.hid`)) { await cdp.eval(`document.getElementById('sp-connect').click()`, true); await sleep(2500); }
await cdp.eval(`(async()=>{ const s = window.__tools.spiSession; if (!s.transport) await s.connectUsb(false); })()`);
if (!await cdp.eval(`!!window.__tools.spiSession.transport`)) { await cdp.eval(`document.getElementById('sp-usb').click()`, true); await sleep(2500); }
const conn = await cdp.json(`({ hid: !!window.__tools.spiSession.hid, usb: !!window.__tools.spiSession.transport })`);
ok(conn.hid && conn.usb, `探针两条链路都连上（hid=${conn.hid} usb=${conn.usb}）`);
if (!conn.hid || !conn.usb) { console.log('失败项：' + failures.join('；')); process.exit(1); }

// 档位：raw（通用帧已能表达 RDID/SE/PP）+ 使能
await cdp.eval(`(async()=>{ const s = window.__tools.spiSession;
    const p = document.getElementById('sp-profile'); if (p && p.value !== 'raw'){ p.value='raw'; p.dispatchEvent(new Event('change')); }
    if (!s.enabled) await s.setEnabled(true);
    return true; })()`).catch(e => console.log('   前置配置告警：' + e.message));
await sleep(600);

/* ---------------- 1) 认器件：JEDEC ID ---------------- */
console.log('\n== 1. JEDEC ID（先确认接线与器件）==');
await cdp.eval(`document.getElementById('sp-fl-readid').click()`, true);
await sleep(1200);
const idOut = await cdp.eval(`return (document.getElementById('sp-fl-out').textContent || '').replace(/\\s+/g, ' ').trim();`);
console.log('   ' + idOut.slice(0, 160));
const idHex = (idOut.match(/(?:ID|JEDEC)[^0-9A-Fa-f]*((?:[0-9A-Fa-f]{2}[ -]){2}[0-9A-Fa-f]{2})/) || [])[1];
const idNorm = (idHex || '').toUpperCase().replace(/-/g, ' ').trim();
ok(!!idHex && idNorm === WANT_ID, `读到的 JEDEC ID = ${idNorm || '(没解析出来)'}（期望 ${WANT_ID}）`, idOut.slice(0, 200));

/* ---------------- 2) 先把第 2 个扇区弄脏 ---------------- */
console.log('\n== 2. 先把第 2 个扇区写成 0x00（让"没擦干净"无处可藏）==');
const dirty = await cdp.json(`(async () => {
    const FL = await import('/app/spi/flash.js');
    const s = window.__tools.spiSession;
    const addr = ${JSON.stringify(ADDR)} ? Number(${JSON.stringify(ADDR)}) : 0;
    const sec2 = FL.sectorOf(addr) + FL.SECTOR_SIZE;
    const data = new Uint8Array(256).fill(0x00);
    await s.sendFrames(FL.eraseItems(sec2, { opcode: FL.OP.SE }), { quiet: true });
    const w1 = await window.__tools.spi.flWaitReady(20000);      // 🚨 扇区擦除要 45~400 ms，不等就白写
    if (!w1.ok) return { err: '擦除等 BUSY 超时' };
    await s.sendFrames(FL.programItems(sec2, data, { pageDelayMs: 3 }), { quiet: true });
    const w2 = await window.__tools.spi.flWaitReady(20000);      // 页编程 ~0.7 ms，也要等
    if (!w2.ok) return { err: '编程等 BUSY 超时' };
    const back = (await window.__tools.spi.flRead(16, { addr: sec2, quiet: true })).bytes;
    return { sec2, data: back ? [...back] : null };
  })()`).catch(e => ({ err: String(e.message) }));
ok(dirty.data && dirty.data.every(b => b === 0), `第 2 个扇区已写成 0x00（0x${(dirty.sec2 || 0).toString(16)} 读回 ${hex(dirty.data)}）`, JSON.stringify(dirty).slice(0, 200));

/* ---------------- 3) 写测速（= 擦 N 扇区 → 写 → 回读校验）---------------- */
console.log(`\n== 3. 写测速：擦 0x${ADDR} 起 ${KB} KB → 写 → 回读校验 ==`);
const bench = await cdp.json(`(async () => {
    const $ = id => document.getElementById(id);
    window.confirm = () => true;                       // 脚本自动确认破坏性操作
    $('sp-fl-armed').checked = true;
    $('sp-fl-addr').value = ${JSON.stringify(ADDR)};
    $('sp-fl-benchkb').value = '${KB}';
    const out0 = $('sp-fl-out').textContent;
    await window.__tools.spi.flWriteBench();
    await new Promise(r => setTimeout(r, 300));
    return { out: $('sp-fl-out').textContent, busy: !!window.__tools.spiSession.busy, was: out0 };
  })()`).catch(e => ({ err: String(e.message) }));
const benchTxt = String(bench.out || '').replace(/\s+/g, ' ').trim();
console.log('   ' + benchTxt.slice(0, 200));
ok(!bench.err && /一致/.test(benchTxt), '写测速回读一致（擦除覆盖了全部扇区）', (bench.err || benchTxt).slice(0, 200));

/* ---------------- 4) 自己再读一遍整个区域对账 ---------------- */
console.log('\n== 4. 独立回读整个区域（不信 UI 的自证）==');
const verify = await cdp.json(`(async () => {
    const FL = await import('/app/spi/flash.js');
    const s = window.__tools.spiSession;
    const addr = Number(${JSON.stringify(ADDR)}), n = ${KB} * 1024;
    const want = new Uint8Array(n); for (let i = 0; i < n; i++) want[i] = (i * 31 + 7) & 0xff;
    const got = new Uint8Array((await window.__tools.spi.flRead(n, { addr, quiet: true })).bytes);
    let bad = -1; for (let i = 0; i < n; i++){ if (got[i] !== want[i]){ bad = i; break; } }
    const secs = [];
    for (let a = FL.sectorOf(addr); a <= FL.sectorOf(addr + n - 1); a += FL.SECTOR_SIZE) secs.push('0x' + a.toString(16));
    return { n, bad, got0: [...got.subarray(0, 16)], secs };
  })()`).catch(e => ({ err: String(e.message) }));
ok(!verify.err && verify.bad === -1,
  `逐字节对账通过：${KB} KB / ${(verify.secs || []).length} 个扇区（${(verify.secs || []).join(' ')}）`,
  verify.err || `第 +${verify.bad} 字节起不一致（读到 ${hex(verify.got0)}）`);

/* ---------------- 5) 擦除路径：擦完必须全 0xFF ---------------- */
console.log('\n== 5. 擦除 → 全 0xFF（扇区对齐）==');
const erased = await cdp.json(`(async () => {
    const FL = await import('/app/spi/flash.js');
    const s = window.__tools.spiSession;
    const addr = Number(${JSON.stringify(ADDR)}), n = ${KB} * 1024;
    const last = FL.sectorOf(addr + n - 1);
    for (let a = FL.sectorOf(addr); a <= last; a += FL.SECTOR_SIZE){
      await s.sendFrames(FL.eraseItems(a, { opcode: FL.OP.SE }), { quiet: true });
      const w = await window.__tools.spi.flWaitReady(20000);    // 🚨 每条 SE 之后都要等 BUSY 清
      if (!w.ok) return { err: '0x' + a.toString(16) + ' 擦除等 BUSY 超时' };
    }
    const got = new Uint8Array((await window.__tools.spi.flRead(n, { addr, quiet: true })).bytes);
    let ff = true; for (const b of got) if (b !== 0xff){ ff = false; break; }
    return { ff, got0: [...got.subarray(0, 16)] };
  })()`).catch(e => ({ err: String(e.message) }));
ok(!erased.err && erased.ff, `擦完 ${KB} KB 全是 0xFF（读回头 16 B = ${hex(erased.got0)}）`, erased.err || hex(erased.got0));

console.log(`\n== 汇总：${pass} 通过 / ${fail} 失败 ==`);
if (failures.length){ console.log('失败项：'); for (const f of failures) console.log('  · ' + f); }
try { await cdp.eval(`(async()=>{ try { await window.__tools.spiSession.setEnabled(false); } catch(e){} return true; })()`); } catch {}
cdp.close();
process.exit(fail ? 1 : 0);
