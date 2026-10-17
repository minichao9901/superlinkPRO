/**
 * Cortex-M 内核寄存器表 + xPSR / CFBP 位域编解码（**纯逻辑**，Node 可直接自测）。
 *
 * 两条"外部约定"都不是猜的，写代码前查过权威实现：
 *
 * 1) **CFBP（DCRSR.REGSEL = 0x14）的 32 位打包**：
 *      byte0 = PRIMASK、byte1 = BASEPRI、byte2 = FAULTMASK、byte3 = CONTROL
 *    依据 OpenOCD `src/target/armv7m.c`：
 *      `case ARMV7M_PRIMASK...ARMV7M_CONTROL: *offset = arm_reg_id - ARMV7M_PRIMASK;`
 *    再按 `offset * 8` 取位；枚举顺序见 `src/target/armv7m.h`（PRIMASK, BASEPRI, FAULTMASK, CONTROL）。
 *    pyOCD 的写法一致（`val = (val >> ((reg & 3) * 8)) & 0xff`）。
 *    ⇒ 一个 32 位读写就能同时看/改这四个特殊寄存器（内核寄存器里没有别的通路）。
 *
 * 2) **DCRSR 的 REGSEL 编号**：0x00~0x0C = R0~R12、0x0D = 当前 SP、0x0E = LR、
 *    0x0F = PC（DebugReturnAddress）、0x10 = xPSR、0x11 = MSP、0x12 = PSP、0x14 = CFBP。
 *
 * xPSR 的位域（ARMv7-M ARM A2.3）：APSR 在 [31:27]，IPSR 在 [8:0]，EPSR 的 T 在 bit24、
 * IT 拆在 [26:25] 与 [15:10] 两段 —— IT 拆两段是最容易看漏的一条。
 */

/** 内核寄存器（走 DCRSR/DCRDR 逐个读写） */
export const CORE_REGS = [
  { sel: 0x00, name: 'R0' }, { sel: 0x01, name: 'R1' }, { sel: 0x02, name: 'R2' }, { sel: 0x03, name: 'R3' },
  { sel: 0x04, name: 'R4' }, { sel: 0x05, name: 'R5' }, { sel: 0x06, name: 'R6' }, { sel: 0x07, name: 'R7' },
  { sel: 0x08, name: 'R8' }, { sel: 0x09, name: 'R9' }, { sel: 0x0a, name: 'R10' }, { sel: 0x0b, name: 'R11' },
  { sel: 0x0c, name: 'R12' },
  { sel: 0x0d, name: 'SP', note: '当前栈指针（MSP 还是 PSP 由 CONTROL.SPSEL 决定）' },
  { sel: 0x0e, name: 'LR', note: '返回地址' },
  { sel: 0x0f, name: 'PC', note: '取指地址（halt 时 = 将要执行的那条指令）' },
  { sel: 0x10, name: 'XPSR', note: 'APSR + IPSR + EPSR（大小写统一成 XPSR，省得每处都比字符串）' },
  { sel: 0x11, name: 'MSP', note: '主栈指针（异常/复位时用）' },
  { sel: 0x12, name: 'PSP', note: '进程栈指针（RTOS 任务里用）' },
];

/** CFBP 容器（四个特殊寄存器打包在一个 32 位里） */
export const CFBP_SEL = 0x14;
export const SPECIAL_REGS = [
  { name: 'PRIMASK', shift: 0, width: 1, note: '1 = 关掉除 NMI/HardFault 外的全部中断' },
  { name: 'BASEPRI', shift: 8, width: 8, note: '屏蔽优先级 ≥ 该值的中断（0 = 不屏蔽）' },
  { name: 'FAULTMASK', shift: 16, width: 1, note: '1 = 连 HardFault 也屏蔽（异常返回时自动清）' },
  { name: 'CONTROL', shift: 24, width: 3, note: 'bit0 nPRIV / bit1 SPSEL / bit2 FPCA' },
];

/** 名字 → 寄存器描述（大小写不敏感；`r13` 之类也认） */
const BY_NAME = new Map();
for (const r of CORE_REGS) BY_NAME.set(r.name.toLowerCase(), { ...r, kind: 'core' });
BY_NAME.set('r13', { ...BY_NAME.get('sp'), name: 'SP', kind: 'core' });
BY_NAME.set('r14', { ...BY_NAME.get('lr'), name: 'LR', kind: 'core' });
BY_NAME.set('r15', { ...BY_NAME.get('pc'), name: 'PC', kind: 'core' });
for (const s of SPECIAL_REGS) BY_NAME.set(s.name.toLowerCase(), { ...s, sel: CFBP_SEL, kind: 'cfbp' });
BY_NAME.set('cfbp', { name: 'CFBP', sel: CFBP_SEL, kind: 'cfbp', note: '四个特殊寄存器打包' });

