/** Workflow hints only. Hardware operations still use the existing user-click handlers. */
export function scopeNextAction(s){
  if(s.hasData||s.running||s.starting)return null;
  if(s.mock)return {kind:'start',label:'开始模拟采样'};
  if(s.canAdopt)return {kind:'adopt',label:'沿用会话目标'};
  if(!s.hasElf)return {kind:s.canReuse?'reuse':'choose',label:s.canReuse?'复用 ELF 并选变量':'载入 ELF 并选变量'};
  if(!s.selected)return {kind:'variables',label:'选择采样变量'};
  if(!s.hasHid)return {kind:'connect',label:'连接探针'};
  if(!s.hasTransport)return {kind:'data',label:'连接数据端点'};
  return {kind:'start',label:'开始采样'};
}
export function scopeHasWarnings(s){
  return !!(s.probeDropped||s.usbDrop||s.lost||s.readErrors||s.store?.overrun||s.planMismatch||s.defMismatch||s.metricsError||s.stream?.resyncs);
}
export function initScopeOnboarding(scope){
  const $=id=>document.getElementById(id),panel=$('tab-scope');
  const row=document.createElement('div');row.className='scope-empty-actions';row.hidden=true;
  const next=document.createElement('button');next.type='button';next.className='primary';next.id='sc-next-step';
  const replay=document.createElement('button');replay.type='button';replay.textContent='打开离线回放';replay.onclick=()=>$('sc-open').click();row.append(next,replay);$('sc-canvas').parentElement.append(row);
  const state=()=>({hasData:!!scope.store?.count,running:scope.running,starting:scope._starting,mock:scope.usingMock,
    canAdopt:!!$('sc-use-session-target')&&!$('sc-use-session-target').disabled,hasElf:!!scope.elf,
    canReuse:!!$('sc-reuse-elf')&&!$('sc-reuse-elf').disabled,selected:scope.selected.length,hasHid:!!scope.hid,hasTransport:!!scope.transport});
  const variables=()=>{if($('sc-layout-sidebar').getAttribute('aria-expanded')==='false')$('sc-layout-sidebar').click();$('sc-side-variables-tab').click();};
  next.onclick=()=>{
    const action=scopeNextAction(state());if(!action)return;
    if(action.kind==='adopt'){$('sc-use-session-target').click();return;}
    if(['choose','reuse','variables'].includes(action.kind)){
      variables();if(action.kind==='choose')$('sc-elf').click();if(action.kind==='reuse')$('sc-reuse-elf').click();return;
    }
    $(action.kind==='connect'?'sc-connect':action.kind==='data'?'sc-usb':'sc-start').click();
  };
  const update=()=>{
    const action=scopeNextAction(state());row.hidden=!action;
    if(action&&next.textContent!=='下一步：'+action.label)next.textContent='下一步：'+action.label;
    const warnings=scopeHasWarnings(scope),quality=$('sc-quality'),label=warnings?'质量 · 有异常':'质量';
    if(quality.textContent!==label)quality.textContent=label;quality.classList.toggle('danger',warnings);
  };
  update();const timer=setInterval(update,750);window.addEventListener('pagehide',()=>clearInterval(timer),{once:true});
}
