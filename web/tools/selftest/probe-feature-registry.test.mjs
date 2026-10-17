import assert from 'node:assert/strict';
import { installProbeManager, PROBE_FEATURES } from '../../app/core/probe-users.js';
import { UsbLease, setUsbResetGuard } from '../../app/core/usb-device.js';
import { runProbeOperation } from '../../app/core/probe-manager.js';

// Add a feature through one descriptor: injection, conflict policy and reset ownership
// must work without adding its ID to main.js or to a reset-kind allowlist.
const events = [], bus = { supported: false };
const tools = {
  dbg: { session: { connected: false } },
  spiSession: {}, i2c: { session: {} }, session: {},
  sensor: { running: false, busy: false }, sensorView: {}, analyzer: { running: false },
};
const features = [...PROBE_FEATURES, {
  id: 'sensor', label: '传感采集器', usbKind: 'sensor',
  client: t => t.sensor, view: t => t.sensorView, resources: ['sensor-pins', 'sensor-stream'],
  active: t => t.sensor.running, guarded: t => t.sensor.busy,
  release: async t => { events.push('sensor:stop'); t.sensor.running = false; },
}, {
  id: 'analyzer', label: '分析仪', usbKind: 'analyzer',
  client: t => t.analyzer, resources: ['sensor-pins'],
  active: t => t.analyzer.running,
  release: async t => { t.analyzer.running = false; },
}];
const m = installProbeManager(tools, { bus, locks: null, features });
assert.equal(tools.probeManager, m);
assert.equal(tools.spiSession.probeManager, m);
assert.equal(tools.i2c.session.probeManager, m);
assert.equal(tools.i2c.bus, bus);
assert.equal(tools.sensor.probeManager, m);
assert.equal(tools.sensorView.probeManager, m);
assert.equal(tools.sensorView.bus, bus);

await runProbeOperation(tools.sensorView, 'sensor', async () => { tools.sensor.running = true; });
const device = { opened: false, async open(){ this.opened = true; },
  configuration: {}, async reset(){ events.push('USB:reset'); },
  async close(){ this.opened = false; } };
const usb = new UsbLease(device, 'sensor');
await usb.open(); await usb.reset();
assert.deepEqual(events, ['USB:reset'], 'new USB kind recognizes its own feature');
await m.run('dbg', async () => { tools.dbg.session.connected = true; });
assert.deepEqual(m.summary().owners, ['sensor', 'dbg']);
await assert.rejects(usb.reset(), /dbg/, 'independent target users still block global reset');
tools.sensor.busy = true;
await assert.rejects(m.run('analyzer', () => assert.fail()), /传感采集器/);
tools.sensor.busy = false;
await assert.rejects(m.run('analyzer', () => assert.fail(), { rejectResources: ['sensor-pins'] }),
  /传感采集器/, 'generic conflicts must identify the declared feature, not CDC');
await m.run('analyzer', async () => { tools.analyzer.running = true; });
assert.deepEqual(m.summary().owners, ['dbg', 'analyzer']);
assert.equal(events.at(-1), 'sensor:stop');
await usb.close(); setUsbResetGuard(null);
console.log('probe-feature-registry: one descriptor wires new clients, USB ownership, coexistence and guarded handoff PASS');
