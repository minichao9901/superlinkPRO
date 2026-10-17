/**
 * 采集落文件：收到的字节**边收边写进本地文件**，不占页面内存。
 *
 * 为什么需要它：接收区是"显示优先"的 —— raw 上限 2 MB（`RxBuffer.maxRaw`），
 * 高速采集几秒就撑爆（WebUSB 实测 330 KB/s → 2 MB 只够 6 秒），超出的只能丢；
 * 「保存数据」也只能保存没被丢的那部分。落文件的字节**不进显示缓冲**，
 * 采集多久都不丢，页面卡不卡、有没有被限速都不影响已经写下去的字节。
 *
 * 两条路：
 *   ① File System Access API（`showSaveFilePicker` + `createWritable`）——
 *      Chrome/Edge 桌面版都有；选好文件后一直往里追加，**停止时落盘**。
 *   ② 没有这个 API（Firefox/Safari，它们本来也没有 Web Serial/WebUSB）→
 *      先在内存里攒（有上限），停止时下载；攒满上限会置 overflow 标记，如实告诉用户。
 *
 * 两种写法：
 *   原样（.bin）：字节流原封不动，二进制安全，方便事后解析；
 *   带时间戳（.txt，默认）：仅文件**开头**一行 [HH:MM:SS.mmm] 标记开始时间，
 *   正文同样是原样字节流（中途打时间戳会把行劈开，已废弃）。
 *
 * ⚠️ 写入是**批量异步**的（默认攒 256 KB 或 300 ms 写一次），push 本身不做 I/O，
 *    不会拖慢接收回路；停止时等最后一批写完再 close。
 *
 * 🚨 **`.crswap` 与"什么时候才真的落盘"（2026-10 用户现场，必须让用户看得见）**
 *    File System Access 的写法是"**先写临时文件，close 时才改名**"：
 *    你选了 `rtt-xxx.txt`，磁盘上先出现的是 **`rtt-xxx.txt.crswap`**（字节全在它里面），
 *    只有 `stop()` → `w.close()` 之后 Chrome 才把它改名成 `rtt-xxx.txt`。由此三条后果：
 *      · 记录中看到 .crswap **是正常的**，别删它（那就是你的数据）；
 *      · **记录中千万别关页面/刷新/让浏览器把标签页回收** —— 写句柄被销毁时 Chrome 会
 *        把 swap 文件**删掉**，那些字节直接没了。实测现场：11.4 MB 的 .crswap 在页面
 *        消失后不见了，而目标文件还是 **0 B**；
 *      · "停止转发"**不等于**停止记录（记录挂在 CDC 串口会话上）—— 想落盘就点「停止记录」。
 *    所以本类把 `pushed / written / backlog()` 三个数交给界面显示：
 *    🚨 页面被切到后台时浏览器会限速整个页面，写盘可能远远跟不上输入
 *    （实测 3 MB/s 的转发流落到文件只有 ~50 KB/s），积压全在内存里 —— 用户必须能看见，
 *    而不是对着一个永远写着 "0 B" 的按钮猜。
 *    为此还挂了两道保护：记录期间 `beforeunload` 拦一下（未落盘就别走），
 *    切到后台时通过 `onNote` 提醒一句。
 */
import { stamp as stampOf, fileStamp, download, bytes as fBytes } from './format.js';

const FLUSH_BYTES = 256 * 1024;
const FLUSH_MS = 300;
const MEM_CAP = 64 * 1024 * 1024;
/** 积压超过这个数就认为"写盘跟不上"，界面要明确告警 */
const BACKLOG_WARN = 4 * 1024 * 1024;

const enc = new TextEncoder();

/**
 * 记录按钮的文字/提示 —— 串口助手 / 终端 / RTT Viewer / RTT 转发四个页面共用一套说法。
 * 🚨 为什么要由这里统一给：以前各页面都是 `■ 停止记录 · ${fBytes(rec.bytes)}`，
 *    而 `rec.bytes` 只在 `onChange`（开始/停止）时才刷到按钮上 —— 记录中按钮**永远显示 0 B**，
 *    用户根本看不出写了多少、更看不出写盘有没有跟上（真机现场就是这个坑）。
 * @returns {{text:string, title:string, primary:boolean}}
 */
