/** Local packages stay same-origin; Pages may use the matching GitHub Release. */
export function downloadHref(info,base){
  const local=new URL(info.file,new URL('downloads/',base));
  if(!info.downloadUrl){
    if(/^[0-9a-f]{64}$/.test(info.sha256||''))local.searchParams.set('sha256',info.sha256);
    return local.href;
  }
  const expected=`https://github.com/minichao9901/superlinkPRO/releases/download/workbench-v${info.version}/${info.file}`;
  if(info.downloadUrl!==expected||!/^\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?$/.test(info.version))throw Error('下载地址与发布版本不匹配');
  return expected;
}
export class DownloadView {
  constructor(){ this.info=null; this.error=null; }
  init(){ document.getElementById('download-refresh').onclick=()=>this.refresh();this.refresh(); }
  async onShow(){ if(!this.info)await this.refresh(); }
  async refresh(){
    if(this.loading)return this.loading;
    this.loading=this._refresh().finally(()=>{this.loading=null;});
    return this.loading;
  }
  async _refresh(){
    const $=id=>document.getElementById(id),link=$('download-exe');
    try{
      const response=await fetch('downloads/manifest.json',{cache:'no-store'});
      if(!response.ok)throw Error('下载清单暂不可用');
      const info=await response.json();
      if(!info.version||!/^superlinkPRO-win64\.zip$/.test(info.file)||!Number.isSafeInteger(info.bytes)||info.bytes<=0||!/^[0-9a-f]{64}$/.test(info.sha256))throw Error('完整包尚未生成，请先在本机构建工作台');
      link.href=downloadHref(info,location.href);link.removeAttribute('aria-disabled');link.removeAttribute('tabindex');
      $('download-version').textContent='v'+info.version;
      $('download-integrity').textContent=`${(info.bytes/1048576).toFixed(1)} MiB · SHA-256 ${info.sha256}`;
      this.info=info;this.error=null;return info;
    }catch(error){
      this.info=null;this.error=error.message;
      link.removeAttribute('href');link.setAttribute('aria-disabled','true');link.setAttribute('tabindex','-1');
      $('download-version').textContent='本地构建';$('download-integrity').textContent=this.error;
      return null;
    }
  }
  summary(){return {version:this.info?.version||null,bytes:this.info?.bytes||null,sha256:this.info?.sha256||null,available:!!this.info,error:this.error};}
}
