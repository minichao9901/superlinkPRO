/**
 * 纯 Node 自测：目标身份解码（「读 IDCODE」按钮用的那几张表 + 纯函数）。
 *
 *   node tools/selftest/stm32-devid.test.mjs
 *
 * 这些数字都是"认芯片"的唯一依据，错一个位就会报错型号 —— 所以逐个钉住：
 *   · DP IDCODE 的位域（设计者/版本/PARTNO/修订）
 *   · CPUID 的 PARTNO → 内核（0xC23=M3、0xC24=M4、0xC27=M7…）
 *   · STM32 DBGMCU DEV_ID → 型号（F1 各密度档 / F4 / F7 / H7 / L4 / G0 …）
 *   · flash 容量寄存器的合理性判据
 */
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const url = p => 'file://' + join(here, '..', '..', 'app', p).replace(/\\/g, '/');
const D = await import(url('flash/devid.js'));

let pass = 0, fail = 0;
const ok = (cond, name, extra = '') => {
  if (cond){ pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name} ${extra}`); }
};

// ==================================================================== 1
console.log('== 1. DP IDCODE（DPIDR）==');
{
  /* 位域按 IEEE 1149.1 的 IDCODE：版本 [31:28] / PARTNO [27:12] / 厂商 [11:1] / [0]=1。
   * 三个值都是真机上见过的：
   *   0x4BA00477 = Cortex-M 的 **JTAG**-DP（PARTNO 0xBA00）
   *   0x2BA01477 = **SW**-DP（PARTNO 0xBA01）—— 本仓 SWD 通路读到的就是它
   *   0x6BA02477 = DPv2 的 SW-DP（PARTNO 0xBA02） */
  const sw = D.decodeDpIdcode(0x2ba01477);
  ok(sw.designer === 0x477 && sw.partno === 0xba01 && sw.revision === 2 && sw.kind === 'SW-DP（DPv1）',
     `0x2BA01477 = ARM · SW-DP · PARTNO 0xBA01 · 修订 2（实测 designer=0x${sw.designer.toString(16)} partno=0x${sw.partno.toString(16)} rev=${sw.revision}）`);
  ok(/ARM/.test(sw.text) && /SW-DP/.test(sw.text), `文案点明设计者与调试口：${sw.text}`);
  const jt = D.decodeDpIdcode(0x4ba00477);
  ok(jt.partno === 0xba00 && /JTAG-DP/.test(jt.text), `0x4BA00477 = JTAG-DP：${jt.text}`);
  const v2 = D.decodeDpIdcode(0x6ba02477);
  ok(v2.partno === 0xba02 && /DPv2/.test(v2.text), `0x6BA02477 = DPv2 的 SW-DP：${v2.text}`);
  ok(D.decodeDpIdcode(0xdeadbeef).designer !== 0x477, '非 ARM 的设计者不会被认成 ARM');
}

// ==================================================================== 2
console.log('== 2. CPUID → 内核 ==');
{
  const m4 = D.decodeCpuid(0x410fc241);
  ok(m4.core === 'Cortex-M4' && m4.variant === 0 && m4.revision === 1,
     `0x410FC241 = Cortex-M4 r0p1（实测 ${m4.core} r${m4.variant}p${m4.revision}）`);
  const m3 = D.decodeCpuid(0x412fc231);
  ok(m3.core === 'Cortex-M3' && m3.variant === 2 && m3.revision === 1, `0x412FC231 = Cortex-M3 r2p1（实测 ${m3.core}）`);
  const m7 = D.decodeCpuid(0x411fc271);
  ok(m7.core === 'Cortex-M7', `0x411FC271 = Cortex-M7（实测 ${m7.core}）`);
  const m0p = D.decodeCpuid(0x410cc601);
  ok(m0p.core === 'Cortex-M0+', `0x410CC601 = Cortex-M0+（实测 ${m0p.core}）`);
  const m33 = D.decodeCpuid(0x410fd211);
  ok(m33.core === 'Cortex-M33', `0x410FD211 = Cortex-M33（实测 ${m33.core}）`);
  const unknown = D.decodeCpuid(0x410f0001);
  ok(unknown.core === null && /未知内核/.test(unknown.text), `表里没有的 PARTNO 不猜：${unknown.text}`);
}

// ==================================================================== 3
console.log('== 3. STM32 DBGMCU DEV_ID → 型号 ==');
{
  const cases = [
    [0x410, 0x1000, 'STM32F10x 中容量', 'F1'],
    [0x414, 0x1001, 'STM32F10x 大容量', 'F1'],
    [0x412, 0x1000, 'STM32F10x 小容量', 'F1'],
    [0x413, 0x1000, 'STM32F405', 'F4'],
    [0x449, 0x1000, 'STM32F745', 'F7'],
    [0x450, 0x1000, 'STM32H742', 'H7'],
    [0x480, 0x1000, 'STM32H7A3', 'H7'],
    [0x415, 0x1000, 'STM32L475', 'L4'],
    [0x460, 0x1000, 'STM32G07x', 'G0'],
  ];
  for (const [dev, rev, wantName, wantFam] of cases){
    const d = D.decodeStm32Dev(dev, rev);
    ok(d.known && d.entry.name.includes(wantName) && d.entry.fam === wantFam,
       `DEV_ID 0x${dev.toString(16).toUpperCase()} → ${d.entry?.name}（${d.entry?.fam}）`);
  }
  const unk = D.decodeStm32Dev(0xabc, 0x1000);
  ok(!unk.known && /表里没有/.test(unk.text), `表里没有的 DEV_ID 原样报出：${unk.text}`);
  ok(D.decodeStm32Dev(0x1410).devId === 0x410, '只取低 12 位（高位是 REV_ID 的一部分）');
  ok(D.decodeStm32Dev(0x410, 0x2003).revId === 0x2003, `REV_ID 原样带出：0x${D.decodeStm32Dev(0x410, 0x2003).revId.toString(16)}`);
  ok(D.STM32_DEV[0x410].page === 1024 && D.STM32_DEV[0x414].page === 2048,
     'F1 中容量 1 KB/页、大容量 2 KB/页（擦除粒度判据，与 RM0008 §3.3.1 一致）');
}

// ==================================================================== 4
console.log('== 4. 候选地址表与容量合理性判据 ==');
{
  ok(D.DBGMCU_BASES.length === 3 &&
     D.DBGMCU_BASES[0] === 0xe0042000 && D.DBGMCU_BASES[1] === 0xe00e1000 && D.DBGMCU_BASES[2] === 0x5c001000,
     `DBGMCU 候选地址：${D.DBGMCU_BASES.map(a => '0x' + a.toString(16).toUpperCase()).join(' / ')}（H7 前两处、H7A3/B3 第三处）`);
  ok(D.FLASH_SIZE_REGS.some(r => r.addr === 0x1ffff7e0) && D.FLASH_SIZE_REGS.some(r => r.addr === 0x1fff7a22),
     '容量寄存器表里有 F1（0x1FFFF7E0）与 F4/F7（0x1FFF7A22）');
  ok(D.saneFlashKb(512) && D.saneFlashKb(1) && D.saneFlashKb(8192), '合理容量认得（1 / 512 / 8192 KB）');
  ok(!D.saneFlashKb(0) && !D.saneFlashKb(0xffff) && !D.saneFlashKb(NaN), '全 0 / 全 1 / 读不到 → 判为无效（别把垃圾当容量）');
}

// ==================================================================== 5
console.log('== 5. 同族靠 flash 容量收窄（真机就是这一步认出来的）==');
{
  ok(D.refineByFlash(0x480, 128) === 'STM32H7B0（128 KB flash 那一档）',
     `DEV_ID 0x480 + 128 KB ⇒ STM32H7B0（本机实测那颗就是它）`);
  ok(D.refineByFlash(0x480, 2048) === 'STM32H7A3（2 MB）', 'DEV_ID 0x480 + 2 MB ⇒ H7A3');
  ok(D.refineByFlash(0x480, 1024) === 'STM32H7B3（1 MB）', 'DEV_ID 0x480 + 1 MB ⇒ H7B3');
  ok(D.refineByFlash(0x480, null) === null, '没读到容量就不硬猜（回落到家族名）');
  ok(/F103x8\/xB/.test(D.refineByFlash(0x410, 128) || ''), `0x410 + 128 KB ⇒ ${D.refineByFlash(0x410, 128)}`);
  ok(D.refineByFlash(0x413, 1024) === null, '表里没写规则的家族不发表意见（返回 null）');
}

console.log(`\n${fail ? '❌' : '✅'} stm32-devid.test: ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
