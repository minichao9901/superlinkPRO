import { OpenOcdTargetSession } from './openocd-session.js';
import { makeOpenOcdProfile, ocdHex } from './openocd-profile.js';

const FILTER = { vendorId:0x0d28 };
const MAX_TRANSFER = 2048;
const ARM_REGS = [...Array.from({ length:13 }, (_, index) => `r${index}`), 'sp', 'lr', 'pc', 'xpsr', 'msp', 'psp'];
const CFBP = ['primask', 'basepri', 'faultmask', 'control'];

export function isHpm6800BulkSram(cfg, address, length){
  if (cfg?.boardId !== 'hpm6800evk' || length < 4096 || address % 4 || length % 4) return false;
  // ILM and the noncacheable AXI SRAM bank. Keep cacheable memory and MMIO
  // on their original access paths.
  return address >= 0 && address + length <= 0x40000 ||
    address >= 0x01240000 && address + length <= 0x01280000;
}

export async function selectOpenOcdDevice({ all = false, forcePick = false } = {}){
  const usb = globalThis.navigator?.usb;
  if (!usb) throw Error('OpenOCD Wasm 需要支持 WebUSB 的浏览器');
  const devices = forcePick ? [] : (await usb.getDevices()).filter(d => d.vendorId === FILTER.vendorId);
  return devices.length === 1 ? devices[0] : usb.requestDevice({ filters:all ? [] : [FILTER] });
}

function checkRange(address, length){
  if (!Number.isInteger(address) || address < 0 || address > 0xffffffff ||
      !Number.isInteger(length) || length < 0 || address + length > 0x100000000)
    throw Error('内存访问超出 32 位地址空间');
}

function widthAt(address, count){
  if (!(address % 4) && count >= 4) return 4;
  return !(address % 2) && count >= 2 ? 2 : 1;
}

export class OpenOcdProbe {
  static supported(){ return !!globalThis.navigator?.usb; }
  static async authorized(){ return await navigator.usb.getDevices(); }

  static async open(device, { cfg = makeOpenOcdProfile(), onLog, runningMemory = false } = {}){
    const probe = new this(device, cfg, onLog, runningMemory);
    await probe.connect();
    return probe;
  }

  constructor(device, cfg, onLog, runningMemory){
    this.device = device;
    this.cfg = cfg;
    this.onLog = onLog;
    this.name = 'OpenOCD Wasm / WebUSB';
    this.clockHz = cfg.clockKhz * 1000;
    this.isRiscv = cfg.arch === 'riscv';
    this.physical = this.isRiscv && runningMemory;
    this.skipNames = this.isRiscv;
    // Commit the read cursor between shorter reads on the generic
    // RISC-V and HPM6800 RTT paths, even when the whole ring is full.
    this.rttLivePreview = this.physical && !cfg.boardId;
    this.rttPollLimit = (this.rttLivePreview || this.physical && cfg.boardId === 'hpm6800evk') ? 8192 : undefined;
  }

  get _faulted(){ return !!this.link && !this.link.worker; }

