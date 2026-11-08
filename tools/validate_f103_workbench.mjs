/** Real F103ZE checks over the production WebUSB/debug code; no Flash writes. */
import fs from 'node:fs/promises';
import {createRequire} from 'node:module';
import {createHash} from 'node:crypto';
import {WebUsbDapProbe} from '../web/app/legacy/rtt/dap-webusb.js';
import {DebugSession} from '../web/app/dbg/session.js';
import {navigatorUsb} from '../etm/tools/node-transport/usb-adapter.mjs';
const require=createRequire(new URL('../etm/tools/node-transport/package.json',import.meta.url));
const {WebUSB}=require('usb');
const folder=process.argv[2];if(!folder)throw Error('Pass a new evidence directory');
await fs.mkdir(folder,{recursive:false});
const usb=navigatorUsb(new WebUSB({allowedDevices:[{vendorId:0x0d28,productId:0x0204}],deviceTimeout:3000}));
Object.defineProperty(globalThis,'navigator',{configurable:true,value:{usb}});
const sha=b=>createHash('sha256').update(b).digest('hex'),result={hardware:'akaLinkPro + F103ZE',checks:[],restoration:{}};
let p,S,wasHalted,regs,ram,fpb,demcr,dwt,clock;const address=0x20001000;
try{
 const devices=await usb.getDevices();if(devices.length!==1)throw Error('Expected unique probe');
 p=await WebUsbDapProbe.open(devices[0],{clockKhz:1000,framing:'short'});
 result.probe={serial:devices[0].serialNumber,idcode:p.idcode};
 const id=await p._readWord(0xe0042000);if((id&4095)!==0x414)throw Error('F103ZE identity required');
 wasHalted=await p.isHalted();await p.halt();
 regs={};for(const i of [...Array(19).keys(),20])regs[i]=await p.regRead(i);
 await fs.writeFile(folder+'/core-private.json',JSON.stringify(regs));
 ram=await p.readMem(address,128);fpb=await p.readMem(0xe0002000,40);demcr=await p._readWord(0xe000edfc);dwt=await p.readMem(0xe0001020,64);
 clock=[await p._readWord(0x40021000),await p._readWord(0x40021004)];
 await fs.writeFile(folder+'/ram-private.bin',ram);await fs.writeFile(folder+'/fpb-private.bin',fpb);
 S=new DebugSession();S.probe=p;S.name=p.name;S.clockHz=p.clockHz;S.idcode=p.idcode;S.log=t=>process.stderr.write(t+'\n');
 await S.refresh();await S.bpInit();
 const old=await S.readReg('R12');await S.writeReg('R12',(old^0x55aa55aa)>>>0);
 if(await S.readReg('R12')!==((old^0x55aa55aa)>>>0))throw Error('Register roundtrip');await S.writeReg('R12',old);
 result.checks.push('register-read-write-restore');
 const code=Uint8Array.of(1,0x20,1,0x30,0xfe,0xe7);
 await p.writeMem(address,code);if(sha(await p.readMem(address,code.length))!==sha(code))throw Error('RAM bytes');
 await S.writeReg('PRIMASK',1);await S.writeReg('XPSR',0x01000000);await S.writeReg('PC',address);
 const steps=result.steps=[];
 for(let i=0;i<3;i++){
  await S.refresh();const before=await S.readReg('PC'),mode=await S.step(),after=await S.readReg('PC'),r0=await S.readReg('R0');
  steps.push({before,after,mode,r0,ok:S.lastStepOk});
  if(mode!=='dhcsr'||!S.lastStepOk)throw Error('Native C_STEP did not complete');
  if(i<2&&after!==address+(i+1)*2||i===2&&after!==address+4)throw Error('Unexpected step PC');
 }
 result.steps=steps;result.checks.push('C_STEP-normal-and-self-branch');
 const dhcsr=await p._readWord(0xe000edf0);if(dhcsr&12)throw Error('Step/MASKINTS residue');
 result.checks.push('step-mask-residue-cleared');
 // Put the application context back before checking a real Flash breakpoint.
 await p.writeMem(address,ram);
 for(const i of [...Array(13).keys(),14,17,18,20,16,13,15])await p.regWrite(i,regs[i]);
 await S.refresh();await S.bpAdd(0x08000f1e,'SysTick acceptance');await S.cont();
 const until=Date.now()+3000;for(;;){await S.refresh();if(S.halted)break;if(Date.now()>until)throw Error('SysTick breakpoint timeout');await new Promise(r=>setTimeout(r,5));}
 if(S.pc!==0x08000f1e)throw Error('Wrong ISR breakpoint PC');
 result.interruptBreakpoint={pc:S.pc};result.checks.push('interrupt-progress-after-step-and-FPB-hit');await S.bpClear();
 await S.dwt.add(address+16,4,'w');await S.dwt.clear();result.checks.push('DWT-configure-and-clear');
 result.passed=true;
}catch(error){result.error=error.stack;process.exitCode=1;}
finally{
 if(p){try{
  await p.setClock(1000000);await p.halt();
  if(ram){await p.writeMem(address,ram);result.restoration.ram=sha(await p.readMem(address,ram.length))===sha(ram);}
  if(dwt)await p.writeMem(0xe0001020,dwt);
  if(demcr!==undefined)await p.writeMem(0xe000edfc,Uint8Array.from([demcr&255,demcr>>>8&255,demcr>>>16&255,demcr>>>24]));
  if(fpb){await p.writeMem(0xe0002000,Uint8Array.of(2,0,0,0));await p.writeMem(0xe0002004,fpb.subarray(4));await p.writeMem(0xe0002000,Uint8Array.of(2|(fpb[0]&1),0,0,0));result.restoration.fpb=sha(await p.readMem(0xe0002000,fpb.length))===sha(fpb);}
  if(regs){for(const i of [...Array(13).keys(),14,17,18,20,16,13,15])await p.regWrite(i,regs[i]);result.restoration.core=true;for(const i of [...Array(19).keys(),20])if(await p.regRead(i)!==regs[i])result.restoration.core=false;}
  if(clock)result.restoration.clock=(await p._readWord(0x40021000))===clock[0]&&(await p._readWord(0x40021004))===clock[1];
  if(!wasHalted)await p.run();result.restoration.running=!await p.isHalted();
 }catch(error){result.restoration.error=error.stack;result.passed=false;process.exitCode=1;}finally{await p.disconnect();}}
 await usb.dispose();
 if(Object.values(result.restoration).some(v=>v===false)){result.passed=false;process.exitCode=1;}
 await fs.writeFile(folder+'/result.json',JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));
}
