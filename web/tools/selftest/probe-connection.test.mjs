import test from 'node:test';
import assert from 'node:assert/strict';
import { initProbeConnection } from '../../app/ui/probe-connection.js';

test('authorized-only top-bar attach never invokes choosers for absent or wrong probes', async () => {
  const saved = new Map(['navigator', 'document', 'window'].map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
  let usb = [], hid = [];
  const nodes = new Map();
  const node = id => {
    if (id === 'toasts') return null;
    if (!nodes.has(id)) nodes.set(id, { disabled:false, value:'', textContent:'', addEventListener(){} });
    return nodes.get(id);
  };
  const deviceApi = getDevices => ({ getDevices, addEventListener(){}, requestDevice(){ assert.fail('device chooser opened'); } });
  let dispose;
  const surface = {
    navigator:{ usb:deviceApi(async () => usb), hid:deviceApi(async () => hid) },
    document:{ getElementById:node, querySelector:() => ({dataset:{tab:'rtt'}}), addEventListener(){} },
    window:{ addEventListener(name, listener){ if (name === 'pagehide') dispose = listener; } },
  };
  const tools = {
    probeManager:{ summary:() => ({owners:[], pending:[], failures:[]}) },
    hid:{ dev:{connected:false} }, rtt:{}, dbg:{}, scope:{}, spiSession:{}, i2c:{session:{}},
  };
  Object.entries(surface).forEach(([name, value]) => Object.defineProperty(globalThis, name, {configurable:true, value}));
  try {
    initProbeConnection(tools);
    await assert.rejects(tools.probeConnection.connectAuthorized('SERIAL', 'rtt'), /已授权 USB 和 HID/);
    usb = [{vendorId:0x0d28, productId:0x0204, serialNumber:'OTHER'}];
    hid = [{vendorId:0x0d28, productId:0x0204, collections:[{usagePage:0xff00}]}];
    await assert.rejects(tools.probeConnection.connectAuthorized('SERIAL', 'rtt'), /指定探针/);
    await assert.rejects(tools.probeConnection.connectAuthorized('OTHER', 'dbg'), /工具页/);
  } finally {
    dispose?.();
    for (const [name, desc] of saved){
      if (desc) Object.defineProperty(globalThis, name, desc);
      else delete globalThis[name];
    }
  }
});
