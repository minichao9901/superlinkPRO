/**
 * Thumb 指令的**最小**解码：只做调试器真正需要的两件事 ——
 *   ① 一条指令多长（2 还是 4 字节）—— 「断点单步」要靠它算下一条指令的地址；
 *   ② 这条指令是不是**带链接的调用**（BL / BLX）以及跳到哪 —— 源码级「单步进入」要靠它。
 *
 * 为什么值得单独一个文件：这两个判断都是**编码细节**（少看一位就整体偏 2 字节 / 算错落点），
 * 而它们不需要浏览器也不需要硬件 —— 放这里就能在 Node 里逐条喂机器码自测。
 *
 * 明确不做：完整反汇编（本页不做反汇编视图）、IT 块状态跟踪、条件执行。
 *   对"下一条指令在哪"来说，IT 块不影响长度判断（IT 指令本身是 16 位，块内也是常规编码）；
 *   对"是不是调用"来说，IT 块里不会出现 BL/BLX（它们是 32 位、不受 IT 管辖）。
 *
 * 编码依据：ARMv7-M ARM（DDI0403E）A7.7.18 BL/BLX(immediate)、A5.3.1 指令长度分组。
 */

/**
 * Thumb 指令长度（16 位半字 → 2 或 4 字节）。
 * 判据是 ARMv7-M 的编码分组：`11101`/`11110`/`11111` 开头才是 32 位，
 * 而 `11100`（0xE000~0xE7FF）是 **16 位**的无条件分支 B —— 差这一档就会把 B 当成 4 字节，
 * 断点单步的落点整体偏 2 字节。
 */
export const thumbLen = hw => ((hw & 0xf800) >= 0xe800 ? 4 : 2);

/** 16 位 BLX <reg>（寄存器间接调用）：0100 0111 1 Rm 000 —— 静态算不出目标 */
export const isCallReg = hw => ((hw & 0xff87) === 0x4780);

/**
 * 32 位 BL/BLX(immediate) 的目标地址；不是这类指令就返回 null。
 *
 * 线上形状：`11110 S imm10` + `11 J1 H J2 imm11`（H=1 → BL，H=0 → BLX）。
 * 偏移 = SignExtend(S:I1:I2:imm10:imm11:'0')，其中 I1=!J1^S、I2=!J2^S；落点 = 指令地址 + 4 + 偏移。
 *
 * @param {number} hw1 第一个半字
 * @param {number} hw2 第二个半字
 * @param {number} pc  这条指令的地址（Thumb 地址，bit0 会被忽略）
 * @returns {{target:number, kind:'bl'|'blx'}|null}
 */
export function decodeCall(hw1, hw2, pc){
  hw1 &= 0xffff; hw2 &= 0xffff;
  if ((hw1 & 0xf800) !== 0xf000) return null;        // 不是 11110 开头的 32 位分支
  if ((hw2 & 0xc000) !== 0xc000) return null;        // 第二个半字必须以 11 开头
  const isBl = (hw2 & 0x1000) !== 0;                 // H 位：1 = BL，0 = BLX
  const S = (hw1 >> 10) & 1;
  const imm10 = hw1 & 0x3ff;
  const J1 = (hw2 >> 13) & 1, J2 = (hw2 >> 11) & 1;
  const imm11 = hw2 & 0x7ff;
  const I1 = (~(J1 ^ S)) & 1, I2 = (~(J2 ^ S)) & 1;
  let off = (S << 24) | (I1 << 23) | (I2 << 22) | (imm10 << 12) | (imm11 << 1);
  if (off & 0x1000000) off -= 0x2000000;             // 25 位有符号偏移
  const base = ((((pc >>> 0) & 0xfffffffe) + 4) >>> 0);
  return { target: (base + off) >>> 0, kind: isBl ? 'bl' : 'blx' };
}

/** 是不是"带链接的调用"（32 位 BL/BLX 或 16 位 BLX <reg>）—— 源码级 step into 用它分流 */
export function isCall(hw1, hw2 = 0, pc = 0){
  return !!decodeCall(hw1, hw2, pc) || isCallReg(hw1);
}

/**
 * `decodeCall` 的反方向：把"从 `from` 调到 `to`"编成一对半字（自测造靶子用，两者互为逆运算）。
 * 只覆盖 32 位 BL/BLX(immediate) 的 ±16 MB 范围 —— 超出就抛错，别静默编出错的指令。
 * @returns {[number, number]} [hw1, hw2]
 */
