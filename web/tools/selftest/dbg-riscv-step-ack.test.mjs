/**
 * RISC-V 单步：**旧 resumeack 不能当成本次执行的证据**（审查 R12 的回归）。
 *
 * 2026-10-18 定因（`7e99739` 的后续）：`step()` 里旧 ack 清不掉时选择"继续单步"，
 * 但 `_waitResumed()` 仍把 `resumeack=1` 立即当成功 —— 两个假设互相矛盾。
 * 当 DM 还没处理新的 resumereq 时，单步会"报告成功并清掉 dcsr.step"，实际一条都没执行。
 *
 * 现在的契约：
 *   ① 旧 ack 清不掉 + 没有 running/PC 变化 → **明确失败**（不再假报 step 成功）；
 *   ② 旧 ack 清不掉 + PC 真的变了        → 仍算成功（独立证据足够）；
 *   ③ 正常（ack 是新边沿）                → 照旧成功。
 *
 * 纯 Node 模型，不碰硬件。
 */
import assert from 'node:assert/strict';
import {RiscvDebugSession} from '../../app/dbg/riscv.js';
import {DMCONTROL} from '../../app/flash/hpm/jtag.js';

const PC0 = 0x80003000;
const DMSTATUS_HALTED = 0x4c03a2;          // bits[9:8]=3，HPM 实测布局
const STALE_ACK = 0x00030000;              // resumeack[17:16] = 3（旧应答没清）

function rig({ackStuck = true, movesOnResume = false} = {}){
  const log = [];
  let resumed = false, resumereqSent = 0, controlWrites = 0;
  const dm = {
    _ctl: (hart, extra = 0) => ((DMCONTROL.dmactive | DMCONTROL.hartsel(hart) | extra) >>> 0),
    async dmiRead(a){
      if (a !== 0x11) return 0;
      return DMSTATUS_HALTED | (ackStuck ? STALE_ACK : 0);
    },
    async dmiWrite(a, value){
      if (a !== 0x10) return;
      if (value & DMCONTROL.resumereq){ resumereqSent++; if (movesOnResume) resumed = true; }
      else controlWrites++;
    },
    async readReg(r){
      if (r === 0x7b1) return resumed ? PC0 + 4 : PC0;   // REGNO.PC
      return 0;                                          // DCSR 等
    },
    async writeReg(){},
    async waitHalted(){},
  };
  const s = Object.create(RiscvDebugSession.prototype);
  Object.assign(s, {
    dm, halted: true, pc: PC0, lastStepMode: null,
    async _ensureHalted(){}, async _withBpCleared(pc, fn){ return fn(); },
    async refresh(){}, async refreshRegs(){}, _log(t){ log.push(t); },
  });
  return {s, counters: () => ({resumereqSent, controlWrites}), log};
}

// ① 旧 ack 清不掉、DM 没处理 resumereq、PC 不动：不许报成功
{
  const {s, log} = rig({ackStuck: true, movesOnResume: false});
  await assert.rejects(s.step(), /resumeack 是上次残留/);
  assert.equal(s.lastStepMode, null, '没有确认执行就不能把 lastStepMode 记成 step');
  assert.ok(log.some(t => /resumeack 未清零/.test(t)), '仍要如实记录旧 ack 未清零');
}

// ② 旧 ack 清不掉，但 PC 确实动了：独立证据足够，算成功
{
  const {s, counters} = rig({ackStuck: true, movesOnResume: true});
  assert.equal(await s.step(), 'step');
  assert.ok(counters().resumereqSent >= 1, '必须真的下发 resumereq');
}

// ③ ack 是新边沿（正常路径）：照旧成功
{
  const {s} = rig({ackStuck: false, movesOnResume: true});
  assert.equal(await s.step(), 'step');
}

console.log('dbg-riscv-step-ack: 旧 ack 不可当证据 / PC 证据可用 / 正常路径不变 —— PASS');
