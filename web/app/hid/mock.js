/**
 * 假探针（协议级 mock）：接口和 AkaLinkHid 一样，但不碰 USB。
 * 用途：没插硬件时演示/自测这张面板（URL 加 ?hid=mock，或调 view.useMock()）。
 *
 * 行为照真固件来：
 *   · START / AUTOSTART 只是**排队**，真正的 SWD 动作在主循环里做 —— 所以第一次查状态时
 *     返回"还没起来"，下一次才有 running 与找到的控制块；
 *   · 启动返回码按固件的语义给（0 正常 / -3 没找到控制块），可用 failNextStart 注入错误。
 */
import { RTT_ACT, startRcText, START_PENDING } from './probe.js';

const DEFAULT_CB = 0x20000000;

export class MockAkaLinkHid {
  constructor(){
    this.device = { productName: 'akaLinkPro (mock)', serialNumber: 'MOCK-0001', opened: true };
    this.running = false;
    this.riscv = false;              // 全局目标类型（HID 0x31 action 10，粘性）
    this.cbAddr = 0;
    this.upAddr = 0;
    this.moved = 0;
    this.polls = 0;
    this.transfers = 0;
    this.rdErr = 0;
    this.rdErrNext = 0;
    this.armed = null;        // 排队中的启动请求
    this.startRc = 0;
    this.clockMhz = 45;       // 固件默认档
    this.chunkBytes = 512;
    this.discard = false;
    this.failNextStart = 0;   // 测试用：下一次启动强制返回这个码
    this.stall = false;       // 测试用：true = 搬运计数冻住（模拟另一路抢走了 RTT 缓冲）
    this.calls = [];
    this.onDisconnect = null;
  }

  static supported(){ return true; }
  get connected(){ return true; }
  get label(){ return 'akaLinkPro (mock) · MOCK-0001'; }

  async request(){ this.calls.push('request'); return this.device; }
  async reconnect(){ this.calls.push('reconnect'); return this.device; }
  async close(){ this.running = false; this.calls.push('close'); }
  async info(){ return { model: 'akaLink CMSIS-DAP (mock)', fw: 'mock-1.0', sn: 'MOCK-0001' }; }

  /** 主循环那一拍：把排队的启动落实 */
  _tick(){
    if (!this.armed) return;
    const a = this.armed;
    this.armed = null;
    if (this.failNextStart){
      this.startRc = this.failNextStart;
      this.failNextStart = 0;
      this.running = false;
      this.calls.push('start-failed:' + this.startRc);
      return;
    }
    this.running = true;
    this.cbAddr = a.addr || DEFAULT_CB;              // 给了地址就用它，否则当作"扫描找到 0x20000000"
    this.upAddr = this.cbAddr + 24;                  // CB + 24 + 通道号×24（通道 0）
    this.moved = 0;
    this.polls = 0;
    this.transfers = 0;
    this.startRc = 0;
    this.calls.push(`start-ok:0x${this.cbAddr.toString(16)}/${a.size}/${a.channel}`);
  }

  async start({ addr = 0, size = 0, channel = 0 } = {}){
    this.armed = { addr, size, channel };
    this.startRc = START_PENDING;                 // 固件把 s_start_rc 重置成 -100 = "结果待定"
    this.calls.push(`start:0x${addr.toString(16)}/${size}/${channel}`);
    return { rc: 0, status: this.statusObj() };   // 只是排队：这一拍还没起来（与真固件一致）
  }

  async autostart(){
    this.armed = { addr: 0, size: 0, channel: 0 };
    this.startRc = START_PENDING;
    this.calls.push('autostart');
    return { rc: 0, status: this.statusObj() };
  }

  async stop(){
    this.running = false;
    this.armed = null;
    this.startRc = 0;
    this.calls.push('stop');
    return this.status();
  }

  async configure({ clockHz = 0, chunkBytes = 0, discard = false } = {}){
    if (clockHz) this.clockMhz = Math.round(clockHz / 1e6);
    if (chunkBytes) this.chunkBytes = chunkBytes;
    this.discard = !!discard;
    this.calls.push(`config:${clockHz}/${chunkBytes}/${discard ? 1 : 0}`);
    return { rc: 0, status: this.statusObj() };
  }

  /** HID 0x31 action 10：切全局目标类型（粘性）。假探针照实记下来，供页面自测断言。 */
  async setTargetType(riscv){
    this.riscv = !!riscv;
    this.calls.push(`target:${riscv ? 'riscv' : 'swd'}`);
    return { rc: 0, status: this.statusObj() };
  }

  async status(){
    this._tick();
    if (this.running && !this.stall){       // stall = 模拟"另一路把 RTT 缓冲读走了，桥搬不到东西"
      this.polls += 37;
      this.transfers += 12;
      this.moved += 4096;
      if (this.rdErrNext){ this.rdErr += this.rdErrNext; this.rdErrNext = 0; }
    } else if (this.running){
      this.polls += 37;
    }
    const st = this.statusObj();
    return { rc: 0, status: st };
  }

  statusObj(){
    return {
      running: this.running,
      channel: 0,
      swdReady: true,
      clockDelay: 1,
      cbAddr: this.cbAddr,
      upAddr: this.upAddr,
      moved: this.moved,
      polls: this.polls,
      transfers: this.transfers,
      rdErr: this.rdErr,
      wrErr: 0,
      lastChunk: this.running ? 512 : 0,
      emptyRing: 0,
      dapYield: 0,
      rescans: 0,
      lastCmdId: RTT_ACT.STATUS,
      lastResp: 0,
      startRc: this.startRc,
      chunkBytes: this.chunkBytes,
      discard: this.discard,
      swdMhz: this.clockMhz,
      rcText: startRcText(this.startRc, this.riscv),
    };
  }
}
