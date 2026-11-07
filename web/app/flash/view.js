/** OpenOCD Wasm firmware programming and target reset. */
import { prepareProbeHandoff } from '../core/probe-users.js';
import { runProbeOperation } from '../core/probe-manager.js';
import { $, setStatus, appendLogLine } from '../ui/dom.js';
import { toast } from '../ui/toast.js';
import { store } from '../core/store.js';
import { CHIPS } from '../core/chips.js';
import { webClockKhz } from '../core/target-clock.js';
import { constrainClockSelect } from '../ui/target-clock.js';
import { OpenOcdProbe, selectOpenOcdDevice } from '../core/openocd-probe.js';
import { prepareOpenOcdJtag } from '../core/openocd-jtag-pins.js';
import { hasOpenOcdFlash, makeOpenOcdProfile, ocdHex } from '../core/openocd-profile.js';
import { hpmBoard } from '../targets/hpm/porting.js';
import { rememberHpmBoard } from '../targets/hpm/select.js';
import { DBGMCU_BASES, FLASH_SIZE_REGS, decodeCpuid, decodeDpIdcode, decodeStm32Dev, refineByFlash, saneFlashKb } from './devid.js';
import { parseFirmware } from './image.js';
import { programOpenOcdFlash } from './openocd-program.js';
import { FirmwareInspector } from './inspector-view.js';
import { bytes as fBytes } from '../core/format.js';

export class FlashView {
  constructor(){
    this.probe = null;
    this.file = null;          // { name, size, dataB64 } —— 「选择文件/拖拽」时有效
    this.busy = false;
    this.bus = null;           // 跨页签探针协调（main.js 注入；见 core/probe-bus.js）
  }

  init(){
    this.inspector = new FirmwareInspector();
    this.inspector.init();
    for (const chip of CHIPS.filter(c => hasOpenOcdFlash(c.v) || hpmBoard(c.v)))
      $('f-chip').appendChild(new Option(chip.label, chip.v));
    store.bind($('f-chip'), 'flash.chip');
    if (!$('f-chip').value) $('f-chip').selectedIndex = 0;
    store.bind($('f-base'), 'flash.base');
    store.bind($('f-verify'), 'flash.verify', 'checked');
    store.bind($('f-reset'), 'flash.reset', 'checked');
    constrainClockSelect($('f-speed'),'arm'); // Add 25MHz before restoring a remembered selection.
    store.bind($('f-speed'), 'flash.speed');
    $('f-chip').addEventListener('change', () => { rememberHpmBoard($('f-chip').value); this._applyChip(); });
    this._applyChip();
    // ---------- 固件文件 ----------
    this._fileInput = document.createElement('input');
    this._fileInput.type = 'file';
    this._fileInput.accept = '.elf,.hex,.bin,.axf,.out';
    this._fileInput.style.display = 'none';
    this._fileInput.addEventListener('change', () => {
      const f = this._fileInput.files?.[0];
      this._fileInput.value = '';                     // 允许重复选同一个文件
      this._onFile(f);
    });
    document.body.appendChild(this._fileInput);
    $('f-pick').addEventListener('click', () => this._fileInput.click());
    const drop = $('f-main');
    drop.addEventListener('dragover', e => e.preventDefault());
    drop.addEventListener('drop', e => { e.preventDefault(); this._onFile(e.dataTransfer?.files?.[0]); });
    this._fileInfo();

    $('f-flash').addEventListener('click', () => this.flash().catch(e => this._err(e)));
    $('btn-global-reset')?.addEventListener('click', () => this.resetTarget().catch(e => this._err(e)));
    /* 「读 IDCODE」：只读地认目标（不动 flash、不改运行状态）。用 `?.` —— 页面混版时可能还没这个按钮，
     * 别让 init 挂在这儿（2026-10 那次白屏的教训）。 */
    $('f-idcode')?.addEventListener('click', () => this.readIdcode().catch(e => this._err(e)));
    $('f-logclear').addEventListener('click', () => { $('f-log').textContent = ''; });
    this._status('空闲');
  }

  // ================= 文件 =================
  async _onFile(f, buffer){
    if (!f) return;
    const revision = this._fileRevision = (this._fileRevision || 0) + 1;
    try {
      const buf = buffer || await f.arrayBuffer();
      if (revision !== this._fileRevision) return;
      if (this.busy) throw new Error('请等待烧录操作完成后再换固件');
      this.file = { name: f.name, size: f.size, dataB64: this._b64(new Uint8Array(buf)) };
      this._fileInfo();
      this.inspector?.load(f.name, buf);
      this.onElfLoaded?.(buf, f.name);
      toast(`已读入 ${f.name}（${fBytes(f.size)}），点「烧录」写入`, 'ok', 4000);
    } catch (e){
      toast(`读文件失败：${e?.message || e}`, 'err');
    }
  }

