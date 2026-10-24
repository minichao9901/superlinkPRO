import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {parseHTML} from 'linkedom';
import {createWorkbenchAutomation,PAGES} from '../../app/automation/workbench.js';
import {createTraceAutomation,browserWorkerId} from '../../app/automation/trace.js';

function rig(){
  const {document,window}=parseHTML('<html><body><section id="tab-serial"><input id="baud" value="115200"><input id="check" type="checkbox"><button id="go">发送</button><button id="off" disabled>禁用</button><input id="file" type="file"><pre id="result">ready</pre></section><section id="tab-panel"></section><section id="tab-spi"></section></body></html>');
  const saved=[];
  class Anchor{click(){saved.push('browser');}}
  const win={document,Event:window.Event,CSS:{escape:s=>s},location:{hash:''},
    URL:{createObjectURL:()=>`blob:${Math.random()}`,revokeObjectURL:()=>{}},HTMLAnchorElement:Anchor,
    atob:v=>Buffer.from(v,'base64').toString('binary'),btoa:v=>Buffer.from(v,'binary').toString('base64'),File,
    DataTransfer:class{constructor(){this.files=[];this.items={add:f=>this.files.push(f)};}}};
  const calls=[];
  class View{
    async send(text){await Promise.resolve();calls.push(text);return {sent:text.length};}
    async fail(){throw Error('USB disconnected');}
    _private(){throw Error('private');}
    summary(){return {sent:calls.length};}
    save(){const a=new win.HTMLAnchorElement();a.href=win.URL.createObjectURL(new Blob(['abc\u4e2d\u6587']));a.download='hello.txt';a.click();win.URL.revokeObjectURL(a.href);}
    fileBuffer(bytes){return Array.from(new Uint8Array(bytes));}
  }
  const spiSession={pollStatus:async()=>({received:12})};
  const tools={assistant:new View(),spi:{},panel:{},spiSession,errors:[]};
  return {a:createWorkbenchAutomation(tools,win),tools,win,calls,saved,document};
}
const request=(op,extra={})=>({action:'workbench',page:'serial',op,...extra});

