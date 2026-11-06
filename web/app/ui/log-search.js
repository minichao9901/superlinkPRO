import {logSnapshot,searchLog} from '../core/log-search.js';

/** Search is a separate display over retained bytes, never a receive-path filter. */
export function initLogSearch({page,prefix,records,isHex=()=>false,isSuppressed=()=>false}){
  const panel=document.getElementById('tab-'+page),main=panel.querySelector('.main');
  const make=(tag,text)=>{const el=document.createElement(tag);if(text)el.textContent=text;return el;};
  const button=(text,title)=>{const b=make('button',text);b.type='button';b.title=title||text;return b;};
  const toggle=button('查找','查找、过滤与高亮接收缓冲（Ctrl+F）');toggle.id=prefix+'-search-toggle';
  toggle.setAttribute('aria-expanded','false');
  document.getElementById(prefix+'-pause').before(toggle);
  const bar=make('div');bar.className='log-search-bar';bar.hidden=true;bar.id=prefix+'-search-bar';
  toggle.setAttribute('aria-controls',bar.id);
  const query=make('input');query.type='search';query.placeholder='查找日志文本…';query.setAttribute('aria-label','查找日志文本');
  const check=(label)=>{const el=make('label');el.className='chk';const box=make('input');box.type='checkbox';el.append(box,document.createTextNode(label));bar.append(el);return box;};
  bar.append(query);
  const matchCase=check('区分大小写'),filter=check('只看匹配行'),live=check('实时更新');
  live.title='每500ms刷新；高速自动停显时请手动刷新';
  const previous=button('↑','上一处（Shift+Enter）'),next=button('↓','下一处（Enter）');
  const refresh=button('刷新','重新读取最近128 KiB；只影响检索视图');
  const close=button('关闭','关闭检索（Esc），返回原接收显示');
  const status=make('span');status.className='log-search-status';status.setAttribute('role','status');
  bar.append(previous,next,refresh,close,status);
  const result=make('pre');result.className='log-search-results';result.hidden=true;result.tabIndex=0;
  result.setAttribute('aria-label','日志检索结果');
  main.querySelector('.toolbar').after(bar);bar.after(result);
  let snapshot={text:'',bytes:0,clipped:false},marks=[],index=-1,timer,debounce;
  let lastQuery='';
  let lastRecord,lastHex;
  const visible=()=>!bar.hidden&&panel.classList.contains('active');
  const updateStatus=(total)=>{
    const state=live.checked?(isSuppressed()?'实时暂停（高速保护）':'实时'):'快照';
    status.textContent=`${state} · 最近 ${snapshot.bytes} B${snapshot.clipped?'（已截取）':''} · ${total} 处`+
      (marks.length?` · ${index+1}/${marks.length}`:'')+(total>marks.length?'（仅高亮前1000处）':'');
    status.title='仅检索最近128 KiB保留字节；文本忽略ANSI控制序列，不含显示时间戳。过滤不改变原始缓冲、导出或持续记录。';
  };
  let total=0;
  const select=(direction=0)=>{
    if(!marks.length)return;
    marks[index]?.classList.remove('current');index=(index+direction+marks.length)%marks.length;
    marks[index].classList.add('current');marks[index].scrollIntoView({block:'nearest'});updateStatus(total);
  };
  const render=()=>{
    lastQuery=query.value;
    const found=searchLog(snapshot.text,query.value,{matchCase:matchCase.checked,filter:filter.checked});
    total=found.total;marks=[];index=0;
    const fragment=document.createDocumentFragment();let offset=0;
    for(const [start,end] of found.matches){
      fragment.append(document.createTextNode(found.text.slice(offset,start)));
      const mark=make('mark',found.text.slice(start,end));fragment.append(mark);marks.push(mark);offset=end;
    }
    fragment.append(document.createTextNode(found.text.slice(offset)));
    result.replaceChildren(fragment);previous.disabled=next.disabled=!marks.length;
    if(!found.text)result.textContent=query.value?'没有匹配的日志':'暂无接收数据';
    updateStatus(total);if(marks.length)select();
  };
  const capture=(force=true)=>{
    if(bar.hidden)return;
    const data=records(),tail=data[data.length-1],hex=isHex();
    if(!force&&tail===lastRecord&&hex===lastHex)return;
    lastRecord=tail;lastHex=hex;snapshot=logSnapshot(data,{hex});render();
  };
  const hide=()=>{clearTimeout(debounce);bar.hidden=result.hidden=true;panel.classList.remove('log-searching');toggle.setAttribute('aria-expanded','false');toggle.focus();};
  const show=()=>{bar.hidden=result.hidden=false;panel.classList.add('log-searching');toggle.setAttribute('aria-expanded','true');capture();query.focus();query.select();};
  toggle.onclick=()=>bar.hidden?show():hide();close.onclick=hide;refresh.onclick=capture;
  previous.onclick=()=>{clearTimeout(debounce);renderIfPending();select(-1);};
  next.onclick=()=>{clearTimeout(debounce);renderIfPending();select(1);};
  query.oninput=()=>{clearTimeout(debounce);debounce=setTimeout(render,120);};
  matchCase.onchange=filter.onchange=render;live.onchange=()=>{if(live.checked)capture();else updateStatus(total);};
  bar.addEventListener('keydown',event=>{
    if(event.key==='Escape'){event.preventDefault();hide();}
    if(event.key==='Enter'){event.preventDefault();clearTimeout(debounce);renderIfPending();select(event.shiftKey?-1:1);}
  });
  // Key navigation must use the latest typed query even before debounce fires.
  const renderIfPending=()=>{if(lastQuery!==query.value){render();lastQuery=query.value;}};
  document.addEventListener('keydown',event=>{
    if(!panel.classList.contains('active')||event.defaultPrevented||document.querySelector('dialog[open]'))return;
    if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='f'){event.preventDefault();show();}
  });
  document.getElementById(prefix+'-clear').addEventListener('click',capture);
  panel.querySelector('.toolbar').addEventListener('click',event=>{if(event.target.closest('.seg')&&visible())queueMicrotask(capture);});
  panel.addEventListener('keydown',event=>{
    if(event.key==='Escape'&&!bar.hidden&&!event.defaultPrevented&&!document.querySelector('dialog[open]')){event.preventDefault();hide();}
  });
  timer=setInterval(()=>{
    if(!visible()||!live.checked)return;
    if(isSuppressed()){updateStatus(total);return;}
    capture(false);
  },500);
  window.addEventListener('pagehide',()=>{clearInterval(timer);clearTimeout(debounce);},{once:true});
  return {show,hide,refresh:capture};
}
