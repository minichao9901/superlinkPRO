/** Real RTT Viewer protocol over the production WebUSB memory adapter. */
import fs from 'node:fs/promises';import {createRequire} from 'node:module';import {createHash} from 'node:crypto';
import {WebUsbDapProbe} from '../web/app/rtt/dap-webusb.js';import {Rtt} from '../web/app/rtt/protocol.js';import {navigatorUsb} from '../etm/tools/node-transport/usb-adapter.mjs';
const require=createRequire(new URL('../etm/tools/node-transport/package.json',import.meta.url)),{WebUSB}=require('usb');
const [folder,cb,speed='45000000']=process.argv.slice(2);if(!folder||!cb)throw Error('Pass new output and control-block address');await fs.mkdir(folder,{recursive:false});
const usb=navigatorUsb(new WebUSB({allowedDevices:[{vendorId:0x0d28,productId:0x0204}],deviceTimeout:3000}));Object.defineProperty(globalThis,'navigator',{configurable:true,value:{usb}});
let p;const result={};try{
 const devices=await usb.getDevices();if(devices.length!==1)throw Error('Unique probe required');p=await WebUsbDapProbe.open(devices[0],{clockKhz:1000,framing:'short'});await p.setClock(Number(speed));p.fast=true;result.requestedHz=Number(speed);
 const rtt=new Rtt(p,{addr:Number(cb)});await rtt.init();const chunks=[],started=performance.now();
 while(performance.now()-started<3000){const v=await rtt.readUp(0);if(v.bytes?.length)chunks.push(v.bytes.slice());}
 const seconds=(performance.now()-started)/1000,raw=Buffer.concat(chunks.map(x=>Buffer.from(x)));await fs.writeFile(folder+'/received.bin',raw);
 const text=raw.toString('ascii'),matches=[...text.matchAll(/seq (\d{7})\r\n/g)];let gaps=0;
 for(let i=1;i<matches.length;i++)if(Number(matches[i][1])!==(Number(matches[i-1][1])+1)%10000000)gaps++;
 Object.assign(result,{seconds,bytes:raw.length,MBps:raw.length/seconds/1e6,records:matches.length,gaps,sha256:createHash('sha256').update(raw).digest('hex'),controlBlock:Number(cb),passed:matches.length>1000&&gaps===0});
 if(!result.passed)process.exitCode=1;
}catch(error){result.error=error.stack;process.exitCode=1;}finally{if(p)await p.disconnect();await usb.dispose();await fs.writeFile(folder+'/result.json',JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));}
