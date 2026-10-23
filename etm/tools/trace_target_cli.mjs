// Local HTTP queue -> the existing WebUSB workbench and command engines.
import readline from 'node:readline';
import {readFileSync} from 'node:fs';
const base=process.env.TRACE_CLI_WEB||'http://127.0.0.1:8878';
const u=new URL(base);
if(!['127.0.0.1','localhost','[::1]'].includes(u.hostname)||u.protocol!=='http:')throw Error('CLI只允许本机HTTP工作台');
const tokenPath=process.env.TRACE_CLI_TOKEN_FILE;
const send=value=>process.stdout.write(JSON.stringify(value)+'\n');
async function request(path,body){
  if(!tokenPath)throw Error('请传 --token-file，启动启用了CLI队列的本地服务，并在浏览器打开CLI授权工作台');
  const token=readFileSync(tokenPath,'ascii').trim();
  const r=await fetch(new URL('/trace-cli/'+path,u),{method:body?'POST':'GET',headers:{'Content-Type':'application/json','X-Trace-CLI':token},body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(10000)});
  if(!r.ok){let detail='';try{detail=(await r.json()).error||'';}catch{}throw Object.assign(Error('网页CLI队列HTTP '+r.status+' '+detail),{status:r.status});}
  return r.json();
}
async function call(value){
  const {id}=await request('request',value),until=Date.now()+(value.action==='flash'?900000:200000);
  for(;;){
    const response=await request('result/'+id);
    if(response.state==='done'){
      try{await request('ack/'+id,{});}catch(e){if(![404,405].includes(e.status))console.error('命令已完成，队列回执失败：'+e.message);}
      return response.result;
    }
    if(Date.now()>until)throw Error('网页命令仍未结束；请web cancel后检查网页状态，禁止自动重试硬件写入');
    await new Promise(r=>setTimeout(r,100));
  }
}
async function command(message){
  try{
    const {action,...args}=message;
    const value=action==='web'?message.request:{...args,action:'target',op:action};
    const reply=await call(value);
    if(!reply.ok)throw Object.assign(Error(reply.error),{results:reply.results});
    let result=reply.value;
    if(action==='init')result={...result.snapshot,cliProbe:result.probe};
    send({type:'result',id:message.id,ok:true,value:result});
  }catch(error){send({type:'result',id:message.id,ok:false,error:error.message,results:error.results});}
}
let queue=Promise.resolve();
const input=readline.createInterface({input:process.stdin});
input.on('line',line=>{try{const message=JSON.parse(line);queue=queue.then(()=>command(message));}catch(e){send({type:'result',ok:false,error:e.message});}});
