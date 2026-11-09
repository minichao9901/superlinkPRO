import { OpenOcdTargetSession } from '../core/openocd-session.js';
import { isHpm6800BulkSram, OpenOcdProbe } from '../core/openocd-probe.js';
import { waitMs } from '../core/pace.js';
import { hpmBoard } from '../targets/hpm/porting.js';
import { DebugSession } from './session.js';
import { elfProgram } from './image.js';
import { hpmFlashGeometry, hpmFlashPlan } from './openocd-flash.js';
import { readFlashRange, writeFlashRange } from '../flash/openocd-program.js';
import { RV_ARCH } from './rv.js';
import { watchSpec } from './dwt.js';

const TARGET = 'hpm6880.cpu0';
const ILM_END = 0x40000;
const REGISTERS = 'zero ra sp gp tp t0 t1 t2 s0 s1 a0 a1 a2 a3 a4 a5 a6 a7 s2 s3 s4 s5 s6 s7 s8 s9 s10 s11 t3 t4 t5 t6 pc'.split(' ');
const hpm = hpmBoard('hpm6800evk');
const openOcdRegName = name => name === 's0' ? 'fp' : name;

const ILM = Object.freeze({ start:0, end:ILM_END });
const fits = (window, addr, length) => window && addr >= window.start && addr + length <= window.end;
const MAX_RAM = 64 * 1024 * 1024;
const sameBytes = (a, b) => a.length === b.length && a.every((byte,i) => byte === b[i]);
const hex = addr => '0x' + addr.toString(16);

function ramPlan(buffer, symbols, window, stop){
  const bytes = new Uint8Array(buffer);
  const { wide, little, type, machine, entry, headers, range } = elfProgram(bytes);
  if (wide || !little || type !== 2 || machine !== 243)
    throw new Error('请选择链接到所声明 RAM 的小端 RISC-V ELF32 可执行文件');
  const loads = headers.filter(h => h.type === 1 && (h.memory || h.size)).sort((a,b) => a.virtual-b.virtual);
  const writes = [];
  let end = window.start;
  for (const h of loads){
    if (h.size) range(h.offset, h.size);
    if (!fits(window,h.virtual,h.memory) || (h.size && !fits(window,h.physical,h.size)))
      throw Error(`PT_LOAD ${hex(h.physical)} 超出声明的可写 RAM（含 BSS）`);
    if (h.virtual < end || h.size > h.memory) throw Error('ELF 的 PT_LOAD 运行范围重叠或段长度无效');
    if ((h.flags & 1) && h.physical !== h.virtual) throw Error('可执行段的加载和运行地址必须一致');
    end = h.virtual + h.memory;
    if (h.size) writes.push({addr:h.physical,length:h.size,data:bytes.subarray(h.offset,h.offset+h.size)});
    if (h.memory > h.size) writes.push({addr:h.virtual+h.size,length:h.memory-h.size});
  }
  writes.sort((a,b) => a.addr-b.addr);
  if (writes.some((w,i) => i>0 && w.addr < writes[i-1].addr+writes[i-1].length))
    throw Error('ELF 的 PT_LOAD 加载区或 BSS 清零区重叠');
  const executable = addr => addr % 2 === 0 &&
    loads.some(h => (h.flags & 1) && h.physical <= addr && addr + 2 <= h.physical + h.size);
  const main = symbols.find('main');
  if (!executable(entry)) throw Error('ELF 入口必须位于已加载的可执行段内');
  const mainAddr = main?.kind === 'func' && executable(main.addr) ? main.addr : null;
  if (!['auto','main','entry'].includes(stop)) throw Error('停点只能选择 auto / main / entry');
  if (stop === 'main' && mainAddr == null) throw Error('ELF 没有位于可执行加载段内的 main');
  return { entry, stop:stop === 'entry' ? entry : mainAddr ?? entry, writes };
}

export class OpenOcdDebugSession extends DebugSession {
  constructor(){
    super();
    this.arch = RV_ARCH;
    this.target = TARGET;
    this.sbaFence = hpm.memory.sbaReadForbidden;
    this.isRiscv = true;
    this.backendName = 'OpenOCD Wasm / WebUSB';
    this.caps = { numCode:null };
    this.bpDetails = new Map();
    this.ramWindow = null;
    this.dwt = new RiscvWatchpoints(this);
    this._verifiedFlash = null;
  }

