/**
 * 假 I2C 探针 + 假器件 —— 协议级 mock，**不碰 USB**。
 *
 * 用途有两处，都很重要：
 *   ① 没插硬件时页面能演示跑通（勾「假探针」），示例脚本（AT24C02/MPU6050/ADS1115/Si5351）
 *      的 while(1) 定时读**在假器件上就能看到活的曲线**；
 *   ② Node 自测里它是"标准答案"：真跑一遍 使能 → 配置 → 扫描 → 写 → 读 → 定时读。
 *
 * 行为**照真固件**（这几条不是装饰，主机侧代码得靠它们才能被验对）：
 *   · XFER/SCAN 只是**登记**：立刻回"已受理 + PENDING"，结果要用 RESULT 轮询取
 *     （`pendingExtra` 控制"还要再轮询几次才算做完"，默认 1 —— 逼主机真的去轮询）；
 *   · 未使能时 XFER/SCAN 回 `E_DISABLED`（真固件连 I2C 寄存器都不读，见上游 a4d5205）；
 *   · 上一条没做完时再发 XFER 回 `E_BUSY`（`cmdRc`），**不是**并发执行；
 *   · 子地址是**按原序发出的字节串**（addr[0] 先发），不是 u32；
 *   · 计数器、`actual_scl_hz` 档位换算（0/≤100k→100k，≤400k→400k，否则 1 MHz）、
 *     RESET 会清计数器但保配置 —— 全按 proto.h。
 */

import {
  ACT, E, ST, ST_SHIFT_CMD, ST_SHIFT_DONE, ST_SHIFT_ERR,
  SCAN_BYTES, SCAN_FIRST, SCAN_LAST, STAT_WORDS, CFG_SIZE,
  parseCfg, packCfg,
} from './protocol.js';

// ============================================================================
// 器件模型
// ============================================================================

/**
 * 寄存器型器件（MPU6050 / ADS1115 / Si5351 的共同底座）：
 * 子地址 = 寄存器指针，写数据从指针开始顺序写、读从指针开始顺序读并自增。
 */
class RegDevice {
  constructor(addr, name, size = 256){
    this.addr = addr; this.name = name;
    this.regs = new Uint8Array(size);
    this.ptr = 0;
    /** 由子类填：读寄存器前先让它把"活的"数据刷新进 regs（模拟传感器采样）*/
    this.onRead = null;
    this.onWrite = null;
  }
  transact(addr, wr, rd){
    if (addr.length) this.ptr = addr[0];
    if (wr.length){
      this.onWrite?.(this.ptr, wr);
      for (let i = 0; i < wr.length; i++) this.regs[(this.ptr + i) % this.regs.length] = wr[i];
      this.ptr = (this.ptr + wr.length) % this.regs.length;
    }
    if (rd){
      this.onRead?.(this.ptr, rd);
      const out = new Uint8Array(rd);
      for (let i = 0; i < rd; i++) out[i] = this.regs[(this.ptr + i) % this.regs.length];
      this.ptr = (this.ptr + rd) % this.regs.length;
      return out;
    }
    return null;
  }
}

/**
 * AT24C02（2 Kbit = 256 B EEPROM）：
 *   · 子地址 1 B（就是存储地址），页大小 8 B；
 *   · 读**自动自增**（分片读靠这个）；
 *   · 写完有 tWR（约 5 ms）：这段时间内**地址相位不 ACK** —— 真器件就是这么表现的，
 *     示例脚本里的 `delay 10ms` 不是凑数。
 */
