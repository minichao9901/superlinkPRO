/** Exercise the production FlashView with real USB and a local DOM adapter. */
import fs from 'node:fs/promises';
import {createRequire} from 'node:module';
import {createHash} from 'node:crypto';
import {FlashView} from '../web/app/flash/view.js';
import {navigatorUsb} from '../etm/tools/node-transport/usb-adapter.mjs';
const require=createRequire(new URL('../etm/tools/node-transport/package.json',import.meta.url));
const {WebUSB}=require('usb'),{parseHTML}=require('linkedom');
const [file,folder,base='0x08000000',rounds='1']=process.argv.slice(2);
if(!file||!folder)throw Error('Pass image, new output directory, optional base and rounds');
await fs.mkdir(folder,{recursive:false});
const usb=navigatorUsb(new WebUSB({allowedDevices:[{vendorId:0x0d28,productId:0x0204}],deviceTimeout:3000}));
Object.defineProperty(globalThis,'navigator',{configurable:true,value:{usb}});
const {document,window}=parseHTML(await fs.readFile(new URL('../web/index.html',import.meta.url),'utf8'));
globalThis.document=document;globalThis.window=window;globalThis.confirm=()=>{throw Error('Unexpected confirmation required');};
for(const el of document.querySelectorAll('select'))Object.defineProperty(el,'value',{writable:true,configurable:true,value:el.querySelector('option[selected],option')?.getAttribute('value')||''});
for(const el of document.querySelectorAll('input[type=checkbox]'))el.checked=el.hasAttribute('checked');
document.getElementById('toasts')?.remove();
document.getElementById('f-chip').value='stm32f103';document.getElementById('f-base').value=base;
document.getElementById('f-speed').value='10000';document.getElementById('f-verify').checked=true;document.getElementById('f-reset').checked=true;
const view=new FlashView(),bytes=await fs.readFile(file),result={image:file,imageSha256:createHash('sha256').update(bytes).digest('hex'),rows:[]};
view._log=text=>process.stderr.write(text+'\n');view._status=view._log;view._bar=()=>{};
view.file={name:file.split(/[\\/]/).pop(),size:bytes.length,dataB64:bytes.toString('base64')};
try{
 for(let i=0;i<Number(rounds);i++){
  const started=performance.now();
  try{await view._flashWebusb(view.file.name);}finally{await view._closeProbe();}
  const text=document.getElementById('f-result').textContent;
  if(!text.includes('校验通过'))throw Error('Flash verification not confirmed: '+text);
  result.rows.push({round:i,seconds:(performance.now()-started)/1000,result:text});
 }
 result.passed=true;
}catch(error){result.error=error.stack;process.exitCode=1;}
finally{await usb.dispose();await fs.writeFile(folder+'/result.json',JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));}
