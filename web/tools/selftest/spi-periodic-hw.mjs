/**
 * Read-only real-hardware test for probe-timed SPI acquisition.
 * Requires the probe and a single-wire W25Q64 (WP#/HOLD# pulled high).
 * It repeatedly reads JEDEC ID 0x9F; no write-enable/program/erase command is sent.
 *
 *   node tools/selftest/spi-periodic-hw.mjs
 */
import { Cdp, sleep } from './cdp-lib.mjs';

const CDP = process.env.CDP || 'http://127.0.0.1:9333';
const APP = process.env.APP || 'http://127.0.0.1:8899/index.html';
const WANT = [0xef, 0x40, 0x17]; // W25Q64
let pass = 0, fail = 0;
const ok = (cond, label, extra = '') => {
  if (cond){ pass++; console.log(`  PASS  ${label}`); }
  else { fail++; console.log(`  FAIL  ${label}${extra ? ` — ${extra}` : ''}`); }
};

const cdp = new Cdp(CDP);
await cdp.connect();
try {
  await cdp.send('Page.navigate', { url: APP + '?t=' + Date.now() + '#spi' });
  let ready = false;
  for (let i = 0; i < 80; i++){
    await sleep(250);
    try { if (await cdp.eval('return !!window.__tools?.spiSession;')){ ready = true; break; } } catch {}
  }
  if (!ready) throw new Error('SPI 页面未启动（确认本地网页服务已运行）');
  await cdp.eval(`document.querySelector('#tabs .tab[data-tab="spi"]')?.click(); return true;`);

  console.log('SPI probe 定时采集真机测试（只读 JEDEC ID；不需要 WebUSB bulk 端点）');
  const result = await cdp.json(`(async()=>{
    const s = window.__tools.spiSession;
    const state = { connectedBefore:s.connected, enabledBefore:false, cfg:null, profile:null,
      rows:[], timing:[], cleanupError:null, runError:null };
    try {
      if (!s.connected) await s.connectHid(false);
      if (!s.connected) throw Error('HID 探针未连接或尚未授权');
      await s.loadCfg({ quiet:true }); await s.loadProfile({ quiet:true });
      state.enabledBefore = s.enabled;
      state.cfg = Object.fromEntries(['sclkHz','mode','bits','csPolicy','txDmaThreshold','padDc','padRst','padCsAux','padBl','padActiveLow','padTe','flags','outRingKb','inRingKb','maxFrameBytes','moduleClkHz'].map(k=>[k,s.cfg[k]]));
      state.profile = Object.fromEntries(['profile','defLines','dcActiveHigh','csHoldInStep','qspiWrOpcode','qspiColorOpcode','qspiAddrBytes','flags'].map(k=>[k,s.profile[k]]));
      if (s.enabled) await s.setEnabled(false);
      await s.applyProfile({ ...s.profile, profile:0, defLines:1 });
      await s.applyConfig({ ...s.cfg, mode:0, bits:8, csPolicy:0, sclkHz:1000000 });
      await s.setEnabled(true);

      const P = await import('/app/spi/protocol.js');
      const { BUS } = await import('/app/core/bus-periodic.js');
      const frame = P.frame(P.T.XFER,
        P.xferPayload({ cmd:0x9f, tcfg:P.TC.CMD_EN, rxLen:3 }),
        { flags:P.F.RSP });
      const group = { period:50, count:12, records:[{ kind:BUS.SPI, data:frame }] };
      const t0 = performance.now();
      await s.runPeriodic([group], { onResult:r => state.rows.push({
        cycle:r.cycle, timeMs:r.timeMs, skipped:r.skipped, err:r.err, data:[...r.data],
      }) });
      state.elapsedMs = performance.now() - t0;
      const status = await s.pollStatus(true);
      state.spiStatus = status ? { enabled:s.enabled, active:P.statusWord(status.status).active,
        cs:P.statusWord(status.status).cs, framesErr:status.framesErr } : null;
      const periodic = new (await import('/app/core/bus-periodic.js')).BusPeriodicClient((cmd,data)=>s.hid.xfer(cmd,data));
      state.engineStatus = await periodic.status();
    } catch(e){ state.runError = e?.message || String(e); }
    finally {
      try { if (s._periodic) await s.stopPeriodic(); } catch(e){ state.cleanupError = '停止周期任务：' + (e?.message || e); }
      if (!state.cleanupError && s.connected && state.cfg && state.profile){
        try {
          if (s.enabled) await s.setEnabled(false);
          await s.applyProfile(state.profile);
          await s.applyConfig(state.cfg);
          if (state.enabledBefore) await s.setEnabled(true);
        } catch(e){ state.cleanupError = '恢复 SPI 原配置：' + (e?.message || e); }
      }
      if (!state.connectedBefore && s.connected){
        try { await s.teardown(); } catch(e){ state.cleanupError ||= '释放 HID：' + (e?.message || e); }
      }
    }
    return state;
  })()`);

  if (result.runError) console.log(`  现场错误：${result.runError}`);
  if (result.cleanupError) console.log(`  清理错误：${result.cleanupError}`);
  const idOk = row => row.err === 0 && row.data.join(',') === WANT.join(',');
  const deltas = result.rows.slice(1).map((r,i)=>r.timeMs-result.rows[i].timeMs);
  ok(!result.runError, '周期任务正常启动、采集并停止', result.runError || '');
  ok(result.rows.length === 12, `收到 12 轮探针采样（${result.rows.length}）`);
  ok(result.rows.length === 12 && result.rows.every(idOk),
    '每轮均读回 W25Q64 JEDEC EF 40 17', JSON.stringify(result.rows.slice(0,3)));
  ok(result.rows.length === 12 && result.rows.every(r=>r.skipped === 0), '无迟拍丢周期');
  ok(deltas.length === 11 && deltas.every(n=>n >= 48 && n <= 52),
    `探针时间戳周期约 50 ms（${deltas.slice(0,5).join('/')} ms）`);
  ok(result.spiStatus?.enabled && !result.spiStatus.active && !result.spiStatus.cs && result.spiStatus.framesErr === 0,
    '采集后 SPI 桥仍使能、空闲、CS 已释放且 frames_err=0', JSON.stringify(result.spiStatus));
  ok(result.engineStatus?.active === 0 && result.engineStatus?.queued === 0 &&
     result.engineStatus?.fault === 0 && result.engineStatus?.cleanup === 0,
    'probe 调度器停止、队列清空、无 fault/cleanup', JSON.stringify(result.engineStatus));
  ok(!result.cleanupError, '恢复原 SPI 配置并释放本次 HID 占用', result.cleanupError || '');
  console.log(`  采集耗时 ${result.elapsedMs?.toFixed?.(0) ?? '?'} ms；原 SCLK=${result.cfg?.sclkHz ?? '?'} Hz、原档=${result.profile?.profile ?? '?'}`);
  console.log(`\n结果：${pass} 通过 / ${fail} 失败`);
  if (fail) process.exitCode = 1;
} finally { cdp.close(); }
// The shared CDP helper has bounded call timers that otherwise keep Node alive.
process.exit(fail ? 1 : 0);
