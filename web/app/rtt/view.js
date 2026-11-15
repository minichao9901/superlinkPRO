/** WebUSB RTT viewer; URL mock mode is reserved for offline checks. */
import { prepareProbeHandoff } from '../core/probe-users.js';
import { runProbeOperation } from '../core/probe-manager.js';
import { $, seg, setStatus, mhzLabel, ensureSelectOption } from '../ui/dom.js';
import { toast } from '../ui/toast.js';
import { store } from '../core/store.js';
import { webClockKhz,isHpmClockTarget } from '../core/target-clock.js';
import { constrainClockSelect } from '../ui/target-clock.js';
import { RxBuffer } from '../core/rxview.js';
import { FileRecorder, recordButtonState } from '../core/recorder.js';
import { Rtt } from './protocol.js';
import { OpenOcdProbe, selectOpenOcdDevice } from '../core/openocd-probe.js';
import { prepareOpenOcdJtag } from '../core/openocd-jtag-pins.js';
import { makeOpenOcdProfile } from '../core/openocd-profile.js';
import { HPM_BOARDS, hpmBoard } from '../targets/hpm/porting.js';
import { selectedHpmBoard, rememberHpmBoard } from '../targets/hpm/select.js';
import { MockProbe } from './mock.js';
import { findSymbol } from './elf.js';
import { parseHex, textToBytes } from '../core/hex.js';
import { waitMs,yieldTask } from '../core/pace.js';
import { rate as fRate, bytes as fBytes, fileStamp, download, stamp as stampOf } from '../core/format.js';

const EOL = { cr: '\r', crlf: '\r\n', lf: '\n', none: '' };
const MAX_RAW = 2 * 1024 * 1024;

const chipSel = () => document.getElementById('r-chip');
const chipArchOf = id => {
  const o = [...(chipSel()?.options || [])].find(x => x.value === id);
  return o?.parentElement?.dataset?.arch || 'arm';
};

const armChipId = () => {
  const v = chipSel()?.value || '';
  return chipArchOf(v) === 'arm' ? v : (store.get('rtt.armChip', store.get('rtt.ocdTarget', '')) || 'stm32f103');
};
/** 当前 RISC-V 组那颗（RAM 窗口用它；选的是 ARM 时回退到上次选过的 RISC-V 那颗）*/
const rvChipId = () => {
  const v = chipSel()?.value || '';
  return chipArchOf(v) === 'riscv' ? v : (store.get('rtt.rvChip', '') || selectedHpmBoard());
};

export class RttView {
  constructor(){
    this.probe = null;
    this.rtt = null;
    this.mode = 'term';
    this.paused = false;
    this.running = false;
    this._sessionGen = 0;
    this.timer = null;
    this.interval = 5;
    this.records = [];
    this.recBytes = 0;
    this.truncated = false;
    this.term = null;
    this.fit = null;
    this.hist = [];
    this.histIdx = -1;
    this.rec = new FileRecorder();      // 高速采集落文件（见 core/recorder.js）
    this._lastWasCR = false;            // 终端模式补 \r 用的跨包状态（\r\n 不能补成 \r\r\n）
    this._terminalBusy = false;
    this._skippedTerminalBytes = 0;
    this.suppressed = false;            // 高速自动关显示（见 _highspeedGate）
    this._ringBackpressure = false;
    this.suppManual = false;            // 用户手动恢复过显示：暂不再自动关，速率回落后重新武装
    this.suppBytes = 0;                 // 关显示期间省略渲染的字节数
    this.stats = { bytes: 0, polls: 0, lost: 0, corrupt: 0, lastBytes: 0, lastPolls: 0, rate: 0, hz: 0 };
  }

