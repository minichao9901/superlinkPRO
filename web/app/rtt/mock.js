/**
 * 模拟目标（Mock Probe）：内存里造一个**真的 RTT 控制块**，并且会像固件一样
 * 往上行缓冲里写日志、从下行缓冲里读命令。
 *
 * 作用：没有硬件也能把「RTT 协议层 + 界面」整条链路跑通并自测（环形缓冲绕回、
 * 丢包检测、下行回环都在里面）。仓库里的 tools/selftest 就是拿它跑 Node 端用例。
 */
import { u32leBytes, latin1, cstr } from '../core/bin.js';

const enc = new TextEncoder();

export class MockProbe {
  constructor(o = {}){
    this.name = '内置模拟目标';
    this.base = o.base ?? 0x20000000;
    this.size = 0x20000;
    this.mem = new Uint8Array(this.size);

    this.cbAddr = this.base + 0x1000;
    this.upBuf = this.base + 0x5000; this.upSize = 1024;
    this.downBuf = this.base + 0x5400; this.downSize = 256;
    this.nameAddr = this.base + 0x0100;

    this.timer = null;
    this.seq = 0;
    this.commands = 0;
    this._writeLayout();
  }

  get connected(){ return this.timer !== null; }

  _setU32(addr, v){ const o = addr - this.base; this.mem[o] = v & 0xff; this.mem[o + 1] = (v >>> 8) & 0xff; this.mem[o + 2] = (v >>> 16) & 0xff; this.mem[o + 3] = (v >>> 24) & 0xff; }
  _getU32(addr){
    const o = addr - this.base;
    return (this.mem[o] | (this.mem[o + 1] << 8) | (this.mem[o + 2] << 16) | (this.mem[o + 3] << 24)) >>> 0;
  }
  _put(addr, bytes){ this.mem.set(bytes, addr - this.base); }

  _writeLayout(){
    const cb = this.cbAddr - this.base;
    const id = enc.encode('SEGGER RTT');
    this.mem.set(id, cb);                                     // acID[16]，后面留 0
    this._setU32(this.cbAddr + 16, 1);                        // MaxNumUpBuffers
    this._setU32(this.cbAddr + 20, 1);                        // MaxNumDownBuffers
    // aUp[0]
    this._setU32(this.cbAddr + 24, this.nameAddr);
    this._setU32(this.cbAddr + 28, this.upBuf);
    this._setU32(this.cbAddr + 32, this.upSize);
    this._setU32(this.cbAddr + 36, 0);                        // WrOff
    this._setU32(this.cbAddr + 40, 0);                        // RdOff
    this._setU32(this.cbAddr + 44, 0);                        // Flags
    // aDown[0]
    this._setU32(this.cbAddr + 48, this.nameAddr);
    this._setU32(this.cbAddr + 52, this.downBuf);
    this._setU32(this.cbAddr + 56, this.downSize);
    this._setU32(this.cbAddr + 60, 0);
    this._setU32(this.cbAddr + 64, 0);
    this._setU32(this.cbAddr + 68, 0);
    this._put(this.nameAddr, enc.encode('Terminal\0'));
  }

  // ---------------- Probe 接口 ----------------
  async connect(){ if (!this.timer) this.timer = setInterval(() => this._tick(), 200); }
  async disconnect(){ clearInterval(this.timer); this.timer = null; }

  async readMem(addr, len){
    const out = new Uint8Array(len);
    const o = addr - this.base;
    if (o >= 0 && o < this.size){
      const n = Math.min(len, this.size - o);
      out.set(this.mem.subarray(o, o + n));
    }
    return out;                                              // 范围外返回 0（真实目标未映射区也常见读成 0/乱码）
  }

  async writeMem(addr, bytes){
    const o = addr - this.base;
    if (o < 0 || o >= this.size) return;
    const n = Math.min(bytes.length, this.size - o);
    this.mem.set(bytes.subarray(0, n), o);
  }

  async reset(){
    this._setU32(this.cbAddr + 36, 0); this._setU32(this.cbAddr + 40, 0);
    this._setU32(this.cbAddr + 60, 0); this._setU32(this.cbAddr + 64, 0);
    this.seq = 0;
    this._pushUp('\r\n\x1b[33m── 模拟目标已复位 ──\x1b[0m\r\n');
    return true;
  }

