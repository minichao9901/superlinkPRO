import { OpenOcdTargetSession } from '../core/openocd-session.js';
import { sleep } from '../core/pace.js';

const NRST = 0x80;
const RESET_CONFIG = [
  'adapter driver cmsis-dap',
  'transport select swd',
  'adapter speed 1000',
  'reset_config none',
].join('\n');

export class OpenOcdResetSession extends OpenOcdTargetSession {
  async connect(device){
    // Initialize the adapter without creating or examining a target.
    await this.open({ script:RESET_CONFIG }, device);
    await this.command('init');
  }

  async _setNReset(release){
    const value = release ? NRST : 0;
    // SWJ_Pins waits for the requested level. Allow 50 ms for the line to rise.
    const wait = release ? '0x50 0xc3' : '0 0';
    const reply = await this.command(`capture {cmsis-dap cmd 0x10 ${value} ${NRST} ${wait} 0 0}`);
    const readback = /Returned data\s+([\da-f]{2})\b/i.exec(reply);
    if (!readback) throw Error('OpenOCD 未返回 NRST 引脚状态');
    const released = (parseInt(readback[1], 16) & NRST) !== 0;
    if (released !== release) throw Error(`探针未确认 NRST 已${release ? '释放' : '拉低'}`);
  }

  async pulse(){
    let assertError;
    try {
      await this._setNReset(false);
      await sleep(20);
    } catch (error){
      assertError = error;
    }
    try {
      await this._setNReset(true);
    } catch (error){
      if (assertError) throw new AggregateError([assertError, error], 'NRST 拉低失败，且无法确认已释放');
      throw error;
    }
    if (assertError) throw assertError;
    await sleep(150);
  }

  disconnect(){ return this.close(); }
}