export class Eeprom24C02 {
  constructor(addr = 0x50, size = 256, page = 8, twrMs = 5){
    this.addr = addr; this.name = `AT24C${String(size * 8 / 1024).padStart(2, '0')}`;
    this.size = size; this.page = page; this.twrMs = twrMs;
    this.mem = new Uint8Array(size);
    this.ptr = 0;
    this.busyUntil = 0;
    /** 测试用：注入"写保护"（数据相位 NACK，和 AT24C02 的 WC 脚拉高一样）*/
    this.writeProtected = false;
  }
  transact(addr, wr, rd, now){
    if (addr.length) this.ptr = addr[0] % this.size;
    if (wr.length){
      if (now < this.busyUntil) return { err: E.NO_ADDR };           // tWR 期间不 ACK（真器件如此）
      if (this.writeProtected) return { err: E.NO_ACK };
      const start = this.ptr;
      for (let i = 0; i < wr.length; i++){
        // 页内回绕（跨页写会绕回本页页首），再按整片回绕 —— 与真 EEPROM 一致
        const inPage = (start % this.page + i) % this.page;
        const at = (start - (start % this.page) + inPage) % this.size;
        this.mem[at] = wr[i];
      }
      this.ptr = (start + wr.length) % this.size;
      this.busyUntil = now + this.twrMs;
      return null;
    }
    if (rd){
      if (now < this.busyUntil) return { err: E.NO_ADDR };
      const out = new Uint8Array(rd);
      for (let i = 0; i < rd; i++) out[i] = this.mem[(this.ptr + i) % this.size];
      this.ptr = (this.ptr + rd) % this.size;
      return out;
    }
    return null;
  }
}

/** MPU6050：WHO_AM_I=0x68，从 0x3B 起 14 B = 加速度(6)+温度(2)+陀螺(6) */
const M_ACCEL_XOUT_H = 0x3b, M_TEMP_OUT_H = 0x41, M_GYRO_XOUT_H = 0x43, M_WHO_AM_I = 0x75;
export class Mpu6050 extends RegDevice {
  constructor(addr = 0x68, now = () => Date.now()){
    super(addr, 'MPU6050', 128);
    this.now = now;
    this.regs[M_WHO_AM_I] = 0x68;
    this.regs[0x6b] = 0x40;                 // PWR_MGMT_1 上电默认 SLEEP=1（真器件如此）
    this.regs[0x1c] = 0x00;                 // ACCEL_CONFIG ±2g
    this.regs[0x1b] = 0x00;                 // GYRO_CONFIG ±250°/s
  }
  /** 采样：加速度 0.2 g 慢正弦（重力在 z 上）+ 温度慢漂 + 陀螺小抖动 */
  _sample(){
    const t = this.now() / 1000;
    const put = (reg, v) => { this.regs[reg] = (v >> 8) & 0xff; this.regs[reg + 1] = v & 0xff; };
    const i16 = x => Math.max(-32768, Math.min(32767, Math.round(x))) & 0xffff;
    put(M_ACCEL_XOUT_H + 0, i16(0.20 * Math.sin(2 * Math.PI * 0.5 * t) * 16384));
    put(M_ACCEL_XOUT_H + 2, i16(0.20 * Math.sin(2 * Math.PI * 0.5 * t + 2.1) * 16384));
    put(M_ACCEL_XOUT_H + 4, i16((1.0 + 0.02 * Math.sin(2 * Math.PI * 0.2 * t)) * 16384));
    // 🚨 温度要按**手册的逆运算**存：TEMP_OUT = (T℃ − 36.53) × 340。
    //    直接存 T×340 的话，主机按 `raw/340+36.53` 解出来会多出 36.53（假探针上量到 73℃）。
    const tC = 36.5 + 0.8 * Math.sin(2 * Math.PI * 0.05 * t);
    put(M_TEMP_OUT_H, i16((tC - 36.53) * 340));
    put(M_GYRO_XOUT_H + 0, i16(12 * Math.sin(2 * Math.PI * 0.7 * t) * 131));
    put(M_GYRO_XOUT_H + 2, i16(9 * Math.sin(2 * Math.PI * 0.9 * t + 1.0) * 131));
    put(M_GYRO_XOUT_H + 4, i16(4 * Math.sin(2 * Math.PI * 1.1 * t) * 131));
  }
  transact(addr, wr, rd){
    if (rd) this._sample();
    return super.transact(addr, wr, rd);
  }
}

