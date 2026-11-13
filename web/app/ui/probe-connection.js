import { authorizedOpenOcdDevices, rememberOpenOcdDevice, selectOpenOcdDevice } from '../core/openocd-probe.js';
import { PROBE_FEATURES } from '../core/probe-users.js';
import { withTimeout } from '../core/timeout.js';
import { AkaLinkHid, authorizedHidDevices, CMD, requestHidDevice, rememberHidDevice } from '../hid/probe.js';
import { toast } from './toast.js';

export function initProbeConnection(tools){
  const $ = id => document.getElementById(id);
  const connect = $('btn-probe-connect'), disconnect = $('btn-probe-disconnect');
  const usbButton = $('btn-probe-usb'), hidButton = $('btn-probe-hid');
  const menu = $('probe-connect-menu'), status = $('probe-connect-status');
  const supported = { usb:!!navigator.usb, hid:AkaLinkHid.supported() };
  const grants = { usb:[], hid:[] };
  const selected = { usb:null, hid:null };
  const manager = tools.probeManager;
  let busy = false, scanning = true, message = '';
  let scan = 0;
  const activeTab = () => document.querySelector('#tabs [aria-current="page"]')?.dataset.tab;
  const mock = tab => (tab === 'dbg' && $('d-backend').value === 'mock') ||
    (tab === 'rtt' && tools.rtt.backend === 'mock') || (tab === 'rttcdc' && !!tools.hid.mock) ||
    (tab === 'i2c' && $('i2-mock').checked);
  const waitingHid = () => supported.hid && selected.usb && !selected.hid && grants.hid.length !== 1;
  const resolveDevice = kind => selected[kind] || (grants[kind].length === 1 ? grants[kind][0] : null);

  function render(){
    const { owners, pending, failures } = manager.summary();
    const protectedOperation = PROBE_FEATURES.some(feature => feature.guarded?.(tools)) ||
      (tools.spiSession.usingMock && tools.spiSession.busy && !tools.spiSession.capabilities().retryStop);
    const attached = PROBE_FEATURES.some(feature => feature.id !== 'serial' && feature.active(tools)) ||
      tools.hid.mock || tools.hid.dev.connected || tools.hid._connecting || tools.rtt.probe || tools.dbg.session?.connected ||
      tools.scope.usingMock || tools.spiSession.usingMock || tools.i2c.session.usingMock;
    connect.disabled = busy || (!mock(activeTab()) && (scanning || !(supported.usb || supported.hid)));
    disconnect.disabled = busy || protectedOperation || !(selected.usb || selected.hid || attached ||
      owners.some(owner => owner !== 'serial') || [...pending, ...failures].some(item => item.owner !== 'serial'));
    usbButton.disabled = busy || !supported.usb;
    hidButton.disabled = busy || !supported.hid;
    const label = waitingHid() && !mock(activeTab()) ? '连接 HID' : '连接探针';
    if (connect.textContent !== label) connect.textContent = label;
    const state = ['usb', 'hid'].map(kind => {
      const device = selected[kind];
      return `${kind.toUpperCase()} ${device ? device.productName || '已选择' : '未选择'}`;
    }).join(' · ');
    const detail = scanning ? '正在读取浏览器已授权的探针…' : [message, state].filter(Boolean).join('\n');
    if (status.textContent !== detail) status.textContent = detail;
    if (connect.title !== state) connect.title = state;
  }

  async function refreshGrants(){
    const request = ++scan;
    const [usb, hid] = await Promise.all([
      withTimeout(authorizedOpenOcdDevices(), 5000, '枚举 USB').catch(() => []),
      withTimeout(authorizedHidDevices(), 5000, '枚举 HID').catch(() => []),
    ]);
    if (request !== scan) return;
    grants.usb = usb;
    grants.hid = hid;
    scanning = false;
    render();
  }

  function remember(usb, hid){
    selected.usb = usb;
    selected.hid = hid;
    rememberOpenOcdDevice(usb);
    rememberHidDevice(hid);
  }

  async function release(reason){
    await tools.hid._initialReconnect;
    await manager.releaseOthers('serial', reason);
    if (tools.hid.mock || tools.hid.dev.connected || tools.hid._connecting){
      if (await tools.hid.disconnect() === false) throw Error('RTT 转发 HID 尚未断开');
    }
    if (tools.rtt._probeMock && tools.rtt.probe) await tools.rtt.disconnect();
    if (tools.dbg.session?.backendName === '模拟目标' && tools.dbg.session.connected) await tools.dbg.disconnect();
    if (tools.scope.usingMock && await tools.scope.disconnect() === false) throw Error('J-Scope 模拟会话尚未断开');
    if (tools.spiSession.usingMock && await tools.spiSession.disconnect() === false) throw Error('SPI 模拟会话尚未断开');
    if (tools.i2c.session.usingMock){ tools.i2c.runner.stop(); await tools.i2c.session.disconnect(); }
  }

  async function attach(tab, usb, hid){
    switch (tab){
      case 'dbg':
        if (tools.dbg.session.connected) return true;
        if (usb || mock(tab)){ await tools.dbg.connect(); return tools.dbg.session.connected; }
        break;
      case 'rtt':
        if (usb || mock(tab)){ await tools.rtt.connectProbe(); return !!tools.rtt.probe; }
        break;
      case 'rttcdc':
        if (hid || mock(tab)) return tools.hid.dev.connected || await tools.hid.connect(hid);
        break;
      case 'scope':
        if (tools.scope.usingMock) break;
        if (hid && !tools.scope.hid?.connected) await tools.scope.connectHid(false);
        if (usb && !tools.scope.transport) await tools.scope.connectUsb(false);
        return (!hid || !!tools.scope.hid?.connected) && (!usb || !!tools.scope.transport);
      case 'spi':
      case 'panel': {
        const session = tools.spiSession;
        if (session.usingMock) return true;
        if (hid && !session.connected && !await session.connectHid(false)) return false;
        if (usb && session.connected && !session.usingMock){
          const prefix = tab === 'spi' ? 'sp' : 'pn';
          return await session.connectUsb(false, { inFlight:Number($(`${prefix}-inflight`).value) || 4 });
        }
        break;
      }
      case 'spicdc':
        if (hid) return !!await tools.spiCdc.session.connect(false);
        break;
      case 'i2c':
        if (hid || mock(tab)) return await tools.i2c.connect({ mock:mock(tab) });
        break;
      case 'analog':
        if (hid) return await tools.analog.connect(hid);
        break;
      case 'etm':
        if (usb) return await tools.etm.connect();
        break;
    }
    // Flash and SWO claim the selected USB device when their operation starts.
  }

  async function sameProbe(usb, hid){
    if (usb.vendorId !== hid.vendorId || usb.productId !== hid.productId || !usb.serialNumber) return false;
    const client = new AkaLinkHid();
    try {
      await client.open(hid);
      const serial = await client.text(CMD.SN);
      return !!serial && serial.toUpperCase() === usb.serialNumber.toUpperCase();
    } finally { await client.close(); }
  }

  async function prepare(kind){
    if (busy) return;
    busy = true;
    message = '';
    const tab = activeTab();
    render();
    try {
      if (kind === 'both' && mock(tab)){ await attach(tab, null, null); return; }
      let usb = kind === 'hid' ? selected.usb : resolveDevice('usb');
      let hid = kind === 'usb' ? selected.hid : resolveDevice('hid');
      const chooseUsb = supported.usb && (kind === 'usb' || kind === 'both' && !usb);
      const chooseHid = supported.hid && (kind === 'hid' || kind === 'both' && !hid && !chooseUsb);
      if (chooseUsb) usb = await selectOpenOcdDevice({ forcePick:true, all:$('probe-usb-all').checked });
      if (chooseHid) hid = await requestHidDevice();
      await tools.hid._initialReconnect;
      if (usb && hid && (usb !== selected.usb || hid !== selected.hid) && !await sameProbe(usb, hid)){
        message = 'USB 和 HID 对应不同设备。请在顶栏选择同一只探针。';
        if (kind === 'hid') usb = null;
        else hid = null;
      }
      const changed = (selected.usb && selected.usb !== usb) || (selected.hid && selected.hid !== hid) ||
        (usb && tools.hid.dev.connected && !hid && !tools.hid.mock) ||
        (hid && tools.hid.dev.connected && tools.hid.dev.device !== hid && !tools.hid.mock);
      if (changed || ((!selected.usb && !selected.hid) && PROBE_FEATURES.some(f =>
        f.id !== 'serial' && f.id !== 'hid' && f.active(tools)))){
        await release('从顶栏更换探针');
      }
      remember(usb, hid);
      // NRST is a HID capability shared by every tab. The idle client does
      // not hold the target engine and is explicitly closed by the header.
      if (hid && !tools.hid.dev.connected && !tools.hid.mock && !await tools.hid.connect(hid))
        message ||= 'HID 已授权，读取探针状态失败';
      if (tab === activeTab() && await attach(tab, usb, hid) === false)
        message ||= '当前工具未接通，请查看工具状态';
      if (!message && kind === 'both' && waitingHid()) message = 'USB 已就绪，请再点一次「连接 HID」';
      if (message) toast(message, 'warn', 6000);
    } catch (error){
      message = error.message;
      toast(message, 'err', 6000);
    } finally {
      busy = false;
      refreshGrants();
      render();
    }
  }

  connect.onclick = () => prepare('both');
  usbButton.onclick = () => { menu.open = false; return prepare('usb'); };
  hidButton.onclick = () => { menu.open = false; return prepare('hid'); };
  disconnect.onclick = async () => {
    if (busy) return;
    menu.open = false;
    busy = true;
    render();
    try {
      await release('从顶栏断开探针');
      remember(null, null);
      message = '';
    } catch (error){
      message = error.message;
      toast('断开失败：' + message, 'err', 6000);
    } finally { busy = false; render(); }
  };
  for (const kind of ['usb', 'hid']){
    navigator[kind]?.addEventListener('connect', refreshGrants);
    navigator[kind]?.addEventListener('disconnect', ({device}) => {
      if (device === selected[kind]){
        remember(kind === 'usb' ? null : selected.usb, kind === 'hid' ? null : selected.hid);
      }
      refreshGrants();
    });
  }
  document.addEventListener('click', event => { if (!menu.contains(event.target)) menu.open = false; });
  menu.addEventListener('keydown', event => {
    if (event.key === 'Escape'){ menu.open = false; menu.querySelector('summary').focus(); }
  });
  const timer = setInterval(render, 500);
  window.addEventListener('pagehide', () => clearInterval(timer), { once:true });
  refreshGrants();
  return render;
}
