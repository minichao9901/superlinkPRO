// Actual production WebUsbDapProbe protocol over a byte-only USB bulk bridge.
import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
import {WebUsbDapProbe} from '../web/app/shared/rtt/dap-webusb.js';
import {EtmTarget} from '../web/app/target.js';
import {api,capture} from './spi-hardware.mjs';
async function usb(op,more={}){const response=await fetch('http://127.0.0.1:8768',{method:'POST',body:JSON.stringify({op,...more})}),data=await response.json();if(data.error)throw Error(data.error);return data.value;}
const p=new WebUsbDapProbe(),t=new EtmTarget(console.log),report={backend:'production WebUsbDapProbe + raw USB bulk byte bridge',passed:false};
p.device={transferOut:async(ep,data)=>{assert.equal(ep,2);return {bytesWritten:await usb('write',{bytes:[...data]})};},transferIn:async(ep)=>{assert.equal(ep,1);const bytes=Uint8Array.from(await usb('read'));return {data:new DataView(bytes.buffer)};}};
p.pkt=512;p.maxWords=126;p.epOut=2;p.epIn=1;p._framing='short';p._ready=true;p.onLog=console.log;t.probe=p;t.hseHz=25e6;
try{
  await p._targetInit({clock:1000000});report.initial=await t.inspect();assert.equal(report.initial.clocks.coreHz,70e6);
  const form=new FormData();form.append('elf',new Blob([await fs.readFile('firmware/stm32h7b0_spi_trace/stable-70-35-v1/fw.elf')]),'fw.elf');
  const response=await fetch('http://127.0.0.1:8766/api/elf',{method:'POST',body:form}),elf=await response.json();assert.ok(response.ok,elf.error);
  await t.verifyElf(elf);const fn=elf.functions.find(f=>f.name==='HAL_SPI_Transmit'),bp=t.breakpoints.add({address:fn.address,label:fn.name});await t.breakpoints.arm(bp);await t.breakpoints.suspend();
  report.reset=await t.reset({address:fn.address});assert.equal(await t.pc(),fn.address);assert.equal(p._faulted,false);
  report.capture=await capture(t,elf,{readResult:false,captureEntry:fn.address,seconds:.002});assert.equal(report.capture.job.state,'done',report.capture.job.error);
  const decoded=JSON.parse(await fs.readFile(`records/${report.capture.id}/decoded.json`,'utf8')),first=decoded.events.find(e=>e.kind==='instruction');
  assert.equal(first.pc,fn.address);assert.equal(first.bytes,'2de9f047');assert.equal(decoded.status.lastErrorCode,0);assert.equal(decoded.quality.overflowPackets,0);assert.equal(decoded.quality.unaccountedBytes,0);
  report.firstInstruction={pc:first.pc,bytes:first.bytes,function:first.function};report.summary=decoded.summary;report.quality=decoded.quality;report.transportFault=p._faulted;assert.equal(p._faulted,false);report.passed=true;
}catch(e){report.error=e.message;}
finally{
  try{t.assertTransport();await t.halt();await t.breakpoints.clear();await t.clearEntryBreak();await t.restore();await t.run();report.running=!(await t.isHalted());}catch(e){report.cleanupError=e.message;}
  t.bus.close();await usb('quit').catch(()=>{});
  await fs.writeFile('docs/validation/2026-10-11-webusb-control-reset-live.json',JSON.stringify(report,null,2));
  console.log(JSON.stringify({passed:report.passed,capture:report.capture?.id,first:report.firstInstruction,summary:report.summary,running:report.running,error:report.error,cleanupError:report.cleanupError,transportFault:report.transportFault}));
  process.exit(report.passed&&!report.cleanupError?0:1);
}
