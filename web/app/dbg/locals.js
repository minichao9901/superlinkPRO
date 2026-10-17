/** Selected-frame locals. DWARF 4/5 locations; read-only and bounded.
 * No parser changes in the shared ELF/JScope code.
 */
import { Reader } from './cfi.js';
import { formatWatchValue, treeRows, summarizeTree } from './watch.js';
const A={location:2,name:3,low:0x11,high:0x12,constant:0x1c,type:0x49,base:0x40,ranges:0x55};
const addressForms=new Set([1,0x1b,0x29,0x2a,0x2b,0x2c]);
const indexes=new WeakMap();
function root(d,rec){let r=rec;for(let i=0;i<128&&r.parent!==-1;i++){r=d.dieAt(r.parent);if(!r)throw new Error('DWARF 父节点无效');}return r;}
function indexedAddress(d,cu,i){const at=(cu.addrBase??8)+i*4,r=new Reader(d.elf.data('.debug_addr')||new Uint8Array(),at);return r.uint(4);}
function listReader(d,rec,attribute,section,baseAttr){
  const b=d.elf.data(section);if(!b)throw new Error('ELF 缺少 '+section);
  let offset=attribute.value;
  if(attribute.listIndex){
    const base=d.num(root(d,rec),baseAttr);if(base==null)throw new Error('DWARF list base 缺失');
    offset=base+new Reader(b,base+offset*4).uint(4);
  }
  if(!Number.isInteger(offset)||offset<0||offset>=b.length)throw new Error('DWARF list 偏移无效');
  return new Reader(b,offset);
}
function rangesOrLocation(d,rec,attribute,pc,location){
  const cu=rec.cu;if(cu.addrSize!==4)throw new Error('局部变量仅支持32位地址');
  let base=d.num(root(d,rec),A.low)??0;
  const modern=cu.version>=5,r=listReader(d,rec,attribute,modern?(location?'.debug_loclists':'.debug_rnglists'):(location?'.debug_loc':'.debug_ranges'),location?0x8c:0x74);
  let fallback=null;
  for(let count=0;count<16384;count++){
    let lo,hi,expr;
    if(!modern){
      lo=r.uint(4);hi=r.uint(4);if(!lo&&!hi)return location?fallback:false;
      if(lo===0xffffffff){base=hi;continue;}lo+=base;hi+=base;
      if(location)expr=r.bytes(r.uint(2));
    }else{
      const op=r.u8();if(!op)return location?fallback:false;
      const addr=()=>indexedAddress(d,cu,r.leb());
      if(op===1){base=addr();continue;}
      if(op===(location?6:5)){base=r.uint(4);continue;}
      if(location&&op===5){fallback=r.bytes(r.leb());continue;}
      if(op===2){lo=addr();hi=addr();}
      else if(op===3){lo=addr();hi=lo+r.leb();}
      else if(op===4){lo=base+r.leb();hi=base+r.leb();}
      else if(op===(location?7:6)){lo=r.uint(4);hi=r.uint(4);}
      else if(op===(location?8:7)){lo=r.uint(4);hi=lo+r.leb();}
      else throw new Error('不支持 DWARF list opcode '+op);
      if(location)expr=r.bytes(r.leb());
    }
    if(lo>hi||hi>0x100000000)throw new Error('DWARF 地址范围无效');
    if(pc>=lo&&pc<hi)return location?expr:true;
  }
  throw new Error('DWARF list 过长');
}
export function inScope(d,rec,pc){
  const range=d.attr(rec,A.ranges);if(range)return rangesOrLocation(d,rec,range,pc,false);
  const lo=d.num(rec,A.low),high=d.attr(rec,A.high);
  if(lo==null||!high)return null;
  const end=addressForms.has(high.form)?high.value:lo+high.value;
  return pc>=lo&&pc<end;
}
function locationAt(d,rec,at,pc){
  const a=d.attr(d.merged(rec),at);if(!a)return null;
  if(a.value instanceof Uint8Array)return a.value;
  return rangesOrLocation(d,rec,a,pc,true);
}
export async function evaluateLocation(bytes,ctx,{frameBase=false}={}){
  const r=new Reader(bytes),stack=[];let direct=false,steps=0;
  const push=v=>{if(!Number.isInteger(v)||v<0||v>0xffffffff)throw new Error('位置表达式地址溢出');stack.push(v);if(stack.length>64)throw new Error('位置栈过深');};
  const pop=()=>{if(!stack.length)throw new Error('位置栈为空');return stack.pop();};
  const reg=n=>{if(n>(ctx.maxRegister??15)||!ctx.known?.has(n))throw new Error(`该帧寄存器 ${ctx.registerPrefix??'R'}${n} 不可恢复`);return ctx.regs[n];};
  while(r.p<r.end){
    if(stack.length>64)throw new Error('位置栈过深');
    if(++steps>256)throw new Error('位置表达式过长');const op=r.u8();
    if(op>=0x30&&op<=0x4f){push(op-0x30);continue;}
    if(op>=0x50&&op<=0x6f){if(stack.length||r.p!==r.end)throw new Error('暂不支持分片寄存器位置');push(reg(op-0x50));direct=true;continue;}
    if(op>=0x70&&op<=0x8f){push(reg(op-0x70)+r.leb(true));continue;}
    switch(op){
      case 3:push(r.uint(4));break;
      case 6:{const a=pop(),b=await ctx.read(a,4);if(b.length!==4)throw new Error('局部变量解引用短读');push(new Reader(b).uint(4));break;}
      case 8:case 10:case 12:push(r.uint(2**((op-8)/2)));break;
      case 9:case 11:case 13:{const n=2**((op-9)/2),v=r.uint(n),sign=2**(n*8-1);stack.push(v>=sign?v-2**(n*8):v);break;}
      case 0x10:push(r.leb());break;
      case 0x11:stack.push(r.leb(true));break;
      case 0x12:{const v=pop();push(v);push(v);break;}
      case 0x13:pop();break;
      case 0x1c:{const b=pop(),a=pop();push(a-b);break;}
      case 0x22:{const b=pop(),a=pop();push(a+b);break;}
      case 0x23:push(pop()+r.leb());break;
      case 0x90:push(reg(r.leb()));direct=true;if(r.p!==r.end)throw new Error('暂不支持寄存器分片');break;
      case 0x91:if(ctx.base==null)throw new Error('帧基址不可用');push(ctx.base+r.leb(true));break;
      case 0x92:{const n=r.leb();push(reg(n)+r.leb(true));break;}
      case 0x9c:if(ctx.cfa==null)throw new Error('CFA 不可用');push(ctx.cfa);break;
      case 0x9f:direct=true;if(r.p!==r.end)throw new Error('stack_value 后仍有指令');break;
      default:throw new Error('位置暂不可用（DW_OP 0x'+op.toString(16)+'，可能经过优化）');
    }
  }
  if(stack.length!==1)throw new Error('位置表达式结果无效');
  const value=stack[0];if(value<0||value>0xffffffff)throw new Error('位置表达式结果溢出');
  return {value,direct:direct&&!frameBase};
}
function functions(d){
  if(indexes.has(d))return indexes.get(d);
  d.index();const list=d._arr.filter(r=>r.tag===0x2e||r.tag===0x1d);indexes.set(d,list);return list;
}
export async function frameLocals(session,frame,{signal}={}){
  if(!session.halted)throw new Error('先暂停目标');
  if(!frame?.regs||frame.kind==='candidate')throw new Error('该帧没有可靠寄存器上下文');
  const d=session.sym?.dwarf;if(!d)return {rows:[],reason:'ELF 缺少 DWARF 调试信息'};
  const cancel=()=>{if(signal?.())throw Object.assign(new Error('局部变量读取已中断'),{cancelled:true});};
  let budget=8192;
  const read=async(a,n)=>{cancel();if(!Number.isInteger(a)||a<0||a+n>0x100000000||n>512||n<1||(budget-=n)<0)throw new Error('局部变量读取超出限额');const b=await session.memRead(a,n);if(b.length!==n)throw new Error('局部变量短读');return b;};
  const riscv=session.arch?.name==='riscv';
  const ctx={regs:frame.regs,known:new Set(frame.known),cfa:frame.cfa,read,maxRegister:riscv?32:15,registerPrefix:riscv?'x':'R'};
  const matches=functions(d).filter(r=>inScope(d,r,frame.lookup)===true);
  const fn=matches.filter(r=>r.tag===0x2e).sort((a,b)=>b.depth-a.depth)[0];
  if(!fn)return {rows:[],reason:'当前帧没有匹配的函数调试信息'};
  let baseReason='';
  try{const expr=locationAt(d,fn,A.base,frame.lookup);if(expr)ctx.base=(await evaluateLocation(expr,ctx,{frameBase:true})).value;}catch(e){baseReason=e.message;}
  const rows=[];
  const walk=async(rec,depth)=>{
    if(depth>64)throw new Error('DWARF 作用域过深');
    for(const child of d.childrenOf(rec)){
      cancel();if(rows.length>=128)return;
      if(child.tag===0x0b||child.tag===0x1d){const active=inScope(d,child,frame.lookup);if(active===true||(active===null&&child.tag===0x0b))await walk(child,depth+1);continue;}
      if(child.tag!==5&&child.tag!==0x34)continue;
      const merged=d.merged(child),name=d.name(merged)||'(匿名)',type=d.type(d.num(merged,A.type));
      const row={name,argument:child.tag===5,type,scope:rec.offset};rows.push(row);
      try{
        const size=type.size;if(!Number.isInteger(size)||size<1||size>512)throw new Error('类型大小不可用或超过512字节');
        const constant=d.attr(merged,A.constant);let bytes;
        if(constant){
          if(constant.value instanceof Uint8Array)bytes=constant.value;
          else if(typeof constant.value==='number'&&size<=4){bytes=new Uint8Array(size);let v=constant.value;for(let i=0;i<size;i++){bytes[i]=v&255;v=Math.floor(v/256);}}
          else throw new Error('不支持该常量类型');
        }else{
          const expr=locationAt(d,child,A.location,frame.lookup);if(!expr)throw new Error('已优化掉或没有位置描述');
          const result=await evaluateLocation(expr,ctx);
          if(result.direct){if(size>4)throw new Error('暂不支持多寄存器/分片值');bytes=new Uint8Array(size);for(let i=0;i<size;i++)bytes[i]=(result.value>>>8*i)&255;}
          else{row.address=result.value;bytes=await read(result.value,size);}
        }
        if(bytes.length<size)throw new Error('局部变量数据不完整');
        row.value=formatWatchValue({size,scalar:type.scalar||(type.kind==='pointer'?'u32':null)},bytes).text;
        if(['struct','union','array'].includes(type.kind)){row.value=summarizeTree({type},bytes);row.children=treeRows({type},bytes);}
      }catch(e){if(e.cancelled)throw e;row.error=e.message+(e.message==='帧基址不可用'&&baseReason?'：'+baseReason:'');}
    }
  };
  await walk(fn,0);
  return {rows,reason:rows.length>=128?'只显示前128项':rows.length?'':'当前作用域没有局部变量/参数',function:d.name(d.merged(fn))};
}
