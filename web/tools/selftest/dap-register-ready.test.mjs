import assert from 'node:assert/strict';
import {WebUsbDapProbe} from '../../app/rtt/dap-webusb.js';
const p=Object.create(WebUsbDapProbe.prototype);let reads=0,dataReads=0,ready=false;
p.writeMem=async()=>{};p.readMem=async addr=>{
 if(addr===0xe000edf8){dataReads++;return Uint8Array.of(0x78,0x56,0x34,0x12);}
 reads++;return Uint8Array.of(0,0,ready&&reads>=3?1:0,0);
};
await assert.rejects(p.regRead(15),/S_REGRDY/);assert.equal(reads,50);assert.equal(dataReads,0,'must not consume stale DCRDR');
ready=true;reads=0;assert.equal(await p.regRead(15),0x12345678);assert.equal(reads,3);assert.equal(dataReads,1);
console.log('dap-register-ready: timeout rejected before stale data read, ready handshake returns value PASS');
