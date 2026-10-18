import test from 'node:test';
import assert from 'node:assert/strict';
import {HistoryView} from '../../web/app/history.js';

test('full record range covers the whole record without loading a window',async()=>{
 const h=Object.create(HistoryView.prototype);
 h.result={eventCount:100};h.timelineData=null;
 assert.deepEqual(h.fullRange(),{start:0,end:99,totalStart:0,totalEnd:99});
 h.timelineData={range:{totalStart:5,totalEnd:104}};
 assert.deepEqual(h.fullRange(),{start:5,end:104,totalStart:5,totalEnd:104});
});
test('a slow zoom/fit does not overwrite a newer explicit cursor selection',async()=>{
 let release;const pending=new Promise(resolve=>{release=resolve;});
 const h=Object.create(HistoryView.prototype);
 Object.assign(h,{result:{eventCount:100,firstSeq:0},recordRevision:1,selectRevision:0,
   selected:2,range:null,rangeHistory:[],scopeBusy:false,updateZoomControlState(){},
   render:()=>pending,select:async seq=>{h.selectRevision++;h.selected=seq;}});
 const zoom=h.setRange({start:0,end:49});
 await h.select(7);release();await zoom;
 assert.equal(h.selected,7);assert.equal(h.scopeBusy,false);
});
