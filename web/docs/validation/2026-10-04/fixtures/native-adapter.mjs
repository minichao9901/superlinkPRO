import {spawn} from 'node:child_process';
import readline from 'node:readline';
export async function nativeProbe(){
 const child=spawn('C:/Users/Administrator/AppData/Local/Programs/Python/Python313/python.exe',['-u','tmp/patch-verification/native-rpc.py'],{stdio:['pipe','pipe','inherit'],windowsHide:true});
 let seq=0;const pending=new Map();
 readline.createInterface({input:child.stdout}).on('line',line=>{const r=JSON.parse(line),p=pending.get(r.id);pending.delete(r.id);r.error?p?.reject(new Error(r.error)):p?.resolve(r.result);});
 child.on('exit',code=>{for(const p of pending.values())p.reject(new Error(`native adapter exit ${code}`));pending.clear();});
 const rpc=(op,opts={})=>new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject});child.stdin.write(JSON.stringify({id,op,...opts})+'\n');});
 const meta=await rpc('meta'),events=new Set(),stats={closes:0,resets:0,releases:[],claims:[]};
 const hd={...meta,opened:false,collections:[{usagePage:0xff00,usage:1}],productName:'akaLinkPro native test adapter',
  open:async()=>{hd.opened=true;},close:async()=>{hd.opened=false;},addEventListener:(_e,fn)=>events.add(fn),removeEventListener:(_e,fn)=>events.delete(fn),
  sendReport:async(id,data)=>{const reply=Uint8Array.from(await rpc('hid',{data:Array.from(data)}));for(const fn of events)fn({device:hd,reportId:2,data:new DataView(reply.buffer)});},
 };
 // WebUSB preserves submission order on an endpoint. The Python worker pool
 // does not: concurrent synchronous libusb writes may enter the OS in reverse
 // order. Serialize writes per endpoint at the JS adapter boundary so this
 // fixture does not scramble the production CS_HOLD frame stream.
 const outChains=new Map();
 const orderedWrite=(ep,data)=>{
  const p=(outChains.get(ep)||Promise.resolve()).then(()=>rpc('write',{ep,data:Buffer.from(data).toString('base64')}));
  outChains.set(ep,p.catch(()=>{}));return p;
 };
 const ud={...meta,opened:false,
  open:async()=>{ud.opened=true;},close:async()=>{await rpc('close');stats.closes++;ud.opened=false;},
  selectConfiguration:async()=>{},claimInterface:async iface=>{await rpc('claim',{iface});stats.claims.push(iface);},
  releaseInterface:async iface=>{await rpc('release',{iface});stats.releases.push(iface);},
  clearHalt:async(dir,ep)=>rpc('clear',{ep:ep|(dir==='in'?128:0)}),
  reset:async()=>{await rpc('reset');stats.resets++;},
  transferIn:async(ep,size)=>{const b=Buffer.from(await rpc('read',{ep:ep|128,size}),'base64');return {status:'ok',data:new DataView(b.buffer,b.byteOffset,b.byteLength)};},
  transferOut:async(ep,data)=>({status:'ok',bytesWritten:await orderedWrite(ep,data)}),
 };
 Object.defineProperty(globalThis,'navigator',{configurable:true,value:{hid:{getDevices:async()=>[hd],addEventListener(){},removeEventListener(){}},usb:{getDevices:async()=>[ud],addEventListener(){},removeEventListener(){}}}});
 return {hd,ud,stats,rpc,close:()=>child.stdin.end()};
}