  async connect({ cfg, device }){
    this.target = cfg.target;
    this.sbaFence = cfg.sbaFence ?? (cfg.script || cfg.raw ? [] : hpm.memory.sbaReadForbidden);
    this.hpm6800 = cfg.boardId === 'hpm6800evk';
    this.hpmSdk = this.hpm6800 && !cfg.script;
    this.importedCfg = !!cfg.raw;
    this.ramWindow = this.hpm6800 ? ILM : null;
    this.caps.numCode = null;
    this._triggersChecked = false;
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
    if (this.bpDetails.size && !link?.worker && [...this.bpDetails.values()].some(bp => bp.kind === 'software'))
      throw Error('调试链路已断开，无法确认软件断点原指令恢复；请用匹配的 RAM ELF 恢复程序');
    if (link?.worker){
      await this.bpClear();
      await this.dwt.clear();
    }
    await link?.close();
    this.link = null;
    this.memory = null;
    this._verifiedFlash = null;
    this._xipFallbackLogged = false;
    this.probe = null;
    this.halted = false;
    this.bps = [];
    this.bpDetails.clear();
    this.bpNotes.clear();
    this.dwt.items = [];
    this.ramWindow = null;
    this.regs = [];
    this.clearFrames();
  }

  _command(line, timeoutMs){ return this.link.command(line, timeoutMs); }

  configureRamWindow(start, length){
    if (!this.connected || !this.importedCfg || this.hpm6800) throw Error('请先连接使用自定义 CFG 的 RISC-V 目标');
    if (this.bpDetails.size) throw Error('修改 RAM 范围前请清空断点');
    if (!Number.isInteger(start) || !Number.isInteger(length) || start < 0 || start % 2 ||
        length < 2 || length > MAX_RAM || start + length > 0x100000000)
      throw Error('RAM 起始地址须为偶数，长度为 2 B 至 64 MiB，且不能越过 32 位地址空间');
    const end = start + length;
    if (this.sbaFence.some(r => start < r.end && end > r.start)) throw Error('RAM 范围与受保护的 Flash/XIP 地址重叠');
    return this.ramWindow = Object.freeze({ start, end });
  }

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
        const { first, last } = range;
        this._log(`Flash 擦写扇区 ${first}–${last}`, 'dim');
        await writeFlashRange(this._command.bind(this), this.link, range, data);
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

