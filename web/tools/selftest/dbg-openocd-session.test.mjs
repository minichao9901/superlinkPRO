import assert from 'node:assert/strict';
import test from 'node:test';
import { OpenOcdDebugSession } from '../../app/dbg/openocd-session.js';
import { callEndingAt, decodeJal, decodeJalr } from '../../app/dbg/rv.js';

test('RV32 halt reads current hardware state and skips the redundant OpenOCD command', async () => {
  const session = new OpenOcdDebugSession();
  session.probe = {};
  session._triggersChecked = true;
  let targetState = 'halted';
  let haltError = new Error('CMSIS-DAP command CMD_DAP_JTAG_SEQ failed.');
  let requests = 0;
  let registerRefreshes = 0;
  session.refreshRegs = async () => { registerRefreshes++; };
  session.link = { command: async line => {
    if (line === 'poll') return '';
    if (line === `${session.target} curstate`) return targetState;
    if (line === `${session.target} get_reg {pc}`) return 'pc 0x80000000';
    assert.equal(line, 'halt');
    requests++;
    if (haltError) throw haltError;
    targetState = 'halted';
    return '';
  } };

  await session.halt();
  assert.equal(session.pc, 0x80000000);
  assert.equal(requests, 0);
  assert.equal(registerRefreshes, 1);

  // A stale UI flag must not hide a running target or a real pause failure.
  targetState = 'running';
  await assert.rejects(session.halt(), haltError);
  assert.equal(session.halted, false);
  assert.equal(requests, 1);
  assert.equal(registerRefreshes, 1);

  haltError = null;
  await session.halt();
  assert.equal(session.halted, true);
  assert.equal(requests, 2);
  assert.equal(registerRefreshes, 2);
});

test('Wasm restores a temporarily removed hardware breakpoint even when stepping fails', async () => {
  const session = new OpenOcdDebugSession();
  const first = 0x8000578c, second = 0x80006094;
  const calls = [];
  session.link = { command: async line => { calls.push(line); return ''; } };
  await session.bpAdd(first, 'engine_linear');
  await session.bpAdd(second);
  await assert.rejects(session._withBpCleared(first, async () => {
    assert.deepEqual(session.bps, [second]);
    throw new Error('step failed');
  }), /step failed/);
  assert.deepEqual(calls.slice(2), [`rbp ${first}`, `bp ${first} 2 hw`]);
  assert.deepEqual(session.bps, [first, second]);
  assert.equal(session.bpNotes.get(first), 'engine_linear');
});

test('Wasm XIP view only uses fully covered ELF bytes and keeps the target transport untouched', async () => {
  const session = new OpenOcdDebugSession();
  const bytes = Uint8Array.of(0x12, 0x34);
  session.sym = { codeBytes: (addr, len) => addr === 0x80003000 && len === 2 ? bytes : null };
  session.link = { command: async () => assert.fail('XIP must not read from the target bus') };
  assert.deepEqual(await session.memRead(0x80003000, 2), bytes);
  await assert.rejects(session.memRead(0x80003000, 3), /未完整覆盖/);
  await assert.rejects(session.memRead(0x7fffffff, 2), /未完整覆盖/);
});

