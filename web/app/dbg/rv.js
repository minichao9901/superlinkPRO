/**
 * RISC-V（RV32GC）指令的**最小**解码 —— 与 `thumb.js` 在 ARM 那条路上做的事一一对应：
 *   ① 一条指令多长（2 / 4 字节，RVC 压缩指令让"下一条指令"不再是 pc+4）；
 *   ② 这条指令是不是**带链接的调用**（`jal rd=ra` / `jalr rd=ra` / `c.jal` / `c.jalr`）以及跳到哪；
 *   ③ **执行完之后 PC 可能在哪**（分支有两个候选）—— 「断点单步」的地基。
 *
 * 为什么必须自己解：这颗探针/内核**不执行 C_STEP 等价物之外的任何"魔法"** ——
 * RISC-V 的单步靠 `dcsr.step`（我们能用），但"单步跳过/进入/跳出"要靠
 * "在下一条指令上放临时触发器"，落点算错就会白等 2 秒再把核停到随机位置
 * （ARM 那边就是踩了这个坑才补出 `nextAddrsOf`，见 app/dbg/thumb.js 的注释）。
 *
 * 不做的：完整反汇编、浮点/向量、RV64（本靶子是 RV32）、C 扩展的全部分支形式
 * （只做会改变 PC 的那几种：c.j / c.jal / c.jr / c.jalr / c.beqz / c.bnez）。
 *
 * 编码依据：RISC-V 非特权规范 v2.2（RV32I 第 2 章）与 C 扩展（第 16 章）。
 */

/** 指令长度：低两位 != 11 → 2 字节（压缩）；否则 4 字节（RV32 没有 6 字节的 RV64 形式） */
export const rvLen = hw => ((hw & 0x3) === 0x3 ? 4 : 2);

/**
 * 半字对齐 + **无符号**归一化。
 * 🚨 别写 `(x >>> 0) & ~1`：`&` 是 32 位有符号运算，0x800058d8 会变成**负数**，
 *    和 `>>> 0` 出来的无符号值一比就永远不相等（本仓在 RISC-V 上正是这么翻过车的，
 *    见 `app/dbg/session.js` 的 `_bpAt()` 注释）。
 */
const align2 = x => (((x >>> 0) & ~1) >>> 0);

const sext = (v, bits) => { const m = 1 << (bits - 1); return ((v & ((1 << bits) - 1)) ^ m) - m; };

/** JAL（opcode 1101111）：{target, rd}；不是 JAL 返回 null */
export function decodeJal(hw1, hw2, pc){
  if ((hw1 & 0x7f) !== 0x6f) return null;
  const rd = (hw1 >>> 7) & 0x1f;
  const imm = (((hw2 >>> 31) & 1) << 20) | (((hw2 >>> 12) & 0xff) << 12) | (((hw2 >>> 20) & 1) << 11)
            | (((hw2 >>> 21) & 0x3ff) << 1);
  return { rd, target: align2((pc >>> 0) + sext(imm, 21)), kind: 'jal' };
}

/** JALR（opcode 1100111）：{rd, rs1, imm}；目标要读寄存器才能算 */
export function decodeJalr(hw1, hw2){
  if ((hw1 & 0x7f) !== 0x67 || (hw1 & 0x7000) !== 0) return null;
  return { rd: (hw1 >>> 7) & 0x1f, rs1: (hw1 >>> 15) & 0x1f, imm: sext(hw2 >>> 20, 12), kind: 'jalr' };
}

/** AUIPC（opcode 0010111）：{rd, imm}（高位立即数，要 <<12） */
export function decodeAuipc(hw1, hw2){
  if ((hw1 & 0x7f) !== 0x17) return null;
  return { rd: (hw1 >>> 7) & 0x1f, imm: (hw2 & 0xfffff000) | 0 };
}

/**
 * "带链接的调用"的目标地址（不读寄存器时只能算直跳的那几种）。
 * @returns {{target:number, kind:'jal'|'jalr'|'c.jal'|'c.jalr', viaReg?:number}|null}
 */
export function decodeCall(hw1, hw2, pc){
  const jal = decodeJal(hw1, hw2, pc);
  if (jal && jal.rd === 1) return { target: jal.target, kind: 'jal' };
  const jalr = decodeJalr(hw1, hw2);
  if (jalr && jalr.rd === 1) return { target: null, kind: 'jalr', viaReg: jalr.rs1 };   // 目标在寄存器里
  return null;
}

/** 这条指令是不是"返回"（`jalr x0, 0(x1)` / `c.jr ra`） */
export function isRet(hw1, hw2 = 0){
  const jalr = decodeJalr(hw1, hw2);
  if (jalr && jalr.rd === 0 && jalr.rs1 === 1 && jalr.imm === 0) return true;
  return (hw1 & 0xffff) === 0x8082;                                  // c.jr ra
}

/**
 * 执行完这条指令之后，PC **可能**在哪（1 或 2 个候选）。
 *
 * @param {number} pc
 * @param {{readHalf:(a:number)=>Promise<number>, readReg?:(n:string|number)=>Promise<number>}} io
 * @returns {Promise<{addrs:number[], why:string, certain:boolean}>}
 */
