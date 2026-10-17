/**
 * 演示串口（DemoPort）：实现 Web Serial 里 SerialPort 的那几个成员，
 * 让整条「串口助手 / 终端」链路在没有硬件、没有授权点击的场合也能跑起来。
 *
 * 用途：
 *   · 网页演示（GitHub Pages 首屏就能看到东西在动）
 *   · 无头自检（tools/selftest/ui.selftest.mjs 用它跑端到端用例）
 * 打开方式：?demo=serial
 */

export function demoEnabled(){
  try { return new URLSearchParams(location.search).get('demo') === 'serial'; } catch { return false; }
}

const enc = new TextEncoder();

export class DemoPort {
  constructor(){
    this.info = { usbVendorId: 0x303a, usbProductId: 0x1001 };
    this.opened = false;
    this._readable = null;
    this._ctl = null;
    this._timer = null;
    this._n = 0;
  }

  getInfo(){ return this.info; }

  async open(o = {}){
    this.opts = o;
    this.opened = true;
    this._readable = new ReadableStream({
      start: c => { this._ctl = c; },
      cancel: () => this._stop(),
    });
    this._writable = new WritableStream({ write: chunk => this._onWrite(chunk) });
    this._timer = setInterval(() => this._beat(), 1000);
    setTimeout(() => this._emit('\r\n\x1b[36m[演示串口] 这不是真硬件，是页面内置的假设备。\r\n' +
      '试试发 AT / AT+GMR / help，或者切到「终端」标签敲同样的命令。\x1b[0m\r\n\r\n'), 350);
  }

  get readable(){ return this._readable; }
  get writable(){ return this._writable; }

  _stop(){
    if (this._timer){ clearInterval(this._timer); this._timer = null; }
    this._readable = null;
    this.opened = false;
  }

  async close(){
    const ctl = this._ctl;
    this._ctl = null;
    this._stop();
    try { ctl?.close(); } catch {}
  }

  async setSignals(){ /* 演示串口没有真实信号线 */ }

  async getSignals(){ return { dataCarrierDetect: false, clearToSend: false, ringIndicator: false, dataSetReady: false }; }

  _emit(text){
    if (!this._ctl) return;
    try { this._ctl.enqueue(enc.encode(text)); } catch {}
  }

  _beat(){
    this._n++;
    const t = new Date();
    const p = (n, w = 2) => String(n).padStart(w, '0');
    const clk = `${p(t.getHours())}:${p(t.getMinutes())}:${p(t.getSeconds())}`;
    this._emit(`[${clk}] 演示串口心跳 #${this._n}  (RX/TX 计数在下面的统计条里)\r\n`);
  }

  /** 设备侧：收到什么就回什么（外加几条常用命令的固定回复） */
  _onWrite(chunk){
    const bytes = chunk instanceof Uint8Array ? chunk : new Uint8Array(chunk);
    const text = new TextDecoder('utf-8', { fatal: false }).decode(bytes);
    const cmd = text.replace(/[\r\n]+$/, '');
    if (!cmd){
      this._emit('\r\n');
      return;
    }
    switch (cmd.toUpperCase()){
      case 'AT': this._emit('\r\nOK\r\n'); break;
      case 'AT+GMR': this._emit('\r\nAT version:3.1.0\r\nSDK version:演示设备 v1.0\r\nOK\r\n'); break;
      case 'HELP': this._emit('\r\n可用命令：AT / AT+GMR / help / hex / flood\r\n'); break;
      case 'HEX': this._emit('\r\n\x01\x03\x00\x00\x00\x0A\xC5\xCD\r\n(上面是 8 字节二进制，切到 HEX 显示看效果)\r\n'); break;
      case 'FLOOD': this._flood(); break;
      default: this._emit(`\r\n收到 ${bytes.length} 字节："${cmd}"\r\nERROR（演示设备只认 AT / AT+GMR / help / hex / flood）\r\n`);
    }
  }

  _flood(){
    let s = '';
    for (let i = 0; i < 400; i++) s += `${String(i).padStart(4, '0')} 演示数据行 · ABCDEFGHIJKLMNOPQRSTUVWXYZ · 0123456789\r\n`;
    this._emit(s);
    this._emit('（上面一次推了 ~23KB，用来试自动滚动/暂停/保存）\r\n');
  }
}
