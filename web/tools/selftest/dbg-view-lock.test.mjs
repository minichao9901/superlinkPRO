import assert from 'node:assert/strict';
import {DebugSession} from '../../app/dbg/session.js';
import {DbgView} from '../../app/dbg/view.js';
const gate=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
const tick=()=>new Promise(r=>setImmediate(r));
const nodes=new Map([['d-mem-addr',{value:'0x20000000'}],['d-mem-len',{value:'4'}]]);
globalThis.document={getElementById:id=>nodes.get(id)||null};
const s=new DebugSession(),v=Object.create(DbgView.prototype),held=gate();let reads=0;
s.probe={readMem:async()=>{reads++;return new Uint8Array(4);}};
Object.assign(v,{session:s,renderMem(){},watch:{length:0}});
const op=s.exclusive(()=>held.promise);const read=v.readMem();const watch=v.refreshWatch();await tick();assert.equal(reads,0);
held.resolve();await Promise.all([op,read,watch]);assert.equal(reads,1);assert.equal(s._opDepth,0);
// Internal refresh already inside an action must not reacquire the non-reentrant lock.
await s.exclusive(()=>v._readMemLocked());assert.equal(reads,2);
console.log('dbg-view-lock: memory/watch entry points queue behind actions, internal refresh avoids nested lock PASS');
// A slower old source read cannot paint its text on a newer PC/file location.
nodes.set('d-src',{});
const source=Object.create(DbgView.prototype),oldText=gate(),paint=[];
Object.assign(source,{session:{connected:true,pc:1},sym:{lines:{},at:pc=>({file:pc===1?'old.c':'new.c',line:pc})},
 src:{ready:true,read:file=>file==='old.c'?oldText.promise:Promise.resolve('new text')},
 _srcPaint:(box,at,lines)=>paint.push({file:at.file,lines})});
const older=source.renderSource();source.session.pc=2;await source.renderSource();
oldText.resolve('old text');await older;
assert.deepEqual(paint,[{file:'new.c',lines:['new text']}]);
console.log('dbg source UI: latest request/location wins, stale file text never repaints current source PASS');
