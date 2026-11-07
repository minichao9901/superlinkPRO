import { ocdHex } from '../core/openocd-profile.js';

const TIMEOUT = 180000;
const bankRows = /^#(\d+)\s*:\s*[^\n]+?\bat\s+(0x[\da-f]+),\s*size\s+(0x[\da-f]+)/gim;
const sectorRows = /^\s*#\s*(\d+):\s*(0x[\da-f]+)\s+\((0x[\da-f]+)\s/gim;

export async function programOpenOcdFlash(probe, regions, { verify, progress }){
  const command = (line, timeout) => probe.command(line, timeout);
  const banks = Array.from((await command('capture {flash banks}')).matchAll(bankRows),
    ([row, index, base]) => ({ index:Number(index), base:Number(base), alias:/\(virtual\)/i.test(row) }))
    .sort((a,b) => a.base - b.base || Number(b.alias) - Number(a.alias));
  if (!banks.length) throw Error('OpenOCD CFG 没有定义 Flash bank');

  async function probeBank(bank){
    await command(`flash probe ${bank.index}`, TIMEOUT);
    const info = await command(`if {[catch {flash info ${bank.index} sectors} geometry] && $errorCode ne {OpenOCD -908}} {error $geometry}; set geometry`, TIMEOUT);
    const row = Array.from(info.matchAll(bankRows)).find(([, index]) => Number(index) === bank.index);
    if (!row || Number(row[2]) !== bank.base || !Number(row[3])) throw Error('Flash bank 容量或地址未确认');
    bank.sectors = Array.from(info.matchAll(sectorRows), ([, index, offset, length]) =>
      ({ index:Number(index), offset:Number(offset), length:Number(length), patches:[] }));
    bank.end = bank.base + Number(row[3]);
  }

  const sorted = [...regions].sort((a,b) => a.addr - b.addr);
  let previousEnd = 0;
  for (const { addr, data } of sorted){
    const end = addr + data.length;
    if (!Number.isInteger(addr) || addr < previousEnd || end > 0x100000000 || !data.length)
      throw Error('固件地址越界、重叠或缺少数据');
    previousEnd = end;
    for (let at = addr; at < end;){
      const bank = banks.findLast(bank => bank.base <= at);
      if (!bank) throw Error('固件超出 Flash bank：' + ocdHex(at));
      if (!bank.sectors) await probeBank(bank);
      const offset = at - bank.base;
      const sector = bank.sectors.find(s => s.offset <= offset && offset < s.offset + s.length);
      if (!sector || at >= bank.end || bank.base + sector.offset + sector.length > bank.end)
        throw Error('固件超出实测 Flash 扇区：' + ocdHex(at));
      const next = Math.min(end, bank.base + sector.offset + sector.length);
      sector.patches.push({ offset:offset - sector.offset, bytes:data.subarray(at - addr, next - addr) });
      at = next;
    }
  }

  const plan = banks.flatMap(bank => (bank.sectors || []).filter(s => s.patches.length).map(s => ({ ...s, bank:bank.index })));
  if (!plan.length) throw Error('固件没有可烧录的数据');
  async function read(sector){
    await command(`flash read_bank ${sector.bank} /flash-readback.bin ${sector.offset} ${sector.length}`, TIMEOUT);
    const { bytes } = await probe.link.request('flashFile', { read:true });
    if (bytes.length !== sector.length) throw Error('Flash 回读长度不匹配');
    return bytes;
  }

  // Build every affected sector before the first erase. Preserve holes and edges.
  for (const sector of plan){
    const full = sector.patches.reduce((sum, patch) => sum + patch.bytes.length, 0) === sector.length;
    sector.data = full ? new Uint8Array(sector.length) : await read(sector);
    for (const patch of sector.patches) sector.data.set(patch.bytes, patch.offset);
  }
  for (const [i, sector] of plan.entries()){
    await probe.link.request('flashFile', { input:sector.data });
    try {
      await command(`flash erase_sector ${sector.bank} ${sector.index} ${sector.index}`, TIMEOUT);
      await command(`flash write_bank ${sector.bank} /flash-input.bin ${sector.offset}`, TIMEOUT);
    } finally {
      await probe.link.request('flashFile').catch(() => {});
    }
    if (verify){
      const actual = await read(sector);
      if (!sector.data.every((byte, offset) => byte === actual[offset])) throw Error(`Flash 扇区 ${sector.index} 读回校验失败`);
    }
    progress((i + 1) / plan.length);
  }
  return { bytes:sorted.reduce((sum, region) => sum + region.data.length, 0), sectors:plan.length, verified:verify };
}
