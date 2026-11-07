/** Layout-only controls. No target operations, timers or acquisition callbacks. */
import { store } from '../core/store.js';

export function initWorkspaces(){
  const panels = [...document.querySelectorAll('[data-workspace]')];
  const summaries=[];
  const sync = () => {
    document.body.classList.toggle('workspace-focused', !!document.querySelector('.panel.active[data-focus]'));
    for(const update of summaries)update();
  };
  const button = (id, text, title) => {
    const b = document.createElement('button'); b.id=id; b.className='workspace-button';
    b.type='button'; b.textContent=text; b.title=title; return b;
  };
  for (const p of panels){
    const name=p.dataset.workspace, prefix=({dbg:'d',scope:'sc',serial:'s',terminal:'t',rtt:'r',rttcdc:'c',flash:'f'})[name];
    const toolbar=p.querySelector('.main>.toolbar'), side=p.querySelector('.ws>.side');
    side.id ||= `${prefix}-settings`;
    const fold=button(`${prefix}-layout-sidebar`,'收起设置','显示或收起设置侧栏；记住本页选择');
    fold.setAttribute('aria-controls',side.id);
    const applyFold=collapsed=>{
      p.classList.toggle('sidebar-collapsed',collapsed);
      fold.textContent=collapsed?'展开设置':'收起设置';
      fold.setAttribute('aria-expanded',String(!collapsed));
    };
    applyFold(!!store.get(`workspace.${name}.sidebarCollapsed`,false));
    fold.addEventListener('click',()=>{
      if(p.dataset.focus){
        p.focusWorkspace(null);applyFold(false);store.set(`workspace.${name}.sidebarCollapsed`,false);return;
      }
      const collapsed=!p.classList.contains('sidebar-collapsed');
      applyFold(collapsed);store.set(`workspace.${name}.sidebarCollapsed`,collapsed);
    });
    toolbar.prepend(fold);
    if(!['dbg','scope'].includes(name)){
      const summary=document.createElement('span');summary.className='workspace-summary';
      const fields=({serial:['s-port','s-baud'],terminal:[],rtt:['r-target','r-chip'],rttcdc:['h-target'],flash:['f-chip']})[name]||[];
      const update=()=>{
        const text=fields.map(id=>{
          const el=document.getElementById(id);if(!el)return '';
          return el.selectedOptions?.[0]?.textContent||el.value||'';
        }).filter(Boolean).join(' · ');
        summary.textContent=text;summary.title=text;
      };
      summaries.push(update);p.addEventListener('change',update);p.addEventListener('input',update);
      fold.addEventListener('click',update);fold.after(summary);update();
    }
    const targets=[];
    const addFocus=(id,host,target,label)=>{
      const b=button(id,label,'放大当前工作区；点击还原，或在非输入区域按 Esc 还原');
      const area={src:'源码',term:'命令行',dock:'检查面板',work:name==='dbg'?'调试工作区':'波形'}[target];
      b.setAttribute('aria-label',`最大化${area}`);
      b.addEventListener('click',()=>p.focusWorkspace(p.dataset.focus===target?null:target));
      host.append(b);targets.push({b,target,label,area});
    };
    if(name==='dbg'||name==='scope') addFocus(`${prefix}-layout-focus`,toolbar,'work',name==='dbg'?'专注调试':'最大化波形');
    if(name==='dbg') for(const [target,id] of [['src','d-box-src'],['term','d-box-term'],['dock','d-box-dock']])
      addFocus(`d-max-${target}`,document.getElementById(id).querySelector('legend'),target,'最大化');
    p.focusWorkspace=target=>{
      if(target)p.dataset.focus=target;else delete p.dataset.focus;
      for(const {b,target:t,label,area} of targets){
        b.textContent=t===target?'还原':label;b.setAttribute('aria-pressed',String(t===target));
        b.setAttribute('aria-label',`${t===target?'还原':'最大化'}${area}`);
      }
      fold.textContent=target||p.classList.contains('sidebar-collapsed')?'展开设置':'收起设置';
      fold.setAttribute('aria-expanded',String(!target&&!p.classList.contains('sidebar-collapsed')));
      sync();
    };
    p.focusWorkspace(null);
    // Fold instructions only. Named live statuses, errors and rate advice stay visible.
    for(const note of side.querySelectorAll('p.hint:not([id])')){
      if(note.closest('details'))continue;
      const previous=note.previousElementSibling;
      if(previous?.classList.contains('workspace-help')){previous.append(note);continue;}
      const help=document.createElement('details');help.className='helpnote workspace-help';
      const summary=document.createElement('summary');summary.textContent='使用说明';
      note.before(help);help.append(summary,note);
    }
  }
  document.addEventListener('keydown',e=>{
    if(e.key!=='Escape'||e.defaultPrevented||e.target.closest?.('input,textarea,select,[contenteditable="true"]'))return;
    if(document.querySelector('dialog[open],[role="dialog"]:not([hidden]),.diag-drawer:not([hidden])'))return;
    const p=document.querySelector('.panel.active[data-focus]');
    if(p){p.focusWorkspace(null);e.preventDefault();e.stopPropagation();}
  });
  const more=document.getElementById('app-more');
  more?.addEventListener('toggle',()=>{if(more.open){const tools=document.getElementById('tool-switch');if(tools)tools.open=false;}});
  document.addEventListener('click',e=>{if(more&&!more.contains(e.target))more.open=false;});
  more?.addEventListener('keydown',e=>{if(e.key==='Escape'){more.open=false;more.querySelector('summary').focus();e.preventDefault();}});
  return { sync };
}
