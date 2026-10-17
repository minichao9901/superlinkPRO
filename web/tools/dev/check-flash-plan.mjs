/**
 * 离线核对：每个芯片算法的 chunkSize()（编程分块）与擦除/编程计划是否合理。
 * 不连硬件 —— 专门用来在没有板子的时候把"参数级"错误挑出来。
 * 用法：node tools/dev/check-flash-plan.mjs [firmware.elf|.hex|.bin ...]
 */
import { ALGOS, SERIES_MAX_KB, F1_DEV, checkFlashRange } from '../../app/flash/algos.js';
import { FlashRunner } from '../../app/flash/runner.js';
import { parseFirmware } from '../../app/flash/image.js';
import fs from 'node:fs';
import path from 'node:path';

const runner = new FlashRunner({});
console.log('=== chunkSize（单次编程块）与页参数对照 ===');
let bad = 0;
for (const [name, algo] of Object.entries(ALGOS)){
  runner.algo = algo;
  const chunk = runner.chunkSize();
  const wg = algo.write_granularity || 4;
  const gap = algo.page_buffers.length > 1 ? algo.page_buffers[1] - algo.page_buffers[0] : 0;
  const problems = [];
  if (chunk > algo.page_size) problems.push(`块(${chunk}) > 擦除粒度(${algo.page_size})`);
  if (chunk % wg) problems.push(`块(${chunk}) 不是编程粒度(${wg})的整数倍`);
  if (chunk < 4) problems.push('块太小');
  if (chunk % 4) problems.push('块不是 4 的倍数（算法按字写）');
  console.log(`  ${name.padEnd(11)} 擦除粒度 ${String(algo.page_size).padStart(7)} B · 编程粒度 ${String(wg).padStart(2)} B · ` +
    `缓冲 ${algo.page_buffers.length} 个${gap > 0 ? `(间距 ${gap})` : '(单缓冲)'} → chunk ${chunk} B${problems.length ? '  ❌ ' + problems.join('; ') : '  ✅'}`);
  if (problems.length) bad++;
}

// 有固件文件的话，把"擦除计划 + 编程分块（含尾块补齐）"也跑一遍（纯计算，不碰硬件）
for (const file of process.argv.slice(2)){
  if (!fs.existsSync(file)){ console.log(`\n${file}: 不存在`); continue; }
  const algoName = /h7b0/i.test(file) ? 'stm32h7b0' : 'stm32f103';
  const algo = ALGOS[algoName];
  runner.algo = algo;
  const u8 = new Uint8Array(fs.readFileSync(file));
  const regions = parseFirmware(path.basename(file), u8, 0x08000000);
  const chunk = runner.chunkSize();
  const wg = algo.write_granularity || 4;
  let eraseCount = 0, progCalls = 0, lastData = 0;
  for (const seg of regions){
    const ps = algo.page_size;
    for (let a = algo.flash_start + Math.floor((seg.addr - algo.flash_start) / ps) * ps; a < seg.addr + seg.data.length; a += ps) eraseCount++;
    for (let off = 0; off < seg.data.length; off += chunk){
      let len = Math.min(chunk, seg.data.length - off);
      if (len % wg) len = Math.ceil(len / wg) * wg;      // 尾块补 0xFF（与 view.js 同规则）
      progCalls++; lastData = len;
    }
  }
  const total = regions.reduce((s, r) => s + r.data.length, 0);
  const inRange = regions.every(r => r.addr >= algo.flash_start && r.addr + r.data.length <= algo.flash_start + algo.flash_length);
  console.log(`\n=== ${path.basename(file)} → ${algoName} ===`);
  console.log(`  段      : ${regions.map(r => '0x' + r.addr.toString(16) + '+' + r.data.length).join(', ')}`);
  console.log(`  擦除    : ${eraseCount} 次（每次 ${algo.page_size} B）`);
  console.log(`  编程    : ${progCalls} 次（每块 ≤ ${chunk} B，尾块补到 ${wg} 的倍数后最大 ${lastData} B）`);
  console.log(`  校验    : 读回 ${total} B 逐字节比对`);
  console.log(`  范围    : ${inRange ? '✅ 全在 flash 范围内' : '❌ 有段越界'}`);
  if (!inRange) bad++;
}

console.log('\n=== 烧录范围检查（代码审查：flash_length 是算法标称区间，不是芯片上限）===');
{
  // 老实现拿 pyOCD 的 `flash_length` 当硬上限，而它常按系列**最小**成员填（F4 = 64KB），
  // 于是 512KB 的 F407 固件会被拒，还提示"芯片选对了吗？"。现在硬上限用系列最大值，
  // 越过标称区间只提示；F1 有 DEV_ID 时按密度档判。
  const seg = (bytes, addr = 0x08000000) => ({ addr, data: { length: bytes } });
  for (const [name, algo] of Object.entries(ALGOS)){
    const maxKb = SERIES_MAX_KB[name] || 0;
    const maxB = maxKb * 1024;
    const okBig = checkFlashRange(algo, seg(maxB), { series: name });
    const over = checkFlashRange(algo, seg(maxB + 4), { series: name });
    const nominal = checkFlashRange(algo, seg(algo.flash_length), { series: name });
    const below = checkFlashRange(algo, seg(1024, 0x07000000), { series: name });
    const good = maxKb > 0 && okBig.ok && !over.ok && below.ok === false;
    console.log(`  ${name.padEnd(11)} 标称 ${String(Math.round(algo.flash_length / 1024)).padStart(4)} KB · ` +
      `系列上限 ${String(maxKb).padStart(4)} KB → 打满上限${okBig.ok ? '✅收' : '❌拒'} · ` +
      `超一点${over.ok ? '❌收' : '✅拒'} · 低于 flash 起址${below.ok ? '❌收' : '✅拒'}` +
      ` · 越标称${nominal.beyondNominal ? '(会提示)' : ''}${good ? '' : '  ❌'}`);
    if (!good) bad++;
  }
  // F1：DEV_ID 决定容量档（0x410 中容量只有 128KB —— 拿它烧 256KB 必须拒，且提示指向芯片）
  const f1 = ALGOS.stm32f103;
  const mid = checkFlashRange(f1, seg(256 * 1024), { series: 'stm32f103', devId: 0x410 });
  const high = checkFlashRange(f1, seg(512 * 1024), { series: 'stm32f103', devId: 0x414 });
  const midOk = !mid.ok && high.ok && high.limitBytes === 512 * 1024;
  console.log(`  STM32F1 DEV_ID 判定：0x410 中容量上限 ${mid.limitBytes / 1024} KB（256KB 固件${mid.ok ? '❌收' : '✅拒'}）· ` +
    `0x414 大容量上限 ${high.limitBytes / 1024} KB（512KB 固件${high.ok ? '✅收' : '❌拒'}）${midOk ? '' : '  ❌'}`);
  if (!midOk) bad++;
  if (!F1_DEV[0x414] || F1_DEV[0x414].page !== 2048 || F1_DEV[0x410].page !== 1024){ console.log('  ❌ F1_DEV 页粒度表被改坏了'); bad++; }
}

console.log(bad ? `\n结论：有 ${bad} 处问题 ❌` : '\n结论：全部自洽 ✅');
process.exit(bad ? 1 : 0);
