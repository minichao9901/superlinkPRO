// Line-delimited local stdio RPC. Reuse production configuration, not register copies.
import readline from 'node:readline';
import {EtmTarget} from '../web/app/target.js';

const send=value=>process.stdout.write(JSON.stringify(value)+'\n');
let next=0, pending=new Map(), initialHalted=true, initialized=false;
const rpc=(op,args={})=>new Promise((resolve,reject)=>{
  const id=++next;pending.set(id,{resolve,reject});send({type:'hardware',id,op,...args});
});
const target=new EtmTarget(message=>process.stderr.write(message+'\n'),{host:{}});
target.probe={
  readMemDiagnostic:async(address,length)=>Uint8Array.from(await rpc('read',{address,length})),
  writeMemDiagnostic:async(address,bytes)=>rpc('write',{address,bytes:[...bytes]}),
  halt:()=>rpc('halt'),run:()=>rpc('run'),
};
function state(){return {schema:1,snapshot:target.info,saved:[...target.saved],locks:[...target.locks],
  osLocked:target.osLocked,traceFamily:target.traceFamily,initialHalted};}
async function restore(){
  await target.halt();await target.restore();
  if(!initialHalted)await target.run();
}
async function command(message){
  let value;
  try{
    switch(message.action){
      case 'init':
        target.profile=message.profile;target.hseHz=message.hseHz;
        initialHalted=await target.isHalted();initialized=true;value=await target.inspect();break;
      case 'inspect':value=await target.inspect();break;
      case 'verify':value={checkedBytes:await target.verifyElf(message.elf)};break;
      case 'configure':
        value=await target.configure(message.options);value={snapshot:value,state:state()};break;
      case 'state':
        value=state();value.expected=[];
        for(const [address]of target.saved)value.expected.push([address,await target.read(address)]);
        break;
      case 'run':await target.run();value={halted:false};break;
      case 'restore':
        if(message.state){
          const saved=message.state,now=await target.inspect();
          if(now.cpuid!==saved.snapshot.cpuid||now.device!==saved.snapshot.device||now.profile!==saved.snapshot.profile)
            throw Error('目标身份与配置状态不匹配，拒绝恢复');
          for(const [address,expected]of saved.expected||[]){
            const mask=new Map(saved.saved).get(address)?.mask;
            const actual=await target.read(address);
            if(mask===undefined||((actual^expected)&mask)!==0)
              throw Error(`目标配置已变化，拒绝用旧状态覆盖：0x${address.toString(16)}，期望0x${expected.toString(16)}，回读0x${actual.toString(16)}；请使用 target stop 停止 Trace`);
          }
          target.saved=new Map(saved.saved);target.locks=new Map(saved.locks);
          target.osLocked=saved.osLocked;target.traceFamily=saved.traceFamily;initialHalted=saved.initialHalted;
        }
        await restore();value={restored:true,halted:await target.isHalted()};break;
      case 'stop':
        await target.stopTrace();if(!message.halt)await target.run();
        value={traceStopped:true,halted:await target.isHalted()};break;
      case 'close':
        if(message.restore&&initialized)await restore();
        send({type:'result',id:message.id,ok:true,value:{closed:true,restored:!!message.restore,
          halted:initialized?await target.isHalted():null}});process.exit(0);break;
      default:throw Error('Unknown target command');
    }
    send({type:'result',id:message.id,ok:true,value});
  }catch(error){send({type:'result',id:message.id,ok:false,error:error.message,state:state()});}
}
let queue=Promise.resolve();
const input=readline.createInterface({input:process.stdin});
input.on('line',line=>{
  const message=JSON.parse(line);
  if(message.type==='reply'){
    const task=pending.get(message.id);if(!task)return;pending.delete(message.id);
    if(message.error)task.reject(Error(message.error));else task.resolve(message.value);
  }else queue=queue.then(()=>command(message));
});
input.on('close',()=>process.exit(1));
