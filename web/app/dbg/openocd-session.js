import { OpenOcdTargetSession } from '../core/openocd-session.js';
import { isHpm6800BulkSram, OpenOcdProbe } from '../core/openocd-probe.js';
import { waitMs } from '../core/pace.js';
import { hpmBoard } from '../targets/hpm/porting.js';
import { DebugSession } from './session.js';
import { elfProgram } from './image.js';
import { hpmFlashGeometry, hpmFlashPlan } from './openocd-flash.js';
import { RV_ARCH } from './rv.js';

const TARGET = 'hpm6880.cpu0';
const ILM_END = 0x40000;
const REGISTERS = 'zero ra sp gp tp t0 t1 t2 s0 s1 a0 a1 a2 a3 a4 a5 a6 a7 s2 s3 s4 s5 s6 s7 s8 s9 s10 s11 t3 t4 t5 t6 pc'.split(' ');
const hpm = hpmBoard('hpm6800evk');
const openOcdRegName = name => name === 's0' ? 'fp' : name;

function assertRam(addr, length){
  if (!Number.isInteger(addr) || !Number.isInteger(length) || addr < 0 || length < 0 || length > ILM_END - addr)
    throw new Error('RAM 模式只允许写入 HPM6880 的 ILM（0x00000000–0x0003FFFF）');
}

function ramPlan(buffer, symbols){
  const bytes = new Uint8Array(buffer);
  const { wide, little, type, machine, entry, headers, range } = elfProgram(bytes);
  if (wide || !little || type !== 2 || machine !== 243)
    throw new Error('请选择为 HPM6880 ILM 链接的小端 RISC-V ELF32 可执行文件');
  const loads = headers.filter(h => h.type === 1 && (h.memory || h.size)).sort((a,b) => a.physical-b.physical);
  let end = 0;
  for (const h of loads){
    range(h.offset, h.size);
    assertRam(h.physical, h.memory);
    if (h.physical !== h.virtual || h.physical < end || h.size > h.memory)
      throw new Error('ELF 的 PT_LOAD 段必须无重叠，加载和运行地址必须一致');
    end = h.physical + h.memory;
  }
  const executable = addr => addr % 2 === 0 &&
    loads.some(h => (h.flags & 1) && h.physical <= addr && addr + 2 <= h.physical + h.size);
  const main = symbols.find('main');
  if (!executable(entry) || main?.kind !== 'func' || !executable(main.addr) || !symbols.lines?.at(main.addr))
    throw new Error('RAM ELF 需要可执行的入口，以及带 DWARF 行号的 main（编译时加 -g）');
  return { entry, main:main.addr, segments:loads.map(h => {
    const data = new Uint8Array(h.memory);
    data.set(bytes.subarray(h.offset, h.offset + h.size));
    return { addr:h.physical, data };
  }) };
}

export class OpenOcdDebugSession extends DebugSession {
  constructor(){
    super();
    this.arch = RV_ARCH;
    this.target = TARGET;
    this.sbaFence = hpm.memory.sbaReadForbidden;
    this.isRiscv = true;
    this.backendName = 'OpenOCD Wasm / WebUSB';
    this.caps = { numCode:8, rev:2 };
    this._verifiedFlash = null;
  }