  _readFlashRange(range){
    return readFlashRange(this._command.bind(this), this.link, range);
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
      try { await this._command(halt ? 'reset halt' : 'reset run'); }
      catch (error){
        let state = '未知';
        try { await this.refresh(); state = this.halted ? '已暂停' : '运行中'; }
        catch { this.halted = false; this.stateKnown = false; }
        throw Error(`${error.message}；调试链路${this.link?.worker ? '仍打开' : '已断开'}，目标状态：${state}。可先断开再重连；复位未重试`, { cause:error });
      }
      await this.refresh();
      if (this.halted !== halt) throw Error('OpenOCD 未确认复位后的目标状态');
      return { halted:this.halted };
    }
    await this.ensureHalted();
    this.clearFrames();
    // A NOP through the program buffer flushes and invalidates OpenOCD's
    // register cache before the HPM system reset pulse.
    await this._command(`${this.target} riscv exec_progbuf 0x00000013`);
    const pollOutput = await this._command('capture {poll}');
    const pollMode = /background polling: (on|off)\b/.exec(pollOutput)?.[1];
    if (!pollMode) throw Error('未确认 OpenOCD 后台轮询状态');
    await this._command('poll off');
    let resetResult, failure;
    try {
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
      resetResult = { dmcontrol:control, dmstatus:status };
    } catch (error) { failure = error; }
    try { await this._command('poll ' + pollMode); }
    catch (error) {
      if (failure) throw new AggregateError([failure, error], failure.message + '；恢复 OpenOCD 后台轮询失败：' + error.message);
      throw error;
    }
    if (failure) throw failure;
    await this.refresh();
    if (this.halted !== halt) throw new Error('OpenOCD 未确认系统复位后的目标状态');
    return resetResult;
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
    this.stateKnown = true;
    if (this.halted) this.pc = await this.readReg('pc');
    if (this.halted && !this._triggersChecked) await this._detectTriggers();
    if (this._frames && (!this.halted || this.pc !== this._framePc)) this.clearFrames();
    return this.statusInfo();
  }

  async ensureHalted(){
    // OpenOCD's RISC-V halt path still accesses JTAG on an already halted hart.
    await this.refresh();
    if (this.halted) return;
    await this._command('halt');
    await this.refresh();
    if (!this.halted) throw new Error('OpenOCD 未能停住目标');
  }

  async halt(){
    await this.ensureHalted();
    await this.refreshRegs();
  }

  async _clearDfsr(){}
  async _detectTriggers(){
    this._triggersChecked = true;
    try {
      const info = await this._command(`capture {${this.target} riscv info}`);
      const value = /^hart\.trigger_count\s+(\d+)\s*$/m.exec(info);
      if (value) this.caps.numCode = Number(value[1]);
    } catch (e){ this._log(`未能读取 RISC-V 硬件触发器能力：${e.message}`, 'warn'); }
  }
  async cont(){
    await this.refresh();
    if (!this.halted) return false;
    return super.cont();
  }
  async run(){
    await this._command('resume');
    this.clearFrames();
    this.halted = false;
  }
  async step(){
    if (!this.halted) throw new Error('请先暂停目标');
    await this._withBpCleared(this.pc, async () => {
      await this._command('step');
      await this.refresh();
    });
  }
  async stepInto(){
    this.clearFrames();
    if (!this.halted) throw Error('请先暂停目标');
    const start = await this.readReg('pc');
    const source = this.sym?.lines?.at(start);
    this.lastStepMode = source ? 'into' : 'into(insn)';
    // A DWARF line can begin several instructions before its call (argument
    // setup, AUIPC/JALR, etc.). Follow actual execution until the line changes.
    for (let i=0;i<64;i++){
      await this.step();
      const next = this.sym?.lines?.at(this.pc);
      if (!source || next?.file !== source.file || next?.line !== source.line || this._bpAt(this.pc) !== undefined)
        return `单步进入 → ${this._locText(this.pc) || hex(this.pc)}`;
    }
    return `单步进入：执行 64 条指令后仍未离开当前源码行（${hex(this.pc)}）`;
  }
  async resetHalt(){
    await this.ensureHalted();
    const breakpoints = this.bps.map(addr => [addr, this.bpNotes.get(addr), this.bpDetails.get(addr).kind]);
    const watches = this.dwt.items.slice();
    await this.bpClear();
    await this.dwt.clear();
    await this._systemReset(true);
    for (const [addr, note, kind] of breakpoints) await this.bpAdd(addr, note, kind);
    for (const item of watches) await this.dwt.add(item.addr, item.size, item.mode);
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
    return REGISTERS.includes(canonical) || ['misa','dcsr','mstatus','mcause','mepc','mtval','mtvec'].includes(canonical)
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
    if ([...this.bpDetails].some(([at,bp]) => bp.kind === 'software' && addr < at + bp.original.length && addr + bytes.length > at))
      throw Error('写入范围内有软件断点，请先清除断点以免覆盖指令');
    return this._withMemory(() => this.memory.writeMem(addr, bytes), addr, bytes.length);
  }

  async _codeBytes(addr, length){
    const bytes = await super._codeBytes(addr,length);
    const patches = [...this.bpDetails].filter(([at,bp]) => bp.original && addr < at+bp.length && addr+length > at);
    if (!patches.length) return bytes;
    const original = bytes.slice();
    for (const [at,bp] of patches){
      const start=Math.max(addr,at), end=Math.min(addr+length,at+bp.length);
      original.set(bp.original.subarray(start-at,end-at),start-addr);
    }
    return original;
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

  async bpAdd(addr, note = '', kind = 'auto'){
    if (!Number.isInteger(addr) || addr < 0 || addr >= 0x100000000 || addr % 2) throw Error('断点地址须为 32 位半字对齐地址');
    if (!['auto','hardware','software'].includes(kind)) throw Error('无效的断点类型');
    const index = this.bps.indexOf(addr);
    if (index >= 0) return { index, warn:'这个地址上已经有断点了' };
    if (kind === 'auto') kind = fits(this.ramWindow,addr,2) && (!this.hpm6800 || this.caps.numCode === 0) ? 'software' : 'hardware';
    const bp = { kind, length:2 };
    if (kind === 'software'){
      if (!fits(this.ramWindow, addr, 2)) throw Error('软件断点只能设在已声明的可写 RAM 内');
      await this.ensureHalted();
      if ([...this.bpDetails].some(([at,b]) => b.kind === 'software' && at < addr && addr < at + b.length))
        throw Error('断点落在另一条已设断点的指令内部');
      const head = await this.memRead(addr,2);
      if (head.length !== 2) throw Error('指令长度读取不完整');
      bp.length = (head[0] & 3) === 3 ? 4 : 2;
      if (!fits(this.ramWindow, addr, bp.length)) throw Error('断点指令跨出可写 RAM');
      if ([...this.bpDetails].some(([at,b]) => b.kind === 'software' && addr < at && at < addr + bp.length))
        throw Error('软件断点指令与已有断点重叠');
      bp.original = bp.length === 2 ? head : await this.memRead(addr,4);
      bp.patch = bp.length === 2 ? Uint8Array.of(0x02,0x90) : Uint8Array.of(0x73,0,0x10,0);
      if (bp.original.length !== bp.length || sameBytes(bp.original,bp.patch)) throw Error('无法备份断点原指令，或该地址已是 EBREAK');
    } else if (this.caps.numCode === 0){
      throw Error('目标没有硬件触发器；请在已声明的可写 RAM 内使用软件断点');
    }
    const remember = () => {
      this.bpDetails.set(addr,bp);
      this.bps.push(addr);
      if (note) this.bpNotes.set(addr,note);
    };
    try {
      await this._command(`bp ${addr} ${bp.length}${kind === 'hardware' ? ' hw' : ''}`);
      bp.installed = true;
      if (bp.original && !sameBytes(await this.memRead(addr,bp.length),bp.patch)) throw Error('软件断点写入回读不一致');
    } catch (error){
      if (bp.original){
        remember();
        try { await this.bpDel(addr); }
        catch (cleanup){ throw new AggregateError([error,cleanup],`${error.message}；${cleanup.message}`); }
      }
      throw error;
    }
    remember();
    return { index:this.bps.length-1, kind };
  }

  async bpDel(addr){
    const index = this.bps.indexOf(addr);
    if (index < 0) return false;
    const bp = this.bpDetails.get(addr);
    if (bp.original) await this.ensureHalted();
    if (bp.installed){
      await this._command('rbp ' + addr);
      bp.installed = false;
    }
    if (bp.original){
      const actual = await this.memRead(addr,bp.length);
      if (sameBytes(actual,bp.patch)){
        await this._withMemory(() => this.memory.writeMem(addr,bp.original),addr,bp.length);
      }
      if (!sameBytes(await this.memRead(addr,bp.length),bp.original))
        throw Error(`软件断点 ${hex(addr)} 的原指令恢复失败；请先恢复匹配的 RAM ELF`);
    }
    this.bps.splice(index,1);
    this.bpDetails.delete(addr);
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
      const entry = /^(Hardware|Software) breakpoint\(IVA\): addr=(0x[0-9a-f]+)/i.exec(line);
      if (!entry) throw new Error('无法识别 OpenOCD 断点：' + line);
      const address = Number(entry[2]);
      if (this.bpDetails.get(address)?.kind !== entry[1].toLowerCase()) throw Error('OpenOCD 断点类型与调试页不一致：' + line);
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
    const kind = this.bpDetails.get(hit).kind;
    await this.bpDel(hit);
    try {
      return await fn();
    } finally {
      await this.bpAdd(hit, note, kind);
      const restored = this.bps.indexOf(hit);
      this.bps.splice(restored, 1);
      this.bps.splice(index, 0, hit);
    }
  }

  bpList(){ return super.bpList().map(bp => ({...bp,kind:this.bpDetails.get(bp.addr)?.kind})); }

  async _startRamDebug(buffer, symbols, {stop='auto'} = {}){
    if (!this.ramWindow) throw Error('请先声明当前 CFG 目标的可写 RAM 起始地址和长度');
    const plan = ramPlan(buffer, symbols, this.ramWindow, stop);
    await this.ensureHalted();
    await this.bpClear();
    for (const {addr,data,length} of plan.writes){
      for (let offset=0; offset<length; offset+=16384){
        const count = Math.min(16384,length-offset), at = addr+offset;
        const part = data ? data.subarray(offset,offset+count) : new Uint8Array(count);
        await this.memWrite(at,part);
        if (!sameBytes(await this.memRead(at,part.length),part)) throw Error(`RAM ${hex(at)} 写入后读回校验失败`);
      }
    }
    await this.writeReg('pc', plan.entry);
    if (plan.stop !== plan.entry) await this.runTo(plan.stop, { label:'main' });
    if (!this.halted || this.pc !== plan.stop) throw new Error(`未停在预期地址 ${hex(plan.stop)}`);
    return plan.stop;
  }
}

