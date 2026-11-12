import { ocdHex } from '../core/openocd-profile.js';
import { OpenOcdProbe } from '../core/openocd-probe.js';

const DBGMCU_IDCODE = 0xe00e1000;
const DBGMCU_CR = DBGMCU_IDCODE + 4;
const H74_H75_DEVICE = 0x450;
const DEBUG_CLOCKS_AND_D1_LOW_POWER = 0x00600007;
const DHCSR = 0xe000edf0;
const S_REGRDY = 1 << 16;
const S_HALT = 1 << 17;
const S_LOCKUP = 1 << 19;

/** H74x/H75x's APB-AP remains accessible when the CPU's AXI access is gated. */
export class OpenOcdH7FlashProbe extends OpenOcdProbe {
  async _initTarget(link){
    await super._initTarget(link);
    const id = await link.command(`${this.cfg.h7DebugTarget} arp_examine`)
      .then(() => this._readDebugWord(DBGMCU_IDCODE)).catch(() => null);
    if (id !== null && (id & 0xfff) === H74_H75_DEVICE){
      const original = await this._readDebugWord(DBGMCU_CR);
      const enabled = (original | DEBUG_CLOCKS_AND_D1_LOW_POWER) >>> 0;
      this.onLog?.(`STM32H7 DBGMCU_IDCODE (AP2) = ${ocdHex(id)}`);
      if (original !== enabled){
        this._savedDebugControl = original;
        await this._writeDebugControl(enabled);
      }
    }
    await link.command(`${this.cfg.target} arp_examine`);
    await link.command(`targets ${this.cfg.target}`);
  }

  async _readDebugWord(address){
    const value = await this.command(`${this.cfg.h7DebugTarget} read_memory ${ocdHex(address)} 32 1`);
    if (!/^(?:0x[\da-f]+|\d+)$/i.test(value) || Number(value) > 0xffffffff)
      throw Error('STM32H7 AP2 调试寄存器回读无效');
    return Number(value);
  }

  async _writeDebugControl(value){
    await this.command(`${this.cfg.h7DebugTarget} write_memory ${ocdHex(DBGMCU_CR)} 32 {${ocdHex(value)}}`);
    if (await this._readDebugWord(DBGMCU_CR) !== value)
      throw Error('STM32H7 DBGMCU_CR 写入回读不一致');
  }

  async _restoreDebugControl(){
    if (this._savedDebugControl === undefined) return;
    await this._writeDebugControl(this._savedDebugControl);
    this._savedDebugControl = undefined;
  }

  async reset(halt = false){
    await this._restoreDebugControl();
    await this.command(halt ? 'reset halt' : 'reset run');
    // AP0 can be temporarily unavailable while the reset firmware starts.
    // Check the core directly and never issue the reset again on a read failure.
    const deadline = performance.now() + 2000;
    do {
      const status = await this._readWord(DHCSR).catch(() => null);
      if ((status & S_REGRDY) && !(status & S_LOCKUP) && Boolean(status & S_HALT) === halt)
        return 'OpenOCD 目标复位';
      await new Promise(resolve => setTimeout(resolve, 25));
    } while (performance.now() < deadline);
    throw Error('未确认 STM32H7 复位后的核心状态');
  }

  async disconnect(){
    let restoreError;
    if (this.link){
      try { await this._restoreDebugControl(); }
      catch (error){ restoreError = new Error(`STM32H7 调试配置恢复失败：${error.message}`, { cause:error }); }
    }
    try { await super.disconnect(); }
    catch (error){
      if (restoreError) throw new AggregateError([restoreError, error], `${restoreError.message}；${error.message}`);
      throw error;
    }
    this._savedDebugControl = undefined;
    if (restoreError) throw restoreError;
  }
}