export async function nextAddrsOf(pc, { readHalf, readReg } = {}){
  const p = align2(pc);
  const hw1 = (await readHalf(p)) & 0xffff;
  const len = rvLen(hw1);
  const fb = (p + len) >>> 0;
  const out = (addrs, why, certain = true) => ({ addrs: addrs.map(a => align2(a)), why, certain });
  const reg = async (n) => { if (!readReg) return null; try { const v = await readReg(n); return v == null ? null : (v >>> 0); } catch { return null; } };

  // ---- 压缩指令（16 位）----
  if (len === 2){
    const funct3 = (hw1 >>> 13) & 0x7;
    const op = hw1 & 0x3;
    if (op === 0x1){
      if (funct3 === 0x5){                                            // C.J
        const imm = (((hw1 >>> 12) & 1) << 11) | (((hw1 >>> 11) & 1) << 4) | (((hw1 >>> 9) & 0x3) << 8)
                  | (((hw1 >>> 8) & 1) << 10) | (((hw1 >>> 7) & 1) << 6) | (((hw1 >>> 6) & 1) << 7)
                  | (((hw1 >>> 3) & 0x7) << 1) | (((hw1 >>> 2) & 1) << 5);
        return out([(p + sext(imm, 12)) >>> 0], 'c.j 跳转');
      }
      if (funct3 === 0x1){                                            // C.JAL（RV32 才有）→ 调用
        const imm = (((hw1 >>> 12) & 1) << 11) | (((hw1 >>> 11) & 1) << 4) | (((hw1 >>> 9) & 0x3) << 8)
                  | (((hw1 >>> 8) & 1) << 10) | (((hw1 >>> 7) & 1) << 6) | (((hw1 >>> 6) & 1) << 7)
                  | (((hw1 >>> 3) & 0x7) << 1) | (((hw1 >>> 2) & 1) << 5);
        return out([(p + sext(imm, 12)) >>> 0], 'c.jal 调用（进入被调函数）');
      }
      if (funct3 === 0x6 || funct3 === 0x7){                          // C.BEQZ / C.BENEZ
        const imm = (((hw1 >>> 12) & 1) << 8) | (((hw1 >>> 10) & 0x3) << 3) | (((hw1 >>> 5) & 0x3) << 6)
                  | (((hw1 >>> 3) & 0x3) << 1) | (((hw1 >>> 2) & 1) << 5);
        return out([(p + sext(imm, 9)) >>> 0, fb], 'c.beqz/c.bnez（两个落点都放比较器）');
      }
    }
    if (op === 0x2){
      const funct4 = (hw1 >>> 12) & 0xf;
      const rs1 = (hw1 >>> 7) & 0x1f;
      if (funct4 === 0x8 && rs1 !== 0){                               // C.JR
        const v = await reg(rs1);
        return v == null ? out([fb], `c.jr x${rs1}（寄存器读不到）`, false)
                         : out([v], rs1 === 1 ? 'c.jr ra（返回）' : `c.jr x${rs1}`);
      }
      if (funct4 === 0x9 && rs1 !== 0){                               // C.JALR → 调用
        const v = await reg(rs1);
        return v == null ? out([fb], `c.jalr x${rs1}（寄存器读不到）`, false) : out([v], `c.jalr x${rs1}（调用）`);
      }
    }
    return out([fb], '其它压缩指令（顺序执行）');
  }

  // ---- 32 位指令 ----
  const hw2 = (await readHalf((p + 2) >>> 0)) & 0xffff;
  const opcode = hw1 & 0x7f;

  if (opcode === 0x6f){                                               // JAL
    const jal = decodeJal(hw1, hw2, p);
    return out([jal.target], jal.rd === 1 ? 'jal ra（调用，进入被调函数）' : 'jal 跳转');
  }
  if (opcode === 0x67){                                               // JALR
    const jalr = decodeJalr(hw1, hw2);
    const v = await reg(jalr.rs1);
    if (v == null) return out([fb], `jalr x${jalr.rs1}（寄存器读不到）`, false);
    const target = align2(v + jalr.imm);
    const why = (jalr.rd === 0 && jalr.rs1 === 1 && jalr.imm === 0) ? 'jalr x0,0(ra)（返回）'
              : jalr.rd === 1 ? 'jalr ra（调用，进入被调函数）' : 'jalr 跳转';
    return out([target], why);
  }
  if (opcode === 0x63){                                               // 条件分支
    const imm = (((hw2 >>> 31) & 1) << 12) | (((hw2 >>> 7) & 1) << 11) | (((hw2 >>> 25) & 0x3f) << 5)
              | (((hw2 >>> 8) & 0xf) << 1);
    return out([(p + sext(imm, 13)) >>> 0, fb], '条件分支（两个落点都放比较器）');
  }
  return out([fb], '其它 32 位指令（顺序执行）');
}