export function encodeCall(from, to, { blx = false } = {}){
  const pc = (from >>> 0) & 0xfffffffe;
  const off = ((to >>> 0) - ((pc + 4) >>> 0)) | 0;
  if (off > 0xffffff || off < -0x1000000) throw new Error('BL/BLX(immediate) 的偏移超出 ±16 MB');
  const imm = off & 0x1ffffff;                       // 25 位补码
  const S = (imm >> 24) & 1;
  const I1 = (imm >> 23) & 1, I2 = (imm >> 22) & 1;
  const imm10 = (imm >> 12) & 0x3ff, imm11 = (imm >> 1) & 0x7ff;
  const J1 = (~(I1 ^ S)) & 1, J2 = (~(I2 ^ S)) & 1;
  const hw1 = (0xf000 | (S << 10) | imm10) & 0xffff;
  const hw2 = (0xc000 | (J1 << 13) | (blx ? 0 : 0x1000) | (J2 << 11) | imm11) & 0xffff;
  return [hw1, hw2];
}

/** 有符号扩展（n 位补码 → JS 整数） */
function sext(v, bits){ const m = 1 << (bits - 1); return ((v & ((1 << bits) - 1)) ^ m) - m; }

/**
 * 架构描述子（ARM/Cortex-M 这份；RISC-V 那份在 rv.js 里的 RV_ARCH，同形）。
 * `session.js` 通过 `this.arch` 分流"按架构不同"的那几处：寄存器名 / 指令长度 /
 * 调用解码 / 落点计算 / 返回地址校验。加新架构只要再写一个同形的对象。
 */
export const ARM_ARCH = {
  name: 'arm',
  LR: 'LR', SP: 'SP', PC: 'PC',
  insnLen: thumbLen,
  decodeCall,
  nextAddrsOf,
  /** "不像返回地址"的判据：0 / 全 1 / 0xFFFFFFFx（EXC_RETURN，核在异常里）*/
  retLooksValid: v => v !== 0 && v !== 0xffffffff && (v >>> 28) !== 0xf,
  retBadMsg: v => `LR = 0x${(v >>> 0).toString(16)} 不是返回地址（0xFFFFFFFx 是 EXC_RETURN：核在异常处理里，`
    + '或者已经是最外层调用）—— 没法"跳出"',
  /**
   * 地址 `R` 处**刚刚执行完的那条指令**是不是"调用 F"的调用（把目标算出来）。
   * 这就是 ARM 的签名：函数的返回地址前面那条指令必然是"调用这个函数"。
   */
  async callEndingAt(readBytes, R){
    const r = (R >>> 0) & 0xfffffffe;
    const b = await readBytes((r - 4) >>> 0, 4).catch(() => null);
    if (!b || b.length < 4) return null;
    const hw = o => (b[o] | (b[o + 1] << 8)) & 0xffff;
    const c32 = decodeCall(hw(0), hw(2), (r - 4) >>> 0);
    if (c32 && c32.kind === 'bl') return c32.target;
    return null;                                  // 16 位间接调用（blx Rn）静态算不出目标
  },
};

/**
 * **这条指令执行完之后 PC 可能在哪** —— 「断点单步」的地基。
 *
 * 🚨 为什么必须有它（2026-10 真机压测定因）：这颗探针/内核**不执行 C_STEP**
 *    （DHCSR 写进去 C_STEP，回读 0x3000f，PC 纹丝不动），于是指令级单步只能靠
 *    "在下一条指令上放临时比较器"这条兜底。而兜底对**分支/返回**指令是错的：
 *    核一跳走，pc+len 上的比较器永远不会命中 —— 旧代码白等 2 秒、再把核停在一个
 *    完全随机的位置（"单步跳出"因此在非叶子函数里连跳三次还在原地打转）。
 *    正解：先把"可能的落点"算出来，把临时比较器放在**真正会去的地方**。
 *
 * 覆盖 ARMv7-M 里真正会改变 PC 的常见形状：
 *   `bx lr` / `bx Rm` / `blx Rm` / `pop {…, pc}`（16/32 位）/ `ldr pc,[sp,#imm]` /
 *   `mov pc, lr` / `b`（16/32 位）/ `bcc`（**两个候选都返回**）/ `cbz`·`cbnz` /
 *   `bl`·`blx imm`（调用：落点就是被调函数入口）/ 其它 → 顺序执行 pc+len。
 *
 * @param {number} pc 指令地址
 * @param {(a:number)=>number} [readReg] 读寄存器（`bx Rm` 要靠它）；不传则遇到寄存器分支返回 `certain:false`
 * @param {(a:number)=>Promise<number>} [readWord] 读内存字（`pop {pc}` / `ldr pc` 要靠它）
 * @param {(a:number)=>Promise<number>} [readHalf] 读半字
 * @returns {Promise<{addrs:number[], why:string, certain:boolean}>}
 */