  init(){
    const group = document.querySelector('#r-chip optgroup[data-arch="riscv"]');
    if (group) group.replaceChildren(...HPM_BOARDS.map(b => new Option(b.name, b.id)),
      new Option('其它 RISC-V', 'riscv-other'));

    this.tx = new RxBuffer($('r-rx'), { mode: 'ascii', maxLines: 4000, maxRaw: MAX_RAW });

    // ---------- 设置 ----------
    store.bind($('r-poll'), 'rtt.poll');
    store.bind($('r-eol'), 'rtt.eol');
    // 芯片（合并后的 `#r-chip`）不再用 store.bind：两块选项各有自己的键，
    // 由下面 applyTarget() / change 处理器显式读写（rtt.ocdTarget = ARM 组那颗、rtt.rvChip = RISC-V 组那颗）
    this._chk($('r-ts'), 'rtt.ts', v => { this.ts = v; this.tx.setTimestamps(v, false); });
    this._chk($('r-autoscroll'), 'rtt.autoscroll', v => { this.tx.setAutoscroll(v); });
    this._chk($('r-hexsend'), 'rtt.hexsend', () => {});

    const mode = store.get('rtt.mode', 'term');
    this.mode = mode;
    this.modeSeg = seg(document.querySelector('[data-group=rttmode]'), mode, v => { store.set('rtt.mode', v); this._setMode(v); });
    this.interval = Math.max(1, Number($('r-poll').value) || 5);
    $('r-poll').addEventListener('input', () => { this.interval = Math.max(1, Number($('r-poll').value) || 5); });


    const chip = chipSel();                 // 合并后的芯片下拉（ARM 组 + RISC-V 组，见文件头的辅助函数）
    const applyTarget = ({ keepChip = false } = {}) => {
      const transport = $('r-target').value;
      const rv = transport === 'riscv';
      const clkLbl = $('r-usb-clock')?.closest('label')?.querySelector('span');
      if (clkLbl) clkLbl.textContent = transport === 'swd' ? 'SWD 时钟' : 'JTAG TCK';
      $('r-reset').disabled = rv;
      $('r-reset').title = rv
        ? '使用调试页复位 RISC-V 目标'
        : '通过 OpenOCD Wasm 复位 Cortex-M';

      if (chip && !keepChip) chip.value = rv ? (store.get('rtt.rvChip', '') || selectedHpmBoard())
        : (store.get('rtt.armChip', store.get('rtt.ocdTarget', '')) || 'stm32f103');
      if (chip && !chip.value) chip.value = rv ? selectedHpmBoard() : 'stm32f103';
      this._applyChipUi();
    };
    // Restore architecture before deriving chip, range, labels and reset capability.
    store.bind($('r-target'), 'rtt.target');
    if (!$('r-target').value) $('r-target').value = 'swd';
    applyTarget();
    this._applyTargetUi = applyTarget;       // 供明确的会话目标沿用入口刷新本地配置
    const onTargetChanged = () => {
      this.clearAddress();
      applyTarget();
      if (this.probe) this.disconnect().catch(error => this._err(error));
    };
    $('r-target').addEventListener('change', onTargetChanged);
    /**
     * 合并后的芯片下拉：换芯片 = ① 记到**那一组自己的键**（两块各自记住上次选的）
     * ② 清除上个目标的 RTT 地址 ③ 跨组时把目标接口也切过去。
     * 内置芯片与导入配置分别选用各自声明的传输方式。
     */
    chip?.addEventListener('change', () => {
      const id = chip.value, arch = chipArchOf(id);
      const imported = this.importedTarget?.id === id ? this.importedTarget.cfg : null;
      if (!imported) store.set(arch === 'riscv' ? 'rtt.rvChip' : 'rtt.armChip', id);
      if (arch === 'riscv') rememberHpmBoard(id);
      this.clearAddress();
      const wantRv = arch === 'riscv';
      const transport = wantRv ? 'riscv' : 'swd';
      $('r-target').value = transport;
      store.set('rtt.target', transport);
      this._applyTargetUi({ keepChip:true });
      if (this.probe) this.disconnect();
    });

    // ---------- 连接按钮 ----------
    $('r-find').addEventListener('click', () => this._startRtt().catch(e => this._err(e)));
    $('r-restart').addEventListener('click', () => this._startRtt().catch(e => this._err(e)));
    $('r-reset').addEventListener('click', () => this.resetTarget());
    $('r-elf').addEventListener('click', () => this._elfInput.click());
    $('r-pause').addEventListener('click', () => this._togglePause());
    $('r-clear').addEventListener('click', () => this._clear());
    $('r-save').addEventListener('click', () => this.save());
    store.bind($('r-usb-clock'), 'rtt.clockKhz');
    // 老版本这里是 select（0/1000/500/200/2000/4000/8000/12000/20000）：存过 8000/12000 的要能显示出来
    ensureSelectOption($('r-usb-clock'), store.get('rtt.clockKhz', ''), mhzLabel(store.get('rtt.clockKhz', '')));
    $('r-usb-clock').value=String(store.get('rtt.clockKhz','0'));
    this._applyClockLimit();
    store.bind($('r-record-ts'), 'rtt.recordTs', 'checked');
    store.bind($('r-record-auto'), 'rtt.recordAuto', 'checked');
    $('r-record').addEventListener('click', () => this._toggleRecord());
    $('r-err').addEventListener('click', () => {
      // 高速关显示时状态栏就是恢复入口（文字会提示"点此恢复显示"）
      if (this.suppressed){ this.suppManual = true; this._setSuppressed(false); }
    });
    this.rec.onChange = () => this._recordBtn();
    // 记录期间"本页被切到后台"这类提醒（见 core/recorder.js 里的说明）
    this.rec.onNote = s => toast(s, 'warn', 8000);
    this._recordBtn();
    $('r-send').addEventListener('click', () => this._sendInput());

    this._elfInput = document.createElement('input');
    this._elfInput.type = 'file';
    this._elfInput.accept = '.elf,.axf,.out';
    this._elfInput.style.display = 'none';
    this._elfInput.addEventListener('change', () => this._loadElf());
    document.body.appendChild(this._elfInput);

    // ---------- 下行输入 ----------
    $('r-tx').addEventListener('keydown', e => {
      if (e.key === 'Enter'){ e.preventDefault(); this._sendInput(); return; }
      if (e.key === 'ArrowUp' || e.key === 'ArrowDown'){
        if (!this.hist.length) return;
        e.preventDefault();
        this.histIdx = e.key === 'ArrowUp'
          ? (this.histIdx < 0 ? this.hist.length - 1 : Math.max(0, this.histIdx - 1))
          : (this.histIdx < 0 ? -1 : Math.min(this.hist.length - 1, this.histIdx + 1));
        $('r-tx').value = this.histIdx < 0 ? '' : this.hist[this.histIdx];
      }
    });

    setInterval(() => this._stats(), 500);
    // 页面切到后台时浏览器会限速定时器（RTT 轮询会明显变慢）—— 如实告诉用户，别让人以为工具卡了
    document.addEventListener('visibilitychange', () => {
      if (!this.running) return;
      if (document.hidden){
        this._wasRunning = true;
        setStatus($('r-err'), '页面在后台：浏览器会限速定时器，RTT 轮询会变慢，请留意目标缓冲水位', null);
      } else if (this._wasRunning){
        this._wasRunning = false;
        setStatus($('r-err'), '', null);
      }
    });
    if (!OpenOcdProbe.supported()) setStatus($('r-err'), '这个浏览器没有 WebUSB（Chrome/Edge 桌面版才有）', 'err');

    // URL 参数：?backend=mock&auto=1 直接连上（做演示链接/自测用）
    const q = new URLSearchParams(location.search);
    const b = q.get('backend') === 'mock' ? 'mock' : 'openocd';
    this.backend = b;
    $('r-mock-box').hidden = b !== 'mock';
    if (q.get('addr')) $('r-addr').value = q.get('addr');
    this._setMode(mode);                       // 起手就要应用显示模式（否则默认的终端模式没容器，数据看不见）
    // 只有当 URL **明确指定了后端**时才自动连接（否则会拿 localStorage 里上次的后端乱连）
    if (b === 'mock' && q.get('auto') === '1') setTimeout(() => this.connectProbe().catch(() => {}), 150);
  }

