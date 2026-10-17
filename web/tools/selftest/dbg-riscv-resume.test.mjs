/**
 * RISC-V「继续」不许盲信缓存（纯 Node，不碰硬件）。
 *
 * 2026-10 真机现场（HPM6800EVK + tcpecho，用户路径 b main → 复位并停 → c）：
 *   `[dm] 抽象命令失败（读寄存器 0x7b0）：抽象命令出错（cmderr=4，abstractcs=0x80004004）`
 *   → 清 cmderr 后重试一次 → 还是 cmderr=4。
 *
 * 读 dcsr 是**抽象命令**，要求 hart 真的停着；而 `this.halted` 只是缓存 —— 自愈（复位 DM）、
 * 外部复位、别处一次 resume 都可能已经把核放跑。老 `run()` 第一步就无条件读 dcsr，于是把
 * 一条"其实只是状态漂移"的假错误推给了用户。现在先按 dmstatus 现场核一遍。
 *
 * 这个用例把纪律钉死：**dmstatus 说在跑时，一条抽象命令都不许下发**，而且不许抛错。
 */
import assert from 'node:assert/strict';
import { RiscvDebugSession } from '../../app/dbg/riscv.js';
import { DMCONTROL } from '../../app/flash/hpm/jtag.js';

const DMSTATUS_HALTED = 0x4c03a2;   // bits[9:8] = 3 → allhalted|anyhalted（HPM 实测布局）
const DMSTATUS_RUNNING = 0x4f0ca2;  // bits[9:8] = 0、bits[11:10] = 3 → allrunning|anyrunning

function rig(dmstatus){
  const calls = { dmiWrite: [], readReg: [], writeReg: [], logs: [] };
  const dm = {
    _ctl: (hart, extra = 0) => ((DMCONTROL.dmactive | DMCONTROL.hartsel(hart) | extra) >>> 0),
    async dmiRead(a){ calls.dmiWrite.push(`dmiRead:0x${(a >>> 0).toString(16)}`); return a === 0x11 ? dmstatus : 0; },
    async dmiWrite(a, v){ calls.dmiWrite.push(`dmiWrite:0x${(a >>> 0).toString(16)}=0x${(v >>> 0).toString(16)}`); },
    async readReg(r){ calls.readReg.push(r); return 0x80003000; },      // 抽象命令：被调用即"下发了一条"
    async writeReg(r, v){ calls.writeReg.push([r, v]); },
  };
  const s = Object.create(RiscvDebugSession.prototype);
  Object.assign(s, { dm, halted: true, pc: 0x80003000, _log(t){ calls.logs.push(t); } });
  return { s, calls };
}

// ① 缓存说停着、dmstatus 说在跑：不许下发任何抽象命令，不许抛，缓存要按硬件纠正
{
  const { s, calls } = rig(DMSTATUS_RUNNING);
  await s.run();
  assert.equal(calls.readReg.length, 0, '校验失败：核在跑时仍然下了抽象命令（读 dcsr / PC）');
  assert.equal(s.halted, false, '按 dmstatus 纠正成"在跑"');
  const last = calls.dmiWrite.filter(x => x.startsWith('dmiWrite:0x10=')).pop() || '';
  assert.ok(last, '要补一次 dmcontrol 写（幂等的 resumereq）');
  assert.equal(parseInt(/=0x([0-9a-f]+)$/.exec(last)[1], 16) & DMCONTROL.resumereq, DMCONTROL.resumereq,
    '收尾这次 dmcontrol 必须带 resumereq（RISC-V 里"清 haltreq"不会让核跑起来）');
  assert.equal(parseInt(/=0x([0-9a-f]+)$/.exec(last)[1], 16) & DMCONTROL.haltreq, 0, '不能留着 haltreq');
  assert.ok(calls.logs.some(t => /其实在跑/.test(t)), '要如实告诉用户"目标其实在跑"');
}

// ② 真的停着：照常读 dcsr、清 step、resume（别为了"稳"把正常路径也跳过）
{
  const { s, calls } = rig(DMSTATUS_HALTED);
  s.halted = true;
  await s.run();
  assert.equal(calls.readReg.length, 1, '停着时应当读一次 dcsr');
  assert.equal(s.halted, false);
  assert.ok(calls.dmiWrite.some(x => x.startsWith('dmiWrite:0x10=')), '要下发 resumereq');
  assert.equal(calls.logs.some(t => /其实在跑/.test(t)), false, '正常路径不该报"其实在跑"');
}

// ③ dmstatus 读不到：不允许根据缓存下发抽象命令。
{
  const { s, calls } = rig(DMSTATUS_HALTED);
  s.dm.dmiRead = async () => { throw new Error('DMI 超时'); };
  await assert.rejects(s.run(), /DMI 超时/);
  assert.equal(calls.readReg.length, 0, '状态未知时不下发抽象命令');
  assert.equal(calls.dmiWrite.some(x=>x.startsWith('dmiWrite:0x10=')), false);
}

console.log('RISC-V 继续：dmstatus 现场核一遍（在跑 ⇒ 不下发抽象命令、只纠正状态、resumereq 幂等），停着照常，读不到保持失败且不发抽象命令 PASS');
