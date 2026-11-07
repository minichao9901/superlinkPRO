import assert from 'node:assert/strict';
import { mkdir, writeFile, unlink, rmdir } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { Cdp, sleep } from '../../../web/tools/selftest/cdp-lib.mjs';

const [elf, output, clockKhz = '1000', cfg, wrongCfg] = process.argv.slice(2);
assert.ok(elf && output, 'Usage: node hardware.mjs RAM.elf NEW_OUTPUT.json [clock_kHz] [target_or_board.cfg] [wrong_id.cfg]');
const c = new Cdp(process.env.CDP);
const lock = resolve('../web-etm-trace-coordination/hardware.lock');
const evidence = {};
let acquired = false, backedUp = false, started = false;

async function waitFor(expression, timeout = 12000){
  const until = Date.now() + timeout;
  do {
    if (await c.eval(`return !!(${expression});`)) return;
    await sleep(100);
  } while (Date.now() < until);
  const log = await c.eval("return document.querySelector('#d-out').innerText.slice(-1600);");
  throw new Error(`Page did not reach expected state: ${expression}\n${log}`);
}

async function upload(selector, path){
  const {root} = await c.send('DOM.getDocument');
  const {nodeId} = await c.send('DOM.querySelector', {nodeId:root.nodeId, selector});
  await c.send('DOM.setFileInputFiles', {nodeId, files:[resolve(path)]});
}