  _chk(el, key, apply){
    store.bind(el, key, 'checked');
    el.addEventListener('change', () => apply(el.checked));
    apply(el.checked);
  }


  _applyChipUi(){
    this._applyClockLimit();
    const note = $('r-chip-note');
    if (note) note.hidden = chipArchOf(chipSel()?.value || '') !== 'riscv';
  }

  _applyClockLimit(){
    const target=chipSel()?.value;
    constrainClockSelect($('r-usb-clock'),target,v=>store.set('rtt.clockKhz',v));
    $('r-usb-clock').title=isHpmClockTarget(target)?'HPM JTAG TCK 最高 25 MHz；默认 8 MHz'
      : $('r-target').value==='riscv'?'其他 RISC-V JTAG TCK 最高 60 MHz':'SWD 时钟最高 60 MHz；默认 1 MHz';
  }

  // ================= 连接 =================
  async connectProbe(){
    if (this._connectPromise || this._disconnectPromise || this.probe) return;
    const g = this._sessionGen = (this._sessionGen || 0) + 1;
    const backend = this.backend || 'openocd';
    this._probeMock = backend === 'mock';
    this._connectPromise = runProbeOperation(this, 'rtt', () => this._connectProbe(g, backend), {
      mock: this._probeMock, reason: 'RTT Viewer 要使用探针', recovery: true,
    });
    try { return await this._connectPromise; }
    catch (e){ this._err(e); return false; }
    finally {
      if (g !== this._sessionGen && (this.probe)) await this.disconnect();
      this._connectPromise = null;
    }
  }

  async _connectProbe(g, b = this.backend || 'openocd'){
    try {
      if (b !== 'mock'){
        await prepareProbeHandoff(this, 'rtt', 'RTT Viewer 要使用探针');
        if (g !== this._sessionGen) return;
      }
      if (b === 'openocd'){
        const riscv = $('r-target').value === 'riscv';
        const target = riscv ? rvChipId() : armChipId();
        const imported = this.importedTarget?.id === target ? this.importedTarget.cfg : null;
        const clockKhz = webClockKhz($('r-usb-clock').value, target, riscv ? 8000 : 1000);
        const cfg = imported || makeOpenOcdProfile({ arch:riscv ? 'riscv' : 'arm', port:hpmBoard(target), family:target, clockKhz });
        const device = await selectOpenOcdDevice({ authorizedOnly:true });
        if (g !== this._sessionGen) return;
        if (riscv) await prepareOpenOcdJtag(device);
        if (g !== this._sessionGen) return;
        this.probe = await OpenOcdProbe.open(device, { cfg, runningMemory:true });
        if (g !== this._sessionGen) return;
        toast('探针已连接：' + this.probe.name, 'ok');
      } else if (b === 'mock'){
        this.probe = new MockProbe();
        await this.probe.connect();
        toast('模拟目标已启动（内存里有个假 RTT 控制块）', 'ok');
      } else { throw new Error('仅支持 OpenOCD Wasm / WebUSB 连接'); }
      this._uiConnected(true);
      if ($('r-record-auto').checked && !this.rec.active) this._autoStartRecord();
      /**
       * 🚨 **探针连通 ≠ RTT 控制块就绪**，两者必须分开报错（用户 2026-10 现场质问：
       *    "连接探针以前不依赖 RTT 地址，只要 probe 可用就能连"）。
       *    这里的失败只说明"控制块没找到 / 那一格读不通"，探针本身是好的 ——
       *    所以**不断开探针、不写「连接失败」**，只把原因写进状态条并给出重试入口。
       *    （旧写法把 _startRtt 放在外层 try 里 → 它的异常会触发 disconnect() + "连接失败"，
       *      把"探针明明连上了"说成连接失败，还顺手把探针断掉。）
       */
      const probeNow = this.probe;
      {
        try { await this._startRtt(g); }
        catch (e){
          if (g === this._sessionGen && this.probe === probeNow){
            this._err(e);
            setStatus($('r-err'), '探针已连接 · RTT 控制块未就绪：' + (e?.message || e)
              + '。请核对 ELF 或控制块地址', 'warn');
          }
        }
      }
    } catch (e){
      /**
       * 🚨 收尾时**要把错误留在状态栏上**（2026-10 真机走查踩到）：
       *    `disconnect()` 最后一句是 `setStatus($('r-err'), '已断开')`，它会把 `_err(e)` 刚写上去的
       *    真正原因**覆盖掉** —— 用户看到的是"已断开"，而实际原因是"没找到 RTT 控制块 / 读失败"，
       *    等于把唯一的线索擦掉了（本机就因此白查了一轮：以为是探针没连上）。
       *    这里在断开之后再补一句，把原因留在界面上。
       */
      const why = e?.message || String(e);
      this._err(e);
      await this.disconnect();
      setStatus($('r-err'), '连接失败：' + why);
    }
  }

  async disconnect(){
    this.probeManager?.cancel('rtt');
    this._sessionGen = (this._sessionGen || 0) + 1;
    this.running = false;
    clearTimeout(this.timer); clearTimeout(this._idleTimer); clearTimeout(this._resetTimer);
    if (this._disconnectPromise) return await this._disconnectPromise;
    this._disconnectPromise = this._disconnectNow();
    try {
      const result = await this._disconnectPromise;
      this.probeManager?.forget('rtt');
      return result;
    } catch (e){
      this.probeManager?.fail('rtt', e);
      throw e;
    } finally { this._disconnectPromise = null; }
  }