/** 归一化寄存器名（`R0`/`r0`/`SP`/`sp` 都认，认不出返回 null） */
export function regInfo(name){
  return BY_NAME.get(String(name || '').trim().toLowerCase()) || null;
}

/** 这个寄存器是不是 CFBP 里的一个字节域 */
export const isCfbpSub = info => !!(info && info.kind === 'cfbp' && typeof info.shift === 'number');

// ---------------------------------------------------------------- xPSR

/** 拆 xPSR：返回各标志位与异常号（纯数据，方便自测断言） */
export function decodeXpsr(v){
  v = v >>> 0;
  return {
    n: (v >>> 31) & 1, z: (v >>> 30) & 1, c: (v >>> 29) & 1, v: (v >>> 28) & 1, q: (v >>> 27) & 1,
    ge: (v >>> 16) & 0xf,
    t: (v >>> 24) & 1,
    it: ((((v >>> 25) & 3) << 6) | ((v >>> 10) & 0x3f)) & 0xff,
    isr: v & 0x1ff,
    iciIt: (v >>> 25) & 3,
  };
}

/** 一句人话描述 xPSR（寄存器表下方与 `info` 命令都用它） */
export function formatXpsr(v){
  const d = decodeXpsr(v);
  const flags = `N=${d.n} Z=${d.z} C=${d.c} V=${d.v} Q=${d.q}`;
  const mode = d.isr === 0 ? 'Thread' : `Handler #${d.isr}`;
  const it = d.it ? ` IT=${d.it.toString(16).padStart(2, '0')}` : '';
  return `${flags} T=${d.t}${it} ${mode}`;
}

/**
 * 改 xPSR 里的标志位（只动指定位，其余原样保留）。
 * 🚨 只允许改 APSR 的 5 个标志：IPSR/EPSR 是**只读或由硬件维护**的
 *    （T 位写 0 会让内核下次取指直接锁死），在调试器里手改它们没有意义还有风险。
 */
export function setXpsrFlags(v, flags = {}){
  let out = v >>> 0;
  const set = (bit, val) => { if (val === undefined || val === null) return; out = val ? (out | (1 << bit)) : (out & ~(1 << bit)); };
  set(31, flags.n); set(30, flags.z); set(29, flags.c); set(28, flags.v); set(27, flags.q);
  return out >>> 0;
}

// ---------------------------------------------------------------- CFBP

/** 从 CFBP 的 32 位值里取一个特殊寄存器 */
export function cfbpGet(v, name){
  const info = regInfo(name);
  if (!isCfbpSub(info)) throw new Error(`${name} 不是 CFBP 里的寄存器`);
  return ((v >>> info.shift) & ((1 << info.width) - 1)) >>> 0;
}

/** 把某个特殊寄存器写回 CFBP 的 32 位值（其余字节原样保留） */
export function cfbpSet(v, name, val){
  const info = regInfo(name);
  if (!isCfbpSub(info)) throw new Error(`${name} 不是 CFBP 里的寄存器`);
  const mask = ((1 << info.width) - 1) >>> 0;
  const shifted = (mask << info.shift) >>> 0;
  return (((v >>> 0) & ~shifted) | (((val >>> 0) & mask) << info.shift)) >>> 0;
}

/** CONTROL 三个位的人话（排障时最常问"为什么进不了中断"就是它在作怪） */
export function formatControl(v){
  const npriv = v & 1, spsel = (v >>> 1) & 1, fpca = (v >>> 2) & 1;
  return `nPRIV=${npriv}(${npriv ? '非特权' : '特权'}) SPSEL=${spsel}(用 ${spsel ? 'PSP' : 'MSP'}) FPCA=${fpca}`;
}

/** 一个特殊寄存器值的显示（1 位的就显示 0/1） */
export function formatSpecial(name, val){
  if (name === 'CONTROL') return `0x${val.toString(16)}　${formatControl(val)}`;
  if (name === 'BASEPRI') return `0x${val.toString(16).padStart(2, '0')}${val ? `（屏蔽优先级 ≥ ${val >> 4} 的中断）` : '（不屏蔽）'}`;
  return String(val & 1);
}
