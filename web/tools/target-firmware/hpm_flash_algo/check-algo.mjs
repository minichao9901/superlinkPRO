/**
 * 烧录算法 blob 的**结构自检**（RV32 机器码级，构建后立刻跑）。
 *
 * 为什么要这么一层：2026-10 真机 bring-up 时踩到一个只在硬件上才暴露的坑 ——
 *   `memset.c` 里那个"字节循环"被 GCC 的 loop idiom recognition
 *   （`-ftree-loop-distribute-patterns`）识别成 `memset` 调用，而这个函数**就是** memset，
 *   于是编出来的代码是「prologue → 调自己 → epilogue」，没有一条 `sb`：
 *
 *       538: c.addi sp,-16
 *       544: c.jal  538 <memset>      ← 调自己，死循环
 *
 *   后果：flash_init 第一步 memset(nor_config,0,256) 就转死，核再也不停，
 *   连 haltreq 都抓不住（页面现象是"烧录卡死/永远不结束"）。
 *   这类"语义上合法、机器码荒谬"的错误，离线自测（只看入口表）和编译器都不会报，
 *   所以在这里做一次真正的指令级检查。
 *
 * 检查项：
 *   ① 入口表能认出 7 项（顺序与 func_table.S 的契约一致）
 *   ② 不存在「无条件后跳、且跨越的区间里没有任何条件分支/间接返回」的紧自循环
 *      —— 正是上面那个 bug 的形状（正常循环一定有条件分支）
 *   ③ 不存在直接的"调用自己"（jal/c.jal 的目标 = 自己）
 *
 * 用法：node tools/target-firmware/hpm_flash_algo/check-algo.mjs <blob.bin>
 *   有问题 → 打印原因并 exit 1（build.ps1 会因此失败）
 */
import { readFileSync } from 'node:fs';

/** 解 J 型（jal）立即数 */
export function jalOffset(inst){
  const x = inst >>> 0;
  let imm = ((x >>> 31) & 1) * 0x100000 + ((x >>> 21) & 0x3ff) * 2 +
            ((x >>> 20) & 1) * 0x800 + ((x >>> 12) & 0xff) * 0x1000;
  if (imm >= 0x100000) imm -= 0x200000;
  return imm;
}

/** 解 CJ 型（c.jal / c.j）立即数：imm[11] | imm[4] | imm[9:8] | imm[10] | imm[6] | imm[7] | imm[3:1] | imm[5] */
export function cjOffset(inst){
  const x = inst & 0xffff;
  let imm = ((x >>> 12) & 1) * 0x800 + ((x >>> 11) & 1) * 0x10 + ((x >>> 9) & 3) * 0x100 +
            ((x >>> 8) & 1) * 0x400 + ((x >>> 7) & 1) * 0x40 + ((x >>> 6) & 1) * 0x80 +
            ((x >>> 3) & 7) * 2 + ((x >>> 2) & 1) * 0x20;
  if (imm & 0x800) imm -= 0x1000;               // 符号位是 bit11（0x800），不是 0x1000
  return imm;
}

/**
 * 扫一遍机器码，找出可疑的"无出口自循环 / 自递归"。
 *
 * 🚨 **不能顺着指令流线性走**：blob 里夹着数据（`nor_config` 是 256 B 的静态结构），
 *    线性解码会在数据里漂掉对齐，等走到真代码那儿已经不落在指令边界上了
 *    （第一版检查就是这么漏掉 memset 那个 bug 的）。所以这里**按 2 字节枚举**候选指令，
 *    再对候选做"区间里有没有出口"的判定。
 *
 * @returns {Array<{at:number, target:number, kind:string}>}
 */
