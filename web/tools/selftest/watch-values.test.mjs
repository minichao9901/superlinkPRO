import test from 'node:test';
import assert from 'node:assert/strict';
import { canEditWatch, encodeWatchValue, formatWatchValue, resolveWatch, treeRows, writeWatchValue } from '../../app/dbg/watch.js';
import { runCmd } from '../../app/dbg/cmd.js';
import { completeLine } from '../../app/dbg/complete.js';
import { Dwarf } from '../../app/elf/dwarf.js';
import { SymTab } from '../../app/dbg/symbols.js';

const addr = 0x20000000;
const scalar = (name, size) => ({ addr, scalar: name, size });

test('hex display keeps signed bit patterns and every bit of a double', () => {
  const bytes = Uint8Array.of(255,255,255,255);
  assert.equal(formatWatchValue(scalar('i32',4),bytes).text,'-1');
  assert.equal(formatWatchValue(scalar('i32',4),bytes,'hex').text,'0xffffffff');
  const double = new Uint8Array(8);
  new DataView(double.buffer).setFloat64(0,1.0625,true);
  assert.equal(formatWatchValue(scalar('f64',8),double,'hex').text,'0x3ff1000000000000');
});

test('typed writes reject overflow and preserve float precision', () => {
  assert.deepEqual([...encodeWatchValue(scalar('i8',1),'-128')],[128]);
  assert.deepEqual([...encodeWatchValue(scalar('i8',1),'0xff')],[255]);
  assert.deepEqual([...encodeWatchValue(scalar('u32',4),'0xffffffff')],[255,255,255,255]);
  for (const [type,size,value] of [['u8',1,'256'],['i8',1,'128'],['u32',4,'0x100000000'],['u16',2,'-1'],['f32',4,'1e100'],['f64',8,'Infinity']])
    assert.throws(()=>encodeWatchValue(scalar(type,size),value));
  const bytes=encodeWatchValue(scalar('f64',8),'1.0000000000000002');
  assert.equal(new DataView(bytes.buffer).getFloat64(0,true),1.0000000000000002);
});

test('nested bitfield editing uses the storage offset and preserves adjacent bits', async () => {
  const u32={kind:'scalar',size:4,scalar:'u32'};
  const type={kind:'struct',size:12,members:[{name:'nested',offset:8,type:{kind:'struct',size:4,members:[
    {name:'field',offset:0,bitOffset:4,bitSize:3,type:u32},
  ]}}]};
  const original=new Uint8Array(12).fill(0xa5);
  const row=treeRows({addr,type},original,{radix:'hex'}).find(r=>r.bitfield);
  assert.equal(row.text,'0x2');
  assert.equal(row.edit.addr,addr+8);
  let live=original.slice(8),written;
  const session={connected:true,halted:true,memRead:async a=>{assert.equal(a,addr+8);return live.slice();},
    memWrite:async(a,b)=>{assert.equal(a,addr+8);live=b.slice();written=b.slice();}};
  await writeWatchValue(session,row.edit,'5');
  assert.deepEqual([...written],[0xd5,0xa5,0xa5,0xa5]);
  const signed={addr,size:1,bit:{inUnit:1,size:3,signed:true}};
  assert.deepEqual([...encodeWatchValue(signed,'-4',Uint8Array.of(0xf1))],[0xf9]);
  assert.throws(()=>encodeWatchValue(signed,'-5',Uint8Array.of(0)));
});

test('read-only types, inherited qualifiers and ELF sections cannot be edited', () => {
  const u32={kind:'scalar',size:4,scalar:'u32'};
  const type={kind:'struct',size:4,readOnly:true,members:[{name:'x',offset:0,type:u32}]};
  const root={addr,type,readOnly:true};
  assert.equal(canEditWatch(treeRows(root,new Uint8Array(4))[0].edit),false);
  const sym=new SymTab({dwarf:{varType:name=>name==='parent'?{addr,type}:null}});
  assert.equal(sym.typeOf('parent.x').type.readOnly,true);
  const ro={typeOf:()=>({addr,type:u32}),find:()=>({addr,size:4}),
    elf:{sections:()=>[{flags:2,addr,size:4}]}};
  assert.equal(canEditWatch(resolveWatch('constant',ro)),false);
  assert.equal(canEditWatch({...scalar('u32',4),addr:0xffffffff}),false);
  assert.match(resolveWatch('0x100000000',ro).error,/范围/);
  assert.match(resolveWatch('constant-0x30000000',ro).error,/范围/);
  // Exercise the real qualifier decoder with a minimal DIE reader.
  const decoded=Dwarf.prototype._typeOf.call({num:()=>1,type:()=>u32},{tag:0x26});
  assert.equal(decoded.readOnly,true);
});

test('writes require a current halt, validate first and report readback failures', async () => {
  let accesses=0;
  const session={connected:true,halted:true,refresh:async()=>{},memRead:async()=>{accesses++;return new Uint8Array(4);},
    memWrite:async()=>{accesses++;}};
  await assert.rejects(writeWatchValue(session,scalar('u8',1),'256'),/范围/);
  assert.equal(accesses,0);
  session.refresh=async()=>{session.halted=false;};
  await assert.rejects(writeWatchValue(session,scalar('u32',4),'1'),/目标已运行/);
  assert.equal(accesses,0);
  session.halted=true;session.refresh=async()=>{};
  await assert.rejects(writeWatchValue(session,scalar('u32',4),'1'),/回读不一致/);
  assert.equal(accesses,2);
});

test('p/x uses the same scalar, bitfield and tree hex values and supports completion', async () => {
  const type={kind:'scalar',size:4,scalar:'i32',name:'int'};
  const sym={typeOf:name=>({addr,type:name==='tree'?{kind:'struct',size:4,members:[{name:'x',offset:0,type}]}:type,
      ...(name==='bits'?{bit:{inUnit:0,size:3,signed:true}}:{})}),locText:()=>'',search:()=>[{name:'value'}]};
  const s={connected:true,sym,memRead:async()=>Uint8Array.of(255,255,255,255)};
  assert.match((await runCmd('p value',s)).lines[0].t,/= -1/);
  assert.match((await runCmd('p/x value',s)).lines[0].t,/= 0xffffffff/);
  assert.match((await runCmd('p/x bits',s)).lines[0].t,/= 0x7/);
  assert.match((await runCmd('p/x tree',s)).lines[1].t,/0xffffffff/);
  assert.equal(completeLine('p/x val',{sym:{vars:[{name:'value'}],funcs:[],objs:[]}}).value.trim(),'p/x value');
});
