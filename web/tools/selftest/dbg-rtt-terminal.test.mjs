import assert from 'node:assert/strict';
import { Cdp, sleep } from './cdp-lib.mjs';

const cdp = new Cdp(process.env.CDP, 12000);
const url = new URL(process.env.APP || 'http://127.0.0.1:8899/index.html');
const mode = process.env.MODULES || 'source';
url.search = new URLSearchParams({ demo: 'serial', hid: 'mock', modules: mode, test: Date.now() });
url.hash = 'dbg';

async function waitFor(expression){
  for (let i = 0; i < 100; i++){
    if (await cdp.eval('return !!(' + expression + ');')) return;
    await sleep(50);
  }
  assert.fail('Timed out waiting for ' + expression);
}

async function key(key, code, virtualCode, text){
  await cdp.send('Input.dispatchKeyEvent', {
    type: text ? 'keyDown' : 'rawKeyDown', key, code, text, windowsVirtualKeyCode: virtualCode,
  });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key, code, windowsVirtualKeyCode: virtualCode });
}

await cdp.connect();
try {
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  await cdp.send('Page.navigate', { url: url.href });
  await waitFor('window.__tools?.dbg');
  assert.equal(await cdp.eval('return document.documentElement.dataset.bootEntry;'), mode);
  await cdp.eval(`
    window.dbg = window.__tools.dbg;
    window.byId = id => document.getElementById(id);
    byId('d-backend').value = 'mock';
    await dbg._syncBackend();
    byId('d-rtt-addr').value = '0x20000100';
    byId('d-rtt-on').checked = true;
    await dbg.connect();
    document.querySelector('#d-dock-tabs [data-dock="rtt"]').click();
  `);
  await waitFor('dbg.rttTerminal?.rows > 5');
  assert.equal(await cdp.eval('return dbg.rttTerminal.options.disableStdin;'), true);
  assert.match(await cdp.eval("return byId('d-rtt-info').textContent;"), /下行通道 0 不可用/);
  await cdp.eval(`
    const probe = dbg.session.probe;
    const ram = new DataView(probe.ram.buffer);
    const wrOffset = 0x13c, rdOffset = 0x140, dataOffset = 0x320, size = 8;
    const put = (off, value) => ram.setUint32(off, value, true);
    const get = off => ram.getUint32(off, true);
    window.downPointers = () => [get(wrOffset), get(rdOffset)];
    window.readDown = () => {
      const bytes = [];
      let rd = get(rdOffset);
      while (rd !== get(wrOffset)){
        bytes.push(probe.ram[dataOffset + rd]);
        rd = (rd + 1) % size;
      }
      put(rdOffset, rd);
      return bytes;
    };
    window.sendUp = text => probe._pushRtt(text);
    window.termText = () => {
      const buffer = dbg.rttTerminal.buffer.active;
      return Array.from({length: buffer.length}, (_, i) => buffer.getLine(i).translateToString(true)).join('\\n').trim();
    };
    window.paste = text => {
      const clipboardData = new DataTransfer();
      clipboardData.setData('text/plain', text);
      dbg.rttTerminal.textarea.dispatchEvent(new ClipboardEvent('paste', { clipboardData, bubbles: true, cancelable: true }));
    };
    byId('d-rtt-stop').click();
    const signature = get(0x100);
    put(0x100, 0);
    put(0x114, 1);
    put(0x134, 0x20000320); put(0x138, size);
    put(wrOffset, 6); put(rdOffset, 6); put(0x144, 0);
    window.initRtt = () => put(0x100, signature);
    byId('d-rtt-on').dispatchEvent(new Event('change'));
  `);
  await waitFor("byId('d-rtt-info').textContent.includes('将自动重试')");
  await cdp.eval("initRtt(); sendUp('msh >');");
  await waitFor("!dbg.rttTerminal.options.disableStdin && termText() === 'msh >'");
  assert.equal(await cdp.eval('dbg.rttTerminal.focus(); return dbg.session.halted;'), true);
  await key('h', 'KeyH', 72, 'h');
  await key('Backspace', 'Backspace', 8);
  await key('Enter', 'Enter', 13);
  await waitFor('!dbg.rttInput');
  assert.deepEqual(await cdp.eval('return downPointers();'), [1, 6]);
  assert.deepEqual(await cdp.eval('return readDown();'), [0x68, 0x7f, 0x0d]);
  assert.equal(await cdp.eval('return termText();'), 'msh >');

  await cdp.eval("paste('help\\nversion');");
  await waitFor('dbg.rttInput?.pending === 5');
  const pasteHead = await cdp.eval('return readDown();');
  await waitFor('!dbg.rttInput');
  const pasteTail = await cdp.eval('return readDown();');
  assert.deepEqual([...pasteHead, ...pasteTail], [...Buffer.from('help\rversion')]);

  await cdp.eval("paste('0123456789');");
  await waitFor('!dbg.rttInput');
  assert.match(await cdp.eval("return byId('d-rtt-info').textContent;"), /3 B 未发送/);
  assert.deepEqual(await cdp.eval('return readDown();'), [...Buffer.from('0123456')]);

  await cdp.eval(`
    let release;
    const held = dbg.session.exclusive(() => new Promise(resolve => { release = resolve; }));
    await new Promise(resolve => setTimeout(resolve, 0));
    paste('stale');
    byId('d-rtt-stop').click();
    release();
    await held;
    await dbg.session.exclusive(() => {});
  `);
  assert.deepEqual(await cdp.eval('return readDown();'), []);
  assert.equal(await cdp.eval('return dbg.rttTerminal.options.disableStdin;'), true);
  await cdp.eval("byId('d-rtt-locate').click();");
  await waitFor('!dbg.rttTerminal.options.disableStdin');
  await cdp.eval("sendUp('中\\x1b[32m OK\\x1b[0m\\r\\nmsh >');");
  await waitFor("termText() === 'msh >中 OK\\nmsh >'");
  await cdp.eval("byId('d-rtt-clear').click(); await new Promise(resolve => dbg.rttTerminal.write('', resolve));");
  assert.equal(await cdp.eval('return termText();'), '');
  assert.equal(await cdp.eval('return dbg.rttTerminal.options.disableStdin;'), false);
  await cdp.eval("sendUp('new prompt');");
  await waitFor("termText() === 'new prompt'");
  await cdp.eval('await dbg.disconnect();');
  assert.equal(await cdp.eval('return dbg.rttTerminal.options.disableStdin;'), true);
  console.log(JSON.stringify({ mode, url: url.origin + url.pathname, result: 'passed',
    verified: ['auto-start', 'read-only', 'late initialization', 'auto-refresh while halted', 'keyboard bytes',
      'paste', 'wrap', 'full ring', 'stop cancels', 'restart', 'UTF-8 and ANSI', 'clear', 'disconnect'] }));
} finally {
  cdp.close();
}
