// 同一 70/35 固件，验证生产控制器的函数入口采集；不烧录、不改频。
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {api,controller,capture,rpc} from './spi-hardware.mjs';
const target=controller(),report={profile:'spi-70',operation:'one-shot function entry capture'};
try{
  const elf=await api('profiles/spi-70',{});await target.verifyElf(elf);
  const fn=elf.functions.find(f=>f.name==='HAL_SPI_Transmit');assert.ok(fn);
  const before=await target.read(0xe0002000),capacity=((before>>>8)&0x70)|((before>>>4)&15);
  const slots=[];for(let n=0;n<capacity;n++)slots.push(await target.read(0xe0002008+4*n));
  report.entry=fn;report.haltedAt=await target.runTo(fn.address);
  assert.equal(await target.pc(),fn.address);assert.equal(target.entryBreak,null);
  const after=[];for(let n=0;n<capacity;n++)after.push(await target.read(0xe0002008+4*n));assert.deepEqual(after,slots);
  report.breakpointsRestored=true;
  report.capture=await capture(target,elf,{readResult:false,captureEntry:fn.address,seconds:.001});
  assert.equal(report.capture.job.state,'done',report.capture.job.error);
  const decoded=JSON.parse(await fs.readFile(`records/${report.capture.id}/decoded.json`,'utf8'));
  const first=decoded.events.find(e=>e.kind==='instruction');
  assert.equal(first.pc,fn.address);assert.equal(decoded.status.lastErrorCode,0);
  assert.equal(decoded.targetSnapshot.clocks.coreHz,70e6);assert.equal(decoded.targetSnapshot.clocks.physicalTraceHz,35e6);
  assert.equal(decoded.quality.overflowPackets,0);assert.equal(decoded.quality.unaccountedBytes,0);
  report.firstInstruction={pc:first.pc,bytes:first.bytes,mnemonic:first.mnemonic,function:first.function};
  report.summary=decoded.summary;report.quality=decoded.quality;report.filter=decoded.laFilter;report.passed=true;
}catch(e){report.error=e.message;report.passed=false;}
finally{
  try{await target.clearEntryBreak();await target.restore();await target.run();report.running=!(await rpc('state'));report.restored=true;}catch(e){report.restoreError=e.message;}
  target.bus.close();await rpc('quit').catch(()=>{});
  await fs.writeFile(process.argv[2]||'docs/validation/2026-10-11-v3-function-entry.json',JSON.stringify(report,null,2));
  console.log(JSON.stringify({passed:report.passed,capture:report.capture?.id,first:report.firstInstruction,summary:report.summary,error:report.error,restoreError:report.restoreError,running:report.running}));
  process.exit(report.passed&&!report.restoreError?0:1);
}