  async _disconnectNow(){
    this.running = false;
    clearTimeout(this.timer);
    // A readUp transaction can still be reading/writing RdOff. Let it retire
    // before DAP_Disconnect and native USB close consume the same reply queue.
    if(this._pollTask)await this._pollTask.catch(()=>{});
    this._pollTask=null;
    await this.rtt?.drain?.();
    await this._rttStartTask?.catch(() => {});
    await this._resetTask?.catch(() => {});
    if (this.rec.active || this.rec.needsClose || this.rec.starting || this.rec.draining){
      const info = await this.rec.stop();
      this._recordBtn();
      if (info?.error) toast('记录落盘出错：' + (info.error.message || info.error), 'err', 8000);
      else if (info) toast(`记录已停止并保存：${info.name}（${fBytes(info.bytes)}）`, 'ok', 6000);
    }
    let failure = null;
    if (this.probe?.disconnect){
      try { await this.probe.disconnect(); this.probe = null; }
      catch (e){ failure = e; }
    }
    if (failure) throw failure;
    this.rtt = null;
    this.suppManual = false;
    if (this.suppressed) this._setSuppressed(false);
    this._uiConnected(false);
    setStatus($('r-err'), '已断开');
  }

  _uiConnected(on){
    if (!on){ $('r-cb').textContent = '—'; $('r-up').textContent = '0'; $('r-down').textContent = '0'; }
  }

  async _startRtt(g){
    if (!this.probe || this._disconnectPromise || this._resetTask) return;
    if (this._rttStartTask) return this._rttStartTask;
    this._rttStartTask = this._openRtt(g);
    try { return await this._rttStartTask; }
    finally { this._rttStartTask = null; }
  }

  async _openRtt(g){
    if (g === undefined) g = this._sessionGen = (this._sessionGen || 0) + 1;
    const probe = this.probe;
    const active = () => g === this._sessionGen && this.probe === probe;
    this.running = false;
    clearTimeout(this.timer);
    const oldRtt = this.rtt;
    this.rtt = null;
    await this._pollTask?.catch(() => {});
    await oldRtt?.drain?.();
    if (!active()) return;
    const address = this._probeMock ? probe.cbAddr : Number($('r-addr').value.trim());
    $('r-cb').textContent = '—';
    $('r-up').textContent = $('r-down').textContent = '0';
    if (!address && !$('r-addr').value.trim()){
      setStatus($('r-err'), '探针已连接 · 请载入 ELF 或填写控制块地址');
      return;
    }
    if (!Number.isInteger(address) || address <= 0 || address > 0xffffffff - 24 || address % 4)
      throw Error('请填写有效的 4 字节对齐控制块地址');
    setStatus($('r-err'), '正在验证 RTT 控制块…');
    const check = await Rtt.validate(probe, address);
    if (!active()) return;
    if (!check.ok) throw Error(`0x${address.toString(16)} 处的 RTT 控制块校验失败：${check.reason}`);
    const rtt = new Rtt(probe, { addr: address });
    await rtt.init();
    if (!active()) return;
    const inff = rtt.info();
    const nm = await rtt.name('up', 0);
    if (!active()) return;
    this.rtt = rtt;
    $('r-cb').textContent = '0x' + address.toString(16);
    $('r-up').textContent = inff.maxUp;
    $('r-down').textContent = inff.maxDown;
    $('r-chlabel').textContent = `下行 ch0${nm ? ' · ' + nm : ''}`;
    const sz = inff.up[0]?.size || 0;
    setStatus($('r-err'), `控制块 0x${address.toString(16)}，上行缓冲 ${sz} B${inff.maxUp > 1 ? `（固件声明 ${inff.maxUp} 个上行通道，本页读 ch0）` : ''}`, 'ok');
    this._startPoll(g);
    this._armIdleWatchdog();
  }

  /**
   * 看门狗：控制块找到了、却一个字节都不来 —— 十有八九是**目标被停住了**
   * （上一次调试会话 halt 了它，或探针 connect 时默认 halt）。
   * 这时主动让它跑起来，并在状态条上说清楚，别让用户以为工具坏了。
   */
  _armIdleWatchdog(){
    const g = this._sessionGen, probe = this.probe;
    clearTimeout(this._idleTimer);
    if (probe?.isRiscv) return;
    this._idleTimer = setTimeout(async () => {
      if (g !== this._sessionGen || !this.running || this.stats.bytes > 0) return;
      if (typeof this.probe?.run !== 'function') return;
      try {
        // ⚠️ 只在**确实停住**时才写 DHCSR 让它跑。
        //    早期版本无条件写 run()，结果把一个正在运行的固件"弄停"了：
        //    现象是"连上后读到几 KB 就不再来数据"（时间点正好在看门狗触发处）。
        const halted = await probe.isHalted?.();
        if (g !== this._sessionGen || probe !== this.probe) return;
        if (halted) await probe.run();
        else { setStatus($('r-err'), '两秒内没收到数据，但目标在运行中（检查固件有没有在写 RTT）', null); return; }
        setStatus($('r-err'), '目标原本处于 halt 状态（固件不跑就没数据），已自动继续运行', 'ok');
      } catch (e){ /* 不支持就算了 */ }
    }, 2000);
  }

