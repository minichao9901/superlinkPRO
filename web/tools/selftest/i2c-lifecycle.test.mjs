import assert from 'node:assert/strict';
import { I2cSession } from '../../app/i2c/session.js';
import { installProbeManager } from '../../app/core/probe-users.js';
import * as P from '../../app/i2c/protocol.js';

for (const failure of ['none', 'disable', 'close']){
  const events = [], session = new I2cSession(); let fail = failure;
  const hid = { connected: true, async xfer(cmd, data){
    events.push('disable'); assert.equal(cmd, P.HID_CMD); assert.deepEqual([...data], [P.ACT.ENABLE, 0]);
    if (fail === 'disable') throw new Error('disable failed');
    return Uint8Array.of(8, P.HID_CMD, P.ACT.ENABLE, 0, 0, 0, 0);
  }, async close(){ events.push('close'); if (fail === 'close') throw new Error('close failed'); } };
  session.hid = hid; session.status = { enabled: true };
  const m = installProbeManager({ i2c: { session }, spiSession: {} }, { locks: null });
  await m.run('i2c', async () => {});
  let release; session._chain = new Promise(r => { release = r; });
  const switching = session.connect(false, { mock: true, enable: true });
  await Promise.resolve(); assert.deepEqual(events, [], 'mode switch drains in-flight transactions');
  release(); const success = await switching; session.stopPoll();
  if (failure !== 'none'){
    assert.equal(success, false); assert.equal(session.hid, hid); assert.equal(session.usingMock, false);
    assert.ok(m.failures.has('i2c'));
    await assert.rejects(m.run('spi', async () => {}), /尚未确认/);
    fail = 'none'; assert.equal(await session.connect(false, { mock: true }), true); session.stopPoll();
  } else { assert.equal(success, true); assert.deepEqual(events, ['disable', 'close']); }
  assert.equal(session.usingMock, true); assert.equal(m.leases.has('i2c'), false);
  assert.equal(m.failures.has('i2c'), false); await session.disconnect();
  await m.run('spi', async () => { events.push('spi'); });
}
console.log('i2c-lifecycle: real-to-mock drains transactions, confirms disable and close, retains failed ownership and retries PASS');
