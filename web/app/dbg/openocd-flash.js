import { parseElfImage } from '../flash/image.js';
import { elfProgram } from './image.js';
import { SymTab } from './symbols.js';

const BASE = 0x80000000;
const MAX_IMAGE_END = BASE + 0x80000;
const SECTOR = 0x1000;

export function hpmFlashPlan(buffer){
  if (!(buffer instanceof ArrayBuffer)) throw new Error('请选择 HPM6800EVK 的 ELF 文件');
  const bytes = new Uint8Array(buffer);
  const { wide, little, type, machine, entry, headers } = elfProgram(bytes);
  if (wide || !little || type !== 2 || machine !== 243)
    throw new Error('Flash 烧录需要 HPM6800EVK 的小端 RV32 ELF 可执行文件');
  const sections = parseElfImage(bytes);
  if (!sections.length || sections.some(({ addr, data }) => addr < BASE || addr + data.length > MAX_IMAGE_END))
    throw new Error('ELF 加载段超出 HPM6800EVK Flash 启动区的前 512 KiB');
  const byteAt = address => {
    const section = sections.find(({ addr, data }) => addr <= address && address < addr + data.length);
    return section?.data[address - section.addr];
  };
  const xip = address => address % 2 === 0 && byteAt(address) !== undefined && byteAt(address + 1) !== undefined &&
    headers.some(h => h.type === 1 && (h.flags & 1) && h.virtual === h.physical &&
      address >= h.physical && address + 2 <= h.physical + h.size && address >= BASE && address < MAX_IMAGE_END);
  const main = SymTab.fromBuffer(buffer).find('main');
  if (![1, 0, 249, 252].every((value, i) => byteAt(BASE + 0x400 + i) === value) ||
      byteAt(BASE + 0x1000) !== 0xbf || !xip(entry) || main?.kind !== 'func' || !xip(main.addr))
    throw new Error('缺少 HPM XPI 配置、ROM 启动头或可从 Flash 执行的入口和 main');

  const ranges = [];
  for (const section of sections){
    const start = Math.floor((section.addr - BASE) / SECTOR);
    const end = Math.ceil((section.addr - BASE + section.data.length) / SECTOR);
    const previous = ranges.at(-1);
    if (previous && start <= previous.end){
      previous.end = Math.max(previous.end, end);
      previous.sections.push(section);
    } else ranges.push({ start, end, sections:[section] });
  }
  return { entry, main:main.addr, ranges:ranges.map(({ start, end, sections:parts }) => ({
    first:start, last:end - 1, offset:start * SECTOR, length:(end - start) * SECTOR,
    sections:parts.map(({ addr, data }) => ({ offset:addr - BASE - start * SECTOR, data })),
  })) };
}

export function hpmFlashGeometry(info, ranges){
  const bank = /^#0 : hpm_xpi at 0x0*80000000, size 0x([0-9a-f]+)/im.exec(info);
  if (!bank) throw new Error('OpenOCD 未探测到地址 0x80000000 的 HPM XPI Flash bank 0');
  const size = Number.parseInt(bank[1], 16);
  const sectors = new Map(Array.from(info.matchAll(/^\s*#\s*(\d+): 0x([0-9a-f]+) \(0x([0-9a-f]+) /gim),
    ([, number, offset, bytes]) => [Number(number), [Number.parseInt(offset, 16), Number.parseInt(bytes, 16)]]));
  for (const { first, last, offset, length } of ranges){
    if (offset + length > size) throw new Error('镜像超出实测 Flash 容量');
    for (let number = first; number <= last; number++){
      const sector = sectors.get(number);
      if (sector?.[0] !== number * SECTOR || sector[1] !== SECTOR)
        throw new Error(`Flash 扇区 ${number} 的布局不匹配`);
    }
  }
  return { size, sectorSize:SECTOR };
}
