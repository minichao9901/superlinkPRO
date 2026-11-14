import {SessionContext,sessionContextBusy} from '../core/session-context.js';
import {store} from '../core/store.js';
import {hpmBoard} from '../targets/hpm/porting.js';
import {rememberHpmBoard} from '../targets/hpm/select.js';
import {HPM_WASM_BOARD} from '../core/openocd-hpm-profile.js';

/** Explicit reuse of local configuration. No synthetic change events or probe calls. */
export function initSessionContext(tools){
  const context=new SessionContext(),$=id=>document.getElementById(id);
  const pages=['flash','dbg','rtt','rttcdc','scope'],prefix={flash:'f',dbg:'d',rtt:'r',rttcdc:'h',scope:'sc'};
  const viewFor=page=>page==='rttcdc'?tools.hid:tools[page];
  const rows=new Map(),localTargets=new Map(),elfBindings=new Map();let applying=false;
  const option=id=>{const el=$(id);return el?.selectedOptions?.[0]?.textContent||el?.value||'';};
  const generic=(page,arch)=>localTargets.get(page)?.arch===arch?localTargets.get(page):{id:arch,arch,label:arch==='arm'?'ARM / SWD（未绑定板型）':'RISC-V / JTAG（未绑定板型）'};
  const imported=cfg=>({id:`cfg:${cfg.hash}:${cfg.target}`,arch:cfg.arch,transport:cfg.transport,label:`CFG · ${cfg.target}`,cfg});
  const readTarget=page=>{
    if(page==='flash'||page==='rtt'){
      const target=viewFor(page).importedTarget;
      if(target?.id===$(prefix[page]+'-chip').value)return target;
    }
    if(page==='flash'){const id=$('f-chip').value;return {id,arch:hpmBoard(id)?'riscv':'arm',label:option('f-chip')};}
    if(page==='rtt'){
      if($('r-target').value==='arm-jtag')return {id:'stm32h743-jtag',arch:'arm',label:'STM32H743 / JTAG'};
      const id=$('r-chip').value;return {id,arch:$('r-target').value==='riscv'?'riscv':'arm',label:option('r-chip')};
    }
    if(page==='dbg'){
      if($('d-backend').value==='mock')return null;
      const cfg=tools.dbg._activeCfg?.();
      if(cfg?.raw)return imported(cfg);
      if(cfg?.files)return {id:HPM_WASM_BOARD,arch:'riscv',label:hpmBoard(HPM_WASM_BOARD).name};
      if(cfg)return {id:`cfg:${cfg.name}:${cfg.target}:${cfg.dpidr??cfg.tapIdcode}:${cfg.hart??''}`,arch:cfg.arch,label:`CFG · ${cfg.target}`};
      if($('d-backend').value==='riscv')return {id:$('d-hpm-target').value,arch:'riscv',label:option('d-hpm-target')};
      const id=$('d-arm-target')?.value;
      return id&&id!=='arm'?{id,arch:'arm',label:option('d-arm-target')}:{id:'arm',arch:'arm',label:'ARM / SWD（未绑定板型）'};
    }
    const stream=$(prefix[page]+'-target').value;
    if(stream==='arm-jtag')return {id:'stm32h743-jtag',arch:'arm',label:'STM32H743 / JTAG'};
    return generic(page,stream==='riscv'?'riscv':'arm');
  };
  const busy=page=>applying||sessionContextBusy(tools,page);
  const publishTarget=(selected,previous)=>{
    // A generic architecture selection cannot erase a more precise session
    // board. The page itself remains explicitly unbound until adoption.
    const clearsCfg=previous?.id.startsWith('cfg:')&&context.target?.id===previous.id;
    if(!clearsCfg&&selected.id===selected.arch&&context.target?.arch===selected.arch&&context.target.id!==selected.id)return;
    context.setTarget(selected);
  };
  const supports=(page,target)=>{
    if(target.cfg)return page==='dbg'||page==='rtt'||page==='flash'&&target.cfg.hasFlash;
    if(target.id==='stm32h743-jtag')return true;
    if(target.id.startsWith('cfg:'))return false;
    if(page==='flash'||page==='rtt')return [...$(prefix[page]+'-chip').options].some(o=>o.value===target.id);
    if(page!=='dbg')return true;
    if(target.id===HPM_WASM_BOARD)return true;
    const id=target.arch==='arm'?'d-arm-target':'d-hpm-target';
    return [...($(id)?.options||[])].some(o=>o.value===target.id);
  };
  const sameTarget=(page,target)=>{
    const local=readTarget(page);
    return local?.arch===target.arch&&local.id===target.id;
  };
  const invalidate=page=>{
    const view=viewFor(page);view.sharedElfSha=null;elfBindings.delete(page);
    if(page==='rtt'){$('r-addr').value='';store.set('rtt.addr','');}
    if(page==='rttcdc'){
      $('h-addr').value='';$('h-size').value='0';
      store.set('hid.addr','');store.set('hid.size','0');
    }
    if(page==='scope'){
      view.selected=[];view.all=[];view.elf=null;view.arrayGroups=[];view.dwarf=null;view.ram=[];view.skipped=[];
      $('sc-arrayexamples').replaceChildren();$('sc-arrayinfo').textContent='请重新载入匹配 ELF';
      view.renderVars();view.updatePlan();$('sc-elfinfo').textContent='目标已变更，请载入匹配 ELF';
    }
    if(page==='dbg'){
      view.sym=null;view.session.sym=null;view.elfName='';view.session.bps=[];view.session.bpNotes.clear();
      view.session.regs=[];view.faultPanel?.invalidate('目标配置已变更');
      view._memAddrStale=true;view.mem=new Uint8Array(0);view.renderMem?.();
      view.watch.items=[];view.renderElfInfo();view.renderBps();view.renderWatch();
    }
    if(page==='flash'){view.file=null;view._fileInfo();view.inspector?.clear();}
  };
  const applyTarget=async(page,target)=>{
    invalidate(page);
    if(target.cfg){
      if(page==='dbg')await tools.dbg._activateImportedCfg(target.cfg);
      else {
        const view=viewFor(page);
        const chip=$(prefix[page]+'-chip');
        const old=view.importedTarget;
        if(old)[...chip.options].find(o=>o.value===old.id)?.remove();
        view.importedTarget=target;
        if(page==='rtt'){
          $('r-target').value=target.arch==='riscv'?'riscv':target.transport==='jtag'?'arm-jtag':'swd';
          view._applyTargetUi();
        }
        const group=page==='rtt'?chip.querySelector(`optgroup[data-arch="${target.arch}"]`):chip;
        const opt=document.createElement('option');opt.value=target.id;opt.textContent=target.label;
        group.append(opt);chip.value=target.id;
        if(page==='flash')view._applyChip();
        if(page==='rtt')$('r-range').value='';
      }
      localTargets.set(page,target);
      return;
    }
    if(target.arch==='riscv')rememberHpmBoard(target.id);
    if(page==='flash'){$('f-chip').value=target.id;store.set('flash.chip',target.id);tools.flash._applyChip();}
    const stream = target.id==='stm32h743-jtag' ? 'arm-jtag' : target.arch==='riscv'?'riscv':'swd';
    if(page==='rtt'){
      $('r-target').value=stream;store.set('rtt.target',stream);
      store.set(target.arch==='riscv'?'rtt.rvChip':'rtt.armChip',stream==='arm-jtag'?'stm32h7':target.id);tools.rtt._applyTargetUi();
    }
    if(page==='dbg'){
      $('d-backend').value=target.arch==='riscv'?'riscv':'webusb';store.set('dbg.backend',$('d-backend').value);
      if(target.arch==='riscv'){
        $('d-hpm-target').value=target.id;store.set('dbg.rvTarget',target.id);
      }
      else if($('d-arm-target')){$('d-arm-target').value=target.id;store.set('dbg.armTarget',target.id);}
      await tools.dbg._syncBackend();
    }
    if(page==='scope'){
      $('sc-target').value=stream;store.set('rtt.target',stream);
      tools.scope.targetType=stream;
      tools.scope.targetRiscv=target.arch==='riscv';tools.scope._askedAt=performance.now();tools.scope._applyBackendUi();
    }
    if(page==='rttcdc'){
      $('h-target').value=stream;store.set('rtt.target',stream);
      tools.hid._applyTargetUi();
    }
    localTargets.set(page,{...target});
  };
  const render=()=>{
    const target=context.target,elf=context.elf;
    for(const [page,row] of rows){
      const locked=busy(page),view=viewFor(page);
      const local=readTarget(page),same=target&&sameTarget(page,target);
      const binding=elfBindings.get(page);
      const key=JSON.stringify([locked,target?.id,elf,context.pending,local?.id,binding,view.sharedElfSha]);
      if(row.key===key)continue;row.key=key;
      const label=t=>hpmBoard(t?.id)?.name||t?.label||'未绑定';
      row.target.textContent='会话目标：'+(target?label(target):'尚未选择');
      row.local.textContent=same?'本页已采用此目标':'本页目标：'+label(local)+' · 尚未沿用';
      row.target.title='用户选择的配置，不代表已识别/加载的硬件身份；点击沿用后才替换本页配置。';
      row.adopt.disabled=locked||!target||same||!supports(page,target);
      row.adopt.title=locked?'请先停止相关探针操作':same?'本页目标已一致':'沿用会话目标，清除本页旧文件/符号地址；不连接或复位硬件';
      const compatible=context.canReuse(local?.arch,local?.id);
      row.elfRow.hidden=!elf&&!context.pending&&!binding;
      row.reuse.textContent=elf?'复用 '+elf.name:context.pending?'正在准备共享 ELF…':'暂无共享 ELF';
      row.reuse.disabled=locked||!compatible||view.sharedElfSha===elf?.sha256;
      const source={flash:'烧录器',dbg:'调试器',rtt:'RTT Viewer',rttcdc:'RTT 转发',scope:'波形'}[elf?.source]||'';
      row.reuse.title=elf?`${elf.arch.toUpperCase()} · SHA-256 ${elf.sha256} · 来自${source}；仅保存在当前页面会话`:'先在任一页面载入 ELF';
      row.identity.textContent='本页 ELF：'+(binding?`${binding.name} · ${binding.sha256.slice(0,12)}`:'未载入共享 ELF');
      if(elf)row.identity.textContent+=binding?.sha256===elf.sha256?' · 与共享一致':` · 共享 ${elf.arch.toUpperCase()} ${elf.sha256.slice(0,12)}`;
      if(elf&&!compatible)row.identity.textContent+=' · 请先选择匹配目标';
    }
  };
  const action=(page,fn,strict=false)=>async()=>{
    const row=rows.get(page);if(busy(page)){
      row.note.textContent='请先停止相关探针操作';
      if(strict)throw Error(row.note.textContent);
      return;
    }
    applying=true;render();row.note.textContent='正在准备…';
    try{await fn();row.note.textContent='已沿用配置；尚未连接或烧录';}
    catch(error){row.note.textContent=error.message||String(error);if(strict)throw error;}
    finally{applying=false;render();}
  };
  const targetInfo=target=>{
    if(!target)return null;
    const {id,arch,label,cfg}=target;
    return {id,arch,label,...(cfg?{name:cfg.name,core:cfg.target,transport:cfg.transport,clockKhz:cfg.clockKhz,
      sha256:cfg.hash,hasFlash:cfg.hasFlash}:{} )};
  };
  tools.sessionTarget={
    info:()=>({shared:targetInfo(context.target),pages:Object.fromEntries(
      ['dbg','flash','rtt'].map(page=>[page,targetInfo(readTarget(page))]))}),
    async use(page){
      if(!['flash','rtt'].includes(page))throw Error('目标沿用只支持 flash 或 rtt');
      const target=context.target;
      if(!target||!supports(page,target))throw Error('本页不支持当前会话目标');
      const changed=!sameTarget(page,target);
      if(changed)await action(page,()=>applyTarget(page,target),true)();
      return {changed,target:targetInfo(readTarget(page))};
    },
  };
  for(const page of pages){
    const p=prefix[page],view=viewFor(page),panel=$('tab-'+page);
    localTargets.set(page,readTarget(page));
    const card=document.createElement('fieldset');card.className='session-context-card';
    const legend=document.createElement('legend');legend.textContent='会话配置';card.append(legend);
    const target=document.createElement('span'),adopt=document.createElement('button'),note=document.createElement('span');
    adopt.type='button';adopt.textContent='沿用目标';adopt.className='mini';note.className='session-context-note';note.setAttribute('role','status');
    adopt.id=p+'-use-session-target';
    const local=document.createElement('span');local.className='session-context-note';
    card.append(target,adopt,local,note);
    (page==='dbg'?$('d-side-session-panel'):page==='scope'?$('sc-side-capture-panel'):panel.querySelector('.side')).prepend(card);
    const reuse=document.createElement('button'),identity=document.createElement('span'),elfRow=document.createElement('div');
    reuse.type='button';reuse.className='mini';reuse.id=p+'-reuse-elf';identity.className='session-context-note';elfRow.className='session-elf-reuse';elfRow.append(reuse,identity);
    const picker=$(page==='flash'?'f-pick':page==='dbg'?'d-elf-pick':p+'-elf');
    if(picker.parentElement.tagName==='FIELDSET')picker.after(elfRow);else picker.parentElement.after(elfRow);
    rows.set(page,{target,local,adopt,note,reuse,identity,elfRow});
    adopt.onclick=action(page,()=>applyTarget(page,context.target));
    reuse.onclick=action(page,async()=>{
      const current=context.elf,bytes=context.elfBytes(),local=readTarget(page);
      if(!current||!context.canReuse(local?.arch,local?.id))throw new Error('请先沿用与 ELF 匹配的目标');
      const file=new File([bytes],current.name);
      let ok;
      if(page==='flash'){await view._onFile(file,bytes);ok=!!view.file;}
      if(page==='dbg'){view.session.bps=[];view.session.bpNotes.clear();ok=!!view.loadElfBuffer(bytes,current.name);}
      if(page==='scope'){view.selected=[];ok=await view.loadElfFile(file,bytes);}
      if(page==='rtt'||page==='rttcdc')ok=await view.loadElfFile(file,bytes);
      if(!ok)throw new Error('本页无法使用该 ELF，请查看解析提示');
      view.sharedElfSha=current.sha256;elfBindings.set(page,{...current,targetId:readTarget(page)?.id});
    });
    view.onElfLoaded=(bytes,name)=>{
      const magic=new Uint8Array(bytes);
      if(page==='flash'&&(magic[0]!==127||magic[1]!==69||magic[2]!==76||magic[3]!==70)){context.clearElf();return;}
      const selected=readTarget(page);if(selected)publishTarget(selected);
      context.publishElf(bytes,name,page,selected).then(elf=>{if(elf){view.sharedElfSha=elf.sha256;elfBindings.set(page,{...elf,targetId:readTarget(page)?.id});render();}})
        .catch(error=>{note.textContent='共享 ELF：'+error.message;});
    };
    const ids={flash:['f-chip'],dbg:['d-backend','d-arm-target','d-hpm-target'],rtt:['r-target','r-chip'],rttcdc:['h-target'],scope:['sc-target']}[page];
    const targetChanged=()=>{
      const previous=localTargets.get(page),selected=readTarget(page);
      if(selected){
        if(!busy(page)&&(previous?.id!==selected.id||previous?.arch!==selected.arch))invalidate(page);
        localTargets.set(page,{...selected});publishTarget(selected,previous);
      }
      render();
    };
    for(const id of ids)$(id)?.addEventListener('change',targetChanged);
    if(page==='dbg'||page==='rttcdc')view.onTargetConfigChanged=targetChanged;
  }
  context.subscribe(render);context.setTarget(readTarget('flash'));
  const timer=setInterval(render,750);window.addEventListener('pagehide',()=>clearInterval(timer),{once:true});
  return context;
}
