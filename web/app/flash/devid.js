/**
 * 目标身份识别 —— 「读 IDCODE」按钮用的**数据表 + 纯函数**。
 *
 * 为什么单独一个文件：这几张表是数据、解码是纯函数，页面（`flash/view.js`）与自测
 * （`tools/selftest/stm32-devid.test.mjs`）共用同一份，免得"页面上认出来的型号"和"测试里认的"两套说法。
 *
 * 数据来源：
 *   · **DP IDCODE（DPIDR）**：ADIv5 规范 —— [31:24] 修订、[23:17] PARTNO、[16] MIN、
 *     [15:12] 版本、[11:1] 设计者（0x477 = ARM）、[0] = 1。注意：**它不含芯片型号**，
 *     只说明"SWD 通了、对端是 ARM 的 DP"。
 *   · **CPUID（0xE000ED00）**：ARMv7-M/v8-M 架构手册 —— [31:24] 实现者、[23:20] 变体、
 *     [19:16] 架构、[15:4] PARTNO、[3:0] 修订。PARTNO 才认得出核（0xC24 = Cortex-M4 …）。
 *   · **STM32 DBGMCU_IDCODE**：各系列参考手册 —— 低 12 位 DEV_ID、高 16 位 REV_ID；
 *     地址多数系列在 `0xE0042000`，**H7 在 `0xE00E1000`**。DEV_ID 才认得出芯片型号。
 *   · **flash 容量寄存器**：各系列手册（F1 0x1FFFF7E0、F4/F7 0x1FFF7A22、L4/G4 0x1FFF75E0 …），
 *     16 位、单位 KB。
 */

/** CPUID [15:4] PARTNO → 内核 */
export const CPUID_PART = {
  0xc20: 'Cortex-M0', 0xc21: 'Cortex-M1', 0xc23: 'Cortex-M3', 0xc24: 'Cortex-M4',
  0xc27: 'Cortex-M7', 0xc60: 'Cortex-M0+', 0xd20: 'Cortex-M23', 0xd21: 'Cortex-M33',
  0xd22: 'Cortex-M55', 0xd23: 'Cortex-M85',
};

/**
 * STM32 DBGMCU DEV_ID → 系列/密度档。表里没有的原样报出（不猜）。
 * `page` / `kb` 只填**该档确定**的：小/中容量 F1 是 1 KB/页，大容量起 2 KB/页（见 RM0008 §3.3.1）。
 */
