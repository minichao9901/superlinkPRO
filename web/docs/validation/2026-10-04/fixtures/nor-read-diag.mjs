import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {nativeProbe} from './native-adapter.mjs';
import {AkaLinkHid} from '../../app/hid/probe.js';
import {SpiSession} from '../../app/spi/session.js';
import {WebUsbDapProbe} from '../../app/rtt/dap-webusb.js';
import * as F from '../../app/spi/flash.js';
const n=await nativeProbe(),h=new AkaLinkHid(),spi=new SpiSession();let dap;
const sha=b=>createHash('sha256').update(b).digest('hex');
const send=async(items)=>{
 const r=await spi.sendFrames(items,{quiet:true,timeoutMs:3000});
 assert.equal(r.failed,0);for(const s of r.rsps){assert.ok(s);assert.equal(s.status,0);}
 return Buffer.concat(r.rsps.map(s=>Buffer.from(s.data)));
};
const fresh=(addr,n,chunk)=>Array.from({length:Math.ceil(n/chunk)},(_,k)=>F.readFrame({opcode:F.OP.READ,addr:addr+k*chunk,addrLen:3,dummy:0,lines:1,rx:Math.min(chunk,n-k*chunk)}));
try{
 await h.open(n.hd);await h.stop();await h.setTargetType(false);
 dap=await WebUsbDapProbe.open(n.ud,{clockKhz:1000,skipInfo:true,framing:'short'});
 assert.equal(await spi.connectHid(false),true);clearInterval(spi.pollTimer);spi.pollTimer=null;
 assert.equal(await spi.connectUsb(false),true);
 await spi.applyConfig({...spi.cfg,sclkHz:1000000,csPolicy:0,mode:0,bits:8});
 await spi.applyProfile({...spi.profile,profile:0,defLines:1});await spi.setEnabled(true);
 console.log('JEDEC', (await send(F.rdidItems())).toString('hex'),'SR1/SR2',(await send([...F.rdsr1Items(),...F.rdsr2Items()])).toString('hex'));
 const base=await send(fresh(0,4096,64));
 console.log('REFERENCE fresh64',sha(base),'nonFF',base.reduce((n,b)=>n+(b!==255),0));
 for(const [name,items] of [
  ['fresh128',fresh(0,4096,128)],
  ['continuous128',F.readItems(0,4096,{mode:F.OP.READ,dummy:0,chunk:128})],
  ['continuous492',F.readItems(0,4096,{mode:F.OP.READ,dummy:0,chunk:492})],
  ['continuous512',F.readItems(0,512,{mode:F.OP.READ,dummy:0,chunk:128})]
 ])for(const concurrent of [false,true]){
  let bad=0;
  for(let k=0;k<20;k++){
   const read=send(items);
   const b=concurrent?(await Promise.all([read,dap.readMem(0xe0042000,4)]))[0]:await read;
   const diffs=[];for(let j=0;j<b.length;j++)if(b[j]!==base[j])diffs.push(j);
   if(diffs.length){bad++;console.log('BAD',name,{concurrent,k,count:diffs.length,first:diffs[0],last:diffs.at(-1),sha:sha(b)});}
  }
  console.log('RESULT',name,concurrent?'withDAP':'withoutDAP',`bad=${bad}/20`);
 }
 console.log('STATUS',JSON.stringify(await spi.pollStatus(true)),'USB',JSON.stringify(n.stats));
 for(const sclkHz of [1000000,10000000]){
  await spi.applyConfig({...spi.cfg,sclkHz});
  // The user confirmed IO2/IO3 are pulled high, not wired to the probe.
  // Exercise only the supported single-wire command stream in acceptance.
  for(const [label,mode,dummy,chunk] of [['READ128',F.OP.READ,0,128],['READ492',F.OP.READ,0,492]]){
   const items=F.readItems(0,4096,{mode,dummy,chunk});
   let bad=0,first=null;
   for(let k=0;k<100;k++){
    const b=await send(items);let count=0,start=-1,last=-1;
    for(let j=0;j<b.length;j++)if(b[j]!==base[j]){count++;if(start<0)start=j;last=j;}
    if(count){bad++;first??={k,count,start,last,sha:sha(b)};}
   }
   console.log('EXTENDED',JSON.stringify({sclkHz,label,bad,iterations:100,first}));
  }
 }
 console.log('END STATUS',JSON.stringify(await spi.pollStatus(true)));
}finally{
 if(spi.connected||spi.transport)await spi.teardown();if(dap)await dap.disconnect();await h.configure({clockHz:60000000});await h.close();n.close();
}