export function recordButtonState(rec){
  const back = rec.backlog();
  if (rec.starting) return { text: '正在选择记录文件…', title: '等待文件选择完成', primary: true };
  if (rec.draining) return {
    text: `■ 正在落盘… ${fBytes(rec.written)} / ${fBytes(rec.pushed)}`,
    title: `正在把积压写进文件（已写 ${fBytes(rec.written)}，共 ${fBytes(rec.pushed)}）。写完 Chrome 才会把 ${rec.name}.crswap 改名成正式文件。`,
    primary: true,
  };
  if (rec.active) return {
    text: `■ 停止记录 · ${fBytes(rec.bytes)}` + (back > 1024 * 1024 ? `（待落盘 ${fBytes(back)}）` : ''),
    title: `正在写入 ${rec.name}（${fBytes(rec.bytes)} / ${rec.frames} 段）。\n`
      + `Chrome 先写成 ${rec.name}.crswap，点「停止记录」才改名成正式文件 —— 记录中别关页面/刷新，否则未落盘的数据会丢。\n`
      + (rec.lagging()
          ? `⚠ 写盘跟不上输入：还有 ${fBytes(back)} 积压在内存里。本页切到后台会被浏览器限速（实测 3 MB/s 的流转发进文件只剩 ~50 KB/s），请让本页留在前台，或把速率降下来。`
          : ''),
    primary: true,
  };
  if (rec.error) return {
    text: rec.needsClose ? '■ 结束失败记录' : '⚠ 记录失败 · 点击重新开始',
    title: `记录失败：${rec.error.message || rec.error}。已写 ${fBytes(rec.written)} / 收到 ${fBytes(rec.pushed)}，文件可能不完整。`,
    primary: rec.needsClose,
  };
  return { text: '● 记录到文件', title: '把收到的字节直接写进本地文件（不走接收区 2MB 上限），高速采集用', primary: false };
}

export class FileRecorder {
  static supported(){
    return typeof window !== 'undefined' && typeof window.showSaveFilePicker === 'function';
  }

  constructor(){
    this.active = false;
    this.draining = false;          // 已停止接收，但还在把积压写进文件
    this.name = '';                 // 文件名（没有 API 时是建议名）
    this.timestamps = false;
    this.bytes = 0;                 // 已记账的**原始**字节数（不含时间戳前缀）
    this.pushed = 0;                // 交给写口的全部字节（含时间戳前缀）
    this.written = 0;               // **已经写进文件**的字节（进度用）
    this.frames = 0;
    this.t0 = 0;
    this.overflow = false;          // 内存兜底模式攒爆了
    this.error = null;
    this.onChange = null;           // 供界面刷新按钮文字（写盘进度也会触发）
    this.onNote = null;             // 给用户的一句提醒（toast）
    this._w = null;                 // FileSystemWritableFileStream
    this._wq = Promise.resolve();   // 写队列（保证顺序、终止时可等）
    this._chunks = [];
    this._pend = 0;
    this._mem = [];
    this._memBytes = 0;
    this._timer = null;
    this._progTimer = null;
    this._notifiedAt = 0;
    this._startPromise = null;
    this._stopPromise = null;
  }

  get starting(){ return !!this._startPromise; }
  get needsClose(){ return !!this._w; }

  /** 尚未写入的字节；即使为零，文件仍须 close 才提交。 */
  backlog(){ return Math.max(0, this.pushed - this.written); }
  /** 积压是否已经大到该告警（界面据此在按钮上加"待落盘 X"） */
  lagging(){ return this.backlog() > BACKLOG_WARN; }