  /** Uint8Array → base64（分块，避免 String.fromCharCode 爆栈） */
  _b64(u8){
    let s = '';
    const CH = 0x8000;
    for (let i = 0; i < u8.length; i += CH) s += String.fromCharCode(...u8.subarray(i, i + CH));
    return btoa(s);
  }

  _fileInfo(){
    const el = $('f-file-info');
    el.textContent = this.file ? `已选择：${this.file.name}（${fBytes(this.file.size)}）` : '选择或拖入 .elf / .hex / .bin 固件文件';
    $('f-base-row').hidden = !/\.bin$/i.test(this.file?.name || '');
  }

  _applyChip(){
    $('f-custom-speed-row').hidden = false;
    const base = $('f-base');
    const family = $('f-chip').value;
    const hpm = hpmBoard(family);
    const imported = this.importedTarget?.id === family ? this.importedTarget.cfg : null;
    if (imported?.flashBase && base) base.value = imported.flashBase;
    else if (base && ['0x08000000', '0x10000000', '0x80000000'].includes(base.value)){
      base.value = hpm ? '0x80000000' : family === 'rp2350' ? '0x10000000' : '0x08000000';
      store.set('flash.base', base.value);
    }
    constrainClockSelect($('f-speed'),$('f-chip').value,v=>store.set('flash.speed',v));
    $('f-speed').disabled = !!imported;
    $('f-speed').title=imported ? `沿用 CFG 的 ${imported.clockKhz} kHz` : hpm
      ? 'HPM JTAG 时钟最高 25 MHz，默认 8 MHz'
      : 'SWD 调试时钟，默认 1 MHz';
  }

  // ================= 烧录 =================
  async _clearProbeUsers(name){
    await prepareProbeHandoff(this, 'flash', '烧录器要使用探针 · ' + name);
    return true;
  }

  async flash(){
    if (this.busy) return;
    if (!this.file) return await this._flashOnce();
    return await this._withProbeOwnership(() => this._flashOnce(), '烧录器要使用探针');
  }

  /** Flash and identification share reservation, retry cleanup and button state. */
  async _withProbeOwnership(fn, reason){
    this.busy = true;
    for (const id of ['f-flash', 'f-idcode', 'btn-global-reset']) if ($(id)) $(id).disabled = true;
    try {
      return await runProbeOperation(this, 'flash', async () => {
        // Retry the retained close before opening another native handle.
        if (this._probeCloseFailed) await this._closeProbe();
        return await fn();
      }, { reason, recovery: true });
    } finally {
      this.busy = false;
      if (!this._probeCloseFailed && !this.probeManager?.failures.has('flash')) this.probeManager?.forget('flash');
      for (const id of ['f-flash', 'f-idcode', 'btn-global-reset']) if ($(id)) $(id).disabled = false;
    }
  }

  async _closeProbe(operationError = null){
    const probe = this.probe;
    if (!probe) return;
    try { await probe.disconnect(); }
    catch (e){
      this._probeCloseFailed = true;
      this.probeManager?.fail('flash', e);
      if (operationError) throw new AggregateError([operationError, e],
        `${operationError.message}；探针关闭失败：${e.message}`);
      throw e;
    }
    this.probe = null;
    this._probeCloseFailed = false;
    this.probeManager?.confirm('flash');
  }

  async resetTarget(){
    if (this.busy) throw Error('探针当前正忙，请等当前操作结束后再复位');
    return await this._withProbeOwnership(async () => {
      await this._clearProbeUsers('复位目标');
      return this._usingTarget(false, async probe => {
        await probe.reset();
        this._status('目标已复位运行', 'ok');
        toast('目标已复位运行', 'ok');
        return true;
      });
    }, '复位目标板');
  }

  async _usingTarget(flash, operation){
    let failure;
    try {
      this.probe = await this._openTarget(flash);
      return await operation(this.probe);
    } catch (error){
      failure = error;
      throw error;
    } finally {
      await this._closeProbe(failure);
    }
  }

