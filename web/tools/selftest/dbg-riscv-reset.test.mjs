/**
 * RISC-V「复位并跑」的顺序回归（纯 Node，不碰硬件）。
 *
 * 2026-10 真机现场（HPM6800EVK + tcpecho）：先 `b main`、再点「复位并跑」→
 *   `复位后重新下发断点失败：抽象命令出错（cmderr=4，abstractcs=0x80004004）`
 * 根因是老 `resetRun()` 在 **ndmreset 还按着**的时候就写触发器 —— 核在复位态、dmstatus 不报
 * halted，抽象命令一律 cmderr=4。正确顺序是（`riscv-dm.js` 的 `_haltByReset` 就是这么做的）：
 *   按住 ndmreset+haltreq → **放开 ndmreset、保持 haltreq** → 等真的 halted → 写触发器 → 放 haltreq。
 *
 * 这个用例把顺序钉死：任何人再把触发器写到"放开复位之前"，这里立刻红。
 */
import assert from 'node:assert/strict';
import { RiscvDebugSession } from '../../app/dbg/riscv.js';
import { DMCONTROL } from '../../app/flash/hpm/jtag.js';

const seq = [];
const dm = {
  _ctl: (hart, extra = 0) => ((DMCONTROL.dmactive | DMCONTROL.hartsel(hart) | extra) >>> 0),
  /** 真流程里这一步会「按住 ndmreset+haltreq → 放开 ndmreset 保持 haltreq → 复验 → waitHalted」*/
  async _haltByReset(hart){ seq.push(`haltByReset:${hart}`); },
  async dmiWrite(a, v){ seq.push(`dmiWrite:0x${(a >>> 0).toString(16)}=0x${(v >>> 0).toString(16)}`); },
  async dmiRead(a){ seq.push(`dmiRead:0x${(a >>> 0).toString(16)}`); return a===0x11?0x4c03a2:0; },
  async writeReg(r, v){ seq.push(`writeReg:0x${(r >>> 0).toString(16)}=0x${(v >>> 0).toString(16)}`); },
  async readReg(r){ seq.push(`readReg:0x${(r >>> 0).toString(16)}`); return 0x21800000; },
};

const s = Object.create(RiscvDebugSession.prototype);
Object.assign(s, {
  dm, halted: true, bps: [0x8000790c], bpNotes: new Map(),
  caps: { numCode: 2 }, pc: 0, _log(){},
  refresh: async () => {}, refreshRegs: async () => {},
});

await s.resetRun();

const iHalt = seq.findIndex(x => x.startsWith('haltByReset'));
const iTrig = seq.findIndex(x => x.startsWith('writeReg:0x7a0'));           // tselect
const last = seq.filter(x => x.startsWith('dmiWrite:0x10=')).pop() || '';
const lastVal = parseInt(/=0x([0-9a-f]+)$/.exec(last)?.[1] || '0', 16);   // 十六进制，别用 Number()

assert.ok(iHalt >= 0, '必须走 dm._haltByReset（复位 + 放开 ndmreset + 等停住）');
assert.ok(iTrig > iHalt, `触发器要在"放开复位并停住"之后才写（实到：${JSON.stringify(seq)}）`);
assert.equal(lastVal & DMCONTROL.haltreq, 0, '收尾那次 dmcontrol 不能留着 haltreq');
/**
 * 🚨 收尾必须是 **resumereq**，不能只是"清 haltreq"（2026-10 真机实测）：
 *    复位流程是"按下复位 → 停住（haltreq）→ 写触发器 → 放开"，这一步核是**被 haltreq 停住的**，
 *    而 RISC-V 里清 haltreq **不会**让它跑起来 —— 只有写 resumereq 才会。
 *    实测症状：只清 haltreq 时核一直停在复位向量（dpc=0x80003000=_start、dcsr.cause=3=haltreq）。
 */
assert.ok(lastVal & DMCONTROL.resumereq, '收尾必须 resumereq（清 haltreq 不会让核跑起来）');
assert.equal(s.halted, false, 'resetRun 结束后页面状态是"运行中"');

console.log('dbg-riscv-reset: 复位并跑 = 先放开复位并停住 → 再写触发器 → 再放 haltreq（顺序钉死）PASS');

/**
 * 传输层硬闸：**SBA 一律不许碰 XIP/flash 窗口**（0x8000_0000–0x9000_0000）。
 *
 * 2026-10 真机：这颗芯片上 SBA 读该窗口会把事务永久挂住（sbcs 常驻 sbbusy|sbbusyerror），
 * 之后**每一次**内存读都失败，只 dm.init() 解不开。以前只在调试页 memRead 里挡，
 * 任何别的调用路径（页面混旧 JS、以后新加的读者）都能把 DM 搞坏 —— 现在闸门在传输层，
 * 要求：**抛错就走人，一个 DMI 命令都不许发**。
 */
{
  const { RiscvTransport } = await import('../../app/flash/hpm/riscv-dm.js');
  const sent = [];
  const t = Object.create(RiscvTransport.prototype);
  t.dmiWrite = async () => { sent.push('write'); };
  t.dmiRead = async () => { sent.push('read'); return 0; };
  let err = '';
  try { await t.readMem(0x80000500, 512); } catch (e){ err = String(e?.message || e); }
  assert.ok(/XIP\/flash 窗口/.test(err), 'SBA 读 flash 窗口必须当场报错：' + err);
  assert.equal(sent.length, 0, '报错前不许发任何 DMI 命令（发了就说明还在试探）：' + JSON.stringify(sent));
  // RAM 照旧放行（不然 flash 烧录的暂存区读写会被误伤）
  const t2 = Object.create(RiscvTransport.prototype);
  let touched = 0;
  t2.sbaConfig = async () => { touched++; };
  t2.dmiWrite = async () => { touched++; };
  t2.dmiRead = async () => { touched++; return 0x4c03a2; };
  await t2.readMem(0x4000b61c, 4).catch(() => {});
  assert.ok(touched > 0, 'RAM 读必须照旧走 SBA（没有被闸门误伤）');
}

console.log('dbg-riscv-reset: SBA 传输层拒绝 XIP 窗口（零 DMI 命令）PASS');

/**
 * `cont()` 要能容忍"界面说已停、硬件其实在跑"（SBA 自愈做过 ndmreset 之后就会这样）：
 * 用户现场 `b main` → `reset` → `c` 报 `cmderr=4`（读 PC 的抽象命令要求先停住）就是这个。
 * 修法：读 PC 失败 → 按 dmstatus 刷新一次 → 真在跑就把"继续"当已完成（返回 true），
 * 仍然停着才把原错抛出去。
 */
{
  const { DebugSession } = await import('../../app/dbg/session.js');
  const mk = (after) => {
    const s = Object.create(DebugSession.prototype);
    Object.assign(s, {
      halted: true, frames: null,
      clearFrames(){}, run: async () => { throw new Error('不该走到 run()'); },
      readReg: async () => { throw new Error('抽象命令出错（cmderr=4，abstractcs=0x80004004）'); },
      refresh: async () => { s.halted = after; },
      _bpAt: () => undefined,
    });
    return s;
  };
  const running = mk(false);
  assert.equal(await running.cont(), true, '硬件其实在跑 → 继续视为已完成，不抛错');
  const stillHalted = mk(true);
  await assert.rejects(() => stillHalted.cont(), /cmderr=4/, '确实还停着 → 原错照抛（不掩盖真故障）');
}

console.log('dbg-riscv-reset: cont() 容忍"状态与硬件不一致"（自愈之后接着敲 c）PASS');