  _startPoll(g){
    if (g === undefined) g = this._sessionGen = (this._sessionGen || 0) + 1;
    const rtt = this.rtt, probe = this.probe;
    const active = () => this.running && g === this._sessionGen && this.rtt === rtt && this.probe === probe;
    clearTimeout(this.timer);
    this.running = true;

    this.stats.polls = 0; this.stats.bytes = 0; this.stats.lost = 0; this.stats.corrupt = 0;
    this._corruptRun = 0;
    this.stats.lastBytes=0;this.stats.lastPolls=0;this._lastStatsAt=performance.now();
    this._ringBackpressure = false;
    this.fullPolls = 0; this.highPolls = 0; this.peak = 0; this._faults = 0;
    this._pollT0 = Date.now(); this._zeroHinted = false; this._zeroHintSet = false;   // 见 _stats 里的"零速率"诊断

    const minGap = 0;
    const loop = async () => {
      if (!active()) return;
      const t0 = performance.now();
      try {
        const { bytes, lost, high, level, corrupt } = await rtt.readUp(0, probe.rttPollLimit);
        if (!active()) return;
        // On the bounded Wasm path, stop feeding the renderer before it can
        // delay the next USB read enough to hide a high rate from _stats().
        this._ringBackpressure = Boolean(probe.rttPollLimit && high && bytes.length >= probe.rttPollLimit / 2);
        if (this._ringBackpressure) this._highspeedGate(this.stats.rate);
        if (bytes.length) this._ingest(bytes, new Date());
        if (lost) this.stats.lost += lost;
        if (high) this.highPolls++;
        if (level > this.peak) this.peak = level;
        if (corrupt){
          this.stats.corrupt++;
          this._corruptRun = (this._corruptRun || 0) + 1;
          // 别刷屏：10 秒提醒一次。错位读常见原因：SWD 时钟过高、接线/共地不良
          //（拔插过 LA/杜邦线后特别常见）、或 USB 响应流错位
          if (!this._corruptAt || Date.now() - this._corruptAt > 10000){
            this._corruptAt = Date.now();
            if (!this.suppressed) setStatus($('r-err'),
              '检测到错位读（读回了控制块内容，已丢弃并重读）—— 查 SWD 时钟、接线与共地', 'err');
          }
          if (this._corruptRun === 15 || this._corruptRun % 60 === 0)
            await this._recoverRtt(rtt, probe, active);
          if (!active()) return;
          if (this._corruptRun > 150){
            this._fail(new Error('连续错位读且自愈无效 —— SWD 链路不稳定：查接线/共地，或把 SWD 时钟调低'));
            return;
          }
        } else {
          this._corruptRun = 0;
        }
      } catch (e){
        /**
         * 🚨 手动断开时，在飞的 readUp 稍后会抛"探针未连接" —— 不在这儿让路的话，
         *    `_fail()` 会把状态栏从"已断开"覆盖成"读取失败"（看起来像出了故障，其实是我们自己停的）。
         */
        if (!active()) return;
        const msg = String(e?.message || e);
        // SWD 访问出错 / 控制块内容不可信 → 先自愈（重新初始化调试口），别立刻放弃
        if (/FAULT|NO ACK|不可信|不合理|没在运行/.test(msg)){
          this._faults = (this._faults || 0) + 1;
          if (this._faults === 3 || this._faults % 15 === 0){
            if (await this._recoverRtt(rtt, probe, active)) this._faults = 0;
            if (!active()) return;
          }
          if (this._faults > 80){ this._fail(new Error('连续 SWD 访问失败，已放弃：' + msg)); return; }
        } else { this._fail(e); return; }
      }
      if (!active()) return;
      this.stats.polls++;
      const cost = performance.now() - t0;
      const gap=Math.max(minGap,this.interval-cost,0);
      await (gap>0?waitMs(gap):yieldTask());
      if(active())this._pollTask=loop();
    };
    this._pollTask = loop();
  }

  async _recoverRtt(rtt, probe, active){
    if (typeof probe.recoverDebugPort !== 'function'){
      setStatus($('r-err'), '调试口未能自动恢复，请从顶栏重新连接探针', 'warn');
      return false;
    }
    setStatus($('r-err'), '正在重新连接 OpenOCD 调试口…', 'warn');
    try {
      await rtt.recoverDebugPort();
      if (!active()) return false;
      const hdr = await probe.readMem(rtt.addr, 16);
      if (!active()) return false;
      if (String.fromCharCode(...hdr.subarray(0, 10)) !== 'SEGGER RTT')
        throw Error('目标 RTT 控制块已变化，请重新载入 ELF 或验证地址');
      setStatus($('r-err'), '调试口已恢复', 'ok');
      return true;
    } catch (e){
      if (active()) setStatus($('r-err'), '恢复调试口失败：' + e.message, 'err');
      return false;
    }
  }

  _fail(e){
    this.running = false;
    clearTimeout(this.timer);
    setStatus($('r-err'), '读取失败：' + (e?.message || e), 'err');
  }

  _err(e){
    setStatus($('r-err'), String(e?.message || e), 'err');
    toast(String(e?.message || e), 'err', 6000);
  }

  // ================= 数据 =================
  _ingest(bytes, t){
    if (!bytes?.length) return;
    this.stats.bytes += bytes.length;
    this.rec.push(bytes, t);            // 落文件在"显示之前"：暂停/丢历史都不影响它
    this.records.push({ t, b: bytes });
    if (this.records.length === 1) this._updateEmptyHint();
    this.recBytes += bytes.length;
    while (this.recBytes > MAX_RAW && this.records.length > 1){
      this.recBytes -= this.records.shift().b.length;
      this.truncated = true;
    }
    if(this.suppressed){this.suppBytes+=bytes.length;return;}
    if (this.paused) return;
    if (this.mode === 'term'){
      if (this.probe?.rttLivePreview) this._previewTerminal(bytes, t);
      else this._termWrite(bytes, t);
    } else this.tx.push(bytes, t);
  }

  _previewTerminal(bytes, t){
    if (!this.term) return;
    if (this._terminalBusy){
      this._skippedTerminalBytes += bytes.length;
      return;
    }
    this._terminalBusy = true;
    if (this._skippedTerminalBytes){
      // Cancel any ANSI sequence cut short by skipped display data.
      this.term.write(textToBytes(`\x18\x1b[0m\r\n[显示跟不上，省略 ${fBytes(this._skippedTerminalBytes)}；接收和记录继续]\r\n`));
      this._lastWasCR = false;
      this._skippedTerminalBytes = 0;
    }
    this._termWrite(bytes, t, () => { this._terminalBusy = false; });
  }