class RiscvWatchpoints {
  constructor(session){ this.s = session; this.items = []; }
  get capacity(){ return this.s.caps.numCode; }
  async init(){
    if (this.capacity == null){ await this.s.ensureHalted(); }
    if (!this.capacity) throw Error(this.capacity === 0 ? '目标没有硬件触发器，不能设置数据观察点' : '未能确认目标的硬件触发器能力');
  }
  async add(addr,size,mode){
    const spec = watchSpec(addr,size,mode);
    await this.init();
    const prior = this.items.find(item => item.addr === addr);
    if (prior){
      if (prior.mode === mode && prior.size === size) return prior;
      throw Error('同一地址已有观察点，请先删除');
    }
    await this.s.ensureHalted();
    await this.s._command(`wp ${addr} ${size} ${mode === 'rw' ? 'a' : mode}`);
    let slot = 0;
    while (this.items.some(item => item.slot === slot)) slot++;
    const item = {...spec,slot};
    this.items.push(item);
    return item;
  }
  async remove(id){
    const item = this.items.find(item => item.slot+1 === id);
    if (!item) throw Error('找不到观察点编号（wpl 查看）');
    await this.s.ensureHalted();
    await this.s._command(`rwp ${item.addr}`);
    this.items.splice(this.items.indexOf(item),1);
    return item;
  }
  async clear(){ for (const item of this.items.slice()) await this.remove(item.slot+1); }
  async haltReason(){ return null; }
}