/** ADS1115：转换寄存器 0x00 / 配置寄存器 0x01（16 位**大端**在寄存器里） */
export class Ads1115 extends RegDevice {
  constructor(addr = 0x48, now = () => Date.now()){
    super(addr, 'ADS1115', 4);
    this.now = now;
    this.cfg = 0x8583;            // 上电默认：MUX=AIN0-AIN1, ±2.048V, 单次, 128SPS
    this.conv = 0;                // 转换结果：上电后保持 0000h 直到第一次转换完成
    this.readyAt = 0;             // 单次转换"出结果"的时刻
    this.pending = false;         // 有一次单次转换在跑（跑完之前转换寄存器还是旧值）
    this.nextCont = 0;            // 连续模式下一次刷新的时刻
    this._writeCfg(this.cfg);
  }
  _writeCfg(word){
    this.cfg = word & 0xffff;
  }
  _lsbVolt(){
    const pga = (this.cfg >> 9) & 7;
    const fsr = [6.144, 4.096, 2.048, 1.024, 0.512, 0.256, 0.256, 0.256][pga];
    return fsr / 32768;
  }
  _sps(){
    const dr = (this.cfg >> 5) & 7;
    return [8, 16, 32, 64, 128, 250, 475, 860][dr];
  }
  /**
   * 真器件是"**到点才更新**转换寄存器"：单次要等 1/DR，连续模式也受数据率节流。
   * 所以读得比数据率还快时，读回来的是同一个值 —— 假器件也照这个来，
   * 否则"忘了 delay 就现算一个新值"的脚本在假探针上永远测不出问题。
   */
  _advance(){
    const now = this.now();
    const single = ((this.cfg >> 8) & 1) === 1;                 // MODE：1 = 单次
    if (single){
      if (this.pending && now >= this.readyAt){ this._sample(); this.pending = false; }
    } else if (now >= this.nextCont){
      this._sample();
      this.nextCont = now + 1000 / this._sps();
    }
  }
  _sample(){
    // 单端 AIN0（MUX=100）：1.0 V ± 0.6 V 慢正弦；差分时给个小的
    const t = this.now() / 1000;
    const mux = (this.cfg >> 12) & 7;
    const v = mux >= 4 ? (1.0 + 0.6 * Math.sin(2 * Math.PI * 0.3 * t) - (mux - 4) * 0.15)
                       : 0.25 * Math.sin(2 * Math.PI * 0.3 * t);
    const raw = Math.max(-32768, Math.min(32767, Math.round(v / this._lsbVolt())));
    this.conv = raw & 0xffff;
  }
  transact(addr, wr, rd){
    const ptr = addr.length ? addr[0] : this.ptr;
    if (wr.length){
      const reg = ptr & 3;
      let w = wr;
      if (reg === 1 && wr.length >= 2){          // 写配置寄存器（大端）
        const word = (wr[0] << 8) | wr[1];
        this._writeCfg(word);
        // OS 位是 bit15：1 = 启动一次单次转换（配置字里就是它）
        if (((word >> 15) & 1) === 1){
          this.pending = true;                   // 转换要 1/DR 才出结果
          this.readyAt = this.now() + Math.ceil(1000 / this._sps()) + 1;
        } else {
          this.pending = false;                  // 连续模式：一直转，读到的就是最近一次
          this.readyAt = 0;
          this.nextCont = 0;
        }
      }
      this.ptr = (reg + wr.length) % 4;
      return null;
    }
    if (rd){
      const reg = ptr & 3;
      if (reg === 0){
        this._advance();                         // 没到点就还是上一次的结果（真器件如此）
        this.regs[0] = (this.conv >> 8) & 0xff;
        this.regs[1] = this.conv & 0xff;
      } else if (reg === 1){
        this._advance();
        // OS 读回：0 = 还在转换中，1 = 当前没在转换（连续模式一直在转 → 恒读 0）
        const single = ((this.cfg >> 8) & 1) === 1;
        const busy = single ? this.pending : true;
        const word = (this.cfg & 0x7fff) | (busy ? 0x0000 : 0x8000);
        this.regs[1] = (word >> 8) & 0xff;
        this.regs[2] = word & 0xff;
      }
      const out = new Uint8Array(rd);
      for (let i = 0; i < rd; i++) out[i] = this.regs[(reg + i) % 4];
      this.ptr = (reg + rd) % 4;
      return out;
    }
    return null;
  }
}