  /**
   * 开始记录。**必须在用户手势里调用**（showSaveFilePicker 的硬要求）。
   * 带时间戳模式：只在**文件开头**写一行 [时:分:秒.毫秒]，正文是原样字节流 ——
   * 之前逢数据块就打时间戳，会把一行从中间劈开（实测 2MB 文件被劈出 1400+ 处
   * "hello worl[ts] hello world!"），现已废弃。
   * @param {{name?:string, timestamps?:boolean}} opts
   * @returns {Promise<string>} 文件名
   */
  async start({ name = 'capture', timestamps = false } = {}){
    if (this.draining || this._stopPromise) throw new Error('上一份记录正在落盘，请等待完成');
    if (this.active) return this.name;
    if (this._startPromise) return await this._startPromise;
    if (this._w) throw new Error('上一份记录尚未关闭，请先停止记录');
    this._startPromise = this._startNow({ name, timestamps });
    this._notify(true);
    try { return await this._startPromise; }
    finally { this._startPromise = null; this._notify(true); }
  }

  async _startNow({ name, timestamps }){
    this.timestamps = timestamps;
    if (FileRecorder.supported()){
      const ext = timestamps ? 'txt' : 'bin';
      const handle = await window.showSaveFilePicker({
        suggestedName: `${name}-${fileStamp()}.${ext}`,
        types: [{ description: timestamps ? '带时间戳的文本日志' : '原始字节流', accept: { 'application/octet-stream': ['.bin', '.log', '.txt'] } }],
      });
      this.name = handle.name || `${name}.${ext}`;
      this._w = await handle.createWritable();
    } else {
      this.name = `${name}-${fileStamp()}.${timestamps ? 'txt' : 'bin'}`;
    }
    this.bytes = 0; this.frames = 0; this.pushed = 0; this.written = 0;
    this._wq = Promise.resolve(); this._chunks = []; this._pend = 0;
    this._mem = []; this._memBytes = 0;
    this.overflow = false; this.error = null; this.draining = false;
    this.t0 = Date.now();
    if (timestamps){
      const head = enc.encode(`[${stampOf()}]\n`);
      this._chunks.push(head);
      this._pend += head.length;            // 文件头不计入 bytes（那是原始字节数）
      this.pushed += head.length;
    }
    this.active = true;
    this._timer = setInterval(() => this._flush(), FLUSH_MS);
    this._armGuards();
    this.onChange?.();
    return this.name;
  }

  /** 收流：只入队，不做 I/O（高速通路上不要在这里写盘）。正文永远原样字节流 */
  push(bytes, t = new Date()){
    if (!this.active || !bytes?.length) return;
    this._chunks.push(bytes);
    this._pend += bytes.length;
    this.bytes += bytes.length;
    this.pushed += bytes.length;
    this.frames++;
    if (this._pend >= FLUSH_BYTES) this._flush();
  }

  _flush(){
    if (this.error){ this._chunks = []; this._pend = 0; return; }
    if (!this._chunks.length) return;
    const chunks = this._chunks;
    this._chunks = [];
    this._pend = 0;
    const total = chunks.reduce((s, c) => s + c.length, 0);
    if (this._w){
      const w = this._w;
      /**
       * 🚨 **一次 flush 合成一次 write**：每个 `w.write()` 都是一次跨进程（Mojo）往返 +
       *    文件系统调用，逐块写会把自己排成一条长队（3 MB/s 的输入下尤其明显）。
       *    合成之后同样的字节数只需要 1/几十 次调用。
       */
      this._wq = this._wq.then(async () => {
        if (this.error) return; // Never append later batches after a failed write.
        if (chunks.length === 1) await w.write(chunks[0]);
        else {
          const buf = new Uint8Array(total);
          let off = 0;
          for (const c of chunks){ buf.set(c, off); off += c.length; }
          await w.write(buf);
        }
        this.written += total;
        this._notify(true);
      }).catch(e => this._fail(e));
    } else {
      for (const c of chunks){
        if (this._memBytes + c.length > MEM_CAP){ this.overflow = true; return; }
        this._mem.push(c); this._memBytes += c.length;
      }
      this.written += total;
    }
  }

  /** 限频地通知界面（写盘是成批的，天然不会太频；再兜一道 150 ms） */
  _notify(force = false){
    const now = Date.now();
    if (!force && now - this._notifiedAt < 150) return;
    this._notifiedAt = now;
    try { this.onChange?.(); } catch { /* 界面的事不影响记录 */ }
  }

