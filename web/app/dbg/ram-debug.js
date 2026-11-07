import { elfProgram } from './image.js';
import { align2, hex32 } from './fmt.js';

export const RAM_BASE = 0x20000000;
const F103_RAM_BYTES = 64 * 1024;

/** Resolve the executable SRAM bounds from silicon identification, independent of CFG work-area. */
export async function readRamDebugSize(readWord, family){
  const cpuid = (await readWord(0xe000ed00) >>> 4) & 0xfff;
  if (family === 'nrf52'){
    if (cpuid !== 0xc24 || await readWord(0x10000100) !== 0x52832)
      throw new Error('nRF52 RAM 调试目前支持 nRF52832');
    const ramKiB = await readWord(0x1000010c);
    if (![16, 32, 64].includes(ramKiB)) throw new Error('无法核验 nRF52832 的 FICR RAM 容量');
    return ramKiB * 1024;
  }
  if (family !== 'stm32f1') throw new Error('当前 CFG 缺少可核验的片上 SRAM 型号支持');
  if (cpuid !== 0xc23 || ((await readWord(0xe0042000)) & 0xfff) !== 0x414 ||
    ((await readWord(0x1ffff7e0)) & 0xffff) !== 512)
    throw new Error('STM32F1 RAM 调试目前支持 STM32F103xE（512 KiB Flash、64 KiB SRAM）');
  return F103_RAM_BYTES;
}

/** ARM ELF with an executable RAM image and a vector table at SRAM base. */
export function ramDebugPlan(buffer, symbols, ramSize = F103_RAM_BYTES){
  const ramEnd = RAM_BASE + ramSize;
  const bytes = new Uint8Array(buffer);
  const { wide, little, type, machine, headers, range } = elfProgram(bytes);
  if (wide || !little || type !== 2 || machine !== 40)
    throw new Error('请选择为目标 SRAM 链接的小端 ARM ELF32 可执行文件');

  const loads = headers.filter(h => h.type === 1 && (h.memory || h.size))
    .sort((a, b) => a.physical - b.physical);
  let end = RAM_BASE;
  for (const h of loads){
    range(h.offset, h.size);
    if (h.size > h.memory || h.physical !== h.virtual || h.physical < end || h.memory > ramEnd - h.physical)
      throw new Error(`ELF 的 PT_LOAD（含 BSS）必须无重叠、加载与运行地址一致，且全部位于 ${hex32(RAM_BASE)}–${hex32(ramEnd - 1)}`);
    end = h.physical + h.memory;
  }
  const vectors = loads[0];
  if (vectors?.physical !== RAM_BASE || vectors.size < 8)
    throw new Error('RAM ELF 必须在 0x20000000 提供 Cortex-M 向量表');

  const view = new DataView(buffer, vectors.offset, 8);
  const sp = view.getUint32(0, true), reset = view.getUint32(4, true);
  const entry = align2(reset);
  if (sp <= end || sp > ramEnd || sp % 8 !== 0)
    throw new Error('向量表的初始 MSP 必须位于 SRAM 中已加载段的上方，并按 8 字节对齐');
  const code = loads.filter(h => (h.flags & 1) && h.size)
    .map(h => ({ start: h.physical, end: h.physical + h.size }));
  const executable = addr => code.some(r => r.start <= addr && addr + 2 <= r.end);
  if (!(reset & 1) || !executable(entry))
    throw new Error('向量表的复位入口必须是 RAM 代码段中的 Thumb 地址');
  const main = symbols.find('main');
  if (main?.kind !== 'func' || !executable(align2(main.addr)) || !symbols.lines?.at(align2(main.addr)))
    throw new Error('RAM ELF 需要位于代码段且带 DWARF 行号的 main 函数（编译时加 -g）');

  const segments = loads.map(h => {
    const data = new Uint8Array(h.memory);
    data.set(bytes.subarray(h.offset, h.offset + h.size));
    return { addr: h.physical, data };
  });
  return { segments, code, sp, entry, main: align2(main.addr) };
}
