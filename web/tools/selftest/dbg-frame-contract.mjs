/** Shared oracle schema / comparison. Never treat missing coverage as a pass. */
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
export const FRAME_CASES=[
 {id:'recursive',checkpoint:'dbg_frame_recursive_checkpoint',required:['depth_copy','seed_copy','frame_value']},
 {id:'leaf',checkpoint:'dbg_frame_leaf_checkpoint',required:['leaf_value','items','record']},
 {id:'shadow',checkpoint:'dbg_frame_shadow_checkpoint',required:['shadow','outer_copy','inner_copy']},
 {id:'shadow-exit',checkpoint:'dbg_frame_shadow_exit_checkpoint',required:['shadow','outer_copy']},
 {id:'register',checkpoint:'dbg_frame_register_checkpoint',required:['arg','register_value']},
 {id:'before-call',checkpoint:'dbg_frame_before_call_checkpoint',required:['arg','stack_value']},
 {id:'after-call',checkpoint:'dbg_frame_after_call_checkpoint',required:['arg','stack_value','result']},
];
export const sha256=b=>createHash('sha256').update(b).digest('hex');
export const readJson=p=>JSON.parse(readFileSync(p,'utf8').replace(/^\uFEFF/,''));
export const frameSourceHash=()=>sha256(readFileSync(new URL('../target-firmware/common/dbg_frames.c',import.meta.url)));
const stableJson=value=>JSON.stringify(value,(_key,item)=>{
 if(!item||typeof item!=='object'||Array.isArray(item))return item;
 return Object.fromEntries(Object.keys(item).sort().map(key=>[key,item[key]]));
});
export function validateBuild(build,elfBytes,board){
 if(build.schema!==1||build.board!==board||build.elfSha256!==sha256(elfBytes))throw new Error('构建信息 board/ELF SHA-256 不匹配');
 if(build.sources?.['dbg_frames.c']!==frameSourceHash())throw new Error('测试源代码已修改，请重新构建 ELF 并采集 GDB 对照');
 if(!['Og','Os'].includes(build.optimization)||![4,5].includes(build.dwarf)||!Array.isArray(build.flags)||!build.sources?.['dbg_frames.c'])throw new Error('构建信息缺少优化/DWARF/源码记录');
}
export function validateOracle(oracle,build,bytes,board){
 validateBuild(build,bytes,board);
 if(oracle.schema!==1||oracle.board!==board||oracle.elfSha256!==sha256(bytes)||stableJson(oracle.build)!==stableJson(build))throw new Error('GDB 对照与当前 ELF/构建参数不匹配');
 if(!oracle.codeVerified||!oracle.tool?.includes('gdb'))throw new Error('对照未验证目标代码或不是 GDB 采集');
 for(const c of FRAME_CASES){
  const item=oracle.cases?.[c.id];if(!item?.frames?.length||!Number.isInteger(item.pc))throw new Error('对照缺少检查点 '+c.id);
  for(const frame of item.frames){
   if(!frame.name?.startsWith('engine_frame_')||!Number.isInteger(frame.pc)||!Number.isInteger(frame.sp)||!Array.isArray(frame.variables))throw new Error('对照帧格式无效 '+c.id);
   const keys=new Set();
   for(const v of frame.variables){
    const key=`${v.name}#${v.occurrence}`;if(keys.has(key)||!Number.isInteger(v.occurrence)||v.occurrence<0||!['ok','unavailable'].includes(v.status)||!Array.isArray(v.fields))throw new Error('对照变量格式/重名索引无效 '+c.id);keys.add(key);
    if(v.status==='ok'&&(!v.fields.length||new Set(v.fields.map(f=>f.path)).size!==v.fields.length||v.fields.some(f=>typeof f.path!=='string'||!/^[-]?\d+$/.test(f.value))))throw new Error('对照复合字段缺失/重复/格式无效 '+c.id);
   }
  }
  if(item.frames[0].pc!==item.pc)throw new Error('对照检查点 PC 不一致 '+c.id);
  for(const name of c.required)if(!item.frames[0].variables?.some(v=>v.name===name&&v.status==='ok'&&v.fields?.length))throw new Error('GDB 必测变量不可用 '+c.id+':'+name);
  if(c.id==='recursive'&&item.frames.filter(f=>f.name==='engine_frame_recursive').length!==5)throw new Error('对照未覆盖五层递归');
 }
 const value=(id,name,path='',frame=0,occurrence=0)=>oracle.cases[id].frames[frame].variables.find(v=>v.name===name&&v.occurrence===occurrence)?.fields?.find(f=>f.path===path)?.value;
 const expect=(id,name,v,path='',frame=0,occurrence=0)=>{if(value(id,name,path,frame,occurrence)!==String(v))throw new Error('GDB 固定输入契约错误 '+id+':'+name+'.'+path);};
 for(let i=0;i<5;i++){expect('recursive','depth_copy',i,'',i);expect('recursive','seed_copy',1028-i*7,'',i);expect('recursive','frame_value',1028-i*7+i*100,'',i);}
 expect('leaf','leaf_value',1128);expect('leaf','items',1028,'[0]');expect('leaf','items',1029,'[1]');expect('leaf','items',1030,'[2]');
 expect('leaf','record',0x12345678,'tag');expect('leaf','record',-17,'signed_value');expect('leaf','record',21,'pair[0]');expect('leaf','record',34,'pair[1]');
 expect('shadow','shadow',60);expect('shadow','shadow',70,'',0,1);expect('shadow','inner_copy',70);
 if(oracle.cases['shadow-exit'].frames[0].variables.filter(v=>v.name==='shadow').length!==1||oracle.cases['shadow-exit'].frames[0].variables.some(v=>v.name==='inner_copy'))throw new Error('GDB shadow-exit 作用域不正确');
 expect('register','arg',23);expect('register','register_value',70);
 for(const id of ['before-call','after-call']){expect(id,'arg',70);expect(id,'stack_value',79);}
 expect('after-call','result',346);
 const before=oracle.cases['before-call'].frames[0].variables.find(v=>v.name==='arg'),after=oracle.cases['after-call'].frames[0].variables.find(v=>v.name==='arg');
 const beforeLocation=dwarfLocationAt(before?.location,oracle.cases['before-call'].pc);
 const afterLocation=dwarfLocationAt(after?.location,oracle.cases['after-call'].pc);
 if(before.address!=null||!/(?:variable in \$(?:r\d+|a[0-7]|x\d+)|DW_OP_reg(?:x\d+|\d+))/i.test(beforeLocation)||
    !/(?:DW_OP_fbreg|DW_OP_breg\d+).*DW_OP_stack_value/s.test(afterLocation))
  throw new Error('该构建没有覆盖参数从寄存器迁移到栈');

}
export function dwarfLocationAt(output,pc){
 if(typeof output!=='string'||!Number.isInteger(pc))return '';
 const ranges=[];let current=null;
 for(const line of output.split(/\r?\n/)){
  const match=line.match(/(?:^|\s)Range\s+(0x[\da-f]+)\s*-\s*(0x[\da-f]+):\s*(.*)$/i);
  if(match){if(current)ranges.push(current);current={start:Number(match[1]),end:Number(match[2]),text:match[3]};}
  else if(current&&line.trim()!=='.')current.text+='\n'+line.trim();
 }
 if(current)ranges.push(current);
 return ranges.find(range=>pc>=range.start&&pc<range.end)?.text||'';
}
const number=text=>{
 const m=String(text).trim().match(/^(?:→\s*)?(-?0x[\da-f]+|-?\d+)(?:\s|$)/i);
 if(!m)throw new Error('无法规范化变量值 '+text);
 return String(Number(m[1]));
};
export function webVariables(rows){
 const occurrences=new Map();return rows.map(row=>{
  const occurrence=occurrences.get(row.name)||0;occurrences.set(row.name,occurrence+1);
  const result={name:row.name,occurrence,argument:!!row.argument,status:row.error?'unavailable':'ok',fields:[]};
  if(row.error){result.reason=row.error;return result;}
  if(row.address!=null)result.address=row.address;
  if(!row.children){result.fields.push({path:'',value:number(row.value)});return result;}
  const parents=[];
  for(const child of row.children){
   parents.length=child.depth;const path=[...parents,child.name].reduce((p,n)=>p?(n.startsWith('[')?p+n:p+'.'+n):n,'');
   if(child.cls==='dim'&&(/^[{\[]/.test(child.text))){parents[child.depth]=child.name;continue;}
   if(child.cls==='dim'||child.overflow)throw new Error('必测复合变量未完整解析 '+row.name+':'+child.text);
   result.fields.push({path,value:number(child.text)});
  }
  return result;
 });
}
const variableKey=v=>`${v.name}#${v.occurrence}`;
export function compareFrames(actual,expected,{allowConservativeUnavailable=[]}={}){
 const failures=[];
 if(actual.length!==expected.length)failures.push(`帧数 ${actual.length} != ${expected.length}`);
 for(let i=0;i<Math.min(actual.length,expected.length);i++){
  const a=actual[i],e=expected[i],prefix=`帧${i}`;
  for(const key of ['name','pc','sp'])if(a[key]!==e[key])failures.push(`${prefix} ${key}: ${a[key]} != ${e[key]}`);
  const av=new Map((a.variables||[]).map(v=>[variableKey(v),v])),ev=new Map((e.variables||[]).map(v=>[variableKey(v),v]));
  for(const key of new Set([...av.keys(),...ev.keys()])){
   const x=av.get(key),y=ev.get(key);if(!x||!y){failures.push(`${prefix} ${key} 作用域/变量缺失`);continue;}
   if(x.argument!==y.argument||x.status!==y.status){
    const conservative=allowConservativeUnavailable.some(rule=>rule.frame===i&&rule.name===x.name&&
     (rule.occurrence??0)===x.occurrence&&x.argument===y.argument&&x.status==='unavailable'&&y.status==='ok'&&
     rule.reasonPattern?.test(x.reason||''));
    if(conservative)continue;
    failures.push(`${prefix} ${key} 参数/可用状态不一致 (${x.reason||''})`);continue;
   }
   if(y.status==='ok'){
    if(x.address!==y.address)failures.push(`${prefix} ${key} 位置地址不一致: ${x.address} != ${y.address}`);
    const fields=v=>Object.fromEntries(v.fields.map(f=>[f.path,f.value]));
    const xf=fields(x),yf=fields(y);for(const path of new Set([...Object.keys(xf),...Object.keys(yf)]))if(xf[path]!==yf[path])failures.push(`${prefix} ${key}.${path}: ${xf[path]} != ${yf[path]}`);
   }
  }
 }
 return failures;
}
