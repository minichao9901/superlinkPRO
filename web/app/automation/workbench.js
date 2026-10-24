/** Local, opt-in CLI projection of the actual workbench. No eval or protocol copy. */
export const PAGES = {
  serial:['串口助手','assistant'], terminal:['终端','terminal'], rtt:['RTT Viewer','rtt'],
  rttcdc:['RTT 转发','stream'], spicdc:['SPI 转发','spiCdc'], scope:['J-Scope 波形','scope'],
  flash:['烧录器','flash'], dbg:['调试器','dbg'], swo:['SWO PC 采样','swo'],
  etm:['ETM 执行轨迹','etm'], spi:['USB SPI/QSPI','spi'], panel:['SPI/QSPI 屏','panel'],
  i2c:['USB I2C','i2c'], analog:['ADC / DAC','analog'], gen:['工程生成','gen'],
};
export const INTERRUPTS={
  rttcdc:['#h-stop'],spicdc:['#si-stop'],scope:['#sc-stop'],dbg:['#d-rtt-stop'],
  swo:['#sw-stop'],etm:['#stop','#cancel-wait'],spi:['#sp-run-abort','#sp-dsl-stop'],
  panel:['#pn-run-abort','#pn-code-stop','#pn-read-stop','#pn-anim-stop'],
  i2c:['#i2-run-stop'],analog:['#an-stop','#an-dac-stop'],
};
const GLOBAL_CONTROLS=['btn-global-reset','global-nrst-open-drain','btn-fullscreen','btn-help'];

function methods(object){
  const result=new Map();
  for(let p=object;p&&p!==Object.prototype;p=Object.getPrototypeOf(p)){
    for(const [name,d] of Object.entries(Object.getOwnPropertyDescriptors(p))){
      if(name==='constructor'||name.startsWith('_')||['init','subscribe','onSession'].includes(name))continue;
      if(typeof d.value==='function'&&!result.has(name))result.set(name,d.value);
    }
  }
  return result;
}

function jsonValue(value,seen=new WeakSet(),depth=0){
  if(value===undefined)return null;
  if(typeof value==='bigint')return String(value);
  if(value===null||typeof value!=='object')return typeof value==='function'?undefined:value;
  if(depth>12)throw Error('结果嵌套过深，请读取页面状态或缩小查询');
  if(value instanceof ArrayBuffer)return {$bytes:Array.from(new Uint8Array(value))};
  if(ArrayBuffer.isView(value))return {$bytes:Array.from(new Uint8Array(value.buffer,value.byteOffset,value.byteLength))};
  if(seen.has(value))return '[circular]';
  seen.add(value);
  let out;
  if(value instanceof Map)out=Array.from(value,([k,v])=>[jsonValue(k,seen,depth+1),jsonValue(v,seen,depth+1)]);
  else if(value instanceof Set)out=Array.from(value,v=>jsonValue(v,seen,depth+1));
  else if(Array.isArray(value))out=value.map(v=>jsonValue(v,seen,depth+1));
  else {
    out={};
    // Do not traverse getters, DOM nodes, devices, or class internals.
    if(Object.getPrototypeOf(value)!==Object.prototype&&Object.getPrototypeOf(value)!==null){seen.delete(value);return {type:value.constructor?.name||'object'};}
    for(const [k,d]of Object.entries(Object.getOwnPropertyDescriptors(value)))if('value'in d)out[k]=jsonValue(d.value,seen,depth+1);
  }
  seen.delete(value);return out;
}