  async connect({ cfg, device }){
    this.target = cfg.target;
    this.sbaFence = cfg.sbaFence ?? (cfg.script || cfg.raw ? [] : hpm.memory.sbaReadForbidden);
    this.hpm6800 = cfg.boardId === 'hpm6800evk';
    this.hpmSdk = this.hpm6800 && !cfg.script;
    if (cfg.script || cfg.raw){
      const memory = await OpenOcdProbe.open(device, { cfg, onLog:line => this._log(line, 'dim') });
      this.link = memory.link;
      this.memory = memory;
      this.probe = memory;
      this.name = cfg.name;
      this.idcode = memory.idcode;
      this.clockHz = memory.clockHz;
      try { await this.refresh(); }
      catch (error){ await this.disconnect(); throw error; }
      return;
    }
    const link = new OpenOcdTargetSession(line => this._log(line, 'dim'));
    try {
      await link.open(cfg, device);
      if (await link.command('target names') !== TARGET) throw new Error('CFG 目标不匹配');
      await link.command('init');
      const id = await link.command('capture {scan_chain}');
      if (!/\b0x1000563d\b/i.test(id)) throw new Error('JTAG 未识别 HPM6800EVK 的 TAP');
      this.link = link;
      this.memory = new OpenOcdProbe(device, { ...cfg, sbaFence:this.sbaFence });
      this.memory.link = link;
      this.probe = { halt:() => this._command('halt') };
      this.name = TARGET;
      this.idcode = 0x1000563d;
      this.clockHz = cfg.clockKhz * 1000;
      await this.refresh();
      this._log('OpenOCD Wasm 已通过 WebUSB 连接 HPM6800EVK', 'ok');
    } catch (error){
      this.link = null;
      this.probe = null;
      await link.close().catch(() => {});
      throw error;
    }
  }

  async disconnect(){
    await this._memoryTask;
    const link = this.link;
    if (link?.worker && this.bps.length) await this.bpClear();
    await link?.close();
    this.link = null;
    this.memory = null;
    this._verifiedFlash = null;
    this._xipFallbackLogged = false;
    this.probe = null;
    this.halted = false;
    this.bps = [];
    this.bpNotes.clear();
    this.regs = [];
    this.clearFrames();
  }

  _command(line, timeoutMs){ return this.link.command(line, timeoutMs); }

  async programFlashElf(buffer){
    if (!this.hpmSdk) throw Error('此命令需要 HPM6800EVK 原版 SDK CFG，也可使用烧录页');
    const { entry, main, ranges } = hpmFlashPlan(buffer);
    return this.exclusive(async () => {
      if (!this.connected || this.bps.length) throw new Error('请先连接 HPM6800EVK 并移除断点');
      this._verifiedFlash = null;
      await this.ensureHalted();
      const geometry = await this._probeFlash(ranges);
      this._log(`已探测 HPM XPI Flash：${geometry.size} B，${geometry.sectorSize} B/扇区`, 'ok');
      const prepared = [];
      for (const range of ranges){
        const data = await this._readFlashRange(range);
        for (const section of range.sections) data.set(section.data, section.offset);
        prepared.push({ range, data });
      }
      for (const { range, data } of prepared){
        const { first, last, offset } = range;
        this._log(`Flash 擦写扇区 ${first}–${last}`, 'dim');
        await this.link.request('flashFile', { input:data });
        try {
          await this._command(`flash erase_sector 0 ${first} ${last}`, 180000);
          await this._command(`flash write_bank 0 /flash-input.bin ${offset}`, 180000);
        } finally {
          await this.link.request('flashFile').catch(() => {});
        }
      }
      const verified = [];
      for (const { range, data } of prepared){
        const readback = await this._readFlashRange(range);
        const mismatch = readback.findIndex((byte, i) => byte !== data[i]);
        if (mismatch !== -1) throw new Error('Flash 回读不一致，地址 0x' + (0x80000000 + range.offset + mismatch).toString(16));
        const digest = await crypto.subtle.digest('SHA-256', readback);
        const sha256 = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('');
        verified.push({ first:range.first, last:range.last, offset:range.offset, bytes:range.length, sha256 });
      }
      this._verifiedFlash = true;
      this._log('Flash 已烧录并逐字节读回校验', 'ok');
      return { ...geometry, entry, main, verified };
    });
  }

  async _probeFlash(ranges){
    await this._command('flash probe 0', 30000);
    // hpm_xpi prints the geometry but returns "unsupported" for protection queries.
    const info = await this._command(
      'if {[catch {flash info 0 sectors} flashGeometry] && $errorCode ne {OpenOCD -908}} {error $flashGeometry}; set flashGeometry',
      30000,
    );
    return hpmFlashGeometry(info, ranges);
  }