  _termWrite(bytes, t, done){
    if (!this.term) return;
    if (this.ts) this.term.write(`\x1b[90m[${stampOf(t)}]\x1b[0m `);
    // 很多固件只发 \n 不发 \r，直接塞给 xterm 会变成阶梯状 → 按字节自动补 \r
    //（和终端标签页 terminal.js 同一套规则；不能按字符串处理，多字节 UTF-8 会被拆坏）
    const out = [];
    for (let i = 0; i < bytes.length; i++){
      const b = bytes[i];
      if (b === 0x0a && !this._lastWasCR) out.push(0x0d);
      out.push(b);
      this._lastWasCR = (b === 0x0d);
    }
    this.term.write(Uint8Array.from(out), done);
  }

  _ensureTerm(){
    if (this.term || !window.Terminal) return;
    this.term = new Terminal({
      fontFamily: '"Cascadia Mono","JetBrains Mono",Consolas,monospace',
      fontSize: Number(store.get('rtt.font', 13)) || 13,
      lineHeight: 1.15, cursorBlink: true, scrollback: 3000,
      theme: { background: '#0a0f15', foreground: '#e6edf3', cursor: '#58a6ff', selectionBackground: '#264f78' },
    });
    try { this.fit = new FitAddon.FitAddon(); this.term.loadAddon(this.fit); } catch {}
    this._termStale = !$('tab-rtt').classList.contains('active');   // 在隐藏状态下创建的话，第一次显示时要重放
    this.term.open($('r-term'));
    this.fit?.fit();
    // 允许直接在终端里敲（等价于下行 ch0），像 JLinkRTTViewer 的 Terminal 模式
    this.term.onData(d => {
      const eol = $('r-eol').value;
      if (d === '\r') this._sendBytes(textToBytes(EOL[eol] ?? '\r'));
      else this._sendBytes(textToBytes(d));
    });
    const ro = new ResizeObserver(() => { if (this.mode === 'term') { try { this.fit?.fit(); } catch {} } });
    ro.observe($('r-term'));
  }

  /** 切到本标签时调用（隐藏状态下建的终端在这里补尺寸/重放） */
  onShow(){
    if (this.mode !== 'term' || !this.term) return;
    try { this.fit?.fit(); } catch {}
    if (this._termStale){ this._redrawAll(); this._termStale = false; }
  }

  _setMode(m){
    this.mode = m;
    $('r-term').hidden = m !== 'term';
    $('r-rx').hidden = m === 'term';
    this._updateEmptyHint();
    if (m === 'term'){ this._ensureTerm(); this._redrawAll(); try { this.fit?.fit(); } catch {} }
    else { this.tx.setMode(m === 'hex' ? 'hex' : 'ascii'); this._redrawAll(); }
  }

  _updateEmptyHint(){
    const hint = $('r-empty');
    if (hint) hint.hidden = this.mode !== 'term' || this.records.length > 0;
  }

  _redrawAll(){
    if (this.mode === 'term'){
      if (!this.term) return;
      this.term.clear();
      for (const r of this.records) this._termWrite(r.b, r.t);
    } else {
      this.tx.clear();
      for (const r of this.records) this.tx.push(r.b, r.t);
    }
  }

  _togglePause(){
    this.paused = !this.paused;
    $('r-pause').textContent = this.paused ? '恢复显示' : '暂停显示';
    $('r-pause').classList.toggle('primary', this.paused);
    if (!this.paused) this._redrawAll();
  }

  _clear(){
    this.records = []; this.recBytes = 0; this.truncated = false;
    this._skippedTerminalBytes = 0;
    this.tx.clear();
    this.term?.clear();
    this._updateEmptyHint();
  }

  // ================= 下行 =================
  _sendInput(){
    const raw = $('r-tx').value;
    if (!raw.trim()) return;
    const bytes = this._build(raw);
    if (!bytes) return;
    if (this.hist[this.hist.length - 1] !== raw) this.hist.push(raw);
    if (this.hist.length > 100) this.hist.shift();
    this.histIdx = -1;
    $('r-tx').value = '';
    this._sendBytes(bytes);
  }

  _build(text){
    let b;
    if ($('r-hexsend').checked){
      const r = parseHex(text);
      if (r.error){ setStatus($('r-err'), r.error, 'err'); return null; }
      b = r.bytes;
    } else {
      b = textToBytes(text);
    }
    const tail = EOL[$('r-eol').value] ?? '';
    if (tail) b = new Uint8Array([...b, ...textToBytes(tail)]);
    return b;
  }

  async _sendBytes(bytes){
    try {
      const rtt = this.rtt;
      const generation = this._sessionGen;
      if (!rtt || this._disconnectPromise || this._resetTask) throw Error('RTT 还没就绪，请先载入 ELF 或验证控制块地址');
      const n = await rtt.writeDown(0, bytes);
      if (generation !== this._sessionGen) return;
      if (n < bytes.length) toast(`下行缓冲只写进 ${n}/${bytes.length} 字节（固件没在取？）`, 'warn');
      if (!this.suppressed) setStatus($('r-err'), '', null);
    } catch (e){ this._err(e); }
  }

  async resetTarget(){
    if (this._disconnectPromise || this._rttStartTask || this._resetTask) return;
    this._resetTask = this._resetTarget();
    try { return await this._resetTask; }
    finally { this._resetTask = null; }
  }

