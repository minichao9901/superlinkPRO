import test from 'node:test';
import assert from 'node:assert/strict';
import {parseHTML} from 'linkedom';
import {flameView} from '../../../etm/web/app/analysis-views.js';

const leaf=(name,value=1)=>({name,value,self:value,children:[]});
function setup(){const {document}=parseHTML('<html><body></body></html>');globalThis.document=document;return document;}
const scope={start:10,end:12,totalStart:0,totalEnd:20};
const result={analysis:{flame:{name:'root',value:3,self:0,children:[leaf('A',1),leaf('B',2)]},timing:{}}};
const base={window:{range:scope,analysis:result.analysis,timeline:[
 {path:['root','A','shared'],startSeq:10,endSeq:10,records:1,unknown:0,firstSeq:10,events:[]},
 {path:['root','B','shared'],startSeq:11,endSeq:12,records:2,unknown:0,firstSeq:11,events:[]}]},
 onError:e=>{throw e;},onReset(){},onBack(){},onToggleSource(){},onMode(){}};

test('timeline export asks for original exact paths; changed ancestors break descendants',async()=>{
 setup();let request;
 const view=flameView(result,()=>{},{...base,mode:'timeline',onExport:async o=>{request=o;}});
 const button=[...view.querySelectorAll('button')].find(b=>b.textContent==='导出当前火焰图 SVG');
 await button.onclick();await new Promise(r=>setImmediate(r));
 assert.deepEqual(request,{mode:'timeline',weight:'records',rootPath:''});assert.equal(button.disabled,false);
 const blocks=[...view.querySelectorAll('g[role="button"]')].filter(g=>g.getAttribute('aria-label').startsWith('shared '));
 assert.equal(blocks.length,2);
});

test('weighted export keeps current zoom and chosen weight; no second details appendix',async()=>{
 setup();let request,shown;
 const view=flameView(result,()=>{},{...base,mode:'aggregate',onExportOptions:o=>{shown=o;},onExport:async o=>{request=o;}});
 [...view.querySelectorAll('g[role="button"]')].find(g=>g.getAttribute('aria-label').startsWith('函数 B，')).onclick();
 const button=[...view.querySelectorAll('button')].find(b=>b.textContent==='导出当前火焰图 SVG');
 await button.onclick();assert.deepEqual(request,{mode:'aggregate',weight:'records',rootPath:'1'});assert.deepEqual(shown,request);
 assert.doesNotMatch(view.textContent,/完整函数明细/);
});