test('catalog exactly covers all real top-level Web pages',async()=>{
  const {document}=parseHTML(await readFile(new URL('../../index.html',import.meta.url),'utf8'));
  assert.deepEqual([...new Set(Array.from(document.querySelectorAll('#tabs [data-tab]'),e=>e.dataset.tab))].sort(),Object.keys(PAGES).sort());
  assert.equal((await rig().a.dispatch({op:'catalog'})).pages.length,15);
});
test('describe discovers controls and only declared public methods, without calling them',async()=>{
  const r=rig();let touched=false;
  Object.defineProperty(r.tools.assistant,'trap',{get(){touched=true;throw Error('getter');}});
  const v=await r.a.dispatch(request('describe'));
  assert(v.controls.some(e=>e.selector==='#go'&&e.label==='发送'));
  assert(v.methods.some(m=>m.name==='send'));
  assert(!v.methods.some(m=>['_private','constructor','toString','trap'].includes(m.name)));
  assert.equal(touched,false);assert.deepEqual(r.calls,[]);
});
test('set delivers both input and change and rejects string booleans / disabled controls',async()=>{
  const r=rig(),events=[];
  const e=r.document.getElementById('check');
  // linkedom does not implement reflected input.type for checkboxes.
  Object.defineProperty(e,'type',{value:'checkbox'});
  e.addEventListener('input',()=>events.push('input'));e.addEventListener('change',()=>events.push('change'));
  const v=await r.a.dispatch(request('set',{selector:'#check',value:true}));
  assert.deepEqual(events,['input','change']);assert.equal(e.checked,true);assert.equal(v.completed,false);
  await assert.rejects(r.a.dispatch(request('set',{selector:'#check',value:'false'})),/boolean/);
  await assert.rejects(r.a.dispatch(request('click',{selector:'#off'})),/禁用/);
});
test('selector is page scoped and must be unique; read does not modify state',async()=>{
  const r=rig();assert.equal((await r.a.dispatch(request('read',{selector:'#result'}))).text,'ready');
  await assert.rejects(r.a.dispatch(request('click',{selector:'button'})),/唯一/);
  await assert.rejects(r.a.dispatch(request('read',{selector:'body'})),/唯一/);
  await assert.rejects(r.a.dispatch({...request('state'),page:'__proto__'}),/未知页面/);
});
test('invoke awaits the actual method and propagates errors / rejects prototype access',async()=>{
  const r=rig();const v=await r.a.dispatch(request('invoke',{method:'send',args:['hello']}));
  assert.deepEqual(v.value,{sent:5});assert.deepEqual(r.calls,['hello']);assert.equal(v.verificationRequired,true);
  await assert.rejects(r.a.dispatch(request('invoke',{method:'fail'})),/USB disconnected/);
  for(const method of ['constructor','__proto__','toString','_private'])await assert.rejects(r.a.dispatch(request('invoke',{method})),/未公开/);
});
test('SPI and panel reuse the same session; target paths cannot escape allowlisted objects',async()=>{
  const r=rig();for(const page of ['spi','panel']){
    const v=await r.a.dispatch({page,op:'invoke',target:'session',method:'pollStatus'});assert.equal(v.value.received,12);
  }
  await assert.rejects(r.a.dispatch(request('invoke',{target:'view.constructor',method:'x'})),/target/);
});
test('file arguments preserve exact bytes and uploads preserve directory relative paths',async()=>{
  const r=rig(),data=Buffer.from([0,128,255]).toString('base64');
  const v=await r.a.dispatch(request('invoke',{method:'fileBuffer',args:[{$base64:data,kind:'buffer'}]}));
  assert.deepEqual(v.value,[0,128,255]);
  const e=r.document.getElementById('file');let changed=0;
  e.addEventListener('change',()=>changed++);
  await r.a.dispatch(request('upload',{selector:'#file',files:[{$base64:data,name:'main.c',relativePath:'src/main.c'}]}));
  assert.equal(e.files[0].webkitRelativePath,'src/main.c');assert.equal(changed,1);
  assert.deepEqual(Array.from(new Uint8Array(await e.files[0].arrayBuffer())),[0,128,255]);
});
test('file inputs attached outside their page remain discoverable and uploadable',async()=>{
  const r=rig();const file=r.document.createElement('input');file.type='file';
  r.document.body.append(file);r.tools.assistant._fileInput=file;
  const d=await r.a.dispatch(request('describe'));
  assert(d.controls.some(c=>c.selector==='@file:view._fileInput'));
  await r.a.dispatch(request('upload',{selector:'@file:view._fileInput',files:[{$base64:'YQ==',name:'a.bin'}]}));
  assert.equal(await file.files[0].text(),'a');
});
test('Blob exports survive URL revocation and chunked download is byte exact',async()=>{
  const r=rig();await r.a.dispatch(request('invoke',{method:'save'}));
  const list=await r.a.dispatch(request('downloads'));assert.equal(list.length,1);assert.equal(list[0].name,'hello.txt');
  const chunks=[];for(let offset=0;offset<list[0].bytes;offset+=2){
    const v=await r.a.dispatch(request('download',{id:list[0].id,offset,length:2}));chunks.push(Buffer.from(v.base64,'base64'));
  }
  assert.equal(Buffer.concat(chunks).toString(),'abc中文');assert.deepEqual(r.saved,[]);
  await assert.rejects(r.a.dispatch(request('download',{id:list[0].id,offset:-1})),/区间/);
  await r.a.dispatch(request('discard',{id:list[0].id}));assert.deepEqual(await r.a.dispatch(request('downloads')),[]);
});
test('events have a fixed allowlist and clicks explicitly report dispatch only',async()=>{
  const r=rig();let clicked=0;r.document.getElementById('go').addEventListener('click',()=>clicked++);
  const v=await r.a.dispatch(request('click',{selector:'#go'}));assert.equal(clicked,1);assert.equal(v.completed,false);
  await assert.rejects(r.a.dispatch(request('event',{selector:'#go',event:'eval'})),/事件类型/);
});
test('new page actions retain the existing Trace transaction guard and busy lock',async()=>{
  const r=rig();r.tools.dbg={session:{connected:true}};
  const a=createTraceAutomation(r.tools,r.win);
  const v=await a.dispatch(request('invoke',{method:'send',args:['shared']}));assert.equal(v.value.sent,6);
});
test('a long page task permits observation and allowlisted stopping but rejects competing writes',async()=>{
  const r=rig();let finish;
  r.tools.scope=r.tools.assistant;
  r.tools.scope.wait=()=>new Promise(resolve=>{finish=resolve;});
  const section=r.document.createElement('section');section.id='tab-scope';
  section.innerHTML='<button id="sc-stop">停止</button><input id="value" value="1">';r.document.body.append(section);
  section.querySelector('#sc-stop').addEventListener('click',()=>finish('stopped'));
  const a=createTraceAutomation(r.tools,r.win);
  const pending=a.dispatch({action:'workbench',page:'scope',op:'invoke',method:'wait'});
  await Promise.resolve();await Promise.resolve();
  assert.equal((await a.dispatch({action:'workbench',page:'scope',op:'read',selector:'#value'})).value,'1');
  await assert.rejects(a.dispatch({action:'workbench',page:'scope',op:'set',selector:'#value',value:2}),/尚未结束/);
  await assert.rejects(a.dispatch({action:'workbench',page:'scope',op:'interrupt',selector:'#value'}),/白名单/);
  await a.dispatch({action:'workbench',page:'scope',op:'interrupt',selector:'#sc-stop'});
  assert.equal((await pending).value,'stopped');
});
test('reload retains worker identity but new navigation cannot inherit another tab identity',()=>{
  const store=new Map();let type='navigate',counter=0;
  const env={crypto:{randomUUID:()=>(++counter).toString(16).padStart(32,'0')},
    sessionStorage:{getItem:key=>store.get(key),setItem:(key,v)=>store.set(key,v)},
    performance:{getEntriesByType:()=>[{type}]}};
  const first=browserWorkerId('token',env);type='reload';
  assert.equal(browserWorkerId('token',env),first);
  type='navigate';assert.notEqual(browserWorkerId('token',env),first);
  type='reload';assert.notEqual(browserWorkerId('new-token',env),first);
});
