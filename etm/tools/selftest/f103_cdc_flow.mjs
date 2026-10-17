// Real CDC acceptance using the Web target controller, SWD bridge and Kingst API.
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import assert from 'node:assert/strict';
import {EtmTarget} from '../../web/app/target.js';
const apiRoot=process.env.ETM_API_ROOT||'http://127.0.0.1:8773/api/';
const bridge=process.env.ETM_BRIDGE_ROOT||'http://127.0.0.1:8772';
const elfPath=process.env.ETM_ELF||'firmware/stm32f103_cdc_trace/fw-ze.elf';
const reportPath=process.env.ETM_REPORT||'docs/validation/f103-cdc-48-ui-default.json';
const broadcast=process.env.ETM_BRANCH_BROADCAST==='1';
const rounds=Number(process.env.ETM_ROUNDS||3);
const seconds=Number(process.env.ETM_SECONDS||.01);
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function request(url,body){
  const r=await fetch(url,body===undefined?{}:body instanceof FormData?{method:'POST',body}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data=await r.json();if(!r.ok)throw Error(data.error||r.statusText);return data;
}
const api=(path,body)=>request(apiRoot+path,body),rpc=(path,body={})=>request(bridge+path,body);
const t=new EtmTarget(message=>console.log(message));
t.probe={readMemDiagnostic:async(address,length)=>new Uint8Array(Buffer.from((await rpc('/read',{address,length})).data,'base64')),
  writeMemDiagnostic:async(address,bytes)=>rpc('/write',{address,data:Buffer.from(bytes).toString('base64')}),
  halt:()=>rpc('/halt'),run:()=>rpc('/run')};
t.hseHz=8e6;
const report={elfPath,seconds,branchBroadcast:broadcast,captures:[],checks:[],created:new Date().toISOString()};
try{
  const form=new FormData();form.append('elf',new Blob([await readFile(elfPath)]),'fw-ze.elf');
  const elf=await api('elf',form);const entry=elf.functions.find(f=>f.name==='cdc_acm_init').address;
  report.elf=elf.sha256;report.entry=entry;
  await t.verifyElf(elf);report.checks.push('Flash matches ELF');
  for(let round=0;round<rounds;round++){
    let jobId=null;
    try{
      await t.reset({address:entry});assert.equal(await t.pc(),entry);
      let info=await t.configure({branchBroadcast:broadcast});
      assert.equal(info.clocks.coreHz,48e6);assert.equal(info.clocks.physicalTraceHz,24e6);
      assert.equal(Boolean(info.config.ctrl&0x100),broadcast);
      const cfgr=await t.read(0x40021004);
      assert.ok(cfgr&(1<<22),'48 MHz PLL must feed USB without /1.5');
      report.identity=info;report.usbClockHz=48e6;
      const created=await api('capture',{elfId:elf.elfId,rate:500000000,seconds,threshold:2,sync:true,decode:true,
        clock:0,data:[1,2,3,4],fraction:.25,config:info.config,targetVerified:true,targetSnapshot:info,
        captureScope:'business-unfiltered-window',captureEntry:entry});
      jobId=created.job;let resumed=false,completed=false;
      for(let i=0;i<600;i++){
        const job=await api('jobs/'+jobId);
        if(job.state==='armed'&&!resumed){await sleep(100);await t.run();resumed=true;}
        if(job.state==='error')throw Error(job.error);
        if(job.state==='done'){
          assert.ok(resumed,'LA completed before CPU resumed');
          const r=await api('browse/'+created.record+'/overview');
          assert.equal(r.protocol,'ETMv3');assert.ok(r.summary.instructions>5000);
          report.captures.push({record:created.record,entryPC:entry,summary:r.summary,quality:r.quality,
            capture:r.capture,physicalClock:r.physical.clock_runs});
          console.log('CDC capture',created.record,'instructions',r.summary.instructions,'overflow',r.quality.overflowPackets);
          completed=true;break;
        }
        await sleep(250);
      }
      if(!completed)throw Error('Capture timed out');
    }finally{
      if(jobId){const job=await api('jobs/'+jobId).catch(()=>null);if(job&&!['done','error'].includes(job.state))await api('stop/'+jobId,{});}
      await t.clearEntryBreak();await t.restore();
    }
  }
  report.overflowFree=report.captures.every(c=>c.quality.overflowPackets===0&&c.quality.packetIssues===0);
  report.checks.push('48/24 clock readback','USB PLL /1 readback','reset to CDC init','Kingst arm before CPU run');
}finally{
  try{await t.clearEntryBreak();await t.breakpoints.clear();await t.restore();}finally{t.bus.close();await rpc('/shutdown').catch(()=>{});}
  await mkdir('docs/validation',{recursive:true});await writeFile(reportPath,JSON.stringify(report,null,2));
}
console.log('CDC acceptance result:',report.overflowFree?'overflow-free':'overflow remains',reportPath);