  async connect(){
    const link = new OpenOcdTargetSession(line => this.onLog?.(line));
    try {
      await link.open(this.cfg, this.device);
      const targets = (await link.command('target names')).split(/\s+/);
      if (!targets.includes(this.cfg.target)) throw Error('OpenOCD CFG 目标不匹配');
      if (this.cfg.raw) await link.command(`targets ${this.cfg.target}`);
      await link.command('init');
      if (this.cfg.raw){
        await link.command(`targets ${this.cfg.target}`);
        // The workbench controls one selected core. A peer can be parked in
        // ROM and make a group resume fail even after the selected core halted.
        if (this.cfg.cores?.length > 1 && !this.isRiscv) await link.command('cortex_m smp off');
      }
      let status = await link.command(`${this.cfg.target} curstate`);
      // Cortex-M examination can finish before the first DHCSR poll updates curstate.
      if (!this.isRiscv && status === 'unknown'){
        await link.command('poll');
        status = await link.command(`${this.cfg.target} curstate`);
      }
      if (!['running', 'halted', 'reset', 'sleeping'].includes(status))
        throw Error('OpenOCD 未识别目标：' + status);
      if (this.isRiscv){
        const chain = await link.command('capture {scan_chain}');
        const id = chain.match(/^\s*\d+\s+\S+\s+Y\s+(0x[0-9a-f]{8})\b/im);
        this.idcode = Number(id?.[1] ?? 0);
        if (!this.idcode || this.idcode === 0xffffffff || this.cfg.tapIdcode != null && this.idcode !== this.cfg.tapIdcode)
          throw Error('OpenOCD 未识别 CFG 中的 JTAG TAP');
        if (this.physical) await link.command(`${this.cfg.target} riscv mem_access sysbus`);
      } else {
        const dap = `${this.cfg.target} cget -dap`;
        this.idcode = Number(await link.command(`capture {[${dap}] dpreg 0}`)) >>> 0;
        if (!this.idcode || this.idcode === 0xffffffff || this.cfg.dpidr != null && this.idcode !== this.cfg.dpidr)
          throw Error('OpenOCD 未确认目标 SWD DPIDR');
      }
      this.link = link;
      this._f103HighDensity = undefined;
    } catch (error){
      await link.close().catch(() => {});
      throw error;
    }
  }

  async command(line, timeoutMs){
    if (!this.link) throw Error('OpenOCD 会话未连接');
    return await this.link.command(line, timeoutMs);
  }

  targetCommand(line){ return this.command(`${this.cfg.target} ${line}`); }

  async disconnect(){
    await this.link?.close();
    this.link = null;
  }

  async recoverDebugPort(){
    await this.disconnect();
    await this.connect();
  }

  async _targetInit(){ await this.recoverDebugPort(); }
  async setClock(hz){ await this.command(`adapter speed ${Math.round(hz / 1000)}`); this.clockHz = hz; }
  async isHalted(){ await this.command('poll'); return await this.targetCommand('curstate') === 'halted'; }
  async halt(){ await this.command('halt'); }
  async run(){ await this.command('resume'); }
  async reset(){ await this.command('reset run'); return 'OpenOCD 目标复位'; }

  async armRegisters(names){
    await this.command('poll');
    const output = await this.targetCommand(`get_reg {${names.join(' ')}}`);
    const values = new Map(Array.from(output.matchAll(/^(\S+)\s+(0x[\da-f]+)\s*$/gim),
      ([, name, value]) => [name, Number(value)]));
    if (names.some(name => !values.has(name))) throw Error('OpenOCD 寄存器回读不完整');
    return values;
  }

  async _cfbpRegisters(){
    if (this.armV6m === undefined){
      const part = (await this._readWord(0xe000ed00) >>> 4) & 0xfff;
      this.armV6m = [0xc20, 0xc60].includes(part);
    }
    return this.armV6m ? ['primask', 'control'] : CFBP;
  }

  async regRead(selector){
    if (selector === 0x14){
      const values = await this.armRegisters(await this._cfbpRegisters());
      return CFBP.reduce((word, name, index) => word | ((values.get(name) ?? 0) << (index * 8)), 0) >>> 0;
    }
    const name = ARM_REGS[selector];
    if (!name) throw Error('OpenOCD 不支持此 Cortex-M 寄存器');
    return (await this.armRegisters([name])).get(name);
  }

  async regWrite(selector, value){
    const name = ARM_REGS[selector];
    if (!name && selector !== 0x14) throw Error('OpenOCD 不支持此 Cortex-M 寄存器');
    const pairs = selector === 0x14 ? (await this._cfbpRegisters()).map(reg =>
      `${reg} ${(value >>> (CFBP.indexOf(reg) * 8)) & 255}`) : [`${name} ${ocdHex(value >>> 0)}`];
    await this.targetCommand(`set_reg {${pairs.join(' ')}}`);
  }

  _checkFence(address, length){
    if (length && this.cfg.sbaFence?.some(range => address < range.end && address + length > range.start))
      throw Error('目标 Flash/XIP 区域禁止通过 SBA 访问');
  }

