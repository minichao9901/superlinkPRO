import {webClockKhz,webClockLimitKhz} from '../core/target-clock.js';

const options=new WeakMap();
/** Remove unsupported choices, including remembered values from earlier versions. */
export function constrainClockSelect(select,target,persist=()=>{}){
  if(!select)return;
  const base=options.get(select)||[];
  for(const o of select.options)if(!base.some(b=>b.value===o.value))base.push({value:o.value,label:o.textContent});
  for(const khz of [25000,30000,36000,40000,45000,50000,60000])
    if(!base.some(o=>Number(o.value)===khz))base.push({value:String(khz),label:`${khz/1000} MHz`});
  options.set(select,base.sort((a,b)=>Number(a.value)-Number(b.value)));
  const previous=select.value,max=webClockLimitKhz(target);
  select.replaceChildren(...options.get(select).filter(o=>Number.isFinite(Number(o.value))&&Number(o.value)>=0&&Number(o.value)<=max)
    .map(o=>new Option(o.label,o.value)));
  const normalized=Number(previous)>max?String(max):previous;
  select.value=normalized;
  if(select.selectedIndex<0)select.value=String(webClockKhz(previous,target,10000));
  if(select.value!==previous)persist(select.value);
}