export const STM32_DEV = {
  0x440: { name: 'STM32F0x0/F05x', fam: 'F0', kb: 64 },
  0x444: { name: 'STM32F03x', fam: 'F0', kb: 32 },
  0x445: { name: 'STM32F04x', fam: 'F0', kb: 32 },
  0x448: { name: 'STM32F07x', fam: 'F0', kb: 128 },
  0x442: { name: 'STM32F09x', fam: 'F0', kb: 256 },
  0x412: { name: 'STM32F10x 小容量', fam: 'F1', kb: 32, page: 1024 },
  0x410: { name: 'STM32F10x 中容量（F103x8/xB 那类）', fam: 'F1', kb: 128, page: 1024 },
  0x420: { name: 'STM32F10x 中容量 value line', fam: 'F1', kb: 128, page: 1024 },
  0x414: { name: 'STM32F10x 大容量（F103xC/D/E）', fam: 'F1', kb: 512, page: 2048 },
  0x428: { name: 'STM32F10x 大容量 value line', fam: 'F1', kb: 512, page: 2048 },
  0x418: { name: 'STM32F105/107 互联型', fam: 'F1', kb: 256, page: 2048 },
  0x430: { name: 'STM32F10x 超大容量（XL）', fam: 'F1', kb: 1024, page: 2048 },
  0x411: { name: 'STM32F2xx（F205/207/215/217）', fam: 'F2' },
  0x413: { name: 'STM32F405/407/415/417', fam: 'F4' },
  0x419: { name: 'STM32F42x/43x', fam: 'F4' },
  0x423: { name: 'STM32F401xB/C', fam: 'F4' },
  0x433: { name: 'STM32F401xD/E', fam: 'F4' },
  0x431: { name: 'STM32F411', fam: 'F4' },
  0x458: { name: 'STM32F410', fam: 'F4' },
  0x441: { name: 'STM32F412', fam: 'F4' },
  0x463: { name: 'STM32F413/423', fam: 'F4' },
  0x421: { name: 'STM32F446', fam: 'F4' },
  0x434: { name: 'STM32F469/479', fam: 'F4' },
  0x449: { name: 'STM32F745/746/756/765/767', fam: 'F7' },
  0x451: { name: 'STM32F76x/77x', fam: 'F7' },
  0x452: { name: 'STM32F72x/73x', fam: 'F7' },
  0x450: { name: 'STM32H742/743/745/747/750/753/755/757', fam: 'H7' },
  0x480: { name: 'STM32H7A3/B3（含 H7B0 系列）', fam: 'H7' },
  0x483: { name: 'STM32H723/725/730/733/735', fam: 'H7' },
  0x416: { name: 'STM32L1xx（小/中容量）', fam: 'L1' },
  0x429: { name: 'STM32L100x/L151x', fam: 'L1' },
  0x427: { name: 'STM32L15x/L16x', fam: 'L1' },
  0x436: { name: 'STM32L1xx 大容量', fam: 'L1' },
  0x417: { name: 'STM32L0x1/L0x2', fam: 'L0' },
  0x447: { name: 'STM32L0x5/L0x6', fam: 'L0' },
  0x425: { name: 'STM32L031/L041', fam: 'L0' },
  0x457: { name: 'STM32L01x/L02x/L03x/L04x', fam: 'L0' },
  0x415: { name: 'STM32L475/476/486', fam: 'L4' },
  0x461: { name: 'STM32L496/L4A6', fam: 'L4' },
  0x462: { name: 'STM32L45x/L46x', fam: 'L4' },
  0x470: { name: 'STM32L4Rx/L4Sx', fam: 'L4' },
  0x471: { name: 'STM32L4P5/L4Q5', fam: 'L4' },
  0x435: { name: 'STM32L43x/L44x', fam: 'L4' },
  0x460: { name: 'STM32G07x/G08x', fam: 'G0' },
  0x466: { name: 'STM32G03x/G04x', fam: 'G0' },
  0x467: { name: 'STM32G0B1/G0C1', fam: 'G0' },
  0x468: { name: 'STM32G431/G441', fam: 'G4' },
  0x469: { name: 'STM32G47x/G48x', fam: 'G4' },
  0x479: { name: 'STM32G491/G4A1', fam: 'G4' },
};

/**
 * DBGMCU_IDCODE 可能在的地址。
 *   0xE0042000 —— F0/F1/F2/F3/F4/F7/L0/L1/L4/G0/G4 那一大批（PPB 里）
 *   0xE00E1000 —— H7（H742/743/745/747/750/753/755/757，RM0433 的 D3 域调试口）
 *   0x5C001000 —— H7A3/B3（含 H7B0 系列，RM0468）
 */
export const DBGMCU_BASES = [0xe0042000, 0xe00e1000, 0x5c001000];

/** flash 容量寄存器（16 位，KB）。按"最可能命中"的顺序试，读到离谱值就跳过 */
export const FLASH_SIZE_REGS = [
  { addr: 0x1ffff7e0, fam: 'F1/F3' },
  { addr: 0x1ffff7cc, fam: 'F0/F3' },
  { addr: 0x1fff7a22, fam: 'F2/F4/F7' },
  { addr: 0x1ff1e880, fam: 'H743/H750' },
  { addr: 0x08fff80c, fam: 'H7A3/B3（OTP 里的容量，PB0 那类就是它）' },
  { addr: 0x1fff75e0, fam: 'L4/G0/G4' },
  { addr: 0x1ff8007c, fam: 'L0' },
  { addr: 0x1ff8004c, fam: 'L1' },
];

/**
 * DP IDCODE（DPIDR）里的 PARTNO → 这是哪种调试口。
 * 排法沿用 IEEE 1149.1 的 IDCODE：`[31:28] 版本 · [27:12] PARTNO · [11:1] 厂商 · [0]=1`，
 * 实测对得上：0x4BA00477 → 0xBA00（JTAG-DP 那类核）、0x2BA01477 → 0xBA01（SW-DP）、
 * 0x6BA02477 → 0xBA02（DPv2 的 SW-DP）。
 */
