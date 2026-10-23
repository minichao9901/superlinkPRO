/** Standalone host for the existing Web command, debug and flash implementations. */
import {readFile} from 'node:fs/promises';
import {WebUSB} from 'usb';
import {navigatorUsb} from './usb-adapter.mjs';
import {finishTargetCommand} from './handoff.mjs';
import {localImages} from './files.mjs';
import readline from 'node:readline';
import {parseHTML} from 'linkedom';
import {WebUsbDapProbe} from '../../../web/app/rtt/dap-webusb.js';
import {DebugSession} from '../../../web/app/dbg/session.js';
import {SymTab} from '../../../web/app/dbg/symbols.js';
import {WatchList,formatWatchValue,summarizeTree,TREE_LIMITS} from '../../../web/app/dbg/watch.js';
import {runCmd} from '../../../web/app/dbg/cmd.js';
import {FlashView} from '../../../web/app/flash/view.js';
import {ALGOS} from '../../../web/app/flash/algos.js';
import {parseFirmware} from '../../../web/app/flash/image.js';
import {EtmTarget} from '../../web/app/target.js';
import {createTraceAutomation} from '../../../web/app/automation/trace.js';

const args=process.argv.slice(2),option=(name,fallback)=>{const i=args.indexOf(name);return i<0?fallback:args[i+1];};
const base=option('--web-url','http://127.0.0.1:8878'),tokenFile=option('--token-file'),serial=option('--probe');
const origin=new URL(base);
if(origin.protocol!=='http:'||!['127.0.0.1','localhost','[::1]'].includes(origin.hostname))throw Error('只允许本机命令队列');
if(!tokenFile)throw Error('缺少 --token-file');
const token=(await readFile(tokenFile,'ascii')).trim();
const usb=navigatorUsb(new WebUSB({allowedDevices:[{vendorId:0x0d28,...(serial?{serialNumber:serial}:{})}],deviceTimeout:5000}));
Object.defineProperty(globalThis,'navigator',{configurable:true,value:{usb}});
const {document,window}=parseHTML(await readFile(new URL('../../../web/index.html',import.meta.url),'utf8'));
globalThis.document=document;
// No UI is executed. The flash view's existing options/results are its adapter.
for(const e of document.querySelectorAll('select'))Object.defineProperty(e,'value',{writable:true,configurable:true,value:e.querySelector('option[selected],option')?.getAttribute('value')||''});
for(const e of document.querySelectorAll('input[type=checkbox]'))e.checked=e.hasAttribute('checked');
document.getElementById('toasts')?.remove();
globalThis.confirm=()=>{throw Error('无人值守模式拒绝需要交互确认的镜像，请使用完整ELF/BIN');};
const log=message=>process.stderr.write(String(message)+'\n');
const session=new DebugSession();session.log=log;
const target=new EtmTarget(log,{host:{probeType:WebUsbDapProbe}});
let suspended=null;
async function exactProbe(){
  const devices=(await usb.getDevices()).filter(d=>!serial||d.serialNumber===serial);
  if(devices.length!==1)throw Error('必须唯一指定 CMSIS-DAP 探针 --probe；匹配 '+devices.length);
  return devices[0];
}
const watch=new WatchList(),view={watch,
  addWatch:expr=>watch.add(expr,session.sym),delWatch:what=>watch.remove(what),watchItems:()=>watch.items,
  showSource:async(file,line)=>{view.sourceLocation={file,line};return true;},
};
async function refreshWatch(){
  if(!session.halted)return;
  for(const item of watch.items){
    if(item.error)continue;
    try{
      const tree=['struct','union','array'].includes(item.kind);
      const bytes=await session.memRead(item.addr,tree?Math.max(1,Math.min(item.size||1,TREE_LIMITS.maxBytes)):(item.size||4));
      item.bytes=tree?bytes:null;
      item.value=tree?{text:summarizeTree(item,bytes)}:formatWatchValue(item,bytes);
    }catch(error){item.bytes=null;item.value={text:'读失败：'+error.message,cls:'err'};}
  }
}
// Local image commands get an explicitly named directory, with traversal and
// symlink checks. dump_image reserves a new file and never clobbers an old one.
const imageRoot=option('--image-root');
if(imageRoot){
  view.imageFiles=await localImages(imageRoot);
}
const dbg={session,sym:null,cancelFlag:false,_out:log,_syncBackend:async()=>{},
  async connect(){
    if(target.probe){await target.disconnect();target.probe=null;}
    if(!session.connected){
      const device=await exactProbe();
      session.probe=await WebUsbDapProbe.open(device,{clockKhz:Number(document.getElementById('d-clock').value)||1000});
      session.name=session.probe.name;session.idcode=session.probe.idcode;
      session.clockHz=session.probe.clockHz;session.backendName='WebUSB (Node USB adapter)';
      await session.refresh();await session.verifyClock();await session.bpInit();
      if(suspended){
        const halted=await session.probe.isHalted();await session.ensureHalted();
        for(const bp of suspended.breakpoints)await session.bpAdd(bp.addr,bp.note);
        for(const wp of suspended.watchpoints)await session.dwt.add(wp.addr,wp.size,wp.mode);
        suspended=null;if(!halted)await session.run();
      }
    }
    return true;
  },
  async disconnect(){
    await session.exclusive(async()=>{
      if(session.connected&&(session.bps.length||session.dwt.items.length)){
        const halted=await session.probe.isHalted();await session.ensureHalted();
        await session.bpClear();await session.dwt.clear();if(!halted)await session.run();
      }
      await session.disconnect();suspended=null;
    });
  },
  loadElfBuffer(buf){dbg.sym=session.sym=SymTab.fromBuffer(buf);},
  async runLine(line){
    try{return await session.exclusive(async()=>{
      await session.refresh();
      await refreshWatch();
      const result=await runCmd(line,session,{view,signal:()=>dbg.cancelFlag});
      await session.refresh();await refreshWatch();return result;
    });}catch(e){return e.cancelled?{cancelled:true,lines:[]}:{error:e.message};}
  },
};
const flash=new FlashView();
flash._log=log;flash._status=log;flash._bar=()=>{};
flash.flash=async()=>{
  const chip=document.getElementById('f-chip').value;
  if(!Object.hasOwn(ALGOS,chip))throw Error('该网页烧录算法不支持此芯片：'+chip);
  flash._devId=null;
  const regions=parseFirmware(flash.file.name,Uint8Array.from(atob(flash.file.dataB64),c=>c.charCodeAt(0)),Number(document.getElementById('f-base').value)||0);
  for(const segment of regions)flash._checkRange(segment,ALGOS[chip]);
  await dbg.disconnect();if(target.probe)await target.disconnect();
  flash.busy=true;
  // A CLI request always verifies the programmed image; it never silently
  // inherits an unchecked GUI option from an unrelated prior session.
  document.getElementById('f-verify').checked=true;
  document.getElementById('f-reset').checked=true;
  try{await flash._flashWebusb(flash.file.name);}finally{await flash._closeProbe();flash.busy=false;}
};
const e={target,state:{operation:false,hardwareOperation:false,job:null,waiting:false}};
const etm={frame:{contentWindow:{__etm:e}},onShow:async()=>{},
  async run(fn){
    if(session.connected){
      suspended={breakpoints:session.bpList(),watchpoints:session.dwt.items.map(item=>({...item}))};
      await session.exclusive(async()=>{await session.bpClear();await session.disconnect();});
    }
    return fn();
  },
  summary:()=>({ready:true,connected:!!target.probe,busy:e.state.operation,target:target.info?.name||null}),
  releaseProbe:async()=>{if(target.probe)await target.disconnect();},
};
const tools={dbg,flash,etm};
const automation=createTraceAutomation(tools,{document,navigator:{usb}});
const worker=crypto.randomUUID().replaceAll('-','');
async function post(path,value){
  const response=await fetch(new URL('/trace-cli/'+path,origin),{method:'POST',headers:{'Content-Type':'application/json','X-Trace-CLI':token},body:JSON.stringify(value),signal:AbortSignal.timeout(10000)});
  if(!response.ok)throw Error('CLI队列HTTP '+response.status);
  return response.json();
}
let stopping=false,cleaned=false;
async function shutdown(){
  if(cleaned)return;cleaned=true;stopping=true;dbg.cancelFlag=true;target.cancelEntry=true;
  try{if(target.probe)await target.disconnect();}catch(e){log('ETM清理失败：'+e.message);process.exitCode=4;}
  try{if(session.connected){const halted=await session.probe.isHalted();await session.ensureHalted();await session.bpClear();if(!halted)await session.run();await session.disconnect();}}catch(e){log('调试清理失败：'+e.message);process.exitCode=4;}
}
const inFlight=new Set();
const input=readline.createInterface({input:process.stdin});
input.on('line',line=>{if(line==='stop'){stopping=true;dbg.cancelFlag=true;target.cancelEntry=true;}});
input.on('close',()=>{stopping=true;dbg.cancelFlag=true;target.cancelEntry=true;});
process.on('SIGINT',()=>{stopping=true;dbg.cancelFlag=true;target.cancelEntry=true;process.exitCode=130;});
log('Native Web command worker ready; pyOCD is not loaded.');
try{
  let prefetched;
  for(let i=0;;i++){
    try{prefetched=await post('poll',{worker});break;}
    catch(e){if(i>=24||!e.cause||e.cause.code!=='ECONNREFUSED')throw e;await new Promise(r=>setTimeout(r,200));}
  }
  while(!stopping){
    const {job}=prefetched||await post('poll',{worker});prefetched=null;if(!job)continue;
    const task=(async()=>{
      let result;
      try{
        result={ok:true,value:await automation.dispatch(job.request)};
        await finishTargetCommand(job.request,{target,dbg,hasSuspended:()=>suspended!==null});
      }catch(e){result={ok:false,error:e.message,results:e.results};}
      await post('complete/'+job.id,{worker,result});
    })().catch(e=>{log('CLI结果回传失败：'+e.message);stopping=true;});
    inFlight.add(task);task.finally(()=>inFlight.delete(task));
  }
}finally{await Promise.allSettled([...inFlight]);await shutdown();await usb.dispose();input.close();process.stdin.pause();}
