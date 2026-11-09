import assert from 'node:assert/strict';
import test from 'node:test';
import {captureFault} from '../../app/dbg/fault.js';
import {OpenOcdDebugSession} from '../../app/dbg/openocd-session.js';
function session(){
 const s=new OpenOcdDebugSession(),commands=[];s.probe={};s.halted=true;s._pollHalted=async()=>true;
 const raw={pc:0x80004000,mcause:11,mepc:0x18000,mtval:0,mstatus:0x1800,mtvec:0x80003101,dcsr:0x4000b0c3};
 s._command=async line=>{commands.push(line);const names=/ get_reg \{([^}]+)\}$/.exec(line)?.[1].split(' ');assert.ok(names,'diagnostics must not halt or write');return names.map(n=>n+' 0x'+raw[n].toString(16)).join('\n');};
 return {s,raw,commands};
}
test('Wasm RV32 fault snapshot reads current CSR transport without a legacy DM or writes',async()=>{
 const {s,raw,commands}=session();const r=await captureFault(s);assert.equal(r.error,null);assert.deepEqual(r.raw,raw);assert.equal(r.pc,raw.pc);assert.ok(commands.length>0);assert.ok(r.findings.some(x=>/M 模式环境调用/.test(x.text)));
});
test('RV32 fault snapshot refuses a running hart before reading CSRs',async()=>{
 const {s,commands}=session();s._pollHalted=async()=>false;const r=await captureFault(s);assert.match(r.error,/未暂停/);assert.equal(commands.length,0);
});
test('RV32 fault snapshot rejects a changed PC rather than publishing stale state',async()=>{
 const {s,raw}=session();let polls=0;s._pollHalted=async()=>{if(++polls===2)raw.pc+=4;return true;};const r=await captureFault(s);assert.match(r.error,/现场已过期/);
});
