/**
 * ETM 复位入口：**"放行后停机"不等于"放行失败"**（审查 R02 的回归）。
 *
 * 2026-10-18 真机现场（H743 + main / cdc_acm_init 两个起点）：
 *   两个入口都正确命中了（最终 PC 与请求地址完全一致），生产 `reset()` 却报
 *   `复位重试后仍失败：放行失败：DHCSR 的 C_HALT 清不掉`，并多做了一次系统复位。
 *   原因：放行后 30 ms 只看 S_HALT —— 初始化很快时，核在这 30 ms 内命中业务断点再次停机，
 *   这正是成功结果，却被当成"写没落地"连写 6 次。
 *
 * 这里把三种放行后状态钉死：
 *   ① 30 ms 内命中起点        → 成功，只复位一次、只放行一次；
 *   ② 6 次放行后仍停在复位处  → 仍报"放行失败"（PPB 写真的没落地时的兜底）；
 *   ③ 停在第三处              → 直接按 unexpectedHalt 报告（不再掩盖真因）。
 *
 * 纯 Node：用内存模型模拟 DHCSR/PC/FPB，不碰探针。
 */
import assert from 'node:assert/strict';
import {EtmTarget} from '../../../etm/web/app/target.js';

const ADDRESS = 0x08002002;

function rig({hit = true, haltPC = ADDRESS, ppbWriteLands = true} = {}){
  const mem = new Map([[0xe0002000, 0x61], [0xe000edfc, 0x01000000]]);
  const stats = {resets: 0, releases: 0, hits: 0};
  let halted = true, pc = 0x08001000;
  const t = new EtmTarget();
  t.probe = {recoverDebugPort: async () => {}, isHalted: async () => halted};
  t.halt = async () => { halted = true; };
  t.stopTrace = async () => {};
  t.pc = async () => pc;
  t.inspect = async () => ({halted, entryPC: pc});
  // DHCSR：bit0 = C_DEBUGEN，bit17 = S_HALT（与生产读取一致）
  t.read = async a => a === 0xe000edf0 ? (1 | (halted ? (1 << 17) : 0)) : (mem.get(a) || 0);
  t.write = async (a, v) => {
    mem.set(a, v >>> 0);
    if (a === 0xe000ed0c){                       // AIRCR：系统复位
      stats.resets++; halted = true; pc = 0x08001000;
      for (let i = 0; i < 6; i++) mem.set(0xe0002008 + 4 * i, 0);   // 复位清空 FPB 比较器
    }
    if (a === 0xe000edf0){
      if (v & 2){ halted = true; return; }        // 保持暂停
      if (!ppbWriteLands){ return; }              // 模拟这颗探针的 PPB 写"回读成功但不落地"
      stats.releases++;
      halted = false;
      if (hit && (mem.get(0xe0002008) & 1)){ pc = haltPC; halted = true; stats.hits++; }
    }
  };
  return {t, stats};
}

// ① 命中业务起点：必须成功，不能报"放行失败"，也不能多做一次复位
{
  const {t, stats} = rig();
  const info = await t.reset({address: ADDRESS, timeout: 50});
  assert.equal(info.entryPC, ADDRESS, '应当停在请求的入口');
  assert.equal(stats.hits, 1, '应当发生一次正确的断点命中');
  assert.equal(stats.resets, 1, '只允许一次系统复位（旧实现会因误判再重试一次）');
  assert.equal(stats.releases, 1, '命中后不应继续重复写放行');
  t.bus.close();
}

// ② PPB 写不落地（每次回读仍是停在复位处）：仍要报"放行失败"，不能被"停机=成功"吞掉
{
  const {t, stats} = rig({ppbWriteLands: false});
  await assert.rejects(t.reset({address: ADDRESS, timeout: 50}), /复位重试后仍失败：放行失败/);
  assert.equal(stats.resets, 2, '失败路径 = 首次 + 一次自动重试（f9daf69 的兜底）');
  t.bus.close();
}

// ③ 停在第三处：直接报告"停在 0x…，未到达起点"，不再连写 6 次
{
  const {t} = rig({haltPC: 0x08003000});
  await assert.rejects(t.reset({address: ADDRESS, timeout: 50}), /停在 0x08003000.*未到达起点/);
  t.bus.close();
}

console.log('etm-reset-hit: 命中即成功 / PPB 不落地仍失败 / 停错处如实报告 —— PASS');
