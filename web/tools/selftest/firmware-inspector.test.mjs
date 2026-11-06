import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {inspectElf,elfTable,firmwareRegions,hexPage} from '../../app/flash/inspect.js';

test('real ARM and H743 ELF headers, sections and symbols are inspectable without target access',()=>{
  for(const name of ['../etm/firmware/stm32f103_cdc_trace/fw-ze.elf','../etm/firmware/stm32h743_cdc_trace/fw.elf']){
    const b=new Uint8Array(fs.readFileSync(name)),elf=inspectElf(b);
    assert.equal(new Map(elf.header).get('机器'),'ARM');
    assert.ok(elf.programs.some(p=>p.type===1));assert.ok(elf.sections.some(s=>s.name==='.text'));
    const symbols=elfTable(elf,'symbols','main');assert.ok(symbols.rows.some(r=>r[2]==='main'));
    assert.ok(symbols.total>=symbols.rows.length);
    assert.ok(firmwareRegions(name,b,'bad unused base').length);
  }
});
test('ELF64 big-endian keeps addresses beyond JS integer precision and permits no section table',()=>{
  const b=new Uint8Array(64),d=new DataView(b.buffer);b.set([127,69,76,70,2,2,1]);d.setUint16(16,2);d.setUint16(18,183);d.setBigUint64(24,0x100000000000001n);d.setUint16(52,64);
  const elf=inspectElf(b);assert.equal(new Map(elf.header).get('入口'),'0x100000000000001');assert.equal(elf.symbols().total,0);
});
test('ELF rejects truncated tables, invalid endian and out-of-file section data',()=>{
  const original=new Uint8Array(fs.readFileSync('../etm/firmware/stm32f103_cdc_trace/fw-ze.elf'));
  assert.throws(()=>inspectElf(original.subarray(0,100)),/越界|截断/);
  const b=original.slice();b[5]=0;assert.throws(()=>inspectElf(b),/端序/);
  b[5]=1;const d=new DataView(b.buffer),sh=d.getUint32(32,true),ent=d.getUint16(46,true);d.setUint32(sh+ent+16,0xfffffff0,true);assert.throws(()=>inspectElf(b),/越界/);
});
test('sparse HEX preserves region addresses, reports checksum failure, and raw bytes are not HEX text',()=>{
  const text=':020000040800F2\n:0400000001020304F2\n:0430000005060708B2\n:00000001FF';
  const bytes=new TextEncoder().encode(text),r=firmwareRegions('two.hex',bytes,0);
  assert.equal(r.length,2);assert.equal(r[1].addr,0x08003000);assert.deepEqual([...r[0].data],[1,2,3,4]);
  assert.throws(()=>firmwareRegions('bad.hex',new TextEncoder().encode(text.replace('F2','F3')),0),/校验和/);
});
test('BIN base validation matches programming, page boundaries and ASCII stay explicit',()=>{
  const b=Uint8Array.from({length:1025},(_,i)=>i&255),r=firmwareRegions('data.bin',b,'0x08000000');assert.equal(r[0].addr,0x08000000);
  assert.throws(()=>firmwareRegions('data.bin',b,'0'),/非零/);
  assert.throws(()=>firmwareRegions('data.bin',b,'0xffffffff'),/超过/);
  assert.equal(hexPage(b,0,1024).split('\n').length,1);
  assert.match(hexPage(new Uint8Array([60,115,62,0]),0x8000000),/3C 73 3E 00.*\|<s>\.\|/);
});
