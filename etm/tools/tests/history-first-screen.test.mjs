/**
 * R0-B 首屏口径回归：默认轨迹图（timeline）必须与 overview/首页行解耦。
 *
 * 旧行为：`HistoryView.render()` 在请求首页行前 `await renderTimeline()`，
 * 大样本 timeline（合成 1 s 实测 497 s）会把执行流/表格一起卡住，45 s 后超时；
 * 因此 “open+overview+page≈0.51 s” 只是对象层部分请求，不是完整首屏。
 *
 * 本测试用 mock API 让 timeline 永不/延迟返回，断言：
 *   1) 首页行/表格在 timeline 尚未返回时已经渲染（render 不等待 timeline）；
 *   2) 轨迹图摘要行有明确进度提示；
 *   3) timeline 成功后才给图表 setData，且同一 key 不重复请求；
 *   4) timeline 失败/超时只影响图表，表格与 render 不受影响、可重试；
 *   5) `load()` 的 overview → 首页行顺序也不等待 timeline；
 *   6) 非执行流视图（如包视图）仍不请求 timeline（既有行为保持）。
 *
 * 不启动浏览器、不读真实 store；浏览器/HTTP 时序见
 * docs/validation/20261020-ds-etm-r0-firstscreen-1891916/。
 */
import test,{after} from 'node:test';
import assert from 'node:assert/strict';
import {HistoryView} from '../../web/app/history.js';

/** 极简 DOM：history.js 只用到 getElementById/createElement/querySelectorAll 与少量元素属性。 */
class ClassList{
  constructor(){this.items=new Set();}
  add(...names){for(const name of names)this.items.add(name);}
  remove(...names){for(const name of names)this.items.delete(name);}
  toggle(name,force){const on=force===undefined?!this.items.has(name):!!force;if(on)this.items.add(name);else this.items.delete(name);return on;}
  contains(name){return this.items.has(name);}
}
class FakeElement{
  constructor(tag){
    this.tagName=String(tag).toUpperCase();this.children=[];this.dataset={};this.attrs={};this.classList=new ClassList();
    this.textContent='';this.className='';this.hidden=false;this.disabled=false;this.checked=false;this.value='';this.title='';
    this.scrollTop=0;this.colSpan=1;this.tabIndex=-1;this.style={};
  }
  append(...nodes){for(const node of nodes)this.children.push(node);return this;}
  replaceChildren(...nodes){this.children=[];return this.append(...nodes);}
  setAttribute(name,value){this.attrs[name]=String(value);}
  getAttribute(name){return this.attrs[name]??null;}
  querySelectorAll(){return [];}
  querySelector(){return null;}
  closest(){return null;}
  getBoundingClientRect(){return {top:0,left:0,width:600,height:300};}
}
const elements=new Map();
globalThis.document={
  getElementById(id){if(!elements.has(id))elements.set(id,new FakeElement('div'));return elements.get(id);},
  createElement(tag){return new FakeElement(tag);},
  querySelector(selector){const key='selector:'+selector;if(!elements.has(key))elements.set(key,new FakeElement('div'));return elements.get(key);},
  querySelectorAll(){return [];},
};
const $=id=>globalThis.document.getElementById(id);

const overview={
  protocol:'ETMv4',name:'测试记录',eventCount:4,firstSeq:2,
  elf:{sha256:'a'.repeat(64)},
  summary:{instructions:4,exceptions:0,functions:{}},
  quality:{validPackets:10,unknownExecution:0,packetIssues:0,overflowPackets:0,unaccountedBytes:0,overlapBytes:0,
    unsynchronizedBytes:0,unknownSourceBytes:0,unsynchronizedRecords:0,processedBytes:100,accountedBytes:100,
    inputBytes:100,unprocessedBytes:0,basis:'测试',timing:{cycleElements:0},firstPathOffset:0},
  status:{lastErrorCode:0},targetVerified:true,analysis:{timing:{timeFlameAvailable:false},functions:[],hotspots:[]},sourceFiles:[],
};
const pageData={offset:0,total:2,rows:[
  {seq:2,kind:'instruction',pc:0x08000000,mnemonic:'mov',operands:'r0, #0',function:'main',line:10,executionState:'executed',executed:true,bytes:'0000'},
  {seq:3,kind:'instruction',pc:0x08000002,mnemonic:'bx',operands:'lr',function:'main',line:11,executionState:'executed',executed:true,bytes:'0000'},
]};
const timelineData={range:{start:0,end:3,totalStart:0,totalEnd:3},binCount:4,
  lanes:[{name:'main',kind:'function',bins:[1,0,2,0],records:3}],markers:[],
  summary:{executed:3,unknown:0,notTaken:0,markers:0}};

const tick=()=>new Promise(resolve=>setTimeout(resolve,5));
async function waitFor(predicate,{label='条件',timeout=2000}={}){
  const until=Date.now()+timeout;
  while(Date.now()<until){if(predicate())return;await tick();}
  throw new Error('等待超时：'+label);
}
const tableRows=()=>{const table=$('table-container').children[0];return table?.children?.[1]?.children?.length||0;};

const created=[];
function makeHistory(api,chart){
  const h=Object.create(HistoryView.prototype);
  Object.assign(h,{api,sid:'sid',result:overview,revision:0,recordRevision:1,selectRevision:0,view:'instructions',offset:0,
    selected:null,selectedFunction:null,rows:[],sources:new Map(),range:null,rangeHistory:[],window:null,flameMode:'timeline',
    flameSourceCollapsed:false,scopeBusy:false,timelineKey:null,timelineData:null,timelineCollapsed:false,timelineZoom:null,
    flameZoomControls:null,loading:false,timelineTask:null,timelinePending:null,timelinePendingKey:null,
    timelineProgressTimer:null,timelineStartedAt:null,timelineChart:chart});
  created.push(h);
  return h;
}
function newChart(){return {data:null,cursor:null,setData(value){this.data=value;},setCursor(value){this.cursor=value;},redraw(){}};}