/**
 * Si5351：寄存器文件。**寄存器地址按数据手册 / AN619**（别把 0x03 当 CLK0 控制）：
 *   · 0x03 = 输出使能（bit7..0 = CLK7..CLK0 的 _OEB，**写 1 = 关掉那一路**），复位值 0 = 全开；
 *   · 0x10..0x17 = CLK0..CLK7 控制（PDN / 整数模式 / PLL 选择 / 反相 / MSx 来源 / 驱动电流）；
 *   · 0x1A PLLA、0x22 PLLB、0x2A MS0 … 参数块；0xB1 = PLL 软复位（**自清位**）。
 * 注意寄存器号最大到 0xBB，所以文件开 192 B（原来 128 B 会把 0xB1 绕回 0x31）。
 */
export class Si5351 extends RegDevice {
  constructor(addr = 0x60){
    super(addr, 'Si5351', 192);
    this.regs[0x00] = 0x00;      // 器件状态：SYS_INIT=0（已初始化）、无 LOL/LOS，bit3:0 = 版本号
    this.regs[0x03] = 0x00;      // 输出使能：复位值 0x00 = 四路都开着（写 1 才是关）
    this.regs[0x10] = 0x00;      // CLK0 控制：AN619 复位值就是 0x00（真片子读到的可能是出厂 NVM 的配置）
  }
  transact(addr, wr, rd){
    const out = super.transact(addr, wr, rd);
    // 0xB1(PLL 软复位) 是自清位：写进去 0xAC，回读就已经是 0 了（真器件如此）
    if (wr.length && addr.length && addr[0] === 0xb1) this.regs[0xb1] = 0x00;
    return out;
  }
}

// ============================================================================
// 假探针（接口与 AkaLinkHid 一致，只多出总线/器件）
// ============================================================================

export class MockI2cProbe {
  /**
   * @param {{pendingExtra?:number, now?:()=>number, devices?:Array}} opts
   *   `pendingExtra` = XFER 之后还要被 RESULT 轮询几次才回结果（默认 1：逼主机真的轮询）
   */
  constructor(opts = {}){
    this.device = { productName: 'akaLinkPro (mock I2C)', serialNumber: 'MOCK-I2C', opened: true };
    this.now = opts.now || (() => Date.now());
    this.pendingExtra = opts.pendingExtra ?? 1;
    this.devices = new Map();
    this.cfg = { sclHz: 100000, pullup: 0, retries: 0 };
    this.enabled = false;
    this.pending = null;          // {kind:'xfer'|'scan', ...}
    this.pendingLeft = 0;
    this.lastResult = null;       // {err, data}
    this.counters = { framesOk: 0, framesErr: 0, bytesTx: 0, bytesRx: 0, nackAddr: 0, nackData: 0, timeouts: 0, busRecover: 0, lastTicks: 0 };
    this.calls = [];
    this.onDisconnect = null;
    /** 测试用：强制总线被拉死（SDA/SCL 常低）*/
    this.busStuck = false;

    const devs = opts.devices || [new Eeprom24C02(0x50), new Mpu6050(0x68, this.now), new Ads1115(0x48, this.now), new Si5351(0x60)];
    for (const d of devs) this.devices.set(d.addr, d);
  }

  static supported(){ return true; }
  get connected(){ return true; }
  get label(){ return 'akaLinkPro (假探针 · I2C)'; }
  get isProbe(){ return true; }

  async request(){ this.calls.push('request'); return this.device; }
  async reconnect(){ this.calls.push('reconnect'); return this.device; }
  async close(){ this.calls.push('close'); this.pending = null; }

  /** 真·实际生效档位（0/≤100k→100k，≤400k→400k，否则 1M）*/
  actualSclHz(){
    const hz = this.cfg.sclHz | 0;
    if (hz <= 100000) return 100000;
    if (hz <= 400000) return 400000;
    return 1000000;
  }

  get countersObj(){
    return { ...this.counters, actualSclHz: this.actualSclHz() };
  }

