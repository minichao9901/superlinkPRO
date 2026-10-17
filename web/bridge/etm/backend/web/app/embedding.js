/** Standalone stays independent; an embedded page shares the parent's USB owner. */
export function embeddedHost(windowObject=globalThis.window){
  try{
    // 内嵌标记可以来自 serve-etm.py 注入的 data-embedded，也可以来自 ?embedded=1
    // （线上静态副本 web/etm/index.html 用查询参数，保证同一份页面源码两种部署都认）。
    const queryEmbedded=new URLSearchParams(windowObject?.location?.search||'').get('embedded')==='1';
    const embedded=!!windowObject?.document?.body?.dataset?.embedded||queryEmbedded;
    if(windowObject?.parent===windowObject||!embedded)return null;
    const host=windowObject.parent.__tools?.etm;
    if(!host)throw Error('ETM 内嵌页面需要主工具的探针管理器');
    return host;
  }catch(e){throw new Error('ETM 内嵌页面与主工具必须同源（线上页面请用 web/etm 副本；详见 web/docs/etm-online-bridge.md）',{cause:e});}
}

export const PROBE_ACTIONS=new Set(['connect','disconnect','recover-debug','reset','halt','run',
  'read-config','read-clock','apply-config','restore-config','run-to','remove-break']);

export function runEmbedded(host,fn){return host?host.run(fn):fn();}