  async _openTarget(flash){
    const family = $('f-chip').value;
    const port = hpmBoard(family);
    const imported = this.importedTarget?.id === family ? this.importedTarget.cfg : null;
    const cfg = imported || makeOpenOcdProfile({ arch:port ? 'riscv' : 'arm', port, family, flash,
      clockKhz:webClockKhz($('f-speed').value, family, port ? 8000 : 1000) });
    const device = this.selectDevice ? await this.selectDevice() : await selectOpenOcdDevice();
    if (cfg.arch === 'riscv') await prepareOpenOcdJtag(device);
    return OpenOcdProbe.open(device, { cfg, onLog:line => this._log(line) });
  }

  async _program(probe, regions, verify){
    await probe.halt();
    this._status('正在检查 Flash 扇区并烧录…');
    return programOpenOcdFlash(probe, regions, { verify, progress:fraction => this._bar(fraction * 99) });
  }

  async _flashOnce(){
    if (!this.file){ toast('请选择或拖入固件文件', 'warn'); return; }
    const { name, dataB64 } = this.file;
    const base = /\.bin$/i.test(name) ? Number($('f-base').value) : 0;
    if (/\.bin$/i.test(name) && (!$('f-base').value.trim() || !Number.isInteger(base) || base < 0 || base > 0xffffffff))
      throw Error('请输入有效的 BIN 烧录基地址');
    const regions = parseFirmware(name, Uint8Array.from(atob(dataB64), ch => ch.charCodeAt(0)), base);
    const verify = $('f-verify').checked;
    const reset = $('f-reset').checked;
    this.inspector?.show('log');
    if (!(await this._clearProbeUsers(name))) return;
    this.lastFlash = null;
    setStatus($('f-result'), '烧录中…', null);
    this._bar(0);
    try {
      this.lastFlash = await this._usingTarget(true, async probe => {
        const result = await this._program(probe, regions, verify);
        if (reset) await probe.reset();
        return { ...result, reset };
      });
      this._bar(100);
      setStatus($('f-result'), `已烧录 ${fBytes(this.lastFlash.bytes)}${verify ? '，读回校验通过' : ''}`, 'ok');
      return this.lastFlash;
    } catch (error){
      $('f-bar').hidden = true;
      setStatus($('f-result'), error.message, 'err');
      throw error;
    } finally {
      this._status('空闲');
    }
  }

  async readIdcode(){
    if (this.busy) throw Error('正在烧录或读取目标身份');
    return await this._withProbeOwnership(async () => {
      if (!(await this._clearProbeUsers('读取目标身份'))) return;
      return this._usingTarget(false, async probe => {
        const id = probe.idcode;
        this._log(probe.isRiscv ? 'JTAG TAP ' + ocdHex(id) : decodeDpIdcode(id).text);
        if (probe.isRiscv) return { id };
        const word = addr => probe._readWord(addr).catch(() => null);
        const cpuid = await word(0xe000ed00);
        if (cpuid != null) this._log(decodeCpuid(cpuid).text);
        if (probe.cfg.family === 'rp2350'){
          const chipId = await word(0x40000000);
          if (chipId == null || (chipId & 0x0fffffff) !== 0x4927)
            throw Error('未读到 RP2350 的 SYSINFO CHIP_ID');
          this._log('RP2350 CHIP_ID ' + ocdHex(chipId));
          return { id, cpuid, chipId };
        }
        let device;
        for (const addr of DBGMCU_BASES){
          const value = await word(addr);
          if (value == null) continue;
          const candidate = decodeStm32Dev(value & 0xfff, value >>> 16);
          if (candidate.known){ device = candidate; break; }
        }
        let flashKb;
        for (const { addr } of FLASH_SIZE_REGS){
          const bytes = await probe.readMem(addr, 2).catch(() => null);
          const size = bytes && (bytes[0] | bytes[1] << 8);
          if (saneFlashKb(size)){ flashKb = size; break; }
        }
        if (device) this._log(refineByFlash(device.devId, flashKb) || device.text);
        if (flashKb) this._log(`Flash ${flashKb} KiB`);
        return { id, cpuid, devId:device?.devId, flashKb };
      });
    }, '读取目标身份');
  }

  _bar(pct){
    const bar = $('f-bar');
    bar.hidden = false;
    bar.value = Math.max(0, Math.min(100, pct));
    if (pct >= 100) setTimeout(() => { bar.hidden = true; }, 1500);
  }
  _status(s, kind){
    setStatus($('f-status'), s, kind);
  }

  _log(s){
    appendLogLine($('f-log'), s, 'dim', 800);
  }
  _err(e){
    this._log('── 出错 ──\n' + (e?.message || e));
    toast(String(e?.message || e), 'err', 6000);
  }
}