export function createWorkbenchAutomation(tools,win=globalThis.window){
  const doc=win.document;
  const captures=new Map();
  function captureDownloads(w){
    if(captures.has(w))return captures.get(w);
    const blobs=new Map(),files=new Map();let next=0;
    const create=w.URL.createObjectURL.bind(w.URL),revoke=w.URL.revokeObjectURL.bind(w.URL);
    w.URL.createObjectURL=blob=>{const url=create(blob);blobs.set(url,blob);return url;};
    w.URL.revokeObjectURL=url=>{blobs.delete(url);return revoke(url);};
    const click=w.HTMLAnchorElement.prototype.click;
    w.HTMLAnchorElement.prototype.click=function(){
      const blob=blobs.get(this.href);
      if(this.download&&blob){
        if(files.size>=32)throw Error('CLI下载暂存已满；请 download 保存或 discard 释放');
        files.set(String(++next),{name:this.download,blob});return;
      }
      return click.call(this);
    };
    captures.set(w,files);return files;
  }
  function page(name){if(!Object.hasOwn(PAGES,name))throw Error('未知页面：'+name);return PAGES[name];}
  async function context(name){
    const [,key]=page(name);
    if(name==='etm'){
      await tools.etm.onShow();
      const child=tools.etm.frame?.contentWindow;
      if(!child?.__etm)throw Error('ETM 子页面尚未就绪，请稍后重试；离线大文件推荐 trace 命令');
      return {root:child.document.getElementById('etm-app'),view:child.__etm,win:child};
    }
    const root=doc.getElementById('tab-'+name);
    if(!root||!tools[key])throw Error('当前网页版本缺少页面：'+name);
    return {root,view:tools[key],win};
  }
  function selector(e,root){
    if(e.id)return '#'+win.CSS.escape(e.id);
    const parts=[];
    for(let p=e;p&&p!==root;p=p.parentElement){
      const siblings=Array.from(p.parentElement.children).filter(s=>s.tagName===p.tagName);
      parts.unshift(p.tagName.toLowerCase()+':nth-of-type('+(siblings.indexOf(p)+1)+')');
    }
    return ':scope > '+parts.join(' > ');
  }
  function control(e,root,query){
    return {selector:query??selector(e,root),tag:e.tagName.toLowerCase(),type:e.type||null,
      label:(e.labels?.[0]?.textContent||e.getAttribute('aria-label')||e.title||e.textContent||e.placeholder||'').trim().slice(0,240),
      value:e.type==='checkbox'||e.type==='radio'?!!e.checked:e.value??null,
      disabled:!!e.disabled,hidden:!!e.hidden,
      options:e.tagName==='SELECT'?Array.from(e.options,o=>({value:o.value,label:o.textContent,disabled:o.disabled})):undefined};
  }
  function locate(root,query,files){
    if(typeof query!=='string'||!query)throw Error('必须指定控件 selector');
    if(files.has(query))return files.get(query);
    if(GLOBAL_CONTROLS.some(id=>query==='#'+id)){
      const e=doc.getElementById(query.slice(1));if(e)return e;
    }
    const hits=root.querySelectorAll(query);
    if(hits.length!==1)throw Error('控件必须唯一匹配，当前 '+hits.length+' 个：'+query);
    return hits[0];
  }
  function argument(v,c){
    if(!v||typeof v!=='object')return v;
    if(Array.isArray(v))return v.map(x=>argument(x,c));
    if(Object.hasOwn(v,'$base64')){
      const bytes=Uint8Array.from(win.atob(v.$base64),x=>x.charCodeAt(0));
      if(v.kind==='file')return new c.win.File([bytes],v.name||'input.bin',{type:v.type||'application/octet-stream'});
      if(v.kind==='buffer')return bytes.buffer;
      return bytes;
    }
    return Object.fromEntries(Object.entries(v).map(([k,x])=>[k,argument(x,c)]));
  }
  return {async dispatch(m){
    if(m.op==='catalog')return {protocol:'workbench/1',pages:Object.entries(PAGES).map(([id,[label]])=>({id,label})),
      operations:['describe','state','read','set','click','upload','invoke','show','event','interrupt','downloads','download','discard'],
      completion:'click/set/upload only dispatch events; invoke awaits returned promise; verify status/results separately'};
    const c=await context(m.page),{root,view}=c;
    const targets={view,session:view.session||view.s,
      ...(m.page==='rttcdc'?{hid:tools.hid}:{}),
      ...(['spi','panel'].includes(m.page)?{session:tools.spiSession}:{}),
      ...(m.page==='spicdc'?{stream:view.stream,serial:tools.session}:{}),
      ...(m.page==='swo'?{capture:view.capture,sources:view.sources}:{}),
      ...(m.page==='etm'?{target:view.target}:{}),
      ...(view.store?{store:view.store}:{}),
    };
    const targetName=m.target||'view';
    if(!Object.hasOwn(targets,targetName)||!targets[targetName])throw Error('当前不可用的target：'+targetName);
    const target=targets[targetName];
    const fileControls=new Map();
    for(const [key,object]of Object.entries(targets)){
      if(!object)continue;
      for(const [name,d]of Object.entries(Object.getOwnPropertyDescriptors(object))){
        const e=d.value;
        if(e?.nodeType===1&&e.tagName==='INPUT'&&e.type==='file'&&!root.contains(e))fileControls.set('@file:'+key+'.'+name,e);
      }
    }
    const downloads=captureDownloads(c.win);
    if(m.op==='downloads')return Array.from(downloads,([id,f])=>({id,name:f.name,bytes:f.blob.size,type:f.blob.type}));
    if(m.op==='download'||m.op==='discard'){
      const f=downloads.get(String(m.id));if(!f)throw Error('下载ID不存在');
      if(m.op==='discard'){downloads.delete(String(m.id));return {discarded:true};}
      const offset=m.offset??0,length=m.length??1048576;
      if(!Number.isSafeInteger(offset)||offset<0||offset>f.blob.size||!Number.isSafeInteger(length)||length<1||length>1048576)throw Error('无效下载区间');
      const b=new Uint8Array(await f.blob.slice(offset,offset+length).arrayBuffer());
      let text='';for(let i=0;i<b.length;i+=8192)text+=String.fromCharCode(...b.subarray(i,i+8192));
      return {name:f.name,total:f.blob.size,offset,base64:win.btoa(text)};
    }
    if(m.op==='describe')return {page:m.page,targets:Object.keys(targets).filter(k=>targets[k]),interrupts:INTERRUPTS[m.page]||[],
      globalControls:GLOBAL_CONTROLS.map(id=>doc.getElementById(id)).filter(Boolean).map(e=>control(e,doc.body)),
      controls:[...Array.from(root.querySelectorAll('input,select,textarea,button,a[download],[role="button"],[data-v]'),e=>control(e,root)),
        ...Array.from(fileControls,([key,e])=>({...control(e,e.ownerDocument.body,key),detachedFromPage:true}))],
      methods:Array.from(methods(target),([name,fn])=>({name,signature:fn.toString().split('{')[0].trim().slice(0,400),
        note:'原网页方法；参数含 DOM/回调的接口应改用控件接口'}))};
    if(m.op==='state')return {page:m.page,summary:typeof view.summary==='function'?jsonValue(await view.summary()):null,
      errors:tools.errors?.slice(-20)||[],text:root.textContent.slice(0,100000)};
    if(m.op==='show'){
      win.location.hash=m.page;
      return {page:m.page,dispatched:true,completed:false};
    }
    if(m.op==='invoke'){
      const fn=methods(target).get(m.method);if(!fn)throw Error('未公开的方法：'+m.method);
      if(!Array.isArray(m.args||[]))throw Error('args 必须为 JSON 数组');
      const start=tools.errors?.length||0;
      const value=await fn.apply(target,(m.args||[]).map(v=>argument(v,c)));
      const errors=tools.errors?.slice(start)||[];
      return {returned:true,value:jsonValue(value),errors,verificationRequired:true};
    }
    const e=locate(root,m.selector,fileControls);
    if(m.op==='read')return {...control(e,root,m.selector),text:e.textContent.slice(0,100000)};
    if(e.disabled||e.matches(':disabled'))throw Error('控件已禁用：'+m.selector);
    if(m.op==='event'){
      const constructors={keydown:'KeyboardEvent',keyup:'KeyboardEvent',mousedown:'MouseEvent',mousemove:'MouseEvent',mouseup:'MouseEvent',
        pointerdown:'PointerEvent',pointermove:'PointerEvent',pointerup:'PointerEvent',wheel:'WheelEvent',dblclick:'MouseEvent',click:'MouseEvent',input:'Event',change:'Event'};
      if(!Object.hasOwn(constructors,m.event))throw Error('未公开的事件类型');
      const options={...m.options,bubbles:true,cancelable:true};
      if(m.options?.x!==undefined||m.options?.y!==undefined){
        const rect=e.getBoundingClientRect();options.clientX=rect.left+(m.options.x||0);options.clientY=rect.top+(m.options.y||0);
      }
      e.dispatchEvent(new c.win[constructors[m.event]](m.event,options));
    }else if(m.op==='set'){
      if(!['INPUT','TEXTAREA','SELECT'].includes(e.tagName)||['file','button','submit','reset','radio'].includes(e.type)||e.readOnly)throw Error('该控件不可 set');
      if(e.type==='checkbox'){
        if(typeof m.value!=='boolean')throw Error('checkbox 必须使用 JSON boolean');
        e.checked=m.value;
      }else{
        const old=e.value;e.value=String(m.value);
        if(e.value!==String(m.value)){e.value=old;throw Error('无效选项或数值');}
      }
      e.dispatchEvent(new c.win.Event('input',{bubbles:true}));e.dispatchEvent(new c.win.Event('change',{bubbles:true}));
    }else if(m.op==='click'||m.op==='interrupt'){
      if(m.op==='interrupt'&&!(INTERRUPTS[m.page]||[]).includes(m.selector))throw Error('该控件未列入停止白名单');
      if(e.type==='file')throw Error('请使用 upload，不能自动打开系统文件选择器');
      e.click();
    }else if(m.op==='upload'){
      if(e.type!=='file')throw Error('upload 需要 file input');
      if(!Array.isArray(m.files)||!m.files.length)throw Error('必须提供文件');
      if(!e.multiple&&m.files.length!==1)throw Error('该控件不支持多文件');
      const dt=new c.win.DataTransfer();
      for(const f of m.files){
        const file=argument({...f,kind:'file'},c);
        if(f.relativePath)Object.defineProperty(file,'webkitRelativePath',{value:f.relativePath});
        dt.items.add(file);
      }
      e.files=dt.files;e.dispatchEvent(new c.win.Event('change',{bubbles:true}));
    }else throw Error('未知 workbench 操作：'+m.op);
    return {dispatched:true,completed:false,control:control(e,root,m.selector)};
  }};
}
