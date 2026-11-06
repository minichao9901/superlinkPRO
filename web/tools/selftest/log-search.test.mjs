import test from 'node:test';
import assert from 'node:assert/strict';
import {logSnapshot,searchLog} from '../../app/core/log-search.js';
const enc=new TextEncoder();
const records=(...parts)=>parts.map(text=>({b:enc.encode(text)}));

test('snapshot joins UTF-8 and search terms across USB chunks without changing raw data',()=>{
  const bytes=enc.encode('前缀 ERROR 中文\n');
  const data=[{b:bytes.subarray(0,2)},{b:bytes.subarray(2,9)},{b:bytes.subarray(9)}];
  const before=Array.from(bytes),snapshot=logSnapshot(data);
  assert.equal(snapshot.text,'前缀 ERROR 中文\n');
  assert.equal(searchLog(snapshot.text,'error').total,1);
  assert.deepEqual(Array.from(bytes),before);
});

test('literal search, case and line filtering preserve the full original text',()=>{
  const source='error a.b [x]\nERROR aXb\n<script>alert(1)</script>\n';
  assert.equal(searchLog(source,'a.b').total,1);
  assert.equal(searchLog(source,'[x]').total,1);
  assert.equal(searchLog(source,'error',{matchCase:true}).total,1);
  assert.equal(searchLog(source,'ERROR',{filter:true}).text,'error a.b [x]\nERROR aXb');
  assert.equal(searchLog(source,'<script>').text,source);
  assert.equal(searchLog(source,'').text,source);
});

test('snapshots have a byte budget, strip terminal controls, and support hex',()=>{
  const data=records('old data','\x1b[31mred\x1b[0m\r\n');
  assert.equal(logSnapshot(data).text,'old datared\n');
  assert.equal(logSnapshot(records('\x1b]0;title\x1b\\keep\x1b]0;next\x1b\\end')).text,'keepend');
  const bounded=logSnapshot(records('123456','789'),{limit:5});
  assert.equal(bounded.text,'56789');assert.equal(bounded.bytes,5);assert.equal(bounded.clipped,true);
  assert.equal(logSnapshot([{b:Uint8Array.of(0,255,65)}],{hex:true}).text,'00 FF 41');
});

test('highlight count is bounded even with many matches and Unicode case variants',()=>{
  const result=searchLog('a'.repeat(2000),'a');
  assert.equal(result.total,2000);assert.equal(result.matches.length,1000);
  const unicode=searchLog('İ x ERROR','error');
  assert.deepEqual(unicode.matches,[[4,9]]);
});
