import test from 'node:test';
import assert from 'node:assert/strict';
import {parseHTML} from 'linkedom';
import {integrityView} from '../../bridge/etm/backend/web/app/analysis-views.js';

function duration(physical,captureExtent){
  globalThis.document=parseHTML('<html><body></body></html>').document;
  const view=integrityView({quality:{timing:{cycleElements:0}},physical,captureExtent});
  return [...view.querySelectorAll('.integrity-row')].find(row=>row.firstChild.textContent==='实际采集时长')?.lastChild.textContent;
}
test('report displays actual 926ms instead of requested 1s',()=>{
  assert.equal(duration({sample_depth:463382775,sample_rate:500000000},{requestedSeconds:1}),
    '926.766 ms · 请求 1000.000 ms');
});
test('historical full capture and CSV window use their own physical duration',()=>{
  assert.equal(duration({sample_depth:500000000,sample_rate:500000000}),'1000.000 ms');
  assert.equal(duration({window_seconds:[-.01,.19]}),'200.000 ms');
});
test('unknown duration is not invented from cycles or the request alone',()=>{
  assert.equal(duration(null,{requestedSeconds:1}),undefined);
  assert.equal(duration({window_seconds:[0,NaN]}),undefined);
});
