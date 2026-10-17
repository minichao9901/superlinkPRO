import fs from 'node:fs/promises';
import {EtmTarget} from '../web/app/target.js';
export const pause=ms=>new Promise(r=>setTimeout(r,ms));
export async function rpc(op,args={}){const response=await fetch('http://127.0.0.1:8767',{method:'POST',body:JSON.stringify({op,...args})});const data=await response.json();if(data.error)throw Error(data.error);return data.value;}
export async function api(path,body){const response=await fetch('http://127.0.0.1:8766/api/'+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await response.json();if(!response.ok)throw Error(data.error||response.statusText);return data;}
export function controller(){const target=new EtmTarget(console.log);target.hseHz=25e6;target.probe={readMemDiagnostic:async(address,length)=>new Uint8Array(await rpc('read',{address,length})),readMem:async(address,length)=>new Uint8Array(await rpc('read',{address,length})),writeMem:(address,bytes)=>rpc('write',{address,bytes:[...bytes]}),halt:()=>rpc('halt'),run:()=>rpc('run'),isHalted:()=>rpc('state'),sysReset:()=>rpc('reset'),disconnect:()=>rpc('run')};return target;}
export async function jobDone(id){for(let n=0;n<600;n++){const job=await api('jobs/'+id);if(job.state==='done'||job.state==='error')return job;await pause(100);}throw Error('等待任务超时');}
export async function capture(target,elf,options={}){
  const {readResult=true,...captureOptions}=options;
  const info=await target.configure(options.etm||{});
  const created=await api('capture',{libraryOrigin:'validation',rate:500000000,seconds:.001,sync:true,decode:true,config:info.config,targetVerified:true,targetSnapshot:info,elfId:elf.elfId,...captureOptions});
  for(let n=0;n<100;n++){const job=await api('jobs/'+created.job);if(job.state==='armed')break;if(job.state==='error')throw Error(job.error);await pause(100);}
  if((await api('jobs/'+created.job)).state!=='armed')throw Error('LA 启动失败');
  await pause(100);await target.run();const job=await jobDone(created.job);
  return {id:created.record,job,result:job.state==='done'&&readResult?await api('records/'+created.record):null};
}
if(process.argv[2]==='backup'){
  const target=controller();await target.halt();
  const data=new Uint8Array(131072);for(let offset=0;offset<data.length;offset+=8192)data.set(await target.probe.readMem(0x08000000+offset,8192),offset);
  const path='docs/validation/2026-10-11-before-spi-flash-128k.bin';
  try{await fs.writeFile(path,data,{flag:'wx'});}catch(e){if(e.code!=='EEXIST')throw e;const old=await fs.readFile(path);if(!old.equals(data))throw Error('已有备份不同，拒绝覆盖');}
  const info=await target.inspect();info.pwr_cr3=await target.read(0x5802480c);info.backupBytes=data.length;
  await fs.writeFile('docs/validation/2026-10-11-before-spi-state.json',JSON.stringify(info,null,2));
  console.log('Flash backup saved:',data.length,'PWR_CR3',info.pwr_cr3.toString(16));
  const elf=await api('default-elf');await target.verifyElf(elf);const reset=await target.reset({address:elf.captureEntry});
  console.log('Reset-to-business:',reset.clocks,await target.pc());
  await target.restore();await rpc('quit');
}
