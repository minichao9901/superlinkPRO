import assert from 'node:assert/strict';
import {readFileSync,existsSync} from 'node:fs';
import {EVKLITE_J3} from '../../app/ui/board-pinout.js';
import {PinMap,pinMapModel,pinMapTable} from '../../app/ui/pin-map.js';
assert.equal(EVKLITE_J3.length,40);assert.deepEqual(EVKLITE_J3.map(p=>p[0]),Array.from({length:40},(_,i)=>i+1));
assert.ok(Object.isFrozen(EVKLITE_J3)&&EVKLITE_J3.every(Object.isFrozen));
for(const [pin,name] of [[10,'PB14'],[19,'PA29'],[21,'PA28'],[6,'GND']])assert.equal(EVKLITE_J3[pin-1][1],name);
const adc=pinMapModel('adc','hpm5301evklite'),i2c=pinMapModel('i2c','hpm5301evklite');
assert.deepEqual(Object.keys(adc.signals),['10']);assert.deepEqual(Object.keys(i2c.signals),['19','21']);
assert.match(adc.wiring,/J3\[10\].*PB14/);assert.match(i2c.wiring,/J3\[19\].*PA29.*J3\[21\].*PA28/);
assert.match(adc.notes.join(' '),/0–VREFH/);assert.match(adc.notes.join(' '),/QSPI IO2/);assert.match(i2c.notes.join(' '),/上拉至 3.3 V/);
const html=pinMapTable(adc);assert.equal((html.match(/<tr>/g)||[]).length,20);assert.equal((html.match(/class="p-pin"/g)||[]).length,40);
assert.equal((html.match(/class="p-name is-signal"/g)||[]).length,1);assert.equal((html.match(/class="p-name is-ground"/g)||[]).length,8);
assert.ok(!pinMapModel('adc','unknown').available);assert.ok(!pinMapModel('adc','hpm5301evklite',{connected:true,supported:false}).available);
assert.match(pinMapModel('i2c','hpm5301evklite',{connected:true,mock:true}).message,/模拟会话/);
assert.match(pinMapModel('i2c','hpm5301evklite',{connected:true}).message,/未自动识别/);
assert.ok(pinMapTable({...adc,signals:{10:'<img src=x onerror=bad>'}}).includes('&lt;img'));
const fw=new URL('../../../5301evk_akaLinkPro/firmware/application_5301/boards/hpm5301evklite/board.h',import.meta.url);
if(existsSync(fw)){
 const text=readFileSync(fw,'utf8');assert.match(text,/#define BOARD_I2C_BRIDGE_SDA_LABEL "PA28\/J3\[21\]"/);assert.match(text,/#define BOARD_I2C_BRIDGE_SCL_LABEL "PA29\/J3\[19\]"/);
}
// Exercise modal lifecycle without hardware: lazy create, selection, Escape/backdrop/focus, live capability changes.
class Element {
 constructor(){this.handlers={};this.attrs={};this.parts={};this.hidden=false;this.value='';}
 addEventListener(k,fn){(this.handlers[k]??=[]).push(fn);}
 setAttribute(k,v){this.attrs[k]=v;}
 querySelector(k){return this.parts[k]??=new Element();}
 focus(){document.activeElement=this;}
 emit(k,extra={}){const e={target:this,preventDefault(){this.prevented=true;},stopPropagation(){},...extra};for(const f of this.handlers[k]||[])f(e);return e;}
}
const button=new Element(),body={children:[],appendChild(x){this.children.push(x);}};
globalThis.document={body,activeElement:button,getElementById(){return button;},createElement(){return new Element();}};
let state={connected:false};const map=new PinMap({buttonId:'an-pinmap-btn',feature:'adc',state:()=>state});map.init();
assert.equal(body.children.length,0);button.emit('click');assert.equal(body.children.length,1);assert.equal(map.box.hidden,false);assert.equal(map.box.attrs.role,'dialog');
assert.equal(map.box.querySelector('[data-chart]').innerHTML,'');map.board.value='hpm5301evklite';map.board.emit('change');
assert.match(map.box.querySelector('[data-chart]').innerHTML,/ADC0.6/);
map.board.focus();assert.ok(map.box.emit('keydown',{key:'Tab'}).prevented);assert.equal(document.activeElement,map.closeButton);
map.box.emit('keydown',{key:'Escape'});assert.ok(map.box.hidden);assert.equal(document.activeElement,button);
button.emit('click');assert.equal(body.children.length,1);state={connected:true,supported:false};map.refresh();assert.equal(map.board.value,'','connection changes invalidate old board selection');assert.equal(map.box.querySelector('[data-chart]').innerHTML,'');
map.box.emit('click');assert.ok(map.box.hidden);
console.log('Pin maps: shared 40-pin data, firmware SDA/SCL agreement, ADC channel/QSPI/range, unknown/unsupported boards, escaped labels, lazy modal/keyboard/focus/reuse PASS');