export async function nextAddrsOf(pc, { readHalf, readWord, readReg } = {}){
  const p = (pc >>> 0) & 0xfffffffe;
  const hw1 = (await readHalf(p)) & 0xffff;
  const len = thumbLen(hw1);
  const fb = (p + len) >>> 0;                                   // 顺序执行的落点
  const out = (addrs, why, certain = true) => ({ addrs: addrs.map(a => (a >>> 0) & 0xfffffffe), why, certain });
  const hw2 = len === 4 ? ((await readHalf((p + 2) >>> 0)) & 0xffff) : 0;
  /** 读一个寄存器（读不到就放弃"确定"这条路；**不能把读不到当成 0**，那会放个地址 0 的比较器） */
  const reg = async (n) => {
    if (!readReg) return null;
    try {
      const v = await readReg(n === 15 ? 'PC' : n === 14 ? 'LR' : n === 13 ? 'SP' : 'R' + n);
      return v == null ? null : (v >>> 0);
    } catch { return null; }
  };
  /** 读一个字（栈上取返回地址用） */
  const word = async (a) => { if (!readWord) return null; try { const v = await readWord(a >>> 0); return v == null ? null : (v >>> 0); } catch { return null; } };
  const pop8 = v => { let n = 0; for (let i = 0; i < 8; i++) if (v & (1 << i)) n++; return n; };

  // ---------------- 32 位 ----------------
  if (len === 4){
    const call = decodeCall(hw1, hw2, p);
    if (call) return out([call.target], `${call.kind} 调用（进入被调函数）`);
    if (hw1 === 0xe8bd){                                          // POP.W {…, pc}
      if (!(hw2 & 0x8000)) return out([fb], 'pop.w（不含 pc）');
      /** LDM/POP 的 PC 是**列表里的最后一个**：偏移 = (列表寄存器个数 - 1) × 4 */
      const n = pop8(hw2 & 0xff) + pop8((hw2 >> 8) & 0x1f) + (((hw2 >> 13) & 1) ? 1 : 0) + (((hw2 >> 14) & 1) ? 1 : 0) + 1;
      const sp = await reg(13);
      const v = sp == null ? null : await word(sp + (n - 1) * 4);
      return v == null ? out([fb], 'pop.w {…, pc}（栈读不到）', false) : out([v], 'pop.w {…, pc}（返回）');
    }
    if ((hw1 & 0xfff0) === 0xf8d0 && ((hw2 >> 12) & 0xf) === 0xf){ // LDR.W pc, [Rn, #imm]
      const base = await reg(hw1 & 0xf);
      const v = base == null ? null : await word(base + (hw2 & 0xfff));
      return v == null ? out([fb], 'ldr.w pc, […]（基址/内存读不到）', false) : out([v], 'ldr.w pc（返回）');
    }
    if ((hw1 & 0xf800) === 0xf000 && (hw2 & 0xd000) === 0x8000){  // B.W（32 位无条件）
      const S = (hw1 >> 10) & 1, J1 = (hw2 >> 13) & 1, J2 = (hw2 >> 11) & 1;
      const I1 = (~(J1 ^ S)) & 1, I2 = (~(J2 ^ S)) & 1;
      let off = (S << 24) | (I1 << 23) | (I2 << 22) | ((hw1 & 0x3ff) << 12) | ((hw2 & 0x7ff) << 1);
      if (off & 0x1000000) off -= 0x2000000;
      return out([(p + 4 + off) >>> 0], 'b.w 无条件跳转');
    }
    return out([fb], '其它 32 位指令（顺序执行）');
  }

  // ---------------- 16 位 ----------------
  if (hw1 === 0x4770) return out([(await reg(14)) ?? 0], 'bx lr（返回）', (await reg(14)) != null);
  /**
   * BX Rm（0x4700）/ BLX Rm（0x4780）：**bit7 是操作码的一部分**，掩码必须保留它（0xff87）——
   * 早期掩码写成 0xff07 把 bit7 抹掉了，于是 `blx r3`（间接调用！）被当成 `bx r3`（自测抓到）。
   */
  if ((hw1 & 0xff87) === 0x4700){                                // BX Rm
    const m = (hw1 >> 3) & 0xf;
    const v = await reg(m);
    return v == null ? out([fb], `bx r${m}（读不到寄存器）`, false) : out([v], m === 14 ? 'bx lr（返回）' : `bx r${m}`);
  }
  if ((hw1 & 0xff87) === 0x4780){                                // BLX Rm
    const m = (hw1 >> 3) & 0xf;
    const v = await reg(m);
    return v == null ? out([fb], `blx r${m}（读不到寄存器）`, false) : out([v], `blx r${m}（间接调用）`);
  }
  if ((hw1 & 0xff00) === 0xbd00){                                // POP {…, pc}（bit8 = P，已由掩码保证）
    const n = pop8(hw1 & 0xff) + 1;                              // PC 是最后一个
    const sp = await reg(13);
    const v = sp == null ? null : await word(sp + (n - 1) * 4);
    return v == null ? out([fb], 'pop {…, pc}（栈读不到）', false) : out([v], 'pop {…, pc}（返回）');
  }
  if ((hw1 & 0xfe00) === 0x9c00 && (hw1 & 0x0700) !== 0){        // LDR pc, [sp, #imm]
    const sp = await reg(13);
    const v = sp == null ? null : await word(sp + ((hw1 & 0xff) << 2));
    return v == null ? out([fb], 'ldr pc, [sp]（栈读不到）', false) : out([v], 'ldr pc, [sp, #imm]（返回）');
  }
  /**
   * `MOV pc, Rm`（0x46xx）/ `ADD pc, …`（0x44xx）：**这里编码很容易看错** ——
   *   `0100 0110 D Rm Rdn`（MOV）/ `0100 0100 D Rm Rdn`（ADD），
   *   目标寄存器 Rd = (D<<3) | Rdn，所以 `mov pc, lr` = **0x46F7**（Rdn=7 是低位、Rm=14 是源）。
   *   Rd=15 时才改变控制流，其它情况照常顺序执行。
   */
  if ((hw1 & 0xff00) === 0x4600 || (hw1 & 0xff00) === 0x4400){
    const isAdd = (hw1 & 0xff00) === 0x4400;
    const rm = (hw1 >> 3) & 0xf, rdn = hw1 & 7;
    const rd = (((hw1 >> 7) & 1) << 3) | rdn;
    if (rd !== 15) return out([fb], isAdd ? 'add（高寄存器）' : 'mov（高寄存器）');
    const a = await reg(rm);
    const b = isAdd ? await reg(rdn) : 0;
    if (a == null || b == null) return out([fb], 'mov/add pc（读不到寄存器）', false);
    return out([(a + b) >>> 0], isAdd ? 'add pc, …' : 'mov pc, …');
  }
  if ((hw1 & 0xf800) === 0xe000) return out([(p + 4 + (sext(hw1 & 0x7ff, 11) << 1)) >>> 0], 'b 无条件跳转');
  if ((hw1 & 0xf000) === 0xd000){                                // Bcc：两条路都可能走到
    const cond = (hw1 >> 8) & 0xf;
    if (cond === 0xf) return out([(p + 4 + (sext(hw1 & 0xff, 8) << 1)) >>> 0], 'svc');
    if (cond === 0xe) return out([fb], 'udf');
    return out([(p + 4 + (sext(hw1 & 0xff, 8) << 1)) >>> 0, fb], `b${cond} 条件分支（两个落点都放比较器）`);
  }
  if ((hw1 & 0xf500) === 0xb100){                                // CBZ / CBNZ
    const i = (hw1 >> 9) & 1, imm = (((i << 6) | ((hw1 >> 3) & 0x1f)) << 1);
    return out([(p + 4 + imm) >>> 0, fb], 'cbz/cbnz（两个落点都放比较器）');
  }
  return out([fb], '其它 16 位指令（顺序执行）');
}
