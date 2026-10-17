/**
 * 极简 CDP 客户端 —— **真页面套件共用**（2026-10 从 `tmp/h743-jscope.mjs` 抽出来，
 * 同月从 `tmp/` 搬到这里：`tools/selftest/` 下的套件必须能在**新克隆的仓库**里跑，
 * 而 `tmp/` 在 .gitignore 里）。
 *
 *   import { Cdp, sleep } from './cdp-lib.mjs';
 *   const cdp = new Cdp(); await cdp.connect();
 *
 * 要点（都是踩过的）：
 *   · 浏览器级 target 也要连 —— WebUSB 的设备选择框（DeviceAccess）只在浏览器级会话里；
 *   · `json()` 里必须 **先 await 再 stringify**，直接 JSON.stringify(promise) 得到 "{}"；
 *   · 每次 CDP 调用都带超时，卡住就抛，不要死等。
 *
 * 兼容：`tmp/cdp-lib.mjs` 现在只是一行 `export *` 转发（tmp 下几十个脚手架脚本还在用它）。
 */
import { setTimeout as _st } from 'node:timers/promises';

export const sleep = ms => _st(ms);
export const DEV_RE = /akaLink|DAP|CMSIS|MicroLink/i;

export class Cdp {
  constructor(base = 'http://127.0.0.1:9333', callTimeout = 180000){
    this.base = base; this.callTimeout = callTimeout;
    this.seq = 0; this.pending = new Map(); this.prompts = [];
  }
  _open(url, onMsg){
    const ws = new WebSocket(url);
    return new Promise((res, rej) => {
      ws.onopen = () => res(ws);
      ws.onerror = () => rej(new Error('CDP WebSocket 连不上：' + url));
      ws.onmessage = e => onMsg(ws, JSON.parse(e.data));
    });
  }
  _dispatch(m){
    if (m.id && this.pending.has(m.id)){
      const p = this.pending.get(m.id); this.pending.delete(m.id);
      clearTimeout(p.timer);
      m.error ? p.rej(new Error(m.error.message)) : p.res(m.result);
      return;
    }
    if (m.method === 'DeviceAccess.deviceRequestPrompted') this.prompts.push(m.params);
    /** 诊断钩子：想收集 console/异常事件就设 `cdp.onEvent = m => {...}` */
    try { this.onEvent?.(m); } catch {}
  }
  async connect(){
    const ver = await (await fetch(this.base + '/json/version')).json();
    this.browserWs = await this._open(ver.webSocketDebuggerUrl, (ws, m) => this._dispatch(m));
    const list = await (await fetch(this.base + '/json/list')).json();
    const page = list.find(t => t.type === 'page' && t.url.includes('index.html')) || list.find(t => t.type === 'page');
    if (!page) throw new Error('没有可用的页面目标');
    this.ws = await this._open(page.webSocketDebuggerUrl, (ws, m) => this._dispatch(m));
    await this.send('Page.enable');
    await this.send('Runtime.enable');
    try { await this.send('Network.enable'); await this.send('Network.setCacheDisabled', { cacheDisabled: true }); } catch {}
    for (const fn of [() => this.sendBrowser('DeviceAccess.enable'), () => this.send('DeviceAccess.enable')]){
      try { await fn(); this.deviceAccessOn = true; break; } catch {}
    }
    if (!this.deviceAccessOn) console.log('  [warn] DeviceAccess 不可用（已授权设备仍可直连）');
  }
  send(method, params = {}){ return this._call(this.ws, method, params); }
  sendBrowser(method, params = {}){ return this._call(this.browserWs, method, params); }
  _call(ws, method, params){
    const id = ++this.seq;
    return new Promise((res, rej) => {
      const pending={res,rej,timer:null};
      this.pending.set(id,pending);
      pending.timer=setTimeout(()=>{
        if(this.pending.get(id)!==pending)return;
        this.pending.delete(id);
        rej(new Error(`CDP ${method} 超时`));
      },this.callTimeout);
      try{ws.send(JSON.stringify({id,method,params}));}
      catch(error){
        if(this.pending.get(id)===pending)this.pending.delete(id);
        clearTimeout(pending.timer);
        rej(error);
      }
    });
  }
  async eval(expr, userGesture = false){
    const r = await this.send('Runtime.evaluate', { expression: `(async()=>{ ${expr} })()`, userGesture, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error('页面异常：' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
    return r.result.value;
  }
  async json(expr, userGesture = false){ return JSON.parse(await this.eval(`return JSON.stringify(await (${expr}));`, userGesture)); }
  async pickDevice(match = DEV_RE, timeout = 25000){
    const t0 = Date.now();
    while (!this.prompts.length){
      if (Date.now() - t0 > timeout) throw new Error('等设备选择框超时');
      await sleep(100);
    }
    const p = this.prompts.shift();
    const dev = p.devices.find(d => match.test(d.name)) || p.devices[0];
    if (!dev) throw new Error('选择框里没有设备：' + JSON.stringify(p.devices));
    await this.sendBrowser('DeviceAccess.selectPrompt', { id: p.id, deviceId: dev.id });
    return dev;
  }
  async settle(match, readyExpr, timeout = 25000){
    const t0 = Date.now();
    for (;;){
      if (this.prompts.length) return await this.pickDevice(match);
      let ready = false;
      try { ready = await this.eval(`return !!(${readyExpr});`); } catch {}
      if (ready) return null;
      if (Date.now() - t0 > timeout) throw new Error(`既没弹选择框也没就绪：${readyExpr}`);
      await sleep(200);
    }
  }
  close(){
    for(const [id,p] of this.pending){clearTimeout(p.timer);p.rej(new Error('CDP client closed'));this.pending.delete(id);}
    try { this.ws.close(); } catch {} try { this.browserWs.close(); } catch {}
  }
}

/** 页面里的源码目录要**真文件**：用 CDP 把 <input webkitdirectory> 填上真路径 */
export async function feedSourceDir(cdp, dirPath, inputId = 'd-src-dir'){
  const doc = await cdp.send('DOM.getDocument', { depth: -1 });
  const node = await cdp.send('DOM.querySelector', { nodeId: doc.root.nodeId, selector: '#' + inputId });
  if (!node.nodeId) throw new Error('页面上找不到 #' + inputId);
  const files = await listFiles(dirPath);
  await cdp.send('DOM.setFileInputFiles', { nodeId: node.nodeId, files });
  return files;
}

import { readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
export function listFiles(dir, out = []){
  for (const e of readdirSync(dir, { withFileTypes: true })){
    const p = join(dir, e.name);
    if (e.isDirectory()){ if (!e.name.startsWith('.')) listFiles(p, out); }
    else out.push(p);
  }
  return out;
}