/**
 * 地址 `R` 处**刚刚执行完的那条指令**是不是"调用当前所在函数"的调用，并把目标算出来。
 *
 * 「单步跳出」的快路径靠它判 `ra` 到底是不是本帧的返回地址（ARM 那边叫 `_validReturnAddr`）：
 *   · `jal ra, f`            → 直接算目标；
 *   · `auipc ra, hi` + `jalr ra, lo(ra)` → **按两条指令重算**（远调用都是这一对，
 *     读寄存器版本读不到"当时的 ra"，所以要从 auipc 的立即数重新算）；
 *   · `c.jal f` / `auipc ra,hi` + `c.jalr ra` 同理。
 * 算不出来（比如 `jalr ra, 0(a5)` 这种从别的寄存器来的）→ 返回 null，
 * 调用方就走"单步走完本函数"的慢路径 —— **不猜**。
 *
 * @param {(addr:number, len:number)=>Promise<Uint8Array>} readBytes
 * @returns {Promise<number|null>} 目标地址（字节地址，未抹低位）
 */
export async function callEndingAt(readBytes, R){
  const r = align2(R);
  const two = await readBytes((r - 2) >>> 0, 2).catch(() => null);
  if (two){
    const hw = two[0] | (two[1] << 8);
    if ((hw & 3) !== 3){                                   // 2 字节指令结尾
      /**
       * 🚨 C.JAL 的判据是**掩码**，不是整字相等（2026-10 真机定因）：
       *    C.JAL = quadrant 1（bit[1:0]=01）+ funct3=001 → 固定位是 `0x2001` 那 16 位里的
       *    `0xE003` 掩码部分，其余 11 位是立即数。早先写成 `hw === 0x2001`，
       *    于是**只有"跳到偏移 0"这种立即数全 0 的 c.jal 才认得出**，实际代码里一条都认不出：
       *    现场（HPM6800EVK）`engine_deep_l4` 用 `c.jal`（0x37d1）调 `deep_l5`，
       *    `callEndingAt()` 因此返回 null → 「单步跳出」判定"ra 不是本帧返回地址" →
       *    改走"一步步走完本函数"的慢路径（几百条指令，还常常走不出去）。
       */
      if ((hw & 0xe003) === 0x2001){                       // c.jal（RV32）
        const imm = (((hw >>> 12) & 1) << 11) | (((hw >>> 11) & 1) << 4) | (((hw >>> 9) & 0x3) << 8)
                  | (((hw >>> 8) & 1) << 10) | (((hw >>> 7) & 1) << 6) | (((hw >>> 6) & 1) << 7)
                  | (((hw >>> 3) & 0x7) << 1) | (((hw >>> 2) & 1) << 5);
        return align2((r - 2) + sext(imm, 12));
      }
      if (((hw >>> 12) & 0xf) === 0x9){                    // c.jalr rs1
        const rs1 = (hw >>> 7) & 0x1f;
        if (rs1 === 1) return null;                        // 目标 = ra 当时的值：算不出来
        const four = await readBytes((r - 6) >>> 0, 4).catch(() => null);
        if (four){
          const a1 = four[0] | (four[1] << 8), a2 = four[2] | (four[3] << 8);
          const au = decodeAuipc(a1, a2);
          if (au && au.rd === rs1) return align2((r - 6) + au.imm);
        }
      }
      return null;
    }
  }
  const four = await readBytes((r - 4) >>> 0, 4).catch(() => null);
  if (!four) return null;
  const hw1 = four[0] | (four[1] << 8), hw2 = four[2] | (four[3] << 8);
  const jal = decodeJal(hw1, hw2, (r - 4) >>> 0);
  if (jal && jal.rd === 1) return jal.target;
  const jalr = decodeJalr(hw1, hw2);
  if (jalr && jalr.rd === 1){
    if (jalr.rs1 === 1) return null;                       // jalr ra, imm(ra)：目标 = 当时的 ra，算不出来
    const prev = await readBytes((r - 8) >>> 0, 4).catch(() => null);
    if (prev){
      const p1 = prev[0] | (prev[1] << 8), p2 = prev[2] | (prev[3] << 8);
      const au = decodeAuipc(p1, p2);
      if (au && au.rd === jalr.rs1) return align2((r - 8) + au.imm + jalr.imm);
    }
  }
  return null;
}

/** 架构描述子：给 `DebugSession` 里的"按架构分流"用（ARM 那份在 thumb.js 里的 ARM_ARCH，同形）*/
export const RV_ARCH = {
  name: 'riscv',
  LR: 'ra', SP: 'sp', PC: 'pc',
  insnLen: rvLen,
  decodeCall,
  nextAddrsOf,
  callEndingAt,
  /** RV 的"不像返回地址"判据：0（从没调用过）/ 全 1 / 低位不是 0（RISC-V 没有 Thumb 位）*/
  retLooksValid: v => v !== 0 && v !== 0xffffffff && (v & 1) === 0,
  retBadMsg: v => `ra = 0x${(v >>> 0).toString(16)} 不像返回地址（0 = 这一层还没调用过任何函数；`
    + '全 1 = 核刚复位）—— 没法"跳出"',
};
