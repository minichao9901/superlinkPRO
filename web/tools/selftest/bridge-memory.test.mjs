import assert from 'node:assert/strict';
import { BridgeClient } from '../../app/rtt/bridge.js';
import { OpenOcdBackend } from '../../bridge/rtt-bridge.mjs';

for (const data of ['', 'AQI=']){
  const client = new BridgeClient(); let calls = 0;
  client._call = async () => { calls++; return { data }; };
  await assert.rejects(client.readMem(0x20000000, 4), /只回来/); assert.equal(calls, 2);
}
const retry = new BridgeClient(); let n = 0;
retry._call = async () => ({ data: ++n === 1 ? 'AQI=' : 'AQIDBA==' });
assert.deepEqual([...await retry.readMem(0x20000000, 4)], [1, 2, 3, 4]);
assert.equal(n, 2); assert.equal((await retry.readMem(0, 0)).length, 0);

const backend = new OpenOcdBackend();
for (const [addr, len, text] of [[0x20000001, 3, '0x11'], [0x20000000, 3, '0x11'], [0x20000000, 8, '0x11'], [0x20000000, 1, '0xGG'], [0x20000000, 1, '0x100']]){
  backend.rpc = async () => text;
  await assert.rejects(backend.readMem(addr, len), /不完整或无效/);
}
let cmds = [];
backend.rpc = async cmd => {
  cmds.push(cmd); return cmd.endsWith('8 3') ? '0x11 0x22 0x33' : '0x77665544';
};
assert.deepEqual([...await backend.readMem(0x20000001, 7)], [0x11,0x22,0x33,0x44,0x55,0x66,0x77]);
assert.equal(cmds.length, 2); cmds = [];
backend.rpc = async cmd => { cmds.push(cmd); return cmd.includes('32') ? '0x44332211 0x88776655' : '0x99'; };
assert.deepEqual([...await backend.readMem(0x20000000, 9)], [0x11,0x22,0x33,0x44,0x55,0x66,0x77,0x88,0x99]);
console.log('bridge-memory: frontend retry rejects short reads; byte/word head and tail never manufacture zeros PASS');
