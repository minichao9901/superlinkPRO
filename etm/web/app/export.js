/** 导出错误留在页面；成功响应才交给浏览器保存，避免把错误 JSON 当下载。 */
export async function fetchExecutionExport(url,{fetchImpl=globalThis.fetch,timeout=45000}={}){
  const controller=new AbortController();let timer;
  try{return await Promise.race([
    (async()=>{
      const response=await fetchImpl(url,{signal:controller.signal});
      if(!response.ok){
        let detail;try{detail=await response.json();}catch{}
        throw Error(detail?.error||'导出失败（HTTP '+response.status+'），请检查本地服务');
      }
      const type=response.headers.get('Content-Type')||'';
      if(type.includes('application/json')){
        const detail=await response.json();throw Error(detail.error||'服务未返回导出文件，请检查桥包版本');
      }
      const disposition=response.headers.get('Content-Disposition')||'';
      const encoded=disposition.match(/filename\*=UTF-8''([^;]+)/i),plain=disposition.match(/filename="([^"]+)"|filename=([^;]+)/i);
      let filename=plain?.[1]||plain?.[2]||'etm-export';
      if(encoded)try{filename=decodeURIComponent(encoded[1]);}catch{}
      return {blob:await response.blob(),filename};
    })(),
    new Promise((_,reject)=>{timer=setTimeout(()=>{controller.abort();reject(Error('导出等待超时；可先选择较小执行区间再导出'));},timeout);})
  ]);}finally{clearTimeout(timer);}
}

export function saveExportFile({blob,filename}){
  const url=URL.createObjectURL(blob),a=document.createElement('a');
  a.href=url;a.download=filename;document.body.append(a);
  try{a.click();}finally{a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);}
}
