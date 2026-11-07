// 历史 SBA 对照模型。工作台的 RTT Viewer 使用 OpenOCD Wasm。
import { resolveHpmTarget, assertHpmIdentity } from '../targets/hpm/porting.js';

export class RiscvMem {
  constructor({ probe, jtag, dm, clockKhz = 0, log = () => {}, perWordMs = 700, target }){
    this.probe = probe;
    this.jtag = jtag;
    this.dm = dm;
    this.port = resolveHpmTarget(target || dm?.port);
    this.clockKhz = clockKhz;
    this.perWordMs = perWordMs;      // 单字读的墙钟上限（正常 ~0.2 ms；给 700 ms 已经很宽松）
    this.log = log;
    this.name = `RISC-V/JTAG · SBA ${clockKhz ? clockKhz / 1000 + 'MHz' : ''}`.trim();
    /** 不是 Cortex-M：没有 halt/run/DHCSR，"快速档"也不适用（保持 false，界面不会去切它）*/
    this.fast = false;
    this.isRiscv = true;
    /**
     * 🚨 **别去读 RTT 的通道名**（`protocol.js` 的 `name()` 会看这个标志跳过）。
     *
     * 通道名是目标内存里的一个指针（`sName`），固件把它放在哪不受我们控制 —— HPM6800EVK 的
     * flood/scope 靶子都放在 **XIP flash（0x8000cf1c）**，而 SBA 读那个窗口**会永久挂起**：
     * 读请求发出去就不回来，`sbbusy` 一直不落，**整条链路跟着不应答**。
     *
     * 真机现场（2026-10，`make hw-campaign-hpm`）就是被它拖死的：
     *   · 首次轮询里夹着这次读 → 撞 1.2 s 超时（`name()` 的保护）→ 超时后那笔事务仍挂着，
     *     下一次 SBA 访问把 `sbcs.sbbusyerror` 置起来（实测 0x4c0ca2 = 只有 bit22）
     *     → 后续每个环读都直接失败（状态栏“SBA 读 0x12410e4 出错”）；
     *   · 就算侥幸没脏，SDK 的 1.2 s + DM 复位 + 叫醒 DM 那一串也要 ~2 s，全被算进
     *     “Viewer 速率”的计时窗口里（实测 8 s 窗口里 0 次轮询 → 判决 0.0 KB/s）。
     * 名字只是锦上添花，ARM 那条路（AHB-AP 读 flash 没问题）照旧读，RISC-V 这边直接不读。
     */
    this.skipNames = true;
  }

  readMem(addr, len){ return this._read(addr, len); }
  writeMem(addr, bytes){ return this.dm.writeMem(addr, bytes); }

  /** 普通 RTT 读取使用共用 SBA 的有界清理；失败不改变目标执行状态。 */
  async _read(addr, len){
    await this.port.hooks.checkMemoryReady(this, addr, len);
    return await this.dm.readMem(addr, len, this.perWordMs);
  }

  /** 错位读/卡住时的自救：重新把链路开一遍（TAP 复位 + DM 唤醒），不重开 USB */
  async recover(){
    try { await this.dm.sbaClearErrors(); } catch {}
    const r = await this.dm.init();
    assertHpmIdentity(this.port, r);
    this.log(`RISC-V 链路已重置：idcode=0x${Number(r?.idcode || 0).toString(16)}`);
    return r;
  }

  async disconnect(){
    try { await this.dm?.sbaClearErrors(); } catch {}
    try { await this.probe?.disconnect(); } catch {}
  }

  info(){
    return {
      kind: 'riscv',
      idcode: this.dm?.idcode,
      dmstatus: this.dm?.lastDmstatus,
      sbcs: this.dm?.lastSbcs,
      scans: this.dm?.scans,
      clockKhz: this.clockKhz,
    };
  }
}
