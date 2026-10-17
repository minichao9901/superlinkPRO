// Hardware acceptance of the production target.js controller and Kingst service.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {EtmTarget} from '../web/app/target.js';
const pause=ms=>new Promise(r=>setTimeout(r,ms));
async function rpc(op,args={}){const response=await fetch('http://127.0.0.1:8767',{method:'POST',body:JSON.stringify({op,...args})});const data=await response.json();if(data.error)throw Error(data.error);return data.value;}
async function api(path,body){const response=await fetch('http://127.0.0.1:8766/api/'+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await response.json();if(data.error)throw Error(data.error);return data;}
const target=new EtmTarget(console.log);
target.probe={readMemDiagnostic:async(address,length)=>new Uint8Array(await rpc('read',{address,length})),readMem:async(address,length)=>new Uint8Array(await rpc('read',{address,length})),writeMem:(address,bytes)=>rpc('write',{address,bytes:[...bytes]}),halt:()=>rpc('halt'),run:()=>rpc('run'),isHalted:()=>rpc('state'),sysReset:()=>rpc('reset'),disconnect:()=>rpc('run')};
const report={};
try{
  report.initial=await target.inspect();
  assert.equal(report.initial.device&4095,0x480);
  await target.verifyElf(await api('default-elf'));
  report.configured=await target.configure({traceid:2});
  assert.equal(report.configured.config.config,1);
  const first=await api('capture',{sync:true,decode:true,rate:100000000,seconds:.01,config:target.config,targetVerified:true});
  for(let i=0;i<80;i++){const job=await api('jobs/'+first.job);if(job.state==='armed')break;if(job.state==='error')throw Error(job.error);await pause(100);}
  assert.equal((await api('jobs/'+first.job)).state,'armed');
  await target.run();
  for(let i=0;i<160;i++){const job=await api('jobs/'+first.job);if(job.state==='done')break;if(job.state==='error')throw Error(job.error);await pause(100);}
  const decoded=await api('records/'+first.record);
  assert.equal(decoded.status.lastErrorCode,0);
  assert.ok(decoded.summary.instructions>1000);
  report.linkedCapture={id:first.record,summary:decoded.summary};
  report.reset=await target.reset();
  assert.equal(report.reset.halted,true);
  await target.configure({traceid:2});
  const pending=await api('capture',{sync:true,decode:false,rate:100000000,seconds:.01,config:target.config,targetVerified:true});
  for(let i=0;i<80;i++){const job=await api('jobs/'+pending.job);if(job.state==='armed')break;if(job.state==='error')throw Error(job.error);await pause(100);}
  const stop=await api('stop/'+pending.job,{});
  assert.equal(stop.stopped,true);
  for(let i=0;i<160;i++){const job=await api('jobs/'+pending.job);if(job.state==='done'||job.state==='error'){report.stop=job;break;}await pause(100);}
  await target.run();
  await target.configure({traceid:2,returnStack:true,cycleCount:true});
  const counted=await api('capture',{sync:true,decode:true,rate:100000000,seconds:.01,config:target.config,targetVerified:true});
  for(let i=0;i<80;i++){const job=await api('jobs/'+counted.job);if(job.state==='armed')break;if(job.state==='error')throw Error(job.error);await pause(100);}
  await target.run();
  for(let i=0;i<160;i++){const job=await api('jobs/'+counted.job);if(job.state==='done')break;if(job.state==='error')throw Error(job.error);await pause(100);}
  const countedData=await api('records/'+counted.record);
  assert.equal(countedData.status.lastErrorCode,0);
  assert.ok(countedData.packets.some(p=>/CCNT|Cycle Count/.test(p.text)));
  report.returnStackAndCycles={id:counted.record,config:countedData.config,summary:countedData.summary};
  await target.configure({traceid:2});await target.run();
  report.passed=true;
}catch(e){report.error=e.message;report.passed=false;try{await target.run();}catch{}}
finally{await rpc('quit').catch(()=>{});await fs.writeFile('docs/validation/2026-10-11-live-controls.json',JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));process.exit(report.passed?0:1);}