  statusWord(){
    let w = 0;
    if (this.enabled) w |= ST.ENABLED;
    if (this.pending) w |= ST.PENDING;
    if (!this.pending) w |= ST.BUS_OK;
    // 线电平：被拉死时 SDA 常低；其余按"上拉把它拉起来了"给 1
    const sda = this.busStuck ? 0 : 1;
    const scl = this.busStuck ? (this.enabled ? 1 : 0) : 1;
    if (sda) w |= ST.SDA;
    if (scl) w |= ST.SCL;
    w |= (this.lastResult?.err ?? 0) << ST_SHIFT_ERR;
    w |= (this.counters.framesOk & 0xff) << ST_SHIFT_DONE;
    return w >>> 0;
  }

  _resp(action, cmdRc = 0, data = new Uint8Array(0)){
    let st = this.statusWord();
    st = (st | (cmdRc << ST_SHIFT_CMD)) >>> 0;
    const out = new Uint8Array(7 + data.length);
    out[0] = 2 + data.length;          // 长度（照文档填，固件不校验）
    out[1] = 0x36;
    out[2] = action;
    out[3] = st & 0xff; out[4] = (st >>> 8) & 0xff; out[5] = (st >>> 16) & 0xff; out[6] = (st >>> 24) & 0xff;
    out.set(data, 7);
    return out;
  }
  _u32arr(values){
    const b = new Uint8Array(values.length * 4);
    const dv = new DataView(b.buffer);
    values.forEach((v, i) => dv.setUint32(i * 4, v >>> 0, true));
    return b;
  }

  dev(addr){ return this.devices.get(addr & 0x7f) || null; }

  _doXfer(x){
    const d = this.dev(x.dev);
    if (!d){ this.counters.nackAddr++; return { err: E.NO_ADDR, data: new Uint8Array(0) }; }
    let r;
    try { r = d.transact(x.addr, x.wr, x.rd, this.now()); }
    catch (e){ return { err: E.STATE, data: new Uint8Array(0) }; }
    if (r && r.err != null){
      if (r.err === E.NO_ADDR) this.counters.nackAddr++;
      else if (r.err === E.NO_ACK) this.counters.nackData++;
      return { err: r.err, data: new Uint8Array(0) };
    }
    const data = r instanceof Uint8Array ? r : new Uint8Array(0);
    this.counters.bytesTx += x.wr.length;
    this.counters.bytesRx += data.length;
    // 粗略耗时：100 kHz 下每字节 9 拍 ≈ 90 µs，再乘档位倍数
    const bits = 9 * (1 + x.addr.length + x.wr.length + data.length) + 2;
    const usPerBit = this.actualSclHz() === 1000000 ? 1 : this.actualSclHz() === 400000 ? 2.5 : 10;
    this.counters.lastTicks = Math.round(bits * usPerBit * 24);
    return { err: E.OK, data };
  }

  _doScan(){
    const bm = new Uint8Array(SCAN_BYTES);
    for (const addr of this.devices.keys()){
      if (addr < SCAN_FIRST || addr > SCAN_LAST) continue;
      const i = addr - SCAN_FIRST;
      bm[i >> 3] |= 1 << (i & 7);
    }
    return { err: E.OK, data: bm };
  }

  _runPending(){
    const p = this.pending;
    this.pending = null;
    const res = p.kind === 'scan' ? this._doScan() : this._doXfer(p.x);
    if (res.err === E.OK) this.counters.framesOk++;
    else this.counters.framesErr++;
    this.lastResult = res;
    return res;
  }