after(()=>{for(const h of created)h.stopTimelineProgress?.();});

test('慢 timeline 不阻塞首页行：表格先渲染，图表后到，进度提示可见',async()=>{
  let release;const gate=new Promise(resolve=>{release=resolve;});
  const calls=[];const chart=newChart();
  const api=path=>{
    calls.push(path);
    if(path.includes('/timeline?'))return gate;
    if(path.includes('/page?'))return Promise.resolve(pageData);
    throw new Error('unexpected '+path);
  };
  const h=makeHistory(api,chart);
  const rendering=h.render();
  await waitFor(()=>tableRows()===2,{label:'首页两行先渲染'});
  assert.equal(chart.data,null,'timeline 未返回时图表不应有数据');
  assert.ok(h.timelineTask,'render 应把 timeline 作为独立任务暴露出来');
  assert.match($('execution-timeline-summary').textContent,/正在加载默认轨迹图/);
  assert.equal($('execution-timeline-panel').hidden,false,'执行流视图应显示轨迹图面板（带进度）');
  assert.ok(calls.some(path=>path.includes('/timeline?')),'timeline 请求应已发起（并发，不阻塞）');
  assert.ok(calls.some(path=>path.includes('/page?')),'首页 page 请求应已发出');

  release(timelineData);
  await rendering;
  await h.timelineTask;
  assert.equal(chart.data,timelineData,'timeline 到达后应设置图表数据');
  assert.match($('execution-timeline-summary').textContent,/条已证实/,'完成后显示轨迹统计，清除进度文案');
  assert.doesNotMatch($('execution-timeline-summary').textContent,/正在加载/);

  const timelineCalls=calls.filter(path=>path.includes('/timeline?')).length;
  await h.render();
  assert.equal(calls.filter(path=>path.includes('/timeline?')).length,timelineCalls,'同一 key 重绘不应重复请求 timeline');
  assert.equal(chart.data,timelineData);
});

test('timeline 失败/超时不影响表格，提供失败提示并可重试',async()=>{
  const chart=newChart();let timelineCalls=0;
  const api=path=>{
    if(path.includes('/timeline?')){timelineCalls++;return Promise.reject(new Error('请求超时（45000 ms）'));}
    if(path.includes('/page?'))return Promise.resolve(pageData);
    throw new Error('unexpected '+path);
  };
  const h=makeHistory(api,chart);
  await h.render();
  await h.timelineTask;
  assert.equal(tableRows(),2,'图表失败后首页行仍应保留');
  assert.equal(chart.data,null);
  assert.equal(h.timelineKey,null,'失败的 timeline 不应写入缓存 key（下次可重试）');
  assert.match($('execution-timeline-summary').textContent,/轨迹图加载失败/);
  assert.match($('execution-timeline-summary').textContent,/执行流与表格不受影响/);

  await h.render();
  assert.equal(timelineCalls,2,'失败后再次 render 应允许重试 timeline');
  await h.timelineTask;
});

test('load(): overview 与首页行先可用，不等待默认轨迹图',async()=>{
  let release;const gate=new Promise(resolve=>{release=resolve;});
  const chart=newChart();const calls=[];
  const api=path=>{
    calls.push(path);
    if(path.includes('/overview'))return Promise.resolve(overview);
    if(path.includes('/timeline?'))return gate;
    if(path.includes('/page?'))return Promise.resolve(pageData);
    if(path.includes('/event/'))return Promise.resolve({event:{seq:2,kind:'instruction',pc:0x08000000,mnemonic:'mov',operands:'r0, #0',function:'main',line:10,executionState:'executed',executed:true,bytes:'0000',file:null},callPath:[],stackBasis:'测试'});
    throw new Error('unexpected '+path);
  };
  const h=makeHistory(api,chart);h.sid=null;
  await h.load('sid','测试记录');
  assert.equal(tableRows(),2,'load() 返回时首页行应已渲染');
  assert.equal(chart.data,null,'load() 不应等待 timeline');
  assert.equal(h.selected,2,'首个位置仍应照常选中');
  assert.match($('execution-timeline-summary').textContent,/正在加载默认轨迹图/);
  const overviewIndex=calls.findIndex(path=>path.includes('/overview'));
  const pageIndex=calls.findIndex(path=>path.includes('/page?'));
  assert.ok(overviewIndex>=0&&pageIndex>overviewIndex,'请求顺序应为 overview → 首页 page');
  release(timelineData);
  await h.timelineTask;
  assert.equal(chart.data,timelineData);
});

test('包/统计视图仍不请求 timeline（既有折叠与视图行为保持）',async()=>{
  const chart=newChart();const calls=[];
  const api=path=>{calls.push(path);if(path.includes('/page?'))return Promise.resolve({offset:0,total:0,rows:[]});throw new Error('unexpected '+path);};
  const h=makeHistory(api,chart);h.view='packets';
  await h.render();
  assert.equal(calls.filter(path=>path.includes('/timeline?')).length,0,'非执行流视图不应请求轨迹图');
  assert.equal($('execution-timeline-panel').hidden,true,'非执行流视图应隐藏轨迹图面板');
});