  async _resetTarget(){
    if (!this.probe){ toast('请先连接目标', 'warn'); return; }
    if (this.probe.isRiscv || typeof this.probe.reset !== 'function'){
      toast('当前连接不支持 RTT 页复位，请使用调试页', 'warn');
      return;
    }
    const probe = this.probe;
    const generation = this._sessionGen = (this._sessionGen || 0) + 1;
    this.running = false;
    clearTimeout(this.timer); clearTimeout(this._idleTimer); clearTimeout(this._resetTimer);
    try {
      if (this._pollTask) await this._pollTask.catch(() => {});
      await this.rtt?.drain?.();
      if (generation !== this._sessionGen || probe !== this.probe) return;
      this.rtt = null;
      const how = await probe.reset();
      if (generation !== this._sessionGen || probe !== this.probe) return;
      toast(`已复位目标（${how}），2 秒后重新读取控制块…`, 'ok');
      const g = generation;
      clearTimeout(this._resetTimer);
      this._resetTimer = setTimeout(() => {
        if (g === this._sessionGen && probe === this.probe) this._startRtt().catch(e => this._err(e));
      }, 2000);
    } catch (e){ this._err(e); }
  }

  // ================= ELF =================
  clearAddress(){
    this._elfRevision = (this._elfRevision || 0) + 1;
    $('r-addr').value = '';
  }

  async _loadElf(){
    const file = this._elfInput.files?.[0];
    this._elfInput.value = '';
    return await this.loadElfFile(file);
  }

  async loadElfFile(file, buffer){
    if (!file) return false;
    const revision = this._elfRevision = (this._elfRevision || 0) + 1;
    try {
      const bytes = buffer ?? await file.arrayBuffer();
      if (revision !== this._elfRevision) return false;
      if (this._connectPromise || this._disconnectPromise || this._rttStartTask || this._resetTask)
        throw Error('探针操作尚未完成，请稍后载入 ELF');
      const symbol = findSymbol(bytes, '_SEGGER_RTT');
      if (!Number.isInteger(symbol?.addr) || symbol.addr <= 0 || symbol.addr > 0xffffffff - 24 || symbol.addr % 4)
        throw Error(`${file.name} 中没有可用的 _SEGGER_RTT 地址，请载入包含符号表的 ELF`);
      const data = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
      const machine = new DataView(data.buffer, data.byteOffset, data.byteLength).getUint16(18, data[5] === 1);
      if (machine !== ($('r-target').value === 'riscv' ? 243 : 40))
        throw Error('ELF 架构与 Viewer 的目标接口不匹配');
      const address = '0x' + symbol.addr.toString(16);
      $('r-addr').value = address;
      this.onElfLoaded?.(bytes, file.name);
      setStatus($('r-err'), `${file.name} · _SEGGER_RTT ${address}`, 'ok');
      if (this.probe && !this._probeMock){
        try { await this._startRtt(); }
        catch (error){ this._err(error); }
      }
      return true;
    } catch (error){
      this._err(error);
      return false;
    }
  }

  // ================= 统计 / 保存 =================
  summary(){
    return {connected:!!this.probe,running:this.running,backend:this.backend || 'openocd',
      probeSerial:this.probe?.device?.serialNumber || null,
      ringBytes:this.rtt?.up?.[0]?.size ?? null,pollingMs:this.interval,
      clockKhz:this.probe?.clockHz ? this.probe.clockHz / 1000 : null, pollLimit:this.probe?.rttPollLimit ?? null,
      stats:{...this.stats},controlBlock:this.rtt?.addr??null,
      bufferedBytes:this.recBytes,displaySuppressed:this.suppressed,error:$('r-err').textContent};
  }
  _stats(){
    const s = this.stats;
    const now=performance.now(),elapsed=Math.max(.001,(now-(this._lastStatsAt??(now-500)))/1000);
    this._lastStatsAt=now;
    s.rate = Math.max(0, s.bytes - s.lastBytes) / elapsed;
    s.hz = Math.max(0, s.polls - s.lastPolls) / elapsed;
    s.lastBytes = s.bytes; s.lastPolls = s.polls;
    $('r-rate').textContent = fRate(s.rate);
    $('r-hz').textContent = Math.round(s.hz);
    $('r-lost').textContent = s.lost;
    $('r-corrupt').textContent = s.corrupt || 0;
    this._highspeedGate(s.rate);
    const peak = Math.round((this.peak || 0) * 100);
    $('r-full').textContent = `${this.highPolls || 0} 次 / 峰值 ${peak}%`;
    $('r-full').parentElement.title =
      '缓冲水位：RTT 不重传，水位打到 3/4 以上说明目标写速 ≥ 主机读速，可能已有覆盖/丢弃。\n' +
      '（缓冲水位和丢弃计数以实际目标及采集窗口为准）';
    if (peak >= 75) $('r-full').classList.add('err');
    if (this.paused) $('r-pause').title = '显示已暂停，接收和记录继续；恢复后显示当前缓冲';
    // 记录/落盘期间按钮要一直刷：字节数与"待落盘"量都得看得见（见 recorder.js 的说明）
    if (this.rec.active || this.rec.draining) this._recordBtn();
    /**
     * **零速率诊断**（2026-10 用户现场：报"我测 RTT Viewer，没有速率" —— 界面上只有 0 B/s，
     * 看不出该去查哪儿）。控制块找到了、轮询也在跑，却连着 4 秒一个字节都没有时，把可能的原因写出来；
     * 后来读到数据就自动把这句话收掉（只清自己写的那条，不覆盖别的错误）。
     */
    if (this.running && this.rtt && s.bytes === 0 && !this._zeroHinted && Date.now() - (this._pollT0 || 0) > 4000){
      this._zeroHinted = true;
      const errEl = $('r-err');
      if (errEl && !errEl.textContent){
        setStatus(errEl, '4 秒未收到 RTT 输出。请检查目标是否在写入，或是否有其他端在读取同一缓冲。', 'warn');
        this._zeroHintSet = true;
      }
    } else if (this._zeroHintSet && s.bytes > 0){
      this._zeroHintSet = false;
      setStatus($('r-err'), '', null);
    }
    if (!this.rtt && this.probe){
      const status = this._rttStartTask && (this._probeMock || $('r-addr').value.trim()) ? '校验中…' : '—';
      if ($('r-cb').textContent !== status) $('r-cb').textContent = status;
    }
  }

