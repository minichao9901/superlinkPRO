import {webClockKhz} from './target-clock.js';
/** Older software probes interpret RISC-V clockHz as idle, so keep their zero. */
export const supportsRiscvStreamClock = info => info?.model === 'ZynqLinkPro';
export function streamClockHz(info,riscv,value){
  if(riscv&&!supportsRiscvStreamClock(info))return 0;
  return webClockKhz(Number(value)/1000,riscv?'hpm':'arm',riscv?10000:0)*1000;
}
