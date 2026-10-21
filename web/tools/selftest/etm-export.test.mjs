import test from 'node:test';
import assert from 'node:assert/strict';
import {fetchExecutionExport} from '../../bridge/etm/backend/web/app/export.js';
import {ui} from './etm-ui-harness.mjs';

test('export errors stay in the page instead of becoming failed JSON downloads',async()=>{
  await assert.rejects(fetchExecutionExport('/export',{fetchImpl:async()=>new Response(JSON.stringify({error:'当前记录尚未载入配套源码文本'}),{status:400,headers:{'Content-Type':'application/json'}})}),/尚未载入配套源码文本/);
  await assert.rejects(fetchExecutionExport('/export',{fetchImpl:async()=>new Response('bad gateway',{status:502})}),/HTTP 502/);
  await assert.rejects(fetchExecutionExport('/export',{fetchImpl:async()=>new Response('{"error":"wrong endpoint"}',{headers:{'Content-Type':'application/json'}})}),/wrong endpoint/);
});

test('successful export preserves the real HTML contents and attachment filename',async()=>{
  const result=await fetchExecutionExport('/export',{fetchImpl:async()=>new Response('<!doctype html><h1>源码</h1>',{headers:{'Content-Type':'text/html; charset=utf-8','Content-Disposition':'attachment; filename=etm-simplified.html'}})});
  assert.equal(result.filename,'etm-simplified.html');assert.equal(await result.blob.text(),'<!doctype html><h1>源码</h1>');
});

test('export timeout aborts the request and reports a local range remedy',async()=>{
  let signal;
  await assert.rejects(fetchExecutionExport('/export',{timeout:5,fetchImpl:async(_,options)=>{signal=options.signal;return new Promise(()=>{});}}),/超时/);
  assert.equal(signal.aborted,true);
});

test('large file transfer may finish after the response deadline without a false timeout',async()=>{
  let signal;
  const result=await fetchExecutionExport('/export',{timeout:5,fetchImpl:async(_,options)=>{
    signal=options.signal;return {ok:true,headers:new Headers({'Content-Type':'text/csv'}),
      blob:async()=>{await new Promise(resolve=>setTimeout(resolve,30));return new Blob(['large export']);}};
  }});
  assert.equal(await result.blob.text(),'large export');assert.equal(signal.aborted,false);
});

test('real export button reports backend errors and releases busy state',async()=>{
  const u=await ui(),original=globalThis.fetch;
  try{
    u.state.current='record-a';u.$('export').disabled=false;u.$('export-type').value='simplified-html';
    globalThis.fetch=async()=>new Response('{"error":"当前记录尚未载入配套源码文本，请选择源码目录"}',{status:400,headers:{'Content-Type':'application/json'}});
    await u.click('export');assert.match(u.$('job-state').textContent,/尚未载入配套源码文本/);
    assert.equal(u.state.operation,false);assert.equal(u.$('export').disabled,false);
    await u.click('record-source-settings');assert.equal(u.$('panel-target').hidden,false);
    assert.equal(u.$('choose-sources').closest('details').open,true);
  }finally{globalThis.fetch=original;u.close();}
});