  // ================= 高速自动关显示 =================
  /**
   * WebUSB 实测 330KB/s 时渲染必然掉队 —— 先崩的总是显示，字节本身不丢
   * （统计/落文件是全量，MicroLink 固件侧的 g_bytes 对账可以对出来）。
   * 速率 > 100KB/s 自动停渲染，降到 50KB/s 以下才恢复（回滞）；
   * 点状态栏提示可手动恢复（之后不再自动关，直到速率回落后重新武装）。
   */
  static HS_OFF = 100 * 1024;   // 超过 100KB/s 停渲染
  static HS_ON  = 50 * 1024;    // 降到 50KB/s 以下才自动恢复（回滞）

  _highspeedGate(r){
    if (!this.probe || this.probe.rttLivePreview && this.mode === 'term'){
      if (this.suppressed) this._setSuppressed(false);
      this.suppManual = false;
      return;
    }
    if (!this.suppressed && !this.suppManual && (this._ringBackpressure || r > RttView.HS_OFF))
      this._setSuppressed(true, r);
    else if (this.suppressed && !this._ringBackpressure && r < RttView.HS_ON){
      this._setSuppressed(false); this.suppManual = false;
    }
  }

  _setSuppressed(on, r = 0){
    if (this.suppressed === on) return;
    this.suppressed = on;
    if (on){
      const reason = this._ringBackpressure ? 'RTT 缓冲积压' : `高速 ${fRate(r)}`;
      setStatus($('r-err'), `${reason}，渲染已停（收数/记录不受影响）· 点此恢复显示`, 'err');
      $('r-err').title = '点击恢复显示。若速率仍高于阈值会再次自动关闭';

      const note = `\r\n[${reason}，已停止渲染。数据仍在接收和记录，点状态栏可恢复显示]\r\n`;
      if (this.mode === 'term' && this.term) this._termWrite(new TextEncoder().encode(note), new Date());
      else this.tx.push(new TextEncoder().encode(note), new Date());
    } else {
      setStatus($('r-err'), '', null);
      $('r-err').title = '';
      if (this.suppBytes > 0){
        const note = `（高速期间省略了 ${fBytes(this.suppBytes)} 的渲染；完整数据用「记录到文件」拿）`;
        this.suppBytes = 0;
        if (this.mode === 'term' && this.term) this._termWrite(new TextEncoder().encode(note + '\r\n'), new Date());
        else this.tx.push(new TextEncoder().encode('\r\n' + note + '\r\n'), new Date());
      }
    }
  }

  async _autoStartRecord(){
    try {
      const name = await this.rec.start({ name: 'rtt', timestamps: $('r-record-ts').checked });
      this._recordBtn();
      toast(`已自动开始记录 → ${name}`, 'ok', 5000);
    } catch (e){
      const why = e?.name === 'NotAllowedError' ? '浏览器要求弹保存框时页面正在响应用户点击' : (e?.message || e);
      toast(`自动记录没启动（${why}）。手动点「记录到文件」即可`, 'warn', 6000);
    }
  }

  // ================= 记录到文件 =================
  /**
   * 高速采集时接收区（raw 上限 2MB）存不下 —— 实测 WebUSB 330 KB/s 只要 6 秒就撑爆，
   * 之后「保存数据」只能保存剩下那段。落文件把字节直接写盘，采集多久都不丢。
   */
  async _toggleRecord(){
    if (this.rec.starting || this.rec.draining) return;
    if (this.rec.active || this.rec.needsClose){
      const info = await this.rec.stop();
      this._recordBtn();
      if (info?.error) toast('记录落盘出错：' + (info.error.message || info.error), 'err', 8000);
      else if (info) toast(`已落盘 ${info.name}：${fBytes(info.bytes)} / ${info.frames} 段 / ${info.seconds.toFixed(1)} s —— .crswap 已改名成正式文件`, 'ok', 7000);
      return;
    }
    try {
      const name = await this.rec.start({ name: 'rtt', timestamps: $('r-record-ts').checked });
      this._recordBtn();
      toast(`记录中 → ${name}：Chrome 先写成 ${name}.crswap，点「停止记录」才改名成正式文件（记录中别关页面/刷新）`, 'ok', 8000);
    } catch (e){
      if (e?.name !== 'AbortError') toast('开始记录失败：' + (e?.message || e), 'err', 6000);
    }
  }

  _recordBtn(){
    const b = $('r-record');
    if (!b) return;
    const s = recordButtonState(this.rec);
    b.disabled = this.rec.starting || this.rec.draining;
    b.textContent = s.text;
    b.title = s.title.replace('收到的字节', '读到的 RTT 上行字节');
    b.classList.toggle('primary', s.primary);
  }

  saveRaw(){
    if(!this.records.length)throw Error('还没有原始数据');
    const name=`rtt-raw-${fileStamp()}.bin`;
    download(name,new Blob(this.records.map(r=>r.b),{type:'application/octet-stream'}));
    return {name,bytes:this.recBytes,truncated:!!this.truncated};
  }

  save(){
    let out = '';
    if (this.mode === 'term' && this.term){
      const buf = this.term.buffer.active;
      for (let i = 0; i < buf.length; i++){
        const line = buf.getLine(i);
        out += (line ? line.translateToString(true) : '') + '\n';
      }
    } else {
      out = (this.truncated ? '（较早的数据已因超出上限被丢弃）\n' : '') + this.tx.render(this.records);
    }
    if (!out.trim()){ toast('还没有数据', 'warn'); return; }
    const name = `rtt-${fileStamp()}.txt`;
    download(name, out);
    toast(`已保存 ${name}`, 'ok');
  }
}
