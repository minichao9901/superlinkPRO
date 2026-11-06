/** Read target identity/clocks and optional Flash SHA; never halts or resets CPU. */
import fs from 'node:fs/promises';import {createRequire} from 'node:module';import {createHash} from 'node:crypto';
import {WebUsbDapProbe} from '../web/app/rtt/dap-webusb.js';import {navigatorUsb} from '../etm/tools/node-transport/usb-adapter.mjs';
const require=createRequire(new URL('../etm/tools/node-transport/package.json',import.meta.url)),{WebUSB}=require('usb');
const [output,expectedFlashSha]=process.argv.slice(2);if(!output)throw Error('Pass a new JSON output path');
const usb=navigatorUsb(new WebUSB({allowedDevices:[{vendorId:0x0d28,productId:0x0204}],deviceTimeout:3000}));Object.defineProperty(globalThis,'navigator',{configurable:true,value:{usb}});let p;
try{
 const d=await usb.getDevices();if(d.length!==1)throw Error('Unique probe required');p=await WebUsbDapProbe.open(d[0],{clockKhz:1000,framing:'short'});
 const result={device:await p._readWord(0xe0042000),cpuid:await p._readWord(0xe000ed00),dhcsr:await p._readWord(0xe000edf0),cr:await p._readWord(0x40021000),cfgr:await p._readWord(0x40021004),flashAcr:await p._readWord(0x40022000),cfsr:await p._readWord(0xe000ed28),hfsr:await p._readWord(0xe000ed2c)};
 const c=result.cfgr,sws=c>>>2&3,mul=Math.min(16,(c>>>18&15)+2),source=c&(1<<16)?8/(c&(1<<17)?2:1):4;
 result.computedHclkMHz=(sws===2?source*mul:sws===1?8:8)/[1,1,1,1,1,1,1,1,2,4,8,16,64,128,256,512][c>>>4&15];result.clockBasis='RCC calculation with board HSE=8MHz; not external frequency measurement';result.running=!(result.dhcsr&(1<<17));
 if(expectedFlashSha){result.flashSha256=createHash('sha256').update(await p.readMem(0x08000000,524288)).digest('hex');result.flashUnchanged=result.flashSha256===expectedFlashSha;if(!result.flashUnchanged)throw Error('Full Flash differs');}
 await fs.writeFile(output,JSON.stringify(result,null,2),{flag:'wx'});console.log(JSON.stringify(result,null,2));
}finally{if(p)await p.disconnect();await usb.dispose();}