test('RV32 BRAM breakpoint uses EBREAK, steps across it and restores the original bytes on disconnect',async()=>{
  const session=new OpenOcdDebugSession(),base=0x80000000;
  const original=Uint8Array.of(0x13,0x05,0x15,0x00,0x01,0x00);
  const mem=original.slice(), installed=new Map();
  session.importedCfg=true;
  session.sbaFence=[];
  session.probe={};
  session.halted=true;
  session.pc=base;
  session.ensureHalted=async()=>{session.halted=true;};
  session.refresh=async()=>{};
  session.memRead=async (addr,len)=>mem.slice(addr-base,addr-base+len);
  session.link={worker:{},close:async()=>assert.deepEqual(mem,original),command:async line=>{
    const [cmd,addr,len,hw]=line.split(' ');
    const at=Number(addr)-base;
    if(cmd==='bp'&&addr){
      assert.equal(hw,undefined);
      const n=Number(len);
      installed.set(at,mem.slice(at,at+n));
      mem.set(n===4?[0x73,0,0x10,0]:[0x02,0x90],at);
    }else if(cmd==='rbp'){
      mem.set(installed.get(at),at);
      installed.delete(at);
    }else if(cmd==='step'){
      assert.deepEqual(mem.slice(0,4),original.slice(0,4));
      session.pc+=4;
    }else if(cmd==='bp') return Array.from(installed,([a,bytes])=>`Software breakpoint(IVA): addr=0x${(base+a).toString(16)}, len=0x${bytes.length}`).join('\n');
    else assert.fail(line);
    return '';
  }};
  session.configureRamWindow(base,mem.length);
  session.caps.numCode=0;
  await assert.rejects(session.bpAdd(base+8),/没有硬件触发器/);
  await assert.rejects(session.dwt.add(base,4,'w'),/没有硬件触发器/);
  await session.bpAdd(base,'entry');
  await assert.rejects(session.memWrite(base+1,Uint8Array.of(0)),/先清除断点/);
  await session.step();
  assert.equal(session.pc,base+4);
  await session.bpAdd(base+4);
  assert.deepEqual(await session.verifyBreakpoints(),[base,base+4]);
  await session.disconnect();
  assert.equal(installed.size,0);
});

test('RV32 RAM download rejects out-of-range BSS and never sets PC after a bad readback',async()=>{
  const base=0x80000000,buffer=new ArrayBuffer(88),elf=new DataView(buffer);
  new Uint8Array(buffer).set([0x7f,69,76,70,1,1]);
  elf.setUint16(16,2,true); elf.setUint16(18,243,true);
  elf.setUint32(24,base,true); elf.setUint32(28,52,true);
  elf.setUint16(42,32,true); elf.setUint16(44,1,true);
  for(const [i,value] of [1,84,base,base,4,8,5,4].entries())elf.setUint32(52+i*4,value,true);
  elf.setUint32(84,0x13,true);
  const session=new OpenOcdDebugSession(),writes=[];
  session.probe={}; session.importedCfg=true;
  session.sbaFence=[];
  session.ensureHalted=async()=>{session.halted=true;};
  session.writeReg=async (name,pc)=>{assert.equal(name,'pc');session.pc=pc;};
  session.memWrite=async (addr,bytes)=>writes.push({addr,bytes:bytes.slice()});
  session.memRead=async ()=>new Uint8Array(8);
  const symbols={find:()=>null};
  session.configureRamWindow(base,4);
  await assert.rejects(session._startRamDebug(buffer,symbols),/超出声明/);
  assert.equal(writes.length,0);
  session.configureRamWindow(base,8);
  await assert.rejects(session._startRamDebug(buffer,symbols),/读回校验失败/);
  assert.equal(session.pc,0);
  assert.deepEqual(writes[0],{addr:base,bytes:Uint8Array.of(0x13,0,0,0)});
  session.memRead=async()=>writes.at(-1).bytes;
  assert.equal(await session._startRamDebug(buffer,symbols),base);
  assert.deepEqual(writes.at(-1),{addr:base+4,bytes:new Uint8Array(4)});
});

test('RV32 finish recognizes the GCC AUIPC/JALR ra pair seen on the Zynq soft core',async()=>{
  const call=0x800008d4;
  const bytes=Uint8Array.of(0x97,0,0,0,0xe7,0x80,0xc0,0xf4);
  const target=await callEndingAt(async (addr,len)=>bytes.slice(addr-call,addr-call+len),call+8);
  assert.equal(target,0x80000820);
  assert.equal(decodeJalr(0x80e7,0xf4c0).imm,-180);
  assert.equal(decodeJalr(0x80e7,0x0007).rs1,15);
  assert.equal(decodeJal(0x00ef,0x0080,0x800008d4).target,0x800008dc);
});
