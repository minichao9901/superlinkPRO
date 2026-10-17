import assert from 'node:assert/strict';
import {ScopeView} from '../../app/scope/view.js';
import {RttCdcView} from '../../app/hid/view.js';
const nodes=new Map(['sc-target','h-target','h-clock'].map(id=>[id,{value:'riscv'}]));globalThis.document={getElementById:id=>nodes.get(id)||null};
for(const busy of ['running','_starting','_stopPromise']){
 let sent=0;const s=Object.create(ScopeView.prototype);Object.assign(s,{[busy]:true,hid:{},targetRiscv:false,setStatusText(){},_applyBackendUi(){},hidXfer:async()=>{sent++;}});
 await s.applyTargetType();assert.equal(sent,0);assert.equal(nodes.get('sc-target').value,'swd');
}
let sent=0;const h=Object.create(RttCdcView.prototype);Object.assign(h,{last:{running:true},_targetRiscv:false,dev:{setTargetType:async()=>sent++}});await h.applyTargetType();assert.equal(sent,0);assert.equal(nodes.get('h-target').value,'swd');
console.log('target-switch: active/pending scope and RTT forwarding cannot mutate global backend PASS');
