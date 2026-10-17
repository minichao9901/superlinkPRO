import test from 'node:test';
import assert from 'node:assert/strict';
import {HistoryView} from '../../web/app/history.js';

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
