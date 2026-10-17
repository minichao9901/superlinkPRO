import {WebUsbDapProbe} from '../rtt/dap-webusb.js';
import {store} from '../core/store.js';

export function workbenchUrl(current=location,saved=store.get('gen.etmPort',8878)){
  const local=['127.0.0.1','localhost','[::1]'].includes(current.hostname),port=Number(local&&current.port?current.port:saved);
  return `http://127.0.0.1:${Number.isInteger(port)&&port>=1024&&port<=65535?port:8878}/#etm`;
}

/**
 * ETM 线上页 + 本地桥（跨源）——见 web/docs/etm-online-bridge.md。
 *
 * 三种拓扑都要能跑：
 *   ① 本地同源工作台（serve-etm.py：/ 是网页、/etm/ 是 ETM 页、/etm/api/ 是接口）—— 不能回归；
 *   ② 线上静态页（GitHub Pages 的 web/）+ 本机桥（http://127.0.0.1:<port>/etm/api/）—— 跨源；
 *   ③ 其它静态服务（如 python -m http.server 只服务 web/）+ 本机桥 —— 等价于 ②。
 *
 * 关键约束：**ETM 子页必须与父页同源**（子页要把自己的 bridge 对象反向挂到
 * `window.parent.__tools.etm`，跨源会被浏览器拦）。所以线上拓扑里 iframe 指向
 * `./etm/index.html`（web/etm 生成副本，同源），只有数据请求走本地桥（CORS）。
 */
export const PAGES_ORIGIN = 'https://minichao9901.github.io';
/** integration.json 的候选端口（并发探测，命中即用） */
export const BRIDGE_PORTS = [8878, 8890, 8899];
export const PROBE_TIMEOUT_MS = 3000;

