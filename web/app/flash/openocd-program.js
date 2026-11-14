import { ocdHex } from '../core/openocd-profile.js';

const TIMEOUT = 180000;
const bankRows = /^#(\d+)\s*:\s*[^\n]+?\bat\s+(0x[\da-f]+),\s*size\s+(0x[\da-f]+)/gim;
const sectorRows = /^\s*#\s*(\d+):\s*(0x[\da-f]+)\s+\((0x[\da-f]+)\s/gim;

export async function resetAfterFlash(probe){
  if (probe.isRiscv) return probe.reset();
  await probe.command('reset run');
  // Read the Cortex-M status directly. A failed read during startup must
  // never replay the reset or report a locked core as running.
  const deadline = performance.now() + 2000;
  do {
    const status = await probe._readWord(0xe000edf0).catch(() => null);
    if ((status & (1 << 16)) && !(status & ((1 << 17) | (1 << 19)))) return;
    await new Promise(resolve => setTimeout(resolve, 25));
  } while (performance.now() < deadline);
  throw Error('Flash 写入完成，未确认复位后的核心运行状态');
}

export async function readFlashRange(command, link, { bank = 0, offset, length, mappedAddress }){
  // HPM XPI's bank probe runs a ROM algorithm that reconfigures the live
  // flash controller. Read the mapped window directly for a byte readback.
  const path = '/flash-readback.bin';
  const read = mappedAddress === undefined
    ? `flash read_bank ${bank} ${path} ${offset} ${length}`
    : `dump_image ${path} ${ocdHex(mappedAddress)} ${length}`;
  await command(read, TIMEOUT);
  const { bytes } = await link.request('flashFile', { read:true });
  if (bytes.length !== length) throw Error('Flash 回读长度不匹配');
  return bytes;
}

export async function writeFlashRange(command, link, { bank = 0, first, last, offset }, data){
  await link.request('flashFile', { input:data });
  try {
    await command(`flash erase_sector ${bank} ${first} ${last}`, TIMEOUT);
    await command(`flash write_bank ${bank} /flash-input.bin ${offset}`, TIMEOUT);
  } finally {
    await link.request('flashFile').catch(() => {});
  }
}

export async function programOpenOcdFlash(probe, regions, { verify, progress }){
  const command = (line, timeout) => probe.command(line, timeout);
  const bankText = await command('capture {flash banks}');
  const banks = Array.from(bankText.matchAll(bankRows),
    ([row, index, base]) => ({
      index:Number(index), base:Number(base), alias:/\(virtual\)/i.test(row), mappedRead:/\(hpm_xpi\)/i.test(row),
    }));
  if (!banks.length) throw Error('OpenOCD CFG 没有定义 Flash bank');

  async function probeBank(bank){
    await command(`flash probe ${bank.index}`, TIMEOUT);
    const info = await command(`if {[catch {flash info ${bank.index} sectors} geometry] && $errorCode ne {OpenOCD -908}} {error $geometry}; set geometry`, TIMEOUT);
    const row = Array.from(info.matchAll(bankRows)).find(([, index]) => Number(index) === bank.index);
    if (!row || (bank.base && Number(row[2]) !== bank.base) || !Number(row[3]))
      throw Error('Flash bank 容量或地址未确认');
    bank.base = Number(row[2]);
    bank.sectors = Array.from(info.matchAll(sectorRows), ([, index, offset, length]) =>
      ({ index:Number(index), offset:Number(offset), length:Number(length), patches:[] }));
    bank.end = bank.base + Number(row[3]);
  }

  // Some official CFGs leave the bank base at zero until the driver probes
  // it. Resolve those bases before choosing a bank for an image address.
  for (const bank of banks) if (!bank.base) await probeBank(bank);
  banks.sort((a,b) => a.base - b.base || Number(b.alias) - Number(a.alias));

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

  const plan = banks.flatMap(bank => (bank.sectors || []).filter(s => s.patches.length).map(s => ({
    ...s, bank:bank.index, mappedAddress:bank.mappedRead ? bank.base + s.offset : undefined,
  })));
  if (!plan.length) throw Error('固件没有可烧录的数据');
  const read = sector => readFlashRange(command, probe.link, sector);

  // Build every affected sector before the first erase. Preserve holes and edges.
  for (const sector of plan){
    const full = sector.patches.reduce((sum, patch) => sum + patch.bytes.length, 0) === sector.length;
    sector.data = full ? new Uint8Array(sector.length) : await read(sector);
    for (const patch of sector.patches) sector.data.set(patch.bytes, patch.offset);
  }
  // H7 block algorithms use RAM. Keep an independent copy: the Wasm backend
  // was observed leaving its final literal word behind despite backup=1.
  let workArea, savedWorkArea;
  if (/\bstm32h7x\b/.test(bankText)){
    const address = Number(await command(`${probe.cfg.target} cget -work-area-phys`));
    const length = Number(await command(`${probe.cfg.target} cget -work-area-size`));
    if (address && length){
      if (!Number.isSafeInteger(address) || !Number.isSafeInteger(length) || length < 0 || length > 0x100000)
        throw Error('H7 Flash 工作区范围无效');
      const mpu = await probe.readMem(0xe000ed94, 4);
      if (mpu.length !== 4) throw Error('H7 MPU 状态回读不完整');
      if (mpu[0] & 1){
        // A live MPU can forbid instruction fetches from the work area.
        // A zero-sized work area makes stm32h7x program via the debug port.
        await command(`${probe.cfg.target} configure -work-area-size 0`);
        if (Number(await command(`${probe.cfg.target} cget -work-area-size`)) !== 0)
          throw Error('H7 Flash 工作区禁用未确认');
        probe.onLog?.('STM32H7 MPU 已启用，经调试端口直接烧录');
      } else {
        workArea = {address, length};
        savedWorkArea = await probe.readMem(address, length);
        if (savedWorkArea.length !== length) throw Error('H7 Flash 工作区备份不完整');
      }
    }
  }
  let operationError;
  try {
    for (const [i, sector] of plan.entries()){
      await writeFlashRange(command, probe.link, { ...sector, first:sector.index, last:sector.index }, sector.data);
      if (verify){
        const actual = await read(sector);
        if (!sector.data.every((byte, offset) => byte === actual[offset])) throw Error(`Flash 扇区 ${sector.index} 读回校验失败`);
      }
      progress((i + 1) / plan.length);
    }
  } catch (error){ operationError = error; throw error; }
  finally {
    if (workArea){
      try {
        await probe.halt();
        await probe.writeMem(workArea.address, savedWorkArea);
        const actual = await probe.readMem(workArea.address, workArea.length);
        if (!savedWorkArea.every((value,i) => value === actual[i])) throw Error('H7 Flash 工作区恢复校验失败');
      } catch (error){
        if (operationError) throw new AggregateError([operationError,error], '烧录失败，且 H7 Flash 工作区恢复未确认');
        throw error;
      }
    }
  }
  return { bytes:sorted.reduce((sum, region) => sum + region.data.length, 0), sectors:plan.length, verified:verify };
}
