/** Read-only firmware inspection. Addresses remain BigInt for ELF64 accuracy. */
import {parseFirmware} from './image.js';
export const hex = value => '0x'+BigInt(value).toString(16).toUpperCase().padStart(8,'0');
const enumName=(map,value)=>map[value]||String(value);
const sectionTypes={0:'NULL',1:'PROGBITS',2:'SYMTAB',3:'STRTAB',4:'RELA',6:'DYNAMIC',7:'NOTE',8:'NOBITS',9:'REL',11:'DYNSYM',14:'INIT_ARRAY',15:'FINI_ARRAY',16:'PREINIT_ARRAY',17:'GROUP',18:'SYMTAB_SHNDX',0x70000001:'ARM_EXIDX',0x70000003:'ARM_ATTRIBUTES'};
const programTypes={0:'NULL',1:'LOAD',2:'DYNAMIC',3:'INTERP',4:'NOTE',6:'PHDR',7:'TLS',0x6474e551:'GNU_STACK',0x6474e552:'GNU_RELRO',0x70000001:'ARM_EXIDX'};
export function inspectElf(input){
  const b=input instanceof Uint8Array?input:new Uint8Array(input),d=new DataView(b.buffer,b.byteOffset,b.byteLength);
  const range=(off,size,label)=>{if(!Number.isSafeInteger(off)||!Number.isSafeInteger(size)||off<0||size<0||off>b.length||size>b.length-off)throw Error(`ELF ${label}越界或被截断`);};
  range(0,16,'标识');
  if(b[0]!==127||b[1]!==69||b[2]!==76||b[3]!==70)throw Error('不是 ELF 文件');
  if(![1,2].includes(b[4])||![1,2].includes(b[5])||b[6]!==1)throw Error('ELF class / 端序 / 版本无效');
  const wide=b[4]===2,le=b[5]===1,headerSize=wide?64:52;range(0,headerSize,'文件头');
  const u16=o=>d.getUint16(o,le),u32=o=>d.getUint32(o,le),word=o=>wide?d.getBigUint64(o,le):BigInt(u32(o));
  const number=(v,label)=>{if(v>BigInt(Number.MAX_SAFE_INTEGER))throw Error(`ELF ${label}超过可寻址文件范围`);return Number(v);};
  const phoff=number(word(wide?32:28),'程序头偏移'),shoff=number(word(wide?40:32),'节表偏移');
  const phent=u16(wide?54:42),shent=u16(wide?58:46);
  let phnum=u16(wide?56:44),shnum=u16(wide?60:48),shstr=u16(wide?62:50);
  const table=(off,count,ent,min,label)=>{if(count&&(!off||ent<min))throw Error(`ELF ${label}入口尺寸无效`);range(off,count*ent,label);};
  // Extended numbering is stored in section zero, not a literal 0/65535 count.
  if(shoff&&(shnum===0||phnum===65535||shstr===65535)){
    table(shoff,1,shent,wide?64:40,'扩展节表');
    if(shnum===0)shnum=number(word(shoff+(wide?32:20)),'节数');
    if(phnum===65535)phnum=u32(shoff+(wide?44:28));
    if(shstr===65535)shstr=u32(shoff+(wide?40:24));
  }
  if(phnum===65535||shstr===65535)throw Error('ELF 扩展编号缺少节表');
  table(phoff,phnum,phent,wide?56:32,'程序头表');table(shoff,shnum,shent,wide?64:40,'节表');
  if(shnum>100000||phnum>100000)throw Error('ELF 表项超过查看器上限 100000');
  if(shstr!==0&&shstr>=shnum)throw Error('ELF 节名表索引越界');
  const sections=[];
  for(let i=0;i<shnum;i++){
    const o=shoff+i*shent,type=u32(o+4),off=number(word(o+(wide?24:16)),'节偏移'),size=number(word(o+(wide?32:20)),'节大小');
    if(type!==0&&type!==8)range(off,size,'节数据');
    sections.push({index:i,nameOff:u32(o),type,flags:word(o+8),addr:word(o+(wide?16:12)),off,size,link:u32(o+(wide?40:24)),info:u32(o+(wide?44:28)),align:word(o+(wide?48:32)),entsize:number(word(o+(wide?56:36)),'节入口尺寸')});
  }
  const string=(s,off)=>{if(!s||s.type!==3||off>=s.size)throw Error('ELF 字符串表引用无效');let end=s.off+off;while(end<s.off+s.size&&b[end])end++;if(end===s.off+s.size)throw Error('ELF 字符串未终止');return new TextDecoder().decode(b.subarray(s.off+off,end));};
  for(const s of sections)s.name=shstr?string(sections[shstr],s.nameOff):'';
  const programs=[];
  for(let i=0;i<phnum;i++){
    const o=phoff+i*phent,p={index:i,type:u32(o),flags:u32(o+(wide?4:24)),off:word(o+(wide?8:4)),vaddr:word(o+(wide?16:8)),paddr:word(o+(wide?24:12)),filesz:word(o+(wide?32:16)),memsz:word(o+(wide?40:20)),align:word(o+(wide?48:28))};
    range(number(p.off,'段偏移'),number(p.filesz,'段文件尺寸'),'段数据');programs.push(p);
  }
  const symbolTables=sections.filter(s=>s.type===2||s.type===11);let symbolCount=0;
  for(const s of symbolTables){if(s.entsize<(wide?24:16)||s.size%s.entsize)throw Error('ELF 符号表入口尺寸无效');if(sections[s.link]?.type!==3)throw Error('ELF 符号字符串表无效');symbolCount+=s.size/s.entsize;}
  // Only one page of symbol objects is retained; stripped files are valid.
  function symbols(search='',offset=0,limit=100){
    const rows=[];let total=0;const query=search.toLowerCase();
    for(const s of symbolTables)for(let i=0;i<s.size/s.entsize;i++){
      const o=s.off+i*s.entsize,name=string(sections[s.link],u32(o)),value=word(o+(wide?8:4)),size=word(o+(wide?16:8)),info=b[o+(wide?4:12)],other=b[o+(wide?5:13)],ndx=u16(o+(wide?6:14));
      if(query&&!`${name} ${hex(value)}`.toLowerCase().includes(query))continue;
      if(total++<offset||rows.length>=limit)continue;
      rows.push([s.name,i,name||'—',hex(value),size.toString(),enumName({0:'NOTYPE',1:'OBJECT',2:'FUNC',3:'SECTION',4:'FILE',6:'TLS'},info&15),enumName({0:'LOCAL',1:'GLOBAL',2:'WEAK'},info>>4),enumName({0:'DEFAULT',1:'INTERNAL',2:'HIDDEN',3:'PROTECTED'},other&3),ndx===0?'UND':ndx===65521?'ABS':ndx===65522?'COMMON':ndx===65535?'XINDEX':String(ndx)]);
    }
    return {rows,total};
  }
  return {sections,programs,symbolCount,symbols,header:[['格式',`ELF${wide?64:32} · ${le?'Little endian':'Big endian'}`],['类型',enumName({1:'REL',2:'EXEC',3:'DYN',4:'CORE'},u16(16))],['机器',enumName({3:'i386',40:'ARM',62:'x86-64',183:'AArch64',243:'RISC-V'},u16(18))],['OS ABI / ABI 版本',`${b[7]} / ${b[8]}`],['入口',hex(word(24))],['标志',hex(u32(wide?48:36))],['文件头尺寸',u16(wide?52:40)],['程序头偏移 / 数量 / 入口尺寸',`${hex(phoff)} / ${phnum} / ${phent}`],['节表偏移 / 数量 / 入口尺寸',`${hex(shoff)} / ${shnum} / ${shent}`],['节名表索引',shstr],['符号数',symbolCount]]};
}
export function elfTable(elf,view,search='',offset=0,limit=100){
  if(view==='symbols')return {headers:['表','序号','名称','值 / VMA','大小','类型','绑定','可见性','节索引'],...elf.symbols(search,offset,limit)};
  let headers,rows;
  if(view==='programs'){
    headers=['序号','类型','文件偏移','VMA','物理地址 / LMA','文件大小','内存大小','权限','对齐'];
    rows=elf.programs.map(p=>[p.index,enumName(programTypes,p.type),hex(p.off),hex(p.vaddr),hex(p.paddr),p.filesz.toString(),p.memsz.toString(),`${p.flags&4?'R':'-'}${p.flags&2?'W':'-'}${p.flags&1?'X':'-'}`,hex(p.align)]);
  }else if(view==='sections'){
    headers=['序号','名称','类型','VMA','文件偏移','大小','标志','Link','Info','对齐','入口尺寸'];
    rows=elf.sections.map(s=>[s.index,s.name||'—',enumName(sectionTypes,s.type),hex(s.addr),hex(s.off),s.size,hex(s.flags),s.link,s.info,hex(s.align),s.entsize]);
  }else{headers=['字段','值'];rows=elf.header;}
  if(search)rows=rows.filter(r=>r.join(' ').toLowerCase().includes(search.toLowerCase()));
  return {headers,total:rows.length,rows:rows.slice(offset,offset+limit)};
}
export function firmwareRegions(name,bytes,base){
  if(/\.(elf|axf|out|hex|ihex)$/i.test(name))return parseFirmware(name,bytes,0);
  const address=Number(base);
  if(!Number.isSafeInteger(address)||address<=0||address>0xffffffff)throw Error('BIN 烧录基地址须为有效的非零 32 位地址；原文件字节无需地址');
  if(address+bytes.length>0x100000000)throw Error('BIN 地址范围超过 32 位');
  return parseFirmware(name,bytes,address);
}
export function hexPage(bytes,base=0,offset=0,length=512){
  const lines=[];
  for(let i=offset;i<Math.min(bytes.length,offset+length);i+=16){const row=bytes.subarray(i,Math.min(i+16,bytes.length));lines.push(`${hex(BigInt(base)+BigInt(i))}  ${Array.from(row,b=>b.toString(16).padStart(2,'0').toUpperCase()).join(' ').padEnd(47)}  |${Array.from(row,b=>b>=32&&b<=126?String.fromCharCode(b):'.').join('')}|`);}
  return lines.join('\n');
}
