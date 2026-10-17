/** Execute the runner against a deterministic browser/session double; no hardware claims. */
import assert from 'node:assert/strict';
import {FRAME_CASES,webVariables} from './dbg-frame-contract.mjs';
import {runFrameStress} from './dbg-frame-hw-runner.mjs';
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
function rig({wrongValue=false,mutate=false,badFlash=false,m7Erratum=false,unconfirmedErratum=false}={}){
 let checkpoint=0x08001000,bps=0,r0=0,selected=0,panel='',buttons=null,racePending=m7Erratum;
 const sp=0x20004000;
 const stackBytes=(n,pc=checkpoint)=>{const b=new Uint8Array(n),v=new DataView(b.buffer);if(n>=32){v.setUint32(24,unconfirmedErratum==='stack'?pc+4:pc,true);v.setUint32(28,0x01000000,true);}return b;};
 const rows=()=>[{name:'test',value:wrongValue?'8':'7',argument:false}];
 const frames=()=>[{pc:checkpoint,lookup:checkpoint,sp,loc:{file:'dbg_frames.c',line:10},regs:Array(16).fill(0),known:[0]}];
 const session={connected:true,halted:true,pc:checkpoint,_frames:null,bps:[],arch:{name:'arm'},caps:{numCode:8,rev:1},
  probe:{_readWord:async a=>a===0xe000ed00?0x410fc271:a===0xe000ed30?(unconfirmedErratum==='dfsr'?0:unconfirmedErratum==='dwt'?6:2):a===0xe0002000?0x80:
   a===0xe0002008?(((checkpoint&0x1ffffffc)|(((checkpoint&2)?2:1)<<30)|1)>>>0):0},
  dwt:{async haltReason(){return null;}},
  exclusive:async fn=>await fn(),refresh:async()=>{if(!session.halted){session.halted=true;if(racePending){racePending=false;session.pc=0x080000f4;}else session.pc=checkpoint;}},
  readReg:async name=>name==='SP'||name==='R13'?sp:name==='XPSR'?(session.pc===0x080000f4?15:0):name==='R15'?session.pc:name==='R0'?r0:0,
  writeReg:async()=>{session._frames=null;},memWrite:async()=>{session._frames=null;},
  memRead:async(a,n)=>a===sp?stackBytes(n):new Uint8Array(n).fill(a<0x20000000&&badFlash?1:0),
  cont:async()=>{session.halted=false;session._frames=null;},run:async()=>{session.halted=false;session._frames=null;},
  halt:async()=>{session.halted=true;},bpClear:async()=>{bps=0;session.bps=[];}};
 const d={session,srcCur:{},sym:{elf:{b:new Uint8Array()},funcAt:()=>({name:'engine_frame_test'})},
  async runLine(line){
   if(line==='bd all'){bps=0;session.bps=[];return {};}
   if(line.startsWith('b ')){checkpoint=Number(line.slice(2));bps=1;session.bps=[checkpoint];return {};}
   if(line.startsWith('bt ')){session._frames={frames:frames()};panel='帧 #0';return {backtrace:session._frames};}
   if(line.startsWith('frame ')){
    if(!session._frames)return {error:'栈帧已失效'};
    selected=Number(line.slice(6));buttons={dataset:{frame:String(selected)}};d.srcCur={file:'dbg_frames.c',line:10};
    if(mutate)r0++;
    return {locals:{rows:rows()}};
   }
   if(line==='s'||line==='c'||line==='reset halt'){session._frames=null;panel='暂停后回溯';return {};}
   throw new Error('unexpected command '+line);
  },
  loadElfBuffer(){session._frames=null;},
  async disconnect(){session._frames=null;session.connected=false;}};
 d.sym.funcAt=pc=>({name:(pc&0xfffffffe)===0x080000f4?'SysTick_Handler':'engine_frame_test'});
 const window={__tools:{dbg:d},__S:{resetToFirmware:async()=>({pc:checkpoint}),leak:async()=>({used:bps,bps,extra:0})}};
 const document={querySelector:()=>buttons,getElementById:()=>({textContent:panel})};
 const cdp={json:async expr=>await new AsyncFunction('window','document','return ('+expr+');')(window,document),eval:async code=>await new AsyncFunction('window','document',code)(window,document)};
 const oracle={cases:Object.fromEntries(FRAME_CASES.map((c,i)=>[c.id,{pc:0x08001000+i*8,frames:[{name:'engine_frame_test',pc:0x08001000+i*8,sp,variables:webVariables([{name:'test',value:'7',argument:false}])}]}]))};
 const results=[];
 return {cdp,oracle,board:m7Erratum||unconfirmedErratum?'h743':'f103cb',rounds:2,code:[{name:'.text',addr:0x08000000,bytes:[0,0]}],ok:(passed,name,details)=>results.push({passed,name,details}),log:()=>{},results,session};
}
const good=rig();const report=await runFrameStress(good);assert.equal(report.length,7);assert.ok(good.results.every(r=>r.passed),JSON.stringify(good.results));assert.equal(good.session.connected,false);
for(const options of [{wrongValue:true},{mutate:true},{badFlash:true}]){
 const r=rig(options);await runFrameStress(r);assert.ok(r.results.some(v=>!v.passed));assert.equal(r.session.connected,false);
}
const m7=rig({m7Erratum:true}),m7report=await runFrameStress(m7);
assert.ok(m7.results.every(r=>r.passed),JSON.stringify(m7.results));
assert.equal(m7report[0].m7ErratumRecoveries.length,1,'严格确认的 M7 异常/FPB 竞态应恢复一次');
assert.equal(m7report.find(r=>r.id==='recursive').pressureM7ErratumRecoveries,0,'报告应记录递归压力中的恢复次数');
for(const unconfirmedErratum of ['stack','dfsr','dwt']){
 const r=rig({m7Erratum:true,unconfirmedErratum});await runFrameStress(r);
 assert.ok(r.results.some(v=>!v.passed),'缺少 erratum 证据时必须保持失败');assert.equal(r.session.connected,false);
}
console.log('dbg-frame-hw-runner: checkpoints, read-only snapshots, stale-cache flow, strict M7 erratum recovery, rejection without stacked-PC/DFSR proof, target mismatch and cleanup PASS');
// RISC-V XIP reads are ELF image bytes, not an independent board-code verifier.
const xip=rig();xip.board='6800evk';xip.code=[{name:'.text',addr:0x80000000,bytes:[0,0]}];
await runFrameStress(xip);
assert.ok(xip.results.some(r=>!r.passed&&/独立 GDB/.test(r.details||r.name)),JSON.stringify(xip.results));
assert.equal(xip.session.connected,false);
console.log('dbg-frame-hw-runner: XIP image cannot self-certify live target code; independent GDB proof required PASS');
