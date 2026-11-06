import {inspectElf,elfTable,firmwareRegions,hexPage,hex} from './inspect.js';
const $=id=>document.getElementById(id);
const node=(tag,text)=>{const e=document.createElement(tag);e.textContent=String(text);return e;};
export class FirmwareInspector {
  constructor(){this.bytes=null;this.elf=null;this.regions=[];this.byteOffset=0;this.elfOffset=0;}
  init(){
    for(const name of ['log','bytes','elf'])$('f-tab-'+name).onclick=()=>this.show(name);
    const tabs=['log','bytes','elf'];
    for(const name of tabs)$('f-tab-'+name).onkeydown=e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const i=tabs.indexOf(name),next=e.key==='Home'?0:e.key==='End'?2:(i+(e.key==='ArrowRight'?1:2))%3;this.show(tabs[next]);$('f-tab-'+tabs[next]).focus();};
    $('f-byte-source').onchange=()=>{this.byteOffset=0;this.byteSources();this.renderBytes();};
    $('f-byte-region').onchange=()=>{this.byteOffset=0;this.renderBytes();};
    $('f-byte-prev').onclick=()=>{this.byteOffset=Math.max(0,this.byteOffset-512);this.renderBytes();};
    $('f-byte-next').onclick=()=>{this.byteOffset+=512;this.renderBytes();};
    $('f-byte-go').onclick=()=>this.jump();$('f-byte-address').onkeydown=e=>{if(e.key==='Enter')this.jump();};
    $('f-elf-view').onchange=()=>{this.elfOffset=0;this.renderElf();};
    $('f-elf-search').oninput=()=>{clearTimeout(this.searchTimer);this.searchTimer=setTimeout(()=>{this.elfOffset=0;this.renderElf();},150);};
    $('f-elf-prev').onclick=()=>{this.elfOffset=Math.max(0,this.elfOffset-100);this.renderElf();};
    $('f-elf-next').onclick=()=>{this.elfOffset+=100;this.renderElf();};
    $('f-base').addEventListener('input',()=>this.refreshRegions());
    this.show('log');this.renderBytes();this.renderElf();
  }
  show(name){
    for(const n of ['log','bytes','elf']){const selected=n===name;$('f-tab-'+n).setAttribute('aria-selected',String(selected));$('f-tab-'+n).tabIndex=selected?0:-1;$(n==='log'?'f-log':'f-pane-'+n).hidden=!selected;}
    $('f-logclear').hidden=name!=='log';
  }
  load(name,buffer){
    this.name=name;this.bytes=new Uint8Array(buffer);this.elf=null;this.elfError='';this.byteOffset=0;this.elfOffset=0;
    $('f-elf-search').value='';$('f-elf-view').value='header';$('f-byte-source').value='image';
    const isElf=this.bytes[0]===127&&this.bytes[1]===69&&this.bytes[2]===76&&this.bytes[3]===70;
    if(isElf||/\.(elf|axf|out)$/i.test(name)){try{this.elf=inspectElf(this.bytes);}catch(e){this.elfError=e.message;}}
    this.refreshRegions();this.renderElf();this.show('bytes');
  }
  clear(){
    this.name='';this.bytes=null;this.elf=null;this.elfError='';this.regions=[];this.byteOffset=0;this.elfOffset=0;
    this.refreshRegions();this.renderElf();this.show('log');
  }
  refreshRegions(){
    this.regions=[];this.imageError='';this.byteOffset=0;
    if(this.bytes)try{this.regions=firmwareRegions(this.name,this.bytes,$('f-base').value);}catch(e){this.imageError=e.message;}
    this.byteSources();this.renderBytes();
  }
  byteSources(){
    const raw=$('f-byte-source').value==='raw';this.sources=raw&&this.bytes?[{addr:0,data:this.bytes}]:this.regions;
    const select=$('f-byte-region');select.replaceChildren();
    this.sources.forEach((r,i)=>{const o=node('option',raw?`原文件 · ${r.data.length} B`:`${hex(r.addr)}–${hex(r.addr+Math.max(0,r.data.length-1))} · ${r.data.length} B`);o.value=String(i);select.append(o);});
    select.disabled=!this.sources.length;$('f-byte-address').placeholder=raw?'文件偏移，如 0x100':'烧录地址，如 0x08000000';
  }
  renderBytes(){
    const raw=$('f-byte-source').value==='raw',r=this.sources?.[Number($('f-byte-region').value)||0];
    if(r)this.byteOffset=Math.max(0,Math.min(this.byteOffset,Math.floor(Math.max(0,r.data.length-1)/512)*512));
    $('f-byte-content').textContent=r?hexPage(r.data,r.addr,this.byteOffset):'';
    $('f-byte-info').textContent=!this.bytes?'导入 BIN、HEX 或 ELF 后查看；仅解析本地文件。':raw?`${this.name} · 原文件字节，地址列为文件偏移。`:this.imageError?`${this.name} · 无法生成烧录映像：${this.imageError}。可切换原文件查看。`:`${this.name} · ${this.regions.length} 个烧录区段；按现有烧录解析器生成。区段内小空隙可能填充 FF，NOBITS 不含文件字节；不表示已写入板卡。`;
    $('f-byte-page').textContent=r?`${this.byteOffset+1}–${Math.min(r.data.length,this.byteOffset+512)} / ${r.data.length} B`:'—';
    $('f-byte-prev').disabled=!r||this.byteOffset===0;$('f-byte-next').disabled=!r||this.byteOffset+512>=r.data.length;$('f-byte-go').disabled=!r;
  }
  jump(){
    const text=$('f-byte-address').value.trim(),address=Number(text);
    if(!text||!Number.isSafeInteger(address)||address<0){$('f-byte-page').textContent='请输入有效地址或偏移';return;}
    const i=(this.sources||[]).findIndex(r=>address>=r.addr&&address<r.addr+r.data.length);
    if(i<0){$('f-byte-page').textContent='该地址没有文件数据（区段间空洞不补造）';return;}
    $('f-byte-region').value=String(i);this.byteOffset=Math.floor((address-this.sources[i].addr)/512)*512;this.renderBytes();
  }
  renderElf(){
    const box=$('f-elf-content');box.replaceChildren();
    $('f-elf-note').textContent=this.elf?`${this.name} · 只读 ELF 结构；支持 ELF32/64、大小端。VMA 为运行地址，文件偏移不是烧录地址；NOBITS 无文件数据。此视图覆盖 readelf 常用的 -h/-l/-S/-s，不展开 DWARF、重定位或动态表。`:this.elfError?`ELF 解析失败：${this.elfError}`:'导入 ELF / AXF / OUT 后查看文件头、程序头、节表和符号表。BIN / HEX 不包含这些结构。';
    for(const id of ['f-elf-view','f-elf-search'])$(id).disabled=!this.elf;
    let result={total:0,rows:[]};
    if(this.elf)try{
      result=elfTable(this.elf,$('f-elf-view').value,$('f-elf-search').value,this.elfOffset);
      const table=document.createElement('table'),head=document.createElement('thead'),tr=document.createElement('tr'),body=document.createElement('tbody');
      for(const h of result.headers)tr.append(node('th',h));head.append(tr);
      for(const row of result.rows){const tr=document.createElement('tr');for(const value of row)tr.append(node('td',value));body.append(tr);}
      table.append(head,body);box.append(table);if(!result.total)box.append(node('p','没有匹配项（无符号表的 ELF 也可正常查看其他结构）。'));
    }catch(e){box.append(node('p','解析失败：'+e.message));}
    $('f-elf-page').textContent=result.total?`${this.elfOffset+1}–${this.elfOffset+result.rows.length} / ${result.total}`:'0 项';
    $('f-elf-prev').disabled=!this.elf||this.elfOffset===0;$('f-elf-next').disabled=this.elfOffset+100>=result.total;
  }
}
