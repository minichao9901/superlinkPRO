import test from 'node:test';
import assert from 'node:assert/strict';
import {initSessionContext} from '../../app/ui/session-context.js';
import {store} from '../../app/core/store.js';

class Element {
  constructor(tag='DIV'){this.tagName=tag;this.children=[];this.handlers={};this.value='';this.options=['stm32f103','stm32h743'].map(value=>({value}));}
  get selectedOptions(){return [{textContent:this.value}];}
  append(...xs){this.children.push(...xs);} prepend(...xs){this.children.unshift(...xs);}
  after(){} replaceChildren(...xs){this.children=xs;} setAttribute(){}
  addEventListener(type,fn){(this.handlers[type]??=[]).push(fn);}
  change(){for(const fn of this.handlers.change||[])fn();}
  querySelector(){return this.side??=new Element();}
  get parentElement(){return this.parent??=new Element('FIELDSET');}
}
async function fixture(run){
  const previous={document:globalThis.document,window:globalThis.window,setInterval:globalThis.setInterval};
  const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  globalThis.document={getElementById:get,createElement:tag=>new Element(tag.toUpperCase())};
  globalThis.window={addEventListener(){}};globalThis.setInterval=()=>0;store.d={};
  get('f-chip').value=get('r-chip').value='stm32f103';get('r-target').value=get('sc-target').value='swd';get('d-backend').value='webusb';
  const view=()=>({session:{bps:[],bpNotes:new Map(),regs:[]},watch:{items:[]},renderVars(){},updatePlan(){},renderElfInfo(){},renderBps(){},renderWatch(){},_fileInfo(){},_applyChip(){},_applyTargetUi(){},_applyBackendUi(){},async _syncBackend(){}});
  const tools={flash:view(),rtt:view(),dbg:view(),scope:view()};
  const context=initSessionContext(tools);
  const card=page=>(page==='scope'?get('sc-side-capture-panel'):page==='dbg'?get('d-side-session-panel'):get('tab-'+page).side).children[0];
  const adopt=page=>card(page).children[2];
  try{await run({get,tools,context,adopt,card});}
  finally{Object.assign(globalThis,previous);store.d={};}
}

test('UI A -> adopt B -> choose A clears B firmware, including repeated adoption',()=>fixture(async({get,tools,adopt})=>{
  get('r-chip').value='stm32h743';get('r-chip').change();
  await adopt('flash').onclick();assert.equal(get('f-chip').value,'stm32h743');
  tools.flash.file={name:'h743.elf'};get('f-chip').value='stm32f103';get('f-chip').change();
  assert.equal(tools.flash.file,null);
  get('r-chip').value='stm32f103';get('r-chip').change();
  get('f-chip').value='stm32h743';get('f-chip').change();
  await adopt('rtt').onclick();get('r-addr').value='0x24000000';
  get('r-chip').value='stm32f103';get('r-chip').change();assert.equal(get('r-addr').value,'');
}));

for(const page of ['scope','dbg'])test(`UI ${page} retains its adopted ARM board until explicit replacement`,()=>fixture(async({get,tools,adopt,card})=>{
  await adopt(page).onclick();
  tools.scope.elf={name:'f103.elf'};tools.scope.selected=[{addr:0x20000000}];
  tools.dbg.sym={name:'f103.elf'};tools.dbg.watch.items=[{addr:0x20000000}];
  get('r-chip').value='stm32h743';get('r-chip').change();
  assert.equal(adopt(page).disabled,false);
  assert.match(card(page).children[3].textContent,/stm32f103/);
  if(page==='scope')assert.equal(tools.scope.elf.name,'f103.elf');else assert.equal(tools.dbg.sym.name,'f103.elf');
  await adopt(page).onclick();assert.equal(adopt(page).disabled,true);
  if(page==='scope'){assert.equal(tools.scope.elf,null);assert.deepEqual(tools.scope.selected,[]);}
  else{assert.equal(tools.dbg.sym,null);assert.deepEqual(tools.dbg.watch.items,[]);}
}));

test('generic page architecture does not downgrade the session board or silently adopt it',()=>fixture(async({get,context,adopt,card})=>{
  get('sc-target').value='swd';get('sc-target').change();
  assert.equal(context.target.id,'stm32f103');assert.equal(adopt('scope').disabled,false);
  assert.match(card('scope').children[3].textContent,/未绑定/);
}));

test('debug target adoption routes HPM6800EVK to Wasm and keeps other RISC-V boards on JTAG',()=>fixture(async({get,adopt})=>{
  get('d-hpm-target').options = [{value:'hpm5301evklite'}];
  get('d-arm-target').value = 'stm32f103';
  get('f-chip').value = 'hpm6800evk';
  get('f-chip').change();
  assert.equal(adopt('dbg').disabled,false);
  await adopt('dbg').onclick();
  assert.equal(get('d-backend').value,'openocd');
  assert.equal(get('d-arm-target').value,'stm32f103');
  assert.equal(adopt('dbg').disabled,true);
  get('f-chip').value = 'hpm5301evklite';
  get('f-chip').change();
  await adopt('dbg').onclick();
  assert.equal(get('d-backend').value,'riscv');
  assert.equal(get('d-hpm-target').value,'hpm5301evklite');
}));
