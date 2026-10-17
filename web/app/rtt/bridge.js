/**
 * 本地桥客户端（可选增强，零安装模式完全不需要它）。
 *
 * 为什么需要桥：J-Link 与 OpenOCD 都是**本机程序**，网页无权启动进程、也无权开 TCP。
 * 桥是一个 Node 单文件（bridge/rtt-bridge.mjs），双击启动后本页通过 WebSocket 连它。
 *
 * 桥暴露两种能力：
 *   · mem    —— 读写目标内存（OpenOCD 后端）：浏览器这边照跑完整 RTT 协议，能力最全
 *   · stream —— 纯字节流（J-Link 的 RTT telnet 19021，只有 ch0，全双工）
 */
import { toB64, fromB64 } from '../core/b64.js';
import { waitMs } from '../core/pace.js';

export class BridgeClient {
  constructor(url){
    // 允许用户只填 "ws://127.0.0.1:17321"：桥的 WebSocket 端点在 /ws，这里自动补上
    let u = String(url || 'ws://127.0.0.1:17321').trim();
    try {
      const p = new URL(u);
      if (!p.pathname || p.pathname === '/') p.pathname = '/ws';
      u = p.toString();
    } catch { /* 保持原样，让 connect() 报错更直观 */ }
    this.url = u;
    this.ws = null;
    this.seq = 0;
    this.pending = new Map();
    this.caps = [];
    this.info = null;
    this.onStream = null;
    this.onClose = null;
    this._closedByUs = false;
  }

  get connected(){ return this.ws && this.ws.readyState === WebSocket.OPEN; }

  async connect(hello = {}){
    if (this.connected) return;
    this._closedByUs = false;
    const ws = new WebSocket(this.url);
    /**
     * 🚨 必须显式设成 arraybuffer：默认是 'blob'，二进制帧会变成 Blob 而不是 ArrayBuffer，
     *    下面 `_msg` 里就得多走一次异步的 blob.arrayBuffer()（多一次拷贝 + 打乱时序）。
     */
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    await new Promise((res, rej) => {
      const t = setTimeout(() => rej(new Error(`连不上桥（${this.url}）：请先双击 bridge/start-bridge.bat 启动它`)), 5000);
      ws.onopen = () => { clearTimeout(t); res(); };
      ws.onerror = () => { clearTimeout(t); rej(new Error(`连接桥失败（${this.url}）`)); };
    });
    ws.onmessage = ev => this._msg(ev.data);
    ws.onclose = () => {
      for (const [, p] of this.pending) p.rej(new Error('桥的连接已断开'));
      this.pending.clear();
      if (!this._closedByUs) this.onClose?.();
    };
    const ready = await this._call({ t: 'hello', ...hello });
    this.caps = ready.caps || [];
    return ready;
  }

  async open(backend, cfg = {}){
    const r = await this._call({ t: 'open', backend, cfg });
    this.info = r.info || null;
    return r;
  }

  _msg(data){
    /**
     * 二进制帧 = RTT 字节流（当前桥就是这么发的，见 bridge/rtt-bridge.mjs 的 sendBinary）。
     * 直接用 Uint8Array 视图交给上层，**不拷贝、不 base64**：老的 JSON+base64 分支还会用，
     * 但那条路每条消息都多 +33% 体积与一次解码。
     * （⚠️ 实测：端到端吞吐的天花板在桥那一侧的**数据源**，不在解码这里；见 docs/backends.md 三。）
     */
    if (data instanceof ArrayBuffer){ this.onStream?.(new Uint8Array(data)); return; }
    let m;
    // JSON 分支保留：兼容还在发 {t:'stream.data', data:base64} 的旧版桥（也用于非流消息）
    try { m = JSON.parse(data); } catch { return; }
    if (m.t === 'stream.data'){ this.onStream?.(fromB64(m.data || '')); return; }
    if (m.t === 'error' && m.id === undefined){ this.onClose?.(new Error(m.message)); return; }
    const p = this.pending.get(m.id);
    if (!p) return;
    this.pending.delete(m.id);
    if (m.t === 'error') p.rej(new Error(m.message || '桥返回错误'));
    else p.res(m);
  }

  _call(msg, timeout = 15000){
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return Promise.reject(new Error('桥未连接'));
    const id = ++this.seq;
    msg.id = id;
    return new Promise((res, rej) => {
      this.pending.set(id, { res, rej });
      try { this.ws.send(JSON.stringify(msg)); }
      catch (e){ this.pending.delete(id); rej(e); return; }
      setTimeout(() => {
        if (this.pending.has(id)){ this.pending.delete(id); rej(new Error('桥响应超时')); }
      }, timeout);
    });
  }

  // ---------------- mem 能力（OpenOCD） ----------------
  async readMem(addr, len){
    if (len <= 0) return new Uint8Array(0);
    // OpenOCD 偶尔会短读（尤其在被高频轮询时）：短读重试一次再报错
    for (let attempt = 1; ; attempt++){
      const r = await this._call({ t: 'mem.read', addr, len }, 20000);
      const b = fromB64(r.data || '');
      if (b.length >= len) return b.subarray(0, len);
      if (attempt >= 2){
        throw new Error(`读内存失败：0x${addr.toString(16)} 要 ${len} 字节，只回来 ${b.length} 字节（OpenOCD 忙不过来？把轮询间隔调大一点）`);
      }
      /* 短等待一律走 pace.waitMs：页面不可见时 setTimeout 会被钳到 ≥1 s（本仓实测过
         把 3.3 KB 固件从 1.4 s 拖成 47 s）。这里是"短读后重试一次"的 settle，正是那一类。 */
      await waitMs(30);
    }
  }

  async writeMem(addr, bytes){
    await this._call({ t: 'mem.write', addr, data: toB64(bytes) }, 20000);
  }

  async reset(){ await this._call({ t: 'target.reset' }); }
  async halt(){ await this._call({ t: 'target.halt' }); }
  async go(){ await this._call({ t: 'target.go' }); }

  /** 烧录（烧录器标签页用）：大固件慢慢烧，超时给足 10 分钟 */
  async flash(cfg){ return (await this._call({ t: 'flash', cfg }, 600000)).info; }

  /**
   * 列出桥所在机器上 OpenOCD 的 cfg 文件（页面里的「选择…」按钮用）。
   * 浏览器拿不到本地文件的完整路径，所以列表只能由桥端提供。
   */
  async cfgs(){ return (await this._call({ t: 'target.cfgs' }, 15000)).info; }

  // ---------------- stream 能力（J-Link ch0） ----------------
  async streamWrite(bytes){ await this._call({ t: 'stream.write', data: toB64(bytes) }, 10000); }

  close(){
    this._closedByUs = true;
    try { this.ws?.close(); } catch {}
    this.ws = null;
  }
}
