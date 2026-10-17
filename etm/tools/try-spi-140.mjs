// Reproducible hardware experiment; KingstVIS filter is recorded, not set by Socket API.
import fs from 'node:fs/promises';
import {api,controller,capture} from './spi-hardware.mjs';
const target=controller();
const profile=process.argv[2]||'spi-140';
const trace70=profile==='spi-140';
const report={filter:{basis:'user-reported KingstVIS setting',channels:[0,1,2,3,4],samples:1,sampleRate:500000000,widthNs:2},captures:[]};
const compactCapture=async(...args)=>{const captured=await capture(...args);if(captured.result){const full=captured.result;captured.result=Object.fromEntries(['summary','quality','status','physical','laFilter'].map(k=>[k,full[k]]));}return captured;};
try{
  report.profile=profile;
  const elf=await api('profiles/'+profile,{});
  await target.verifyElf(elf);
  report.reset=await target.reset({address:elf.captureEntry});
  report.captures.push(await compactCapture(target,elf));
  for(const pinSpeed of (trace70?[2,1]:[3])){
    await target.reset({address:elf.captureEntry});
    report.captures.push({pinSpeed,...await compactCapture(target,elf,{etm:{pinSpeed}})});
  }
}catch(e){report.error=e.message;}
finally{
  try{report.restored=await target.restore();await target.run();}catch(e){report.restoreError=e.message;}
  target.bus?.close();
  await fs.writeFile(`docs/validation/2026-10-11-${profile}-filter1-retest.json`,JSON.stringify(report,null,2));
  console.log(JSON.stringify({profile,captures:report.captures.map(c=>({id:c.id,state:c.job.state,summary:c.result?.summary})),error:report.error,restoreError:report.restoreError},null,2));
  process.exit(report.error?1:0);
}
