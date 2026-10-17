// 固定 70/35 档：独立短窗口复验 + 10 ms 无地址过滤业务窗口。
import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
import {api,controller,capture,rpc} from './spi-hardware.mjs';
const target=controller();const report={profile:'spi-70',captures:[],scope:'all-addresses business window; startup not captured'};
try{
  const elf=await api('profiles/spi-70',{});
  await target.verifyElf(elf);
  report.elfSha=elf.sha256;
  for(const seconds of [.001,.01]){
    const reset=await target.reset({address:elf.captureEntry});
    assert.equal(reset.clocks.coreHz,70000000);assert.equal(reset.clocks.physicalTraceHz,35000000);
    const saved=await capture(target,elf,{seconds,readResult:false});
    report.captures.push({seconds,...saved});
    if(saved.job.state!=='done')throw Error(saved.job.error);
    console.log(`${seconds*1000} ms: ${saved.id} / ${saved.job.summary.instructions} 条路径记录`);
  }
}catch(e){report.error=e.message;}
finally{
  try{await target.restore();await target.run();report.restored=true;report.running=!(await rpc('state'));}catch(e){report.restoreError=e.message;}
  target.bus.close();
  await fs.writeFile('docs/validation/2026-10-11-spi-70-35-baseline.json',JSON.stringify(report,null,2));
  console.log(JSON.stringify({error:report.error,restoreError:report.restoreError,restored:report.restored,running:report.running}));
  process.exit(report.error||report.restoreError?1:0);
}