  async verifyFlashElf(buffer){
    if (!this.hpmSdk) throw Error('此命令需要 HPM6800EVK 原版 SDK CFG，也可使用烧录页');
    const { entry, main, ranges } = hpmFlashPlan(buffer);
    return this.exclusive(async () => {
      if (!this.connected || this.bps.length) throw new Error('请先连接 HPM6800EVK 并移除断点');
      this._verifiedFlash = null;
      await this.ensureHalted();
      await this._probeFlash(ranges);
      let bytes = 0;
      for (const range of ranges){
        const readback = await this._readFlashRange(range);
        for (const section of range.sections){
          if (!section.data.every((byte, i) => readback[section.offset + i] === byte))
            throw new Error('Flash 镜像回读不匹配');
          bytes += section.data.length;
        }
      }
      this._verifiedFlash = true;
      return { bytes, entry, main };
    });
  }

  async _readFlashRange({ offset, length }){
    await this._command(`flash read_bank 0 /flash-readback.bin ${offset} ${length}`, 180000);
    const { bytes } = await this.link.request('flashFile', { read:true });
    if (bytes.length !== length) throw new Error('Flash 回读长度不匹配');
    return bytes;
  }

  async bootVerifiedFlash(){
    return this.exclusive(async () => {
      if (!this.connected || !this._verifiedFlash || this.bps.length) throw new Error('请先在当前会话校验 Flash');
      const result = await this._systemReset(false);
      this._log('已观测到系统复位标志和复位后运行状态', 'ok');
      return { ...result, running:true };
    });
  }

  async _systemReset(halt){
    if (!this.hpm6800){
      this.clearFrames();
      await this._command(halt ? 'reset halt' : 'reset run');
      await this.refresh();
      if (this.halted !== halt) throw Error('OpenOCD 未确认复位后的目标状态');
      return { halted:this.halted };
    }
    await this.ensureHalted();
    this.clearFrames();
    // A NOP through the program buffer flushes and invalidates OpenOCD's
    // register cache before the HPM system reset pulse.
    await this._command(`${this.target} riscv exec_progbuf 0x00000013`);
    const readStatus = async () => Number(await this._command(`${this.target} riscv dmi_read 0x11`));
    const writeControl = value => this._command(`${this.target} riscv dmi_write 0x10 0x${value.toString(16)}`);
    const ackHaveReset = 0x10000000;
    const anyHaveReset = 0x40000;
    const allHalted = 0x200;
    const allRunning = 0x800;
    const active = 1;
    const haltRequest = halt ? 0x80000000 : 0;
    await writeControl(ackHaveReset + active);
    if (await readStatus() & anyHaveReset) throw new Error('旧复位标志未清除');
    try {
      await writeControl(haltRequest + active + 2);
      await waitMs(50);
    } finally {
      await writeControl(haltRequest + active);
    }
    const control = Number(await this._command(`${this.target} riscv dmi_read 0x10`));
    if ((control & 3) !== active) throw new Error('未确认系统复位已释放');
    const mask = anyHaveReset | (halt ? allHalted : allRunning);
    const deadline = performance.now() + 3000;
    let status = await readStatus();
    while ((status & mask) !== mask && performance.now() < deadline){
      await waitMs(10);
      status = await readStatus();
    }
    if ((status & mask) !== mask) throw new Error('未确认系统复位后的目标状态');
    await writeControl(ackHaveReset + active);
    await this.refresh();
    if (this.halted !== halt) throw new Error('OpenOCD 未确认系统复位后的目标状态');
    return { dmcontrol:control, dmstatus:status };
  }

  async _pollHalted(){
    await this._command('poll');
    const state = await this._command(this.target + ' curstate');
    if (state !== 'halted' && state !== 'running') throw new Error('OpenOCD 目标状态：' + state);
    return state === 'halted';
  }

  async refresh(){
    if (!this.connected) return this.statusInfo();
    this.halted = await this._pollHalted();
    if (this.halted) this.pc = await this.readReg('pc');
    if (this._frames && (!this.halted || this.pc !== this._framePc)) this.clearFrames();
    return this.statusInfo();
  }

