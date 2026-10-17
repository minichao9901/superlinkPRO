// Non-desktop acceptance: the actual browser controller + real SWD + real capture API.
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import assert from 'node:assert/strict';
import {EtmTarget} from '../../web/app/target.js';
const apiRoot=process.env.ETM_API_ROOT||'http://127.0.0.1:8770/api/',bridge=process.env.ETM_BRIDGE_ROOT||'http://127.0.0.1:8772';
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function request(url,body){const r=await fetch(url,body===undefined?{}:body instanceof FormData?{method:'POST',body}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await r.json();if(!r.ok)throw Error(data.error||r.statusText);return data;}
const api=(path,body)=>request(apiRoot+path,body),rpc=(path,body={})=>request(bridge+path,body);
const t=new EtmTarget(message=>console.log(message));
t.probe={
  readMemDiagnostic:async(address,length)=>new Uint8Array(Buffer.from((await rpc('/read',{address,length})).data,'base64')),
  writeMemDiagnostic:async(address,bytes)=>rpc('/write',{address,data:Buffer.from(bytes).toString('base64')}),
  halt:()=>rpc('/halt'),run:()=>rpc('/run')
};
t.hseHz=8e6;const report={captures:[],checks:[]};
try{
  let info=await t.inspect();assert.equal(info.profile,'stm32f103ze');assert.equal(info.clocks.coreHz,48e6);assert.equal(info.clocks.physicalTraceHz,24e6);
  report.identity=info;
  const elfPath='firmware/stm32f103_cdc_trace/fw-ze-48-workload.elf';
  const form=new FormData();form.append('elf',new Blob([await readFile(elfPath)]),'fw-ze-48-workload.elf');
  const elf=await api('elf',form);const entry=elf.functions.find(f=>f.name==='trace_workload_round').address;
  await t.verifyElf(elf);const bp=t.breakpoints.add({address:entry,label:'trace_workload_round'});await t.breakpoints.arm(bp);
  for(const reset of [true,false,true]){
    await t.verifyElf(elf);const held=t.breakpoints.items.filter(b=>b.lease);await t.breakpoints.suspend();
    try{
      info=reset?await t.reset({address:entry}):await t.runTo(entry);
      assert.equal(await t.pc(),entry);report.checks.push(reset?'reset-to-entry':'run-to-entry');
      info=await t.configure();assert.equal(info.config.protocol,'ETMv3');
      assert.equal((info.config.ctrl&0xc01),0x800);assert.equal(info.width,8);
      const created=await api('capture',{elfId:elf.elfId,rate:500000000,seconds:Number(process.env.ETM_SECONDS||.002),threshold:2,sync:true,decode:true,
        clock:0,data:[1,2,3,4],fraction:.25,config:info.config,targetVerified:true,targetSnapshot:info,
        captureScope:'business-unfiltered-window',captureEntry:entry});
      let resumed=false;
      for(let i=0;i<400;i++){
        const job=await api('jobs/'+created.job);
        if(job.state==='armed'&&!resumed){await sleep(100);await t.run();resumed=true;}
        if(job.state==='error')throw Error(job.error);
        if(job.state==='done'){
          assert.ok(resumed,'LA completed before CPU was resumed');
          const result=await api('records/'+created.record);assert.equal(result.protocol,'ETMv3');
          assert.equal(result.quality.packetIssues,0);assert.equal(result.quality.overflowPackets,0);
          assert.equal(result.status.lastErrorCode,0);assert.ok(result.summary.instructions>5000);
          assert.ok(result.events.some(e=>e.function==='trace_even'));assert.ok(result.events.some(e=>e.function==='trace_odd'));
          report.captures.push({record:created.record,reset,summary:result.summary,quality:result.quality,entryPC:entry});
          await api('browse/'+created.record+'/timeline');await api('browse/'+created.record+'/window?start=1000&end=2000');
          break;
        }
        if(i===399)throw Error('Capture job timeout');await sleep(100);
      }
    }finally{await t.clearEntryBreak();await t.restore();await t.halt();await t.breakpoints.resume(held);}
  }
  const bad={...elf,segments:elf.segments.map((s,i)=>i?{...s}:{...s,data:Buffer.from([0,0,0,0]).toString('base64')})};
  await assert.rejects(t.verifyElf(bad),/不匹配/);report.checks.push('mismatched-ELF-rejected');
  await t.breakpoints.suspend();const cancelled=t.runTo(0x08070000,{timeout:1000});
  setTimeout(()=>{t.cancelEntry=true;},80);await assert.rejects(cancelled,/已取消/);
  assert.equal(t.entryBreak,null);assert.ok(await t.isHalted());report.checks.push('entry-cancel-cleanup');
  // Stop an armed LA while the CPU remains halted, rather than collecting an invented path.
  info=await t.configure();
  const stop=await api('capture',{elfId:elf.elfId,rate:500000000,seconds:Number(process.env.ETM_SECONDS||.002),threshold:2,sync:true,decode:false,
    clock:0,data:[1,2,3,4],config:info.config,targetSnapshot:info,targetVerified:true,fraction:.25});
  for(let i=0;i<100;i++){const job=await api('jobs/'+stop.job);if(job.state==='armed'){await api('stop/'+stop.job,{});report.checks.push('armed-LA-stop');break;}if(job.state==='error')throw Error(job.error);await sleep(100);}
  for(let i=0;i<200;i++){const job=await api('jobs/'+stop.job);if(['done','error'].includes(job.state)){report.stoppedJob=job;break;}await sleep(100);}
  assert.equal(report.stoppedJob?.state,'done');
  await mkdir('docs/validation',{recursive:true});await writeFile(process.env.ETM_REPORT||'docs/validation/f103-web-flow.json',JSON.stringify(report,null,2));
  console.log('F103 web flow passed',report.captures.map(c=>c.record));
}finally{
  try{await t.clearEntryBreak();await t.breakpoints.clear();await t.restore();}finally{t.bus.close();await rpc('/shutdown').catch(()=>{});}
}