  _fail(e){
    if (this.error) return;
    this.error = e;
    this.active = false;
    clearInterval(this._timer); this._timer = null;
    this._chunks = []; this._pend = 0;
    try { this.onNote?.(`记录写入失败：${e.message || e}。已停止接收，文件可能不完整，请点「结束失败记录」关闭文件`); } catch {}
    this._notify(true);
  }

  // ---------------- 两道"别把数据弄丢"的保护 ----------------
  _armGuards(){
    if (typeof addEventListener !== 'function') return;
    /**
     * 记录中离开页面要拦一下：写句柄被销毁 = .crswap 被 Chrome 删掉 = 未落盘的字节全丢
     * （真机现场就是这么丢的 11.4 MB）。积压清零不等于提交，必须保护到 close 完成。
     */
    this._bye = e => {
      if (!this.needsClose && !this.active && !this.draining) return;
      e.preventDefault();
      e.returnValue = '';
      return '';
    };
    this._vis = () => {
      if (document.hidden && this.active){
        this.onNote?.('本页切到后台了：浏览器会限速这个页面，写盘可能跟不上（看「停止记录」按钮上的"待落盘"）—— 高速记录时请让本页留在前台');
      }
    };
    try { addEventListener('beforeunload', this._bye); } catch {}
    try { document.addEventListener('visibilitychange', this._vis); } catch {}
  }
  _disarmGuards(){
    if (typeof removeEventListener !== 'function') return;
    try { removeEventListener('beforeunload', this._bye); } catch {}
    try { document.removeEventListener('visibilitychange', this._vis); } catch {}
  }

  /** 停止并落盘；@returns {{name,bytes,frames,seconds,overflow,error}} */
  async stop(){
    if (this._stopPromise) return await this._stopPromise;
    this._stopPromise = this._stopNow();
    try { return await this._stopPromise; }
    finally { this._stopPromise = null; this._notify(true); }
  }

  async _stopNow(){
    if (this._startPromise) await this._startPromise.catch(() => {});
    if (!this.active && !this._w) return null;
    const info = { name: this.name, bytes: this.bytes, frames: this.frames, seconds: (Date.now() - this.t0) / 1000, overflow: this.overflow, error: this.error };
    this.active = false;
    this.draining = true;
    clearInterval(this._timer); this._timer = null;
    this._flush();
    /**
     * 落盘可能要等一会儿（积压多 / 页面被限速时尤其久）—— 期间每 300 ms 报一次进度，
     * 让界面显示「正在落盘 12.3 / 45.6 MB」而不是像卡住了。
     */
    this._progTimer = setInterval(() => this._notify(true), 300);
    this._notify(true);
    try { await this._wq; } catch { /* _fail 里已记 */ }
    info.error = this.error || info.error;
    clearInterval(this._progTimer); this._progTimer = null;
    /**
     * 🚨 **`draining` 要一直挂到 `close()` 返回为止**（2026-10 真机基准测试抓到）：
     *    Chrome 的写法是"先写 `.crswap`，`close()` 时才改名成正式文件"，所以在 close 落地之前
     *    **磁盘上那个正式文件还是 0 字节**。早期版本在这里就把 draining 置 false，于是：
     *      · 界面上按钮已经变回「● 记录到文件」，用户以为落盘完了；
     *      · 自动化脚本按"draining=false 即落盘完成"去读文件，**读到 0 字节**
     *        （实测 10 秒 13.98 MB 的记录，判成 0.00 MB —— 白跑一轮）。
     *    顺序换一下，`draining` = "文件还没改名完"，语义和按钮文字就能对上。
     */
    if (this._w){
      try { await this._w.close(); } catch (e){ info.error = info.error || e; this.error = info.error; }
      this._w = null;
    } else if (this._mem.length){
      download(this.name, new Blob(this._mem, { type: 'application/octet-stream' }));
    }
    this.draining = false;
    info.pushed = this.pushed;
    info.written = this.written;
    this._mem = []; this._memBytes = 0;
    this._disarmGuards();
    this.onChange?.();
    return info;
  }
}
