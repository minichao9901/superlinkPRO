import assert from 'node:assert/strict';
import test from 'node:test';
import {OpenOcdDebugSession} from '../../app/dbg/openocd-session.js';
function fixture({mode='on',halt=true,pulseError=false,restoreError=false}={}){
 const s=new OpenOcdDebugSession();s.hpm6800=true;s.ensureHalted=async()=>{};s.clearFrames=()=>{};s.refresh=async()=>{s.halted=halt;};
 let polling=mode,reset=false,acknowledged=false;const commands=[];
 s._command=async line=>{
  commands.push(line);
  if(line==='capture {poll}')return 'background polling: '+polling+'\nTAP: enabled';
  if(line==='poll off'){polling='off';return '';}
  if(line==='poll on'){if(restoreError)throw Error('restore USB failure');polling='on';return '';}
  if(line.endsWith('riscv dmi_read 0x10'))return '0x1';
  if(line.endsWith('riscv dmi_read 0x11')){if(polling==='on'&&reset)reset=false;return '0x'+((reset?0x40000:0)|(halt?0x200:0x800)).toString(16);}
  if(line.includes('dmi_write 0x10')){
   const value=Number(line.split(' ').at(-1));
   if(value&2){reset=true;if(pulseError)throw Error('pulse USB failure');}
   if(value&0x10000000){if(reset)acknowledged=true;reset=false;}
  }
  return '';
 };
 return {s,commands,get polling(){return polling;},get acknowledged(){return acknowledged;}};
}
for(const mode of ['on','off'])for(const halt of [true,false])test(`HPM reset ${halt?'halt':'run'} preserves ${mode} polling and reset evidence`,async()=>{
 const f=fixture({mode,halt});const r=await f.s._systemReset(halt);
 assert.ok(r.dmstatus&0x40000);assert.equal(f.s.halted,halt);assert.equal(f.polling,mode);assert.ok(f.acknowledged);
 assert.ok(f.commands.indexOf('poll off')<f.commands.findIndex(x=>x.endsWith(halt?'0x80000003':'0x3')));
});
test('HPM reset failure restores background polling and keeps the original error',async()=>{
 const f=fixture({pulseError:true});await assert.rejects(f.s._systemReset(true),/pulse USB failure/);assert.equal(f.polling,'on');
});
test('HPM reset reports both pulse and polling cleanup failures',async()=>{
 const f=fixture({pulseError:true,restoreError:true});await assert.rejects(f.s._systemReset(true),e=>e instanceof AggregateError&&e.errors.length===2&&e.errors[0].message==='pulse USB failure'&&e.errors[1].message==='restore USB failure');
});
