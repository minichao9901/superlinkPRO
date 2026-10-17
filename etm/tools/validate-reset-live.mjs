// 使用原有 70/35 固件验证持久断点、源码行和首指令采集；不烧录、不改时钟。
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {api,controller,capture,rpc,pause} from './spi-hardware.mjs';
const t=controller(),report={operation:'default reset-to-business capture after control-write fix',passed:false};
async function slots(){const ctrl=await t.read(0xe0002000),count=((ctrl>>>8)&0x70)|((ctrl>>>4)&15),values=[];for(let i=0;i<count;i++)values.push(await t.read(0xe0002008+4*i));return {ctrl,values};}
async function halted(){const until=Date.now()+3000;while(!(await t.probe.isHalted())){if(Date.now()>until)throw Error('断点命中超时');await pause(30);}return t.pc();}
let before;
try{
  report.identity=await t.inspect();assert.equal(report.identity.profile,'stm32h7b0');assert.equal(report.identity.clocks.coreHz,70e6);assert.equal(report.identity.clocks.physicalTraceHz,35e6);
  const form=new FormData();form.append('elf',new Blob([await fs.readFile('firmware/stm32h7b0_spi_trace/stable-70-35-v1/fw.elf')]),'fw.elf');
  const response=await fetch('http://127.0.0.1:8766/api/elf',{method:'POST',body:form}),elf=await response.json();assert.ok(response.ok,elf.error);await t.verifyElf(elf);before=await slots();
  const fn=elf.functions.find(f=>f.name==='HAL_SPI_Transmit'),line=(await api('elf/'+elf.elfId+'/breakpoint',{spec:'b main:255'})).candidates[0];
  report.lineBreakpoint=line;const first=t.breakpoints.add({address:fn.address,label:fn.name}),second=t.breakpoints.add({...line});await t.breakpoints.resume();report.hardwareCapacity=t.breakpoints.capacity;
  await t.run();report.firstHit=await halted();assert.ok([first.address,second.address].includes(report.firstHit));
  await t.run();report.continuedHit=await halted();assert.notEqual(report.firstHit,report.continuedHit);report.stepMaskCleared=!(await t.read(0xe000edf0)&8);assert.ok(report.stepMaskCleared);
  await t.breakpoints.suspend();await t.reset({address:fn.address});assert.equal(await t.pc(),fn.address);
  report.capture=await capture(t,elf,{readResult:false,captureEntry:fn.address,seconds:.002});assert.equal(report.capture.job.state,'done',report.capture.job.error);
  const decoded=JSON.parse(await fs.readFile(`records/${report.capture.id}/decoded.json`,'utf8')),instruction=decoded.events.find(e=>e.kind==='instruction');
  assert.equal(instruction.pc,fn.address);assert.equal(decoded.status.lastErrorCode,0);assert.equal(decoded.quality.overflowPackets,0);assert.equal(decoded.quality.unaccountedBytes,0);
  report.firstInstruction={pc:instruction.pc,bytes:instruction.bytes,function:instruction.function};report.summary=decoded.summary;report.quality=decoded.quality;
  const running=!(await t.probe.isHalted());await t.halt();await t.breakpoints.resume();if(running)await t.run();
  report.breakpointRestoredHit=await halted();assert.ok([first.address,second.address].includes(report.breakpointRestoredHit));
  await t.breakpoints.clear();const after=await slots();assert.deepEqual(after.values,before.values);assert.equal(after.ctrl&1,before.ctrl&1);report.slotsRestored=true;report.passed=true;
}catch(e){report.error=e.message;}
finally{
  try{await t.halt();await t.breakpoints.clear();await t.clearEntryBreak();await t.restore();await t.run();report.running=!(await rpc('state'));}catch(e){report.cleanupError=e.message;}
  t.bus.close();await rpc('quit').catch(()=>{});
  await fs.writeFile('docs/validation/2026-10-11-control-write-reset-live.json',JSON.stringify(report,null,2));
  console.log(JSON.stringify({passed:report.passed,capture:report.capture?.id,firstHit:report.firstHit,continuedHit:report.continuedHit,firstInstruction:report.firstInstruction,summary:report.summary,slotsRestored:report.slotsRestored,running:report.running,error:report.error,cleanupError:report.cleanupError}));
  process.exit(report.passed&&!report.cleanupError?0:1);
}
