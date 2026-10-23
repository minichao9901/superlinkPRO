import test from 'node:test';
import assert from 'node:assert/strict';
import {createTraceAutomation} from '../../app/automation/trace.js';
import {runCmd} from '../../app/dbg/cmd.js';

function rig(){
  const device={productName:'akaLinkPro CMSIS-DAP',serialNumber:'probe'},calls=[],elements=new Map();
  const document={getElementById:id=>{if(!elements.has(id))elements.set(id,{value:'',checked:false,textContent:'passed',type:id==='f-verify'||id==='f-reset'?'checkbox':'text'});return elements.get(id);}};
  const session={connected:true,probe:{device},statusInfo:()=>({connected:true,halted:false}),bpList:()=>[],exclusive:async fn=>fn(),refresh:async()=>({halted:true}),halted:true,pc:0x08000000};
  const t={probe:{device},info:{cpuid:1,device:2,profile:'stm32h743'},saved:new Map([[1,{mask:255,before:4}]]),locks:new Map(),osLocked:false,
    isHalted:async()=>false,inspect:async()=>t.info,read:async()=>5,halt:async()=>calls.push('halt'),run:async()=>calls.push('run'),
    clearEntryBreak:async()=>calls.push('clear'),restore:async()=>calls.push('restore'),verifyElf:async()=>10,
    reset:async options=>{calls.push(['reset',options]);return {entryPC:options.address};}};
  const e={target:t,state:{operation:false,job:null,waiting:false}};
  const tools={dbg:{session,runLine:async line=>{calls.push(line);return {lines:[{t:line}]};},disconnect:async()=>calls.push('disconnect'),loadElfBuffer:()=>calls.push('elf')},
    flash:{flash:async()=>calls.push('flash')},etm:{onShow:async()=>{},frame:{contentWindow:{__etm:e}},run:async fn=>fn(),summary:()=>({ready:true})}};
  const win={document,navigator:{usb:{getDevices:async()=>[device]}}};
  return {a:createTraceAutomation(tools,win),tools,calls,e,elements,win};
}
test('commands are forwarded verbatim to the existing Web command runner',async()=>{
  const r=rig(),commands=['b main.c:42','bl','s','n','si','fin','r r0','wp g_var w 4','bd all'];
  const result=await r.a.dispatch({action:'command',commands,probe:'probe'});
  assert.deepEqual(r.calls,commands);assert.equal(result.results.length,commands.length);
});
test('runtime software readiness never enumerates or opens a USB device',async()=>{
  const r=rig();r.win.navigator.usb.getDevices=()=>{throw Error('must not access hardware');};
  const result=await r.a.dispatch({action:'ping'});assert.equal(result.ready,true);assert.equal(result.hardwareAccessed,false);
});
test('a command error stops the batch and preserves prior results',async()=>{
  const r=rig();r.tools.dbg.runLine=async line=>{r.calls.push(line);return line==='bad'?{error:'bad command'}:{lines:[]};};
  await assert.rejects(r.a.dispatch({action:'command',commands:['halt','bad','c']}),e=>{assert.equal(e.results.length,2);return /bad command/.test(e.message);});
  assert.deepEqual(r.calls,['halt','bad']);
});
test('red error result lines cannot be reported as a successful memory write',async()=>{
  const r=rig();r.tools.dbg.runLine=async()=>({lines:[{c:'err',t:'readback mismatch'}]});
  await assert.rejects(r.a.dispatch({action:'command',commands:['mw 0x20000000 01']}),/readback mismatch/);
});
test('probe mismatch and multiple authorized probes fail before command execution',async()=>{
  const r=rig();await assert.rejects(r.a.dispatch({action:'command',commands:['c'],probe:'other'}),/不一致/);assert.deepEqual(r.calls,[]);
  r.tools.dbg.session.connected=false;r.win.navigator.usb.getDevices=async()=>[{productName:'DAP',serialNumber:'a'},{productName:'DAP',serialNumber:'b'}];
  await assert.rejects(r.a.dispatch({action:'connect',probe:'a'}),/多个/);
});
test('flash delegates to the Web flasher with verification and reset enabled',async()=>{
  const r=rig();await r.a.dispatch({action:'flash',probe:'probe',name:'test.bin',data:'AA==',chip:'stm32h7',base:0x08000000});
  assert.deepEqual(r.calls,['flash']);assert.equal(r.tools.flash.file.dataB64,'AA==');assert.equal(r.elements.get('f-verify').checked,true);
});
test('target operations reuse the page object and release its busy guard',async()=>{
  const r=rig();r.e.target.saved.clear();await r.a.dispatch({action:'target',op:'init',profile:'stm32h743',probe:'probe'});
  assert.equal(r.e.state.hardwareOperation,true);
  await r.a.dispatch({action:'target',op:'reset',address:0x08000100,timeoutMs:3000});
  assert.deepEqual(r.calls[0],['reset',{address:0x08000100,timeout:3000}]);
  await r.a.dispatch({action:'target',op:'close',restore:true});
  assert.equal(r.e.state.operation,false);assert.ok(r.calls.includes('restore'));
});
test('stale target state is rejected before changing saved register ownership',async()=>{
  const r=rig();await r.a.dispatch({action:'target',op:'init',profile:'stm32h743',probe:'probe'});
  const saved=r.e.target.saved;
  await assert.rejects(r.a.dispatch({action:'target',op:'restore',state:{snapshot:r.e.target.info,saved:[[1,{mask:255}]],expected:[[1,6]]}}),/配置已变化/);
  assert.equal(r.e.target.saved,saved);await r.a.dispatch({action:'target',op:'close',restore:true});
});
test('cancellation is delivered even while an existing command is waiting',async()=>{
  const r=rig();let release;r.tools.dbg.runLine=()=>new Promise(resolve=>release=()=>resolve({cancelled:true}));
  const command=r.a.dispatch({action:'command',commands:['rc main']});await new Promise(resolve=>setImmediate(resolve));
  await r.a.dispatch({action:'cancel'});assert.equal(r.tools.dbg.cancelFlag,true);release();await assert.rejects(command,/中断/);
});
test('the same adapter exposes all Web debugger help, including image and watchpoint commands',async()=>{
  const {lines}=await rig().a.dispatch({action:'help'}),text=lines.map(l=>l.t).join('\n');
  for(const command of ['load_image','dump_image','wp','fill_inc','info locals','fin / out'])assert.ok(text.includes(command),command);
});
test('raw addresses work without ELF and info reports Hz in MHz correctly',async()=>{
  const s={connected:true,sym:null,clockHz:1000000,name:'probe',idcode:0,halted:true,pc:0,
    memRead:async()=>Uint8Array.of(1,2),bpAdd:async addr=>{assert.equal(addr,0x08000100);return {index:0};},
    statusInfo:()=>({halted:true,pc:0})};
  assert.ok((await runCmd('md 0x20000000 2',s)).lines.some(l=>l.t.includes('01 02')));
  await runCmd('b *0x08000100',s);
  assert.ok((await runCmd('info',s)).lines[0].t.includes('SWD 1 MHz'));
});
test('capture suspends/restores only its original active Web breakpoints',async()=>{
  const r=rig(),item={lease:{slot:1}};
  r.e.target.saved.clear();r.e.target.configure=async()=>({});
  r.e.target.breakpoints={items:[item],suspend:async()=>r.calls.push('suspend'),resume:async items=>{assert.deepEqual(items,[item]);r.calls.push('resume-bps');}};
  await r.a.dispatch({action:'target',op:'init',purpose:'capture',profile:'stm32h743',probe:'probe'});
  await r.a.dispatch({action:'target',op:'configure',options:{}});
  await r.a.dispatch({action:'target',op:'close',restore:true});
  assert.ok(r.calls.indexOf('suspend')<r.calls.indexOf('resume-bps'));
  assert.ok(r.calls.indexOf('resume-bps')<r.calls.indexOf('run'));
});
test('validation errors cannot restore a pending configuration from another CLI transaction',async()=>{
  const r=rig();await r.a.dispatch({action:'target',op:'init',purpose:'restore',profile:'stm32h743',probe:'probe'});
  await r.a.dispatch({action:'target',op:'close',restore:true});assert.ok(!r.calls.includes('restore'));
  await assert.rejects(r.a.dispatch({action:'target',op:'init',purpose:'capture',profile:'stm32h743',probe:'probe'}),/待恢复/);
});