export const DP_PART = {
  0xba00: 'JTAG-DP / DPv0',
  0xba01: 'SW-DP（DPv1）',
  0xba02: 'SW-DP v2（DPv2）',
  0xba03: 'SW-DP v3（DPv3）',
};

const h = v => '0x' + (v >>> 0).toString(16).toUpperCase();

/** DP IDCODE → { designer, partno, revision, kind, text }（designer 是 JEP106 码，ARM = 0x477）*/
export function decodeDpIdcode(v){
  const w = v >>> 0;
  /**
   * 🚨 厂商字段是 [11:1]，而**规范里 bit0 恒为 1、它正是 JEP106 码的最低位** ——
   *    所以要把字段左移一位再补 1 才是标准 JEP106 码（否则 ARM 会显示成 0x23B 这个半截值）。
   */
  const field = (w >>> 1) & 0x7ff;
  const out = {
    revision: (w >>> 28) & 0xf,
    partno: (w >>> 12) & 0xffff,
    designer: ((field << 1) | 1) >>> 0,
    kind: null,
  };
  out.kind = DP_PART[out.partno] || null;
  const designer = out.designer === 0x477 ? 'ARM' : `JEP106 0x${out.designer.toString(16).toUpperCase()}`;
  out.text = `${h(w)} → 设计者 ${designer} · ${out.kind || `DP PARTNO 0x${out.partno.toString(16).toUpperCase()}`}` +
             ` · 修订 ${out.revision}`;
  return out;
}

/** CPUID → { core, partno, revision, text } */
export function decodeCpuid(v){
  const w = v >>> 0;
  const partno = (w >>> 4) & 0xfff;
  const core = CPUID_PART[partno] || null;
  const out = {
    implementer: (w >>> 24) & 0xff,
    variant: (w >>> 20) & 0xf,
    arch: (w >>> 16) & 0xf,
    partno,
    revision: w & 0xf,
    core,
  };
  out.text = `${h(w)} → ${core || `未知内核（PARTNO 0x${partno.toString(16).toUpperCase()}）`}` +
             ` r${out.variant}p${out.revision} · 实现者 0x${out.implementer.toString(16).toUpperCase()} · 架构 0x${out.arch.toString(16).toUpperCase()}`;
  return out;
}

/** DBGMCU DEV_ID（+REV_ID）→ { known, entry, text } */
export function decodeStm32Dev(devId, revId = 0){
  const id = devId & 0xfff;
  const entry = STM32_DEV[id] || null;
  const out = { devId: id, revId: revId & 0xffff, known: !!entry, entry };
  out.text = `DEV_ID 0x${id.toString(16).toUpperCase().padStart(3, '0')}（${entry ? entry.name : '表里没有 —— 原样报出'}）` +
             ` · REV_ID 0x${out.revId.toString(16).toUpperCase().padStart(4, '0')}`;
  return out;
}

/** 由 flash 容量寄存器读回的 16 位值判断真假（1~8192 KB 才算数）*/
export const saneFlashKb = kb => Number.isFinite(kb) && kb >= 1 && kb <= 8192;

/**
 * **DEV_ID 只认到"家族"，有些家族要再靠 flash 容量才分得开** —— 补这一步才叫出准确型号。
 *
 * 最典型的就是 `0x480`：H7A3 = 2 MB / H7B3 = 1 MB / **H7B0 = 128 KB**
 * （2026-10 真机实测：DEV_ID 0x480 + 128 KB ⇒ STM32H7B0VBT6）。
 */
export function refineByFlash(devId, kb){
  const id = devId & 0xfff;
  if (!kb) return null;
  if (id === 0x480){
    if (kb === 128) return 'STM32H7B0（128 KB flash 那一档）';
    if (kb === 1024) return 'STM32H7B3（1 MB）';
    if (kb === 2048) return 'STM32H7A3（2 MB）';
  }
  if (id === 0x410 && kb === 128) return 'STM32F103x8/xB（中容量 128 KB 那档）';
  if (id === 0x414) return `STM32F103x${kb >= 512 ? 'C/D/E' : ''}（大容量 ${kb} KB）`;
  return null;
}