  /**
   * 目标侧的写（模拟固件调用 SEGGER_RTT_Write）
   * @param {string|Uint8Array} text
   * @param {boolean} force true = 不管有没有空间都写（模拟"目标写太快、覆盖了主机还没读走的数据"）
   */
  _pushUp(text, force = false){
    let data = typeof text === 'string' ? enc.encode(text) : text;
    const wrA = this.cbAddr + 36, rdA = this.cbAddr + 40;
    const wr = this._getU32(wrA);
    const rd = this._getU32(rdA);
    const free = (this.upSize - 1 + rd - wr + this.upSize) % this.upSize;
    if (!force && data.length > free){
      // RTT_MODE_NO_BLOCK_SKIP：放不下就丢掉（真实固件最常见的行为，主机侧看不见）
      this.dropped = (this.dropped || 0) + data.length;
      return;
    }
    if (data.length >= this.upSize) data = data.subarray(data.length - (this.upSize - 1));   // 环形缓冲只能留下最后一段
    const part = Math.min(data.length, this.upSize - wr);
    this._put(this.upBuf + wr, data.subarray(0, part));
    if (part < data.length) this._put(this.upBuf, data.subarray(part));
    this._setU32(wrA, (wr + data.length) % this.upSize);
  }

  /** 目标侧读下行（模拟固件从 SEGGER_RTT_Read 取命令） */
  _readDown(){
    const wrA = this.cbAddr + 60, rdA = this.cbAddr + 64;
    const wr = this._getU32(wrA), rd = this._getU32(rdA);
    if (wr === rd) return '';
    let buf;
    if (wr > rd) buf = this.mem.slice(this.downBuf - this.base + rd, this.downBuf - this.base + wr);
    else buf = Uint8Array.from([...this.mem.slice(this.downBuf - this.base + rd, this.downBuf - this.base + this.downSize),
                                ...this.mem.slice(this.downBuf - this.base, this.downBuf - this.base + wr)]);
    this._setU32(rdA, wr);
    return cstr(buf).replace(/[\r\n\0]/g, '');
  }

  _tick(){
    this.seq++;
    const t = new Date();
    const p = (n, w = 2) => String(n).padStart(w, '0');
    const clk = `${p(t.getHours())}:${p(t.getMinutes())}:${p(t.getSeconds())}.${p(t.getMilliseconds(), 3)}`;

    const cmd = this._readDown();
    if (cmd){
      this.commands++;
      this._pushUp(`\r\nmsh />${cmd}\r\n`);
      if (cmd === 'help'){
        this._pushUp('-- 模拟目标命令 --\r\n  help   显示这条帮助\r\n  ps     线程列表\r\n  flood  灌 8KB 数据（用来看丢包检测）\r\n  reboot 复位\r\nmsh />');
      } else if (cmd === 'ps'){
        this._pushUp('thread   pri  status  stack  max  left\r\n-------  ---  ------  -----  ---  ----\r\ntshell    20  ready    2048  712  1336\r\ntidle0    31  ready     512  176   336\r\nmsh />');
      } else if (cmd === 'reboot'){
        this.reset();
      } else if (cmd === 'flood'){
        // 一次写远超缓冲大小的数据：先绕圈覆盖（这部分物理上不可观测），
        // 再把缓冲灌到满，好让主机侧的「缓冲读满」信号亮起来
        this._pushUp('X'.repeat(2048), true);
        this._pushUp('（上面灌了 2KB 到 1KB 缓冲里 → 没读走的数据被覆盖，看“缓冲读满/溢出丢弃”）\r\n', true);
        this._pushUp('Y'.repeat(this.upSize - 40), true);
      } else {
        this._pushUp(`没这个命令：${cmd}（试试 help）\r\nmsh />`);
      }
      return;
    }
    if (this.seq % 5 === 0){
      this._pushUp(`[${clk}] \x1b[32mINFO\x1b[0m heartbeat #${this.seq}  cpu=${(this.seq * 7) % 40}%  heap=${100000 - this.seq * 13} B\r\n`);
    } else {
      this._pushUp(`[${clk}] seq=${this.seq} 计数器模拟输出\r\n`);
    }
  }
}