  async ensureHalted(){
    if (!await this._pollHalted()) await this._command('halt');
    await this.refresh();
    if (!this.halted) throw new Error('OpenOCD 未能停住目标');
  }

  async _clearDfsr(){}
  async run(){
    await this._command('resume');
    this.clearFrames();
    this.halted = false;
  }
  async step(){
    if (!this.halted) throw new Error('请先暂停目标');
    await this._command('step');
    await this.refresh();
  }
  async resetHalt(){
    await this.ensureHalted();
    const breakpoints = this.bps.map(addr => [addr, this.bpNotes.get(addr)]);
    try {
      await this.bpClear();
      await this._systemReset(true);
    } finally {
      if (breakpoints.length){
        await this.ensureHalted();
        for (const [addr, note] of breakpoints) await this.bpAdd(addr, note);
      }
    }
    await this.refreshRegs();
    return 'OpenOCD 系统复位 + 停住';
  }

  async resetRun(){
    await this.resetHalt();
    await this.run();
    return 'OpenOCD 系统复位 + 运行';
  }
  async verifyClock(){ return { ok:true, hz:this.clockHz, checked:false }; }

  regInfo(name){
    const key = String(name).toLowerCase();
    const index = /^x(?:[0-9]|[12][0-9]|3[01])$/.test(key) ? Number(key.slice(1)) : -1;
    const canonical = REGISTERS[index] || (key === 'fp' ? 's0' : key);
    return REGISTERS.includes(canonical) || ['misa','dcsr','mstatus','mcause','mtval'].includes(canonical)
      ? { name:canonical, bits:32 } : null;
  }

  async readRegs(names){
    const openOcdNames = names.map(name => openOcdRegName(this.regInfo(name)?.name));
    if (openOcdNames.some(name => !name)) throw new Error('未知寄存器');
    const out = await this._command(this.target + ' get_reg {' + openOcdNames.join(' ') + '}');
    const entries = new Map([...out.matchAll(/(\w+)\s+(0x[0-9a-f]+)/gi)].map(([,name,value]) => [name, Number(value)]));
    if (openOcdNames.some(name => !entries.has(name))) throw new Error('OpenOCD 寄存器回包不完整：' + out);
    return new Map(names.map((name, i) => [name, entries.get(openOcdNames[i])]));
  }

  async readReg(name){
    const reg = this.regInfo(name);
    if (!reg) throw new Error('未知寄存器：' + name);
    return (await this.readRegs([reg.name])).get(reg.name);
  }

  async writeReg(name, value){
    const reg = this.regInfo(name);
    if (!reg || !Number.isInteger(value)) throw new Error('无效的寄存器写入');
    await this._command(this.target + ' set_reg {' + openOcdRegName(reg.name) + ' ' + (value >>> 0) + '}');
    if (reg.name === 'pc') this.pc = value >>> 0;
    this.clearFrames();
  }

  async refreshRegs(){
    if (!this.halted) return this.regs;
    const values = await this.readRegs(REGISTERS);
    const prev = new Map(this.regs.map(r => [r.name,r.value]));
    this.regs = REGISTERS.map(name => ({ name, value:values.get(name), changed:prev.has(name) && prev.get(name) !== values.get(name) }));
    this.pc = values.get('pc');
    return this.regs;
  }

  async memRead(addr, len){
    if (!Number.isInteger(addr) || !Number.isInteger(len) || addr < 0 || len < 0 || addr + len > 0x100000000)
      throw new Error('内存地址或长度超出 32 位地址空间');
    if (len && this.sbaFence.some(range => addr < range.end && addr + len > range.start)){
      const inside = this.sbaFence.some(range => addr >= range.start && addr + len <= range.end);
      const bytes = inside ? this.sym?.codeBytes?.(addr, len) : null;
      if (!bytes || bytes.length !== len) throw new Error('XIP/flash 数据不可用：载入的 ELF 只读段未完整覆盖请求');
      if (!this._xipFallbackLogged){
        this._xipFallbackLogged = true;
        this._log('XIP/flash 使用已载入 ELF 的只读镜像，请核对 ELF 与目标固件一致。', 'warn');
      }
      return bytes;
    }
    return this._withMemory(() => this.memory.readMem(addr, len), addr, len);
  }

