/** Production WebUSB SRAM timings; writes include the existing readback check. */
import fs from 'node:fs/promises';
import {createRequire} from 'node:module';
import {createHash} from 'node:crypto';
import {WebUsbDapProbe} from '../web/app/rtt/dap-webusb.js';
import {navigatorUsb} from '../etm/tools/node-transport/usb-adapter.mjs';
const require=createRequire(new URL('../etm/tools/node-transport/package.json',import.meta.url)),{WebUSB}=require('usb');
const folder=process.argv[2];if(!folder)throw Error('Pass a new output directory');await fs.mkdir(folder,{recursive:false});
const usb=navigatorUsb(new WebUSB({allowedDevices:[{vendorId:0x0d28,productId:0x0204}],deviceTimeout:3000}));Object.defineProperty(globalThis,'navigator',{configurable:true,value:{usb}});
const sha=b=>createHash('sha256').update(b).digest('hex'),size=20480,address=0x20000000,loops=4,result={rows:[],boundary:'production readMem / writeMem API wall time; write includes verification; no file IO in timing'};
const pattern=Uint8Array.from({length:size},(_,i)=>(i*37+19)&255);let p,old,halted,clock;
try{
 const devices=await usb.getDevices();if(devices.length!==1)throw Error('Unique probe required');p=await WebUsbDapProbe.open(devices[0],{clockKhz:1000,framing:'short'});
 halted=await p.isHalted();await p.halt();clock=[await p._readWord(0x40021000),await p._readWord(0x40021004)];old=await p.readMem(address,size);await fs.writeFile(folder+'/backup-private.bin',old);
 for(const speed of [10000,20000,30000,40000,45000,50000,60000]){
  await p.setClock(speed*1000);await p.writeMem(address,pattern);
  for(let round=0;round<3;round++){
   let t=performance.now();for(let i=0;i<loops;i++){if(sha(await p.readMem(address,size))!==sha(pattern))throw Error('Timed read bytes differ');}const readMs=performance.now()-t;
   t=performance.now();for(let i=0;i<loops;i++)await p.writeMem(address,pattern);const writeMs=performance.now()-t;
   if(sha(await p.readMem(address,size))!==sha(pattern))throw Error('Written bytes differ');
   result.rows.push({requestedKHz:speed,round,loops,bytesPerTransfer:size,readMs,writeVerifiedMs:writeMs,readMBps:size*loops/readMs/1000,writeVerifiedMBps:size*loops/writeMs/1000,byteExact:true});
  }
 }
 result.passed=true;
}catch(error){result.error=error.stack;process.exitCode=1;}
finally{
 if(p){try{await p.setClock(1000000);if(old){await p.writeMem(address,old);result.restored=sha(await p.readMem(address,size))===sha(old);}if(clock)result.clockUnchanged=(await p._readWord(0x40021000))===clock[0]&&(await p._readWord(0x40021004))===clock[1];if(!halted)await p.run();result.running=!await p.isHalted();}catch(error){result.restoreError=error.stack;process.exitCode=1;}finally{await p.disconnect();}}
 if(result.restored!==true||result.clockUnchanged!==true){result.passed=false;process.exitCode=1;}
 await usb.dispose();await fs.writeFile(folder+'/result.json',JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));
}
