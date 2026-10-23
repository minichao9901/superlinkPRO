/** CLI adapter. Hardware algorithms remain in the existing Web views/commands. */
import {runCmd} from '../dbg/cmd.js';

export function createTraceAutomation(tools, win=globalThis.window){
  let transaction=null, busy=false,lastDevices=[];
  const doc=win.document;
  const element=id=>{const e=doc.getElementById(id);if(!e)throw Error('工作台缺少控件：'+id);return e;};
  const set=(id,value)=>{const e=element(id);if(e.type==='checkbox')e.checked=!!value;else{e.value=String(value);if(e.value!==String(value))throw Error('不支持的选项：'+id+'='+value);}return e;};
  const bytes=b64=>Uint8Array.from(atob(b64),c=>c.charCodeAt(0));
  async function etm(){
    await tools.etm.onShow();
    for(let i=0;i<100;i++){
      const child=tools.etm.frame?.contentWindow;
      if(child?.__etm)return child.__etm;
      await new Promise(r=>setTimeout(r,50));
    }
    throw Error('ETM 子页面未就绪；请先启动本地工作台服务');
  }
  function authorized(probe){
    return win.navigator.usb.getDevices().then(devices=>{
      lastDevices=devices;
      const matches=devices.filter(d=>(!probe||d.serialNumber===probe)&&/akaLink|CMSIS|DAP/i.test(d.productName||''));
      if(matches.length!==1)throw Error('须授权并唯一选择探针；已匹配 '+matches.map(d=>d.serialNumber).join(','));
      if(devices.filter(d=>/akaLink|CMSIS|DAP/i.test(d.productName||'')).length!==1)throw Error('当前网页连接入口按首个设备选择，CLI拒绝多个已授权探针；请只授权一个或启动按序列号限定的Native worker');
      return matches[0];
    });
  }
  function checkProbe(p,probe){
    if(probe&&p?.device?.serialNumber!==probe&&p?.usb?.serialNumber!==probe&&p?.dev?.serialNumber!==probe)
      throw Error('当前网页探针与 --probe 不一致；请断开并在网页选择指定探针');
  }
  function targetState(t,initialHalted){return {schema:1,snapshot:t.info,saved:[...t.saved],locks:[...t.locks],
    osLocked:t.osLocked,traceFamily:t.traceFamily,initialHalted};}
  async function suspendBreakpoints(){
    if(!transaction||transaction.breakpoints!==null)return;
    const b=transaction.t.breakpoints;
    transaction.breakpoints=b?b.items.filter(item=>item.lease):[];
    transaction.e.state.pendingBreakpointRestore=transaction.breakpoints;
    if(b)await b.suspend();
  }
  async function resumeBreakpoints(){
    if(!transaction?.breakpoints)return;
    await transaction.t.halt();
    await transaction.t.breakpoints?.resume(transaction.breakpoints);
    transaction.breakpoints=null;transaction.e.state.pendingBreakpointRestore=[];
  }
  async function restore(t,initialHalted){await t.clearEntryBreak();await t.halt();await t.restore();await resumeBreakpoints();if(!initialHalted)await t.run();}
  async function debugConnect(m){
    if(!tools.dbg.session?.connected){
      await authorized(m.probe);
      set('d-backend','webusb');set('d-clock',(m.swdHz||1000000)/1000);set('d-rtt-on',false);
      await tools.dbg._syncBackend();
      if(!await tools.dbg.connect())throw Error('网页调试器连接失败；详情见调试页日志');
    }
    checkProbe(tools.dbg.session.probe,m.probe);
    return tools.dbg.session;
  }
  async function execute(m){
    if(m.action==='ping')return {protocol:'web-trace/1',ready:true,hardwareAccessed:false};
    if(m.action==='status')return {protocol:'web-trace/1',connected:!!tools.dbg.session?.connected,
      debug:tools.dbg.session?.statusInfo(),etm:tools.etm.summary(),flashBusy:!!tools.flash.busy,
      cached:busy,devices:(busy?lastDevices:(lastDevices=await win.navigator.usb.getDevices())).map(d=>({serialNumber:d.serialNumber,productName:d.productName,vendorId:d.vendorId,productId:d.productId}))};
    if(m.action==='cancel'){
      tools.dbg.cancelFlag=true;
      if(transaction){transaction.t.cancelEntry=true;}
      return {requested:true,debug:true,entry:!!transaction,flashInterruptSupported:false};
    }
    if(m.action==='target')return targetCommand(m);
    if(transaction)throw Error('CLI ETM 事务尚未结束，拒绝抢占探针');
    if(m.action==='connect'){const s=await debugConnect(m);return s.statusInfo();}
    if(m.action==='disconnect'){await tools.etm.releaseProbe?.();await tools.dbg.disconnect();return {disconnected:true};}
    if(m.action==='elf'){
      tools.dbg.loadElfBuffer(bytes(m.data).buffer,m.name);
      return {name:m.name,symbols:tools.dbg.sym?.all?.length,loaded:true};
    }
    if(m.action==='command'){
      const s=await debugConnect(m);tools.dbg.cancelFlag=false;
      const commands=m.commands||[];if(!commands.length||commands.length>1000)throw Error('命令数须为1～1000');
      const results=[];
      for(const line of commands){
        const result=await tools.dbg.runLine(line);
        results.push({command:line,...result});
        const failedLine=result.lines?.find(line=>line.c==='err');
        if(result.error||result.cancelled||failedLine)throw Object.assign(Error(result.error||failedLine?.t||'命令已中断'),{results});
      }
      return {results,status:s.statusInfo(),breakpoints:s.bpList()};
    }
    if(m.action==='wait-halt'){
      const s=await debugConnect(m),until=Date.now()+m.timeoutMs;tools.dbg.cancelFlag=false;
      while(true){
        if(tools.dbg.cancelFlag)throw Error('等待已取消');
        const value=await s.exclusive(()=>s.refresh());
        if(s.halted){if(m.pc!==undefined&&s.pc!==m.pc)throw Error('目标停在非预期地址：0x'+s.pc.toString(16));return value;}
        if(Date.now()>until)throw Error('等待暂停超时；未擅自复位或继续目标');
        await new Promise(r=>setTimeout(r,25));
      }
    }
    if(m.action==='flash'){
      await authorized(m.probe);
      // FlashView selects the first authorized device. Refuse ambiguity rather
      // than pretending its selector honors a serial argument.
      const ds=await win.navigator.usb.getDevices();
      if(ds.filter(d=>/akaLink|CMSIS|DAP/i.test(d.productName||'')).length!==1)throw Error('烧录需要网页只授权一个调试探针');
      set('f-backend','webusb');set('f-chip',m.chip);set('f-base',m.base||0);
      set('f-verify',true);set('f-reset',true);
      if(tools.flash.busy)throw Error('网页烧录器正在运行，请等待完成');
      tools.flash.file={name:m.name,dataB64:m.data};element('f-path').value='';
      await tools.flash.flash();
      return {programmed:true,name:m.name,chip:m.chip,result:element('f-result').textContent};
    }
    if(m.action==='help')return runCmd('help',tools.dbg.session);
    throw Error('未知Web CLI动作：'+m.action);
  }
  async function targetCommand(m){
    if(m.op==='init'){
      if(transaction)throw Error('已有CLI ETM事务，禁止覆盖');
      const e=await etm(),t=e.target;
      if(e.state.operation||e.state.job||e.state.waiting)throw Error('网页ETM正在操作，请等待完成');
      if(t.saved.size&&['capture','configure','run-to','reset'].includes(m.purpose))throw Error('仍有待恢复的Trace配置；请先 target restore、stop 或 recover');
      await authorized(m.probe);
      e.state.operation=true;e.state.hardwareOperation=true;
      transaction={e,t,initialHalted:true,initialized:false,breakpoints:null,holdOnError:false,purpose:m.purpose,
        baselineConfigured:!!t.saved.size,traceChanged:false};
      try{
        return await tools.etm.run(async()=>{
          t.profile=m.profile;t.hseHz=m.hseHz;
          if(!t.probe)await t.connect();
          checkProbe(t.probe,m.probe);
          transaction.initialHalted=await t.isHalted();transaction.initialized=true;
          const snapshot=await t.inspect();
          return {snapshot,probe:t.probe.device?.serialNumber||t.probe.dev?.serialNumber};
        });
      }catch(error){e.state.operation=false;e.state.hardwareOperation=false;transaction=null;throw error;}
    }
    if(!transaction)throw Error('CLI ETM事务未初始化或页面已刷新');
    const {t,e}=transaction;
    return tools.etm.run(async()=>{
      let value;
      switch(m.op){
        case 'inspect':return t.inspect();
        case 'verify':return {checkedBytes:await t.verifyElf(m.elf)};
        case 'configure':
          transaction.traceChanged=true;
          if(transaction.purpose==='capture')await suspendBreakpoints();
          return {snapshot:await t.configure(m.options),state:targetState(t,transaction.initialHalted)};
        case 'state':
          value=targetState(t,transaction.initialHalted);value.expected=[];
          for(const [address]of t.saved)value.expected.push([address,await t.read(address)]);
          return value;
        case 'run':await t.run();return {halted:await t.isHalted()};
        case 'halt':await t.halt();return {halted:true,pc:await t.pc()};
        case 'recover':transaction.traceChanged=true;return t.recoverDebugPort();
        case 'run-to':case 'reset':
          transaction.traceChanged=true;
          await suspendBreakpoints();
          try{return m.op==='reset'?await t.reset({address:m.address,timeout:m.timeoutMs}):await t.runTo(m.address,{timeout:m.timeoutMs});}
          catch(error){transaction.holdOnError=true;throw error;}
        case 'restore':
          if(m.state){
            const s=m.state,now=await t.inspect();
            if(now.cpuid!==s.snapshot.cpuid||now.device!==s.snapshot.device||now.profile!==s.snapshot.profile)throw Error('目标身份与状态不匹配');
            for(const [address,expected]of s.expected||[]){
              const mask=new Map(s.saved).get(address)?.mask;
              if(mask===undefined||((await t.read(address)^expected)&mask)!==0)throw Error('目标配置已变化，拒绝旧状态恢复');
            }
            t.saved=new Map(s.saved);t.locks=new Map(s.locks);t.osLocked=s.osLocked;t.traceFamily=s.traceFamily;
            transaction.initialHalted=s.initialHalted;
          }
          transaction.traceChanged=true;
          await restore(t,m.keepHalted?true:transaction.initialHalted);return {restored:true,halted:await t.isHalted()};
        case 'stop':
          await t.stopTrace();await resumeBreakpoints();if(!m.halt)await t.run();
          t.saved.clear();t.locks.clear();t.osLocked=null;t.config=null;
          return {traceStopped:true,halted:await t.isHalted()};
        case 'close':
          try{
            if(m.restore&&transaction.initialized){
              if(transaction.baselineConfigured){
                if(transaction.traceChanged)throw Error('已有配置的恢复未确认；保留现场，请使用 target recover/stop');
                // Invalid state-file/input must not restore someone else's
                // still-active manual configuration as an exception cleanup.
                if(transaction.initialHalted)await t.halt();else if(await t.isHalted())await t.run();
              }else await restore(t,transaction.holdOnError?true:transaction.initialHalted);
            }
            else await resumeBreakpoints();
            return {closed:true,restored:!!m.restore,halted:await t.isHalted()};
          }finally{e.state.operation=false;e.state.hardwareOperation=false;transaction=null;}
        default:throw Error('未知ETM动作：'+m.op);
      }
    });
  }
  return {
    async dispatch(m){
      if(m.action==='cancel'||m.action==='status'||m.action==='ping')return execute(m);
      if(busy)throw Error('另一条CLI命令尚未结束');
      busy=true;
      try{return await execute(m);}finally{busy=false;}
    },
  };
}

export const automation=globalThis.window?.__tools?createTraceAutomation(window.__tools):null;

/** Opt-in browser worker; one authorized page, explicit commands, no eval. */
export async function startWorker(){
  const token=new URLSearchParams(location.search).get('trace-cli');
  if(!/^[0-9a-f]{64}$/.test(token||''))throw Error('CLI令牌无效');
  if(!['127.0.0.1','localhost','[::1]'].includes(location.hostname))throw Error('CLI只允许本地工作台');
  const worker=crypto.randomUUID().replaceAll('-','');
  const call=async(path,value)=>{
    const response=await fetch('/trace-cli/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-Trace-CLI':token},body:JSON.stringify(value)});
    if(!response.ok)throw Error('CLI队列HTTP '+response.status);
    return response.json();
  };
  // Polling continues while a command runs so cancellation can reach that command.
  for(;;){
    const {job}=await call('poll',{worker});
    if(!job)continue;
    (async()=>{
      let result;
      try{result={ok:true,value:await automation.dispatch(job.request)};}
      catch(e){result={ok:false,error:e.message,results:e.results};}
      await call('complete/'+job.id,{worker,result});
    })().catch(e=>console.error('CLI结果回传失败；请检查目标状态',e));
  }
}