  async memWrite(addr, bytes){
    if (!(bytes instanceof Uint8Array)) throw Error('内存写入需要字节数组');
    return this._withMemory(() => this.memory.writeMem(addr, bytes), addr, bytes.length);
  }

  async _withMemory(fn, address, length){
    const task = (this._memoryTask || Promise.resolve()).then(() => this._accessMemory(fn, address, length));
    this._memoryTask = task.catch(() => {});
    return task;
  }

  async _accessMemory(fn, address, length){
    const memory = this.memory;
    if (await this._pollHalted() && !isHpm6800BulkSram(memory.cfg, address, length)) return fn();
    const prefix = this.target + ' riscv mem_access';
    const previous = await this._command(prefix);
    if (!/^(?:progbuf|sysbus|abstract)(?: (?:progbuf|sysbus|abstract))*$/.test(previous))
      throw Error('无法确认 OpenOCD RISC-V 内存访问方式');
    await this._command(prefix + ' sysbus');
    memory.physical = true;
    let failure;
    try { return await fn(); }
    catch (error){ failure = error; throw error; }
    finally {
      memory.physical = false;
      try { await this._command(prefix + ' ' + previous); }
      catch (cleanup){
        if (failure) throw new AggregateError([failure, cleanup], `${failure.message}；恢复内存访问方式失败：${cleanup.message}`);
        throw cleanup;
      }
    }
  }

  async bpAdd(addr, note = ''){
    const index = this.bps.indexOf(addr);
    if (index >= 0) return { index, warn:'这个地址上已经有断点了' };
    await this._command('bp ' + (addr >>> 0) + ' 2 hw');
    this.bps.push(addr);
    if (note) this.bpNotes.set(addr,note);
    return { index:this.bps.length-1 };
  }

  async bpDel(addr){
    const index = this.bps.indexOf(addr);
    if (index < 0) return false;
    await this._command('rbp ' + (addr >>> 0));
    this.bps.splice(index,1);
    this.bpNotes.delete(addr);
    return true;
  }

  async bpClear(){
    const bps = this.bps.slice();
    for (const addr of bps) await this.bpDel(addr);
    return bps.length;
  }

  async verifyBreakpoints(){
    const output = await this._command('bp');
    const installed = output ? output.split(/\r?\n/).map(line => {
      const address = /^Hardware breakpoint\(IVA\): addr=(0x[0-9a-f]+)/i.exec(line)?.[1];
      if (!address) throw new Error('无法识别 OpenOCD 断点：' + line);
      return Number(address);
    }) : [];
    if (installed.length !== this.bps.length || this.bps.some(addr => !installed.includes(addr)))
      throw new Error('OpenOCD 实际断点与调试页不一致：' + output);
    return installed;
  }

  async _withBpCleared(addr, fn){
    const hit = this._bpAt(addr);
    if (hit === undefined) return await fn();
    const index = this.bps.indexOf(hit);
    const note = this.bpNotes.get(hit);
    await this.bpDel(hit);
    try {
      return await fn();
    } finally {
      await this.bpAdd(hit, note);
      const restored = this.bps.indexOf(hit);
      this.bps.splice(restored, 1);
      this.bps.splice(index, 0, hit);
    }
  }

  async _startRamDebug(buffer, symbols){
    const plan = ramPlan(buffer, symbols);
    await this.ensureHalted();
    await this.bpClear();
    for (const {addr,data} of plan.segments){
      await this.memWrite(addr,data);
      const check = await this.memRead(addr,data.length);
      if (!check.every((byte,i) => byte === data[i])) throw new Error('ILM 写入后读回校验失败');
    }
    await this.writeReg('pc', plan.entry);
    await this.runTo(plan.main, { label:'main' });
    if (!this.halted || this.pc !== plan.main) throw new Error('未停在 main');
    return plan.main;
  }
}