  /**
   * 与 AkaLinkHid.xfer(cmd, data, timeout) 同签名。data = `[action, ...参数]`。
   * 响应布局与真机一致：res[0]=长度 res[1]=0x36 res[2]=action res[3..6]=状态字 res[7..]=数据。
   */
  async xfer(cmd, data = new Uint8Array(0), timeout = 3000){
    void timeout;
    if (cmd !== 0x36) throw new Error(`假探针只实现 HID 0x36（收到 0x${cmd.toString(16)}）`);
    const action = data[0];
    this.calls.push(`act:${action}`);
    switch (action){
      case ACT.STATUS:
        return this._resp(action, 0, this._u32arr([
          this.counters.framesOk, this.counters.framesErr, this.counters.bytesTx, this.counters.bytesRx,
          this.counters.nackAddr, this.counters.nackData, this.counters.timeouts, this.counters.busRecover,
          this.actualSclHz(), this.counters.lastTicks,
        ]));
      case ACT.ENABLE:
        this.enabled = !!data[1];
        if (!this.enabled){ this.pending = null; }
        return this._resp(action);
      case ACT.RESET:
        this.pending = null;
        this.counters = { framesOk: 0, framesErr: 0, bytesTx: 0, bytesRx: 0, nackAddr: 0, nackData: 0, timeouts: 0, busRecover: 1, lastTicks: 0 };
        this.lastResult = null;
        this.busStuck = false;
        for (const d of this.devices.values()) d.busyUntil = 0;
        return this._resp(action);
      case ACT.SET_CFG: {
        const c = parseCfg(data.subarray(1, 1 + CFG_SIZE));
        if (c.flags !== 0) return this._resp(action, E.RANGE);
        this.cfg = { sclHz: c.sclHz, pullup: c.pullup, retries: c.retries };
        return this._resp(action);
      }
      case ACT.GET_CFG: {
        const b = new Uint8Array(CFG_SIZE);
        b.set(packCfg(this.cfg), 0);
        new DataView(b.buffer).setUint32(8, this.actualSclHz(), true);
        return this._resp(action, 0, b);
      }
      case ACT.XFER: {
        if (!this.enabled) return this._resp(action, E.DISABLED);
        if (this.pending) return this._resp(action, E.BUSY);
        const flags = data[1], dev = data[2], addrLen = data[3], wrLen = data[4], rdLen = data[5];
        if (flags !== 0 || dev & 0x80 || addrLen > 4 || wrLen > 51 || rdLen > 54) return this._resp(action, E.RANGE);
        const x = {
          dev, addr: Array.from(data.subarray(6, 6 + addrLen)),
          wr: Array.from(data.subarray(10, 10 + wrLen)), rd: rdLen,
        };
        this.pending = { kind: 'xfer', x };
        this.pendingLeft = this.pendingExtra;
        return this._resp(action, 0);
      }
      case ACT.SCAN: {
        if (!this.enabled) return this._resp(action, E.DISABLED);
        if (this.pending) return this._resp(action, E.BUSY);
        this.pending = { kind: 'scan' };
        this.pendingLeft = this.pendingExtra;
        return this._resp(action, 0);
      }
      case ACT.RESULT: {
        if (this.pending){
          if (this.pendingLeft > 0){ this.pendingLeft--; return this._resp(action, 0); }   // 还在做
          const r = this._runPending();
          const out = new Uint8Array(2 + r.data.length);
          out[0] = r.err; out[1] = r.data.length; out.set(r.data, 2);
          return this._resp(action, 0, out);
        }
        const r = this.lastResult || { err: E.OK, data: new Uint8Array(0) };
        const out = new Uint8Array(2 + r.data.length);
        out[0] = r.err; out[1] = r.data.length; out.set(r.data, 2);
        return this._resp(action, 0, out);
      }
      case ACT.PINTEST: {
        let v = 0;
        if (!this.busStuck) v |= 1 | 2;              // 空闲 SDA/SCL 都是 1
        if (this.cfg.pullup) v |= (1 << 2) | (1 << 3);
        v |= (1 << 16) | (1 << 17);                  // 事务中确实驱动过总线（不做假）
        return this._resp(action, 0, this._u32arr([v]));
      }
      case ACT.DBG: {
        const words = [
          this.enabled ? 0x00000001 : 0, this.pending ? 0x00000020 : 0, 0, 0, 0, 0,
          0x0001_0001, 0x0001_0001, this.cfg.sclHz, this.actualSclHz(),
          this.statusWord(), this.counters.framesOk, 0,
        ];
        return this._resp(action, 0, this._u32arr(words));
      }
      case ACT.BITPROBE: {
        const addr = data[1] & 0x7f;
        const present = !!this.dev(addr);
        const bits = (addr & 0x7f) | (present ? 0 : 1 << 8) | (1 << 9) | (1 << 10);
        return this._resp(action, present ? 0 : E.NO_ADDR, this._u32arr([bits]));
      }
      default:
        return this._resp(action, E.BAD_FRAME);
    }
  }
}