try {
  await mkdir(dirname(lock), {recursive:true});
  await mkdir(lock);
  acquired = true;
  await writeFile(lock + '/owner.json', JSON.stringify({owner:'ram-debug-f103ze', pid:process.pid}), {flag:'wx'});
  await c.connect();
  evidence.bundle = await c.eval('return window.__useWebBundle;');
  await c.eval(`
    const devices = (await navigator.usb.getDevices()).filter(d => d.vendorId === 0x0d28 && d.productId === 0x0204);
    if (devices.length !== 1) throw new Error('Exactly one previously authorized akaLinkPro is required');
    const d = window.__tools.dbg;
    if (d.session.connected) throw new Error('Debug page must start disconnected');
    document.querySelector('#d-backend').value = 'webusb';
    document.querySelector('#d-clock').value = ${JSON.stringify(cfg ? '10000' : clockKhz)};
  `);
  if (wrongCfg){
    await upload('#d-cfg-file', wrongCfg);
    await waitFor('window.__tools.dbg.openOcdConfig && !window.__tools.dbg._cfgImporting');
    await c.eval("document.querySelector('#d-connect').click();");
    await waitFor("!window.__tools.dbg._connecting && document.querySelector('#d-out').innerText.includes('实际目标为')");
    evidence.wrongDpidrRejected = await c.eval('return !window.__tools.dbg.session.connected;');
    assert.ok(evidence.wrongDpidrRejected);
    await c.eval("document.querySelector('#d-cfg-clear').click();");
    assert.ok(await c.eval("return !window.__tools.dbg.openOcdConfig && !document.querySelector('#d-clock-row').hidden;"));
  }
  if (cfg){
    await upload('#d-cfg-file', cfg);
    await waitFor('window.__tools.dbg.openOcdConfig && !window.__tools.dbg._cfgImporting');
    evidence.cfg = await c.eval(`
      return { ...window.__tools.dbg.openOcdConfig,
        info:document.querySelector('#d-cfg-info').textContent,
        manualClock:document.querySelector('#d-clock').value,
        clockRowHidden:getComputedStyle(document.querySelector('#d-clock-row')).display === 'none',
        disconnected:!window.__tools.dbg.session.connected };
    `);
    assert.ok(evidence.cfg.disconnected && evidence.cfg.clockRowHidden);
    assert.equal(evidence.cfg.clockKhz, Number(clockKhz));
    assert.equal(evidence.cfg.manualClock, '10000');
  }
  await c.eval("document.querySelector('#d-connect').click();");
  await waitFor("window.__tools.dbg.session.connected && !window.__tools.dbg._connecting");
  evidence.before = await c.eval(`
    const d = window.__tools.dbg;
    d._stopWatch();
    return await d.session.exclusive(async () => {
      const s = d.session, p = s.probe;
      const id = { dp:p.idcode, cpuid:await p._readWord(0xe000ed00), dev:await p._readWord(0xe0042000) & 0xfff,
        flashKiB:await p._readWord(0x1ffff7e0) & 0xffff };
      if (s.backendName !== 'WebUSB' || ((id.cpuid >>> 4) & 0xfff) !== 0xc23 || id.dev !== 0x414 || id.flashKiB !== 512)
        throw new Error('Expected F103xE via WebUSB: ' + JSON.stringify(id));
      if (await p.isHalted()) throw new Error('Target is already halted. Finish the existing debug task first.');
      const h = window.__ramEvidence = {};
      await s.halt();
      h.hash = async bytes => [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))]
        .map(b => b.toString(16).padStart(2,'0')).join('');
      h.ram = await p.readMemDiagnostic(0x20000000,65536);
      const ramSha256 = await h.hash(h.ram);
      if (await h.hash(await p.readMemDiagnostic(0x20000000,65536)) !== ramSha256) throw new Error('Unstable SRAM backup');
      h.flash = await h.hash(await p.readMemDiagnostic(0x08000000,524288));
      return { id, clockHz:s.clockHz, fpb:s.caps, flashSha256:h.flash, ramSha256, ram:btoa(String.fromCharCode(...h.ram)) };
    });
  `);
  backedUp = true;
  if (cfg){
    assert.equal(evidence.before.clockHz, evidence.cfg.clockKhz * 1000);
    assert.equal(evidence.before.id.dp, evidence.cfg.dpidr);
  }
  await writeFile(resolve(dirname(output), 'original-sram.bin'), Buffer.from(evidence.before.ram, 'base64'), {flag:'wx'});
  delete evidence.before.ram;

  await upload('#d-ram-elf', 'web/tools/fixtures/f103ze-acceptance/scope72.elf');
  await waitFor("!window.__tools.dbg.session.busy && document.querySelector('#d-out').innerText.includes('全部位于 0x20000000')");
  evidence.flashElfRejected = await c.eval(`
    const h = window.__ramEvidence, s = window.__tools.dbg.session;
    return await s.exclusive(async () => await h.hash(await s.probe.readMemDiagnostic(0x20000000,65536)) === await h.hash(h.ram));
  `);
  assert.ok(evidence.flashElfRejected);

  await upload('#d-src-dir', 'docs/validation/ram-debug-f103ze-20261107');
  await waitFor('window.__tools.dbg.src.ready');
  started = true;
  await upload('#d-ram-elf', elf);
  await waitFor("!window.__tools.dbg.session.busy && document.querySelector('#d-out').innerText.includes('RAM 下载完成，已停在 main')");
  const snapshot = () => c.eval(`
    const d = window.__tools.dbg, s = d.session;
    return await s.exclusive(async () => {
      const pc = await s.readReg('PC'), at = d.sym.at(pc), marker = d.sym.find('marker').addr;
      const bytes = await s.memRead(pc, 2);
      return { pc, main:d.sym.find('main').addr, halted:await s.probe.isHalted(), line:at?.line, file:at?.file,
        source:document.querySelector('#d-src-pos').textContent,
        sourceLoaded:document.querySelector('#d-src').innerText.includes('marker'),
        highlightedLine:Number(document.querySelector('#d-src .srcrow.cur')?.dataset.line),
        marker:new DataView((await s.memRead(marker,4)).buffer).getUint32(0,true),
        originalInstruction:bytes.every((b,i) => b === d.sym.codeBytes(pc,2)?.[i]) };
    });
  `);
  evidence.main = await snapshot();
  assert.equal(evidence.main.pc, evidence.main.main);
  assert.equal(evidence.main.marker, 0);
  evidence.steps = [];
  for (const expected of [41, 42]){
    await c.eval("document.querySelector('#d-step-over').click();");
    await waitFor('!window.__tools.dbg.session.busy');
    const step = await snapshot();
    assert.ok(step.line > (evidence.steps.at(-1) || evidence.main).line);
    assert.equal(step.marker, expected);
    evidence.steps.push(step);
  }
  assert.ok([evidence.main,...evidence.steps].every(x => x.halted && x.originalInstruction &&
    x.sourceLoaded && x.highlightedLine === x.line));
} catch (error){
  evidence.error = error.message;
} finally {
  if (c.ws){
    try {
      evidence.cleanup = await c.eval(`
        const d = window.__tools?.dbg, s = d?.session, h = window.__ramEvidence;
        if (!s?.connected) return null;
        const result = await s.exclusive(async () => {
          if (!h) return {};
          const p = s.probe;
          const result = {};
          if (${started} && ${backedUp}){
            await s.resetHalt();
            await p.writeMemDiagnostic(0x20000000,h.ram);
            result.sramRestored = await h.hash(await p.readMemDiagnostic(0x20000000,65536)) === await h.hash(h.ram);
            if (!result.sramRestored) throw new Error('SRAM restore mismatch');
          }
          if (h.flash) result.flashSha256 = await h.hash(await p.readMemDiagnostic(0x08000000,524288));
          result.flashUnchanged = result.flashSha256 === h.flash;
          await s.run();
          result.running = !await p.isHalted();
          return result;
        });
        await d.disconnect();
        return result;
      `);
    } catch (error){ evidence.cleanupError = error.message; }
    c.close();
  }
  if (acquired){
    await unlink(lock + '/owner.json');
    await rmdir(lock);
  }
  evidence.passed = !evidence.error && !evidence.cleanupError && !!evidence.cleanup?.sramRestored &&
    evidence.cleanup.flashUnchanged && evidence.cleanup.running;
  if (evidence.before) delete evidence.before.ram;
  await writeFile(resolve(output), JSON.stringify(evidence,null,2) + '\n', {flag:'wx'});
  console.log(JSON.stringify(evidence,null,2));
  process.exitCode = evidence.passed ? 0 : 1;
}