export function findSuspiciousLoops(bytes){
  const bad = [];
  const len = bytes.length;
  const u16 = o => bytes[o] | (bytes[o + 1] << 8);
  const u32 = o => (bytes[o] | (bytes[o + 1] << 8) | (bytes[o + 2] << 16) | (bytes[o + 3] << 24)) >>> 0;

  /** 这条 2 字节位置上的指令算不算"出口"（条件分支 / 返回 / 间接跳）*/
  const isExit = o => {
    if ((bytes[o] & 0x3) === 0x3){
      const inst = u32(o);
      const op = inst & 0x7f;
      if (op === 0x63) return true;                                  // 条件分支
      if (op === 0x67) return true;                                  // jalr（含 ret）
      if (op === 0x6f && ((inst >>> 7) & 0x1f) === 0) return true;    // jal x0 = 无条件跳（当作路径终点）
      return false;
    }
    const inst = u16(o);
    if ((inst & 0x3) !== 0x1) return false;
    const f3 = (inst >>> 13) & 0x7;
    if (f3 === 0x6 || f3 === 0x7) return true;                       // c.beqz / c.bnez
    if (f3 === 0x4 && ((inst >>> 12) & 0x1)) return true;            // c.jalr / c.jr
    return false;
  };

  for (let off = 0; off + 2 <= len; off += 2){
    let target = null, kind = null, isCall = false, size = 2;
    if ((bytes[off] & 0x3) === 0x3 && off + 4 <= len){
      const inst = u32(off);
      const rd = (inst >>> 7) & 0x1f;
      if ((inst & 0x7f) === 0x6f){
        target = (off + jalOffset(inst)) >>> 0; kind = rd === 0 ? 'jal x0' : 'jal'; isCall = rd !== 0; size = 4;
      }
    } else if ((bytes[off] & 0x3) === 0x1){
      const inst = u16(off);
      const f3 = (inst >>> 13) & 0x7;
      if (f3 === 0x5){ target = (off + cjOffset(inst)) >>> 0; kind = 'c.j'; }
      else if (f3 === 0x1){ target = (off + cjOffset(inst)) >>> 0; kind = 'c.jal'; isCall = true; }
    }
    if (target == null || target > off) continue;
    // 后跳（或跳到自己）：区间 [target, off) 里必须有出口，否则就是转不出来的自循环
    let hasExit = false, codeBytes = 0;
    for (let a = target; a < off; a += 2){
      if (u16(a) !== 0) codeBytes += 2;
      if (isExit(a)){ hasExit = true; break; }
    }
    if (!hasExit && codeBytes >= 2){
      bad.push({ at: off, target, size,
                 kind: kind + (isCall ? '（无条件自递归/自调用）' : '（后跳且区间内无出口 = 转不出来的死循环）') });
    }
  }
  return bad;
}

/** 顺带把入口表也认一遍（与 app/flash/hpm/entry.js 同源逻辑，这里只做形状检查） */
export function entryOffsets(bytes, max = 16){
  const out = [];
  let off = 0;
  while (off + 2 <= bytes.length && out.length < max){
    const b0 = bytes[off], b1 = bytes[off + 1];
    if ((b0 & 0x3) === 0x3){
      const inst = (b0 | (b1 << 8) | (bytes[off + 2] << 16) | (bytes[off + 3] << 24)) >>> 0;
      if ((inst & 0x7f) !== 0x6f) break;
      out.push(off); off += 4;
    } else {
      const inst = b0 | (b1 << 8);
      if (!((inst & 0x3) === 0x1 && ((inst >>> 13) & 0x7) === 0x1)) break;
      out.push(off); off += 2;
    }
    const after = bytes[off] | (bytes[off + 1] << 8);
    if (after === 0x9002) off += 2;
    else if (after === 0x0073) off += 4;
    else break;
  }
  return out;
}

/** 跑全部检查，返回问题列表（空数组 = 通过）*/
export function checkAlgo(bytes){
  const problems = [];
  const entries = entryOffsets(bytes);
  if (entries.length !== 7){
    problems.push(`入口表只认出 ${entries.length} 项（期望 7）：[${entries.map(o => '0x' + o.toString(16)).join(', ')}]`);
  } else if (entries.join(',') !== '0,6,12,18,24,30,36'){
    problems.push(`入口偏移形状不对：[${entries.map(o => '0x' + o.toString(16)).join(', ')}]（期望 0x0,0x6,0xc,0x12,0x18,0x1e,0x24）`);
  }
  for (const b of findSuspiciousLoops(bytes)){
    problems.push(`偏移 0x${b.at.toString(16)} 处 ${b.kind} → 目标 0x${b.target.toString(16)}` +
      '（💡 十有八九是 memset/memcpy 这类 libc 替身被 GCC 的循环识别优化成了递归调用：' +
      '给那个文件加 -fno-tree-loop-distribute-patterns -fno-builtin）');
  }
  return { entries, problems };
}

// ------------------------------------------------------------------ CLI
if (process.argv[1] && process.argv[1].endsWith('check-algo.mjs')){
  const path = process.argv[2];
  if (!path){ console.error('用法：node check-algo.mjs <blob.bin>'); process.exit(2); }
  const bytes = new Uint8Array(readFileSync(path));
  const { entries, problems } = checkAlgo(bytes);
  console.log(`blob ${bytes.length} B（0x${bytes.length.toString(16)}）· 入口 ${entries.length} 项：` +
    entries.map(o => '0x' + o.toString(16)).join(' '));
  if (problems.length){
    console.error('❌ 结构自检没过：');
    for (const p of problems) console.error('   · ' + p);
    process.exit(1);
  }
  console.log('✅ 结构自检通过（无无出口自循环 / 无自调用；入口表 7 项）');
}