/** 父页白名单：本站 + GitHub Pages（线上页）。 */
export function originAllowedParent(origin, self = location.origin){
  return origin === self || origin === PAGES_ORIGIN;
}
/** 子页白名单：本站 + 本机回环（任意端口）。 */
export function originAllowedChild(origin, self = location.origin){
  return origin === self || /^http:\/\/(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$/.test(origin);
}

/** `127.0.0.1:8899` / `http://127.0.0.1:8899/` / `http://localhost:8878` → origin；非法返回 ''。 */
export function normalizeBridge(value){
  const text = String(value ?? '').trim();
  if (!text) return '';
  try {
    const url = new URL(/^[a-z]+:\/\//i.test(text) ? text : `http://${text}`);
    if (!['http:', 'https:'].includes(url.protocol)) return '';
    return url.origin;
  } catch { return ''; }
}

/** 候选桥：输入框/记忆值优先，其次本机 8878/8890/8899；同源候选由 probeBridge 额外加在最前。 */
export function bridgeCandidates(saved = ''){
  const list = [];
  const add = value => { const origin = normalizeBridge(value); if (origin && !list.includes(origin)) list.push(origin); };
  add(saved);
  for (const port of BRIDGE_PORTS) add(`http://127.0.0.1:${port}`);
  return list;
}

/**
 * 探测 integration.json。策略（见 web/docs/etm-online-bridge.md）：
 *   ① **同源优先**：本地工作台（serve-etm.py）第一次就中，不去骚扰其它端口（也避免控制台噪声）；
 *   ② 用户显式填了桥地址 → 只探它（换端口自救时不该再扫一遍默认端口）；
 *   ③ 否则**并发**探测 127.0.0.1 的 8878/8890/8899（每个 3 s 超时，命中即用）。
 * 返回 {base, origin, apiBase, info} 或 null。base='' 表示同源命中。
 */
export async function probeBridge({ saved = '', timeoutMs = PROBE_TIMEOUT_MS, fetchImpl = (...a) => fetch(...a), current = location } = {}){
  const one = ({ base, origin }) => new Promise((resolve, reject) => {
    const controller = new AbortController();
    let settled = false;
    const timer = setTimeout(() => { if (!settled){ settled = true; controller.abort(); reject(Error(`${origin || '同源'} 超时`)); } }, timeoutMs);
    fetchImpl(`${base}/etm/integration.json`, { cache: 'no-store', signal: controller.signal }).then(async response => {
      if (!response.ok) throw Error(`HTTP ${response.status}`);
      const info = await response.json();
      if (info?.protocol !== 1) throw Error('integration.json 协议不匹配');
      if (settled) return;
      settled = true; clearTimeout(timer);
      resolve({ base, origin, apiBase: `${base}/etm/api/`, info });
    }).catch(error => {
      if (settled) return;
      settled = true; clearTimeout(timer);
      reject(error);
    });
  });
  const first = await one({ base: '', origin: current.origin }).catch(() => null);
  if (first) return first;
  const explicit = normalizeBridge(saved);
  const origins = explicit ? [explicit] : bridgeCandidates('');
  const list = origins.filter(origin => origin !== current.origin).map(origin => ({ base: origin, origin }));
  try { return await Promise.any(list.map(one)); }
  catch { return null; }
}

/**
 * 选 iframe 地址：
 *   同源命中（本地工作台）→ `/etm/?embedded=1`（由 serve-etm.py 注入 data-api-base）；
 *   跨源桥（Pages/静态页）→ `./etm/index.html?embedded=1&api=<桥>/etm/api/`（同源副本 + 跨源数据）。
 */
export function pickIframeSrc(result){
  if (!result) return null;
  if (!result.base) return '/etm/?embedded=1';
  return `./etm/index.html?embedded=1&api=${encodeURIComponent(result.apiBase)}`;
}

/** Same-origin embedded ETM; the parent owns probe arbitration and USB leases. */
export class EtmView {
  constructor(){
    this.bridge=null;this.loading=null;this.started=false;this.probeType=WebUsbDapProbe;
    this.bridgeBase=null;this.apiBase=null;this.bridgeInfo=null;this.candidates=[];
  }
  get probe(){return this.bridge?.probe()||null;}
  get busy(){return !!this.bridge?.busy();}
  init(){
    this.frame=document.getElementById('etm-frame');this.notice=document.getElementById('etm-notice');
    document.getElementById('etm-retry').onclick=()=>this.retry();
    const input=document.getElementById('etm-bridge');
    if(input){
      input.value=String(store.get('etm.bridge','')||'');
      input.onchange=()=>{const value=normalizeBridge(input.value);input.value=value;store.set('etm.bridge',value);};
      input.onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();this.retry();}};
    }
    window.addEventListener('pagehide',()=>{if(this.bridge)this.childUnloaded(this.bridge);});
    this.updateOpenLink();
    this.frame.addEventListener('load',()=>{
      if(!this.started)return;
      if(!this.childReady())this.showError('ETM 页面未就绪；请点击重新检测。旧连接会先安全释放');
    });
  }
  bridgeInput(){return normalizeBridge(document.getElementById('etm-bridge')?.value||store.get('etm.bridge',''));}
  updateOpenLink(){
    const open=document.getElementById('etm-open');if(!open)return;
    const base=this.bridgeBase||this.bridgeInput();
    open.href=base?`${base}/#etm`:workbenchUrl();
  }
  async onShow(){
    this.updateOpenLink();
    if(this.bridge){if(!this.childReady())this.showError('ETM 子页面已失联；请点击重新检测，先释放旧连接');return;}
    if(this.loading)return this.loading;
    this.loading=this.load().finally(()=>this.loading=null);return this.loading;
  }
  async load(){
    const status=document.getElementById('etm-status');
    status.textContent='正在探测本地 ETM 服务（同源 + 127.0.0.1 的 8878/8890/8899）…';
    let result=null;
    try{ result=await probeBridge({saved:this.bridgeInput()}); }
    catch(e){ status.textContent=e.message||String(e); }
    if(!result){
      this.candidates=bridgeCandidates(this.bridgeInput());
      this.notice.hidden=false;this.frame.hidden=true;this.started=false;
      status.textContent='没有找到本地 ETM 服务（桥）。请先启动桥（默认 127.0.0.1:8899），'+
        '或在下面的「桥地址」里填地址后点「重新检测」。候选：'+
        ['同源 /etm/',...this.candidates].join(' · ');
      return false;
    }
    this.bridgeBase=result.base;this.apiBase=result.apiBase;this.bridgeInfo=result.info;
    if(result.base)store.set('etm.bridge',result.base);
    const sameOrigin=!result.base;
    status.textContent=sameOrigin?'已连接本地工作台（同源），正在载入 ETM…':`已找到本地桥 ${result.base}（跨源），正在载入 ETM…`;
    // 跨源时先确认线上副本确实发布了：否则 iframe 会白屏，只在控制台留下 404。
    if(!sameOrigin){
      const ok=await this.copyAvailable();
      if(!ok){
        this.notice.hidden=false;this.frame.hidden=true;this.started=false;
        status.textContent='本地桥已找到，但本页没有 ETM 页面副本（./etm/index.html 404）。'+
          '请确认线上部署的是合并后的 web/（含 web/etm/），或改用本地工作台打开。';
        return false;
      }
    }
    // 顺带确认 API 真能跨源访问（CORS/预检有问题时这里就会失败，比让子页报错更早）。
    const apiCheck=await this.verifyApi(result.apiBase);
    if(!apiCheck.ok)status.textContent+=`（注意：${result.apiBase}status 未通过：${apiCheck.error||apiCheck.status}）`;
    this.started=true;this.frame.hidden=false;this.frame.src=pickIframeSrc(result);
    status.textContent+=' 正在载入 ETM 执行轨迹…';
    this.updateOpenLink();
    return true;
  }
  async copyAvailable(){
    try{
      const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),PROBE_TIMEOUT_MS);
      try{const response=await fetch('./etm/index.html',{cache:'no-store',signal:controller.signal});return response.ok;}
      finally{clearTimeout(timer);}
    }catch{return false;}
  }
  async verifyApi(apiBase){
    try{
      const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),PROBE_TIMEOUT_MS);
      try{
        const response=await fetch(`${apiBase}status`,{cache:'no-store',signal:controller.signal});
        return {ok:response.ok,status:`HTTP ${response.status}`};
      }finally{clearTimeout(timer);}
    }catch(e){return {ok:false,error:e.message||String(e)};}
  }
  attach(bridge,child){
    if(child!==this.frame.contentWindow) throw Error('ETM 页面来源不匹配');
    let childOrigin=null;
    try{ childOrigin=child.location.origin; }catch{ throw Error('ETM 页面来源不匹配（读不到子页 origin）'); }
    if(!originAllowedParent(location.origin)||!originAllowedChild(childOrigin))
      throw Error(`ETM 页面来源不在白名单（父 ${location.origin} / 子 ${childOrigin}）`);
    if(this.bridge&&this.probe){this.showError('旧 ETM 页面仍连接探针；点击重新检测会先停止旧任务并释放连接');throw Error('请先释放原 ETM 探针再重新载入页面');}
    this.bridge=bridge;this.notice.hidden=true;
    child.addEventListener('pagehide',()=>this.childUnloaded(bridge),{once:true});
    return this;
  }
  childReady(){try{return !!this.bridge&&this.frame.contentWindow.__etmBridge===this.bridge;}catch{return false;}}
  async childUnloaded(bridge){
    if(this.bridge!==bridge)return;
    if(this.detaching){await this.detaching.catch(()=>{});if(this.bridge!==bridge)return;}
    this.showError('ETM 子页面已卸载，正在停止旧任务并释放连接…');
    this.detaching=(async()=>{
      if(bridge.busy())await bridge.requestStop();
      const release=()=>this.releaseProbe(bridge);
      if(this.probeManager)await this.run(release);else await release();
      if(this.bridge===bridge){this.bridge=null;this.showError('旧 ETM 连接已释放，请点击重新检测服务');}
    })();
    try{await this.detaching;}catch(e){this.showError('旧页面卸载后的清理未完成：'+e.message+'；保留连接信息，请重新检测或恢复原调试口');}
    finally{this.detaching=null;}
  }
  showError(message){this.notice.hidden=false;document.getElementById('etm-status').textContent=message;}
  async retry(){
    try{
      if(this.detaching)await this.detaching;
      if(this.bridge){
        if(this.busy&&this.bridge.requestStop)await this.bridge.requestStop();
        const release=async()=>{
          // Explicit retry can recover a faulted old realm whose controls no
          // longer exist. Recovery restores trace state and leaves CPU halted.
          if(!this.childReady()&&this.bridge.summary?.()?.transportFault&&this.bridge.recoverForRelease)await this.bridge.recoverForRelease();
          await this.releaseProbe();this.bridge=null;
        };
        if(this.probeManager)await this.probeManager.run('etm',release,{recovery:true,reason:'重新检测并释放旧 ETM 连接'});else await release();
      }
      this.started=false;return this.onShow();
    }catch(e){this.showError(e.message);}
  }
  run(fn){return this.probeManager.run('etm',fn,{reason:'ETM 执行轨迹使用探针'});}
  async releaseProbe(bridge=this.bridge){
    if(bridge?.busy())throw Error('ETM 正在操作目标或采集，请先停止采集');
    if(bridge)await bridge.release();
    if(bridge?.probe())throw Error('ETM 探针仍未确认断开，请保留当前页面并重试');
    if(this.bridge===bridge)this.probeManager?.forget('etm');
  }
  summary(){return {ready:!!this.bridge,connected:!!this.probe,busy:this.busy,bridgeBase:this.bridgeBase,apiBase:this.apiBase,...this.bridge?.summary()};}
}
