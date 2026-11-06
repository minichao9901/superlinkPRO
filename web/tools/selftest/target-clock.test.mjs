import test from 'node:test';
import assert from 'node:assert/strict';
import {webClockKhz,webClockLimitKhz} from '../../app/core/target-clock.js';
import {constrainClockSelect} from '../../app/ui/target-clock.js';

test('Web HPM cap is 25MHz; ARM and other RISC-V retain 60MHz',()=>{
  assert.equal(webClockLimitKhz('hpm6800evk'),25000);
  assert.equal(webClockLimitKhz({id:'hpm5301evklite'}),25000);
  for(const target of ['stm32h7','riscv-other','arm'])assert.equal(webClockLimitKhz(target),60000);
  assert.equal(webClockKhz(66667,'hpm6800evk'),25000);
  assert.equal(webClockKhz(66667,'riscv-other'),60000);
  assert.equal(webClockKhz(25000,'hpm6800evk'),25000);
  assert.equal(webClockKhz('','hpm6800evk',10000),10000);
});

test('target switch removes high choices, migrates remembered high clock, restores ARM choices',()=>{
  const previous=globalThis.Option;
  globalThis.Option=class{constructor(label,value){this.textContent=label;this.value=value;}};
  const select={options:[{value:'',textContent:'默认10MHz'},{value:'10000',textContent:'10MHz'},{value:'60000',textContent:'60MHz'}],value:'60000',
    replaceChildren(...options){this.options=options;},get selectedIndex(){return this.options.findIndex(o=>o.value===this.value);}};
  const saved=[];
  try{
    constrainClockSelect(select,'hpm6800evk',v=>saved.push(v));
    assert.equal(select.value,'25000');assert.deepEqual(saved,['25000']);
    assert.ok(select.options.every(o=>Number(o.value)<=25000));
    constrainClockSelect(select,'stm32h7');
    assert.ok(select.options.some(o=>o.value==='60000'));assert.equal(select.value,'25000');
    select.value='';constrainClockSelect(select,'hpm6800evk');assert.equal(select.value,'');
    select.options.push(new Option('8 MHz','8000'));select.value='8000';
    constrainClockSelect(select,'hpm6800evk');assert.equal(select.value,'8000');
  }finally{globalThis.Option=previous;}
});