  async _readUnits(address, width, count){
    const out = await this.targetCommand(`read_memory ${ocdHex(address)} ${width * 8} ${count}${this.physical ? ' phys' : ''}`);
    const values = out ? out.split(/\s+/) : [];
    if (values.length !== count || values.some(value => !/^(?:0x[\da-f]+|\d+)$/i.test(value)))
      throw Error('OpenOCD 内存回读格式或长度不匹配');
    return values.map(Number);
  }

  async _usesF103SramImage(address, length){
    if (this.cfg?.family !== 'stm32f103' || this.cfg.raw || length < 4096 || address % 4 || length % 4 ||
        address < 0x20000000 || address + length > 0x20010000) return false;
    if (this._f103HighDensity === undefined){
      const device = await this._readWord(0xe0042000).catch(() => null);
      if (device === null) return false;
      this._f103HighDensity = (device & 0xfff) === 0x414;
    }
    return this._f103HighDensity;
  }

  async readMem(address, length){
    checkRange(address, length);
    const start = this.physical && length ? address - address % 4 : address;
    const end = this.physical && length ? Math.ceil((address + length) / 4) * 4 : address + length;
    this._checkFence(start, end - start);
    if ((this.physical && isHpm6800BulkSram(this.cfg, start, end - start)) ||
        await this._usesF103SramImage(start, end - start)){
      const { bytes } = await this.link.request('memoryImage', {
        address:start, length:end - start, ...(!this.physical && { physical:false }),
      });
      if (bytes?.length !== end - start) throw Error('OpenOCD 二进制内存回读长度不匹配');
      return bytes.subarray(address - start, address - start + length);
    }
    const data = new Uint8Array(end - start);
    const view = new DataView(data.buffer);
    for (let at = start; at < end;){
      const width = widthAt(at, end - at);
      const count = at % 4 ? 1 : Math.min(MAX_TRANSFER, end - at) / width | 0;
      const values = await this._readUnits(at, width, count);
      for (const value of values){
        if (width === 4) view.setUint32(at - start, value, true);
        else if (width === 2) view.setUint16(at - start, value, true);
        else view.setUint8(at - start, value);
        at += width;
      }
    }
    return data.subarray(address - start, address - start + length);
  }

  async writeMem(address, bytes){
    checkRange(address, bytes.length);
    this._checkFence(address, bytes.length);
    if ((this.physical && isHpm6800BulkSram(this.cfg, address, bytes.length)) ||
        await this._usesF103SramImage(address, bytes.length)){
      await this.link.request('memoryImage', {
        address, length:bytes.length, input:bytes, ...(!this.physical && { physical:false }),
      });
      return;
    }
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    for (let offset = 0; offset < bytes.length;){
      const at = address + offset, remaining = bytes.length - offset;
      const width = widthAt(at, remaining);
      if (this.physical && width < 4){
        const first = at - at % 4, n = Math.min(4 - at % 4, remaining);
        this._checkFence(first, 4);
        const [word] = await this._readUnits(first, 4, 1);
        const aligned = new DataView(new ArrayBuffer(4));
        aligned.setUint32(0, word, true);
        for (let i = 0; i < n; i++) aligned.setUint8(at % 4 + i, bytes[offset + i]);
        await this._writeUnits(first, 4, [aligned.getUint32(0, true)]);
        offset += n;
        continue;
      }
      const count = at % 4 ? 1 : Math.min(MAX_TRANSFER, remaining) / width | 0;
      const values = Array.from({ length:count }, (_, i) => {
        const index = offset + i * width;
        return width === 4 ? view.getUint32(index, true) : width === 2 ? view.getUint16(index, true) : bytes[index];
      });
      await this._writeUnits(at, width, values);
      offset += count * width;
    }
  }

  _writeUnits(address, width, values){
    return this.targetCommand(`write_memory ${ocdHex(address)} ${width * 8} {${values.join(' ')}}${this.physical ? ' phys' : ''}`);
  }

  readMemDiagnostic(address, length){ return this.readMem(address, length); }
  writeMemDiagnostic(address, bytes){ return this.writeMem(address, bytes); }
  async _readWord(address){ return (await this._readUnits(address, 4, 1))[0]; }
  async _dhcsr(value){ await this._writeUnits(0xe000edf0, 4, [value >>> 0]); }
}
