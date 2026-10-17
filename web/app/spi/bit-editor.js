/**
 * 「字节 → 位」开关板（`#pn-bitpop`）：点解析表里的某个参数字节 → 弹出它的 8 个 bit，
 * 点一下就翻转那一位，改完立刻写回那一行。
 *
 * 交互**照 `tools/bmp_sender.html`（esp-idf-s31/spi_lcd_bmp）那版做**（用户 2026-09-30 指定参考）：
 *   · 8 个**大方块**横排（上：bit 号；中：0/1；下：权重），比一列小勾选框好点得多；
 *   · 和**原值**不同的位套一圈黄框（`chg`）—— "我到底动了哪几位"一眼可见；
 *   · 一排快捷键：清零 `00` / 全置 1 `FF` / 逐位取反 / **恢复原值** / 完成；
 *   · 表格里那一格本身就是可编辑的十六进制输入框，手打与点 bit 走**同一条路**（两边同步）；
 *   · `position:fixed` + 跟随滚动/缩放重新定位（表格在滚动容器里，absolute 会被裁掉）。
 *
 * 为什么要有它：面板初始化命令的参数就是**寄存器值**（0x36 MADCTL、0x3A COLMOD…）。想试
 * "换个色序 / 换 16bpp / 打开某一位"时，人得先在脑子里把 0x55 换成 0101 0101 再数第几位 ——
 * 把 8 个位摆出来点，比"改文本 → 重解析 → 再数位"快一个数量级。
 *
 * 两条语义纪律（与 view 的分工）：
 *   1. 写回一律走 `panel-code.js` 的 `setRowByte()`（**换新的 Uint8Array**，别污染共享常量）；
 *   2. 改的是"这份步骤表"，**不回写上面的文本框**（原文是用户的资产），
 *      想丢弃改动就点「解析并预览」重建，或在行尾点「改回」只还原那一行。
 */
import { $ } from '../ui/dom.js';
import { byteBits, bitsText, toggleBit, bitNamesFor, setRowByte } from './panel-code.js';

const h2 = v => (v & 0xff).toString(16).toUpperCase().padStart(2, '0');

export class BitPopover {
  /**
   * @param {{onEdit?:(row:object,i:number,k:(number|'cmd'),v:number)=>void}} opts
   *        onEdit 在**每次**改动后回调（view 用它重绘表格行 + 更新摘要）
   */
  constructor({ onEdit } = {}){
    this.onEdit = onEdit || null;
    this.cell = null;          // { row, base, i, k, anchor }
    this._outside = e => {
      const el = $('pn-bitpop');
      if (!el || el.hidden) return;
      if (el.contains(e.target) || e.target.closest?.('input.bx')) return;   // 点格子本身 = 换一格，不关
      this.close();
    };
    this._key = e => { if (e.key === 'Escape') this.close(); };
    this._follow = () => { if (this.isOpen) this.reposition(); };
  }

  get isOpen(){ return !!this.cell; }
  /** 当前编辑目标（表格重绘后用它把锚点找回来）*/
  get target(){ return this.cell ? { index: this.cell.i, k: this.cell.k } : null; }

  init(){
    const el = $('pn-bitpop');
    if (!el) return;
    // 位格：8 个大方块（事件委托 —— 每次重绘都重建，逐个挂监听会漏）
    $('pn-bitpop-bits').addEventListener('click', e => {
      const b = e.target.closest('button.bit');
      if (b && this.isOpen) this.setValue(toggleBit(this.value(), +b.dataset.k));
    });
    // 快捷键一排
    el.addEventListener('click', e => {
      const btn = e.target.closest('button[data-bit]');
      if (!btn || !this.isOpen) return;
      const act = btn.dataset.bit;
      if (act === 'zero') this.setValue(0x00);
      else if (act === 'ones') this.setValue(0xff);
      else if (act === 'inv') this.setValue(~this.value() & 0xff);
      else if (act === 'orig') this.setValue(this.original() ?? this.value());
      else if (act === 'close') this.close();
    });
    document.addEventListener('pointerdown', this._outside, true);
    document.addEventListener('keydown', this._key);
    // fixed 定位不会自己跟着滚动走 → 手动跟（页面滚动、表格滚动、窗口缩放都要）
    window.addEventListener('scroll', this._follow, { passive: true, capture: true });
    window.addEventListener('resize', this._follow, { passive: true });
  }

  /**
   * 打开（或挪到另一个字节上）。
   * @param {{row:object, base:object|null, index:number, k:(number|'cmd'), anchor:Element}} o
   *        `base` = 解析出来的原值那一行（用来标"哪些位被你动过"与「恢复原值」）
   */
  open({ row, base, index, k, anchor }){
    if (!row || !anchor) return;
    this.cell = { row, base: base || null, i: index, k, anchor };
    const el = $('pn-bitpop');
    el.hidden = false;
    this.render();
    this.reposition();
  }

  close(){
    if (!this.isOpen) return;
    this.cell = null;
    const el = $('pn-bitpop');
    if (el) el.hidden = true;
  }

  /** 当前字节值 */
  value(){
    const c = this.cell;
    if (!c) return 0;
    return (c.k === 'cmd' ? c.row.cmd : (c.row.data[c.k] ?? 0)) & 0xff;
  }

  /** 原值（没有基准就返回 null）*/
  original(){
    const c = this.cell;
    if (!c || !c.base) return null;
    return (c.k === 'cmd' ? c.base.cmd : (c.base.data[c.k] ?? null));
  }

  /** 改值（唯一入口）：写回行 → 通知 view 重绘（含脏标记/摘要）→ 自己重绘 */
  setValue(v){
    const c = this.cell;
    if (!c) return;
    setRowByte(c.row, c.k, v & 0xff);
    this.onEdit?.(c.row, c.i, c.k, v & 0xff);
    if (this.isOpen) this.render();
  }

  /** 表格重绘之后把锚点元素找回来（重绘会换掉 DOM 节点），并把弹窗挪到它的新位置 */
  reattach(){
    const c = this.cell;
    if (!c) return;
    const sel = c.k === 'cmd'
      ? `#pn-code-body input.bx.cmd[data-i="${c.i}"]`
      : `#pn-code-body input.bx[data-i="${c.i}"][data-b="${c.k}"]`;
    const el = document.querySelector(sel);
    if (el) c.anchor = el;
    else this.close();
  }

  reposition(){
    const c = this.cell;
    if (!c) return;
    if (!c.anchor.isConnected){ this.close(); return; }
    const P = $('pn-bitpop');
    const r = c.anchor.getBoundingClientRect();
    const left = Math.min(Math.max(6, r.left - 40), window.innerWidth - P.offsetWidth - 8);
    let top = r.bottom + 6;
    if (top + P.offsetHeight > window.innerHeight - 8) top = Math.max(8, r.top - P.offsetHeight - 6);
    P.style.left = Math.round(left) + 'px';
    P.style.top = Math.round(top) + 'px';
  }

  render(){
    const c = this.cell;
    if (!c) return;
    const v = this.value(), o = this.original();
    const row = c.row;
    const info = bitNamesFor(row.cmd);
    const what = c.k === 'cmd' ? '命令字节' : `第 ${c.k} 字节`;
    $('pn-bitpop-title').textContent = `第 ${c.i} 条（0x${h2(row.cmd)}）· ${what}`;
    $('pn-bitpop-val').textContent = `0x${h2(v)} = ${v} = 0b${bitsText(v)}` +
      (o != null && o !== v ? `　（原 0x${h2(o)}）` : '');
    // 高位在左，和写二进制一致；和原值不同的位套黄框
    const bits = byteBits(v);
    const parts = [];
    for (let k = 7; k >= 0; k--){
      const on = bits[k] ? 1 : 0;
      const was = o == null ? on : ((o >> k) & 1);
      const nm = info?.bits?.[k] ? `<span class="bn">${info.bits[k]}</span>` : '';
      parts.push(`<button class="bit${on ? ' on' : ''}${on !== was ? ' chg' : ''}" data-k="${k}"` +
        ` title="bit${k}（权重 ${1 << k}）—— 点一下翻转${info?.bits?.[k] ? ` · ${info.bits[k]}` : ''}">` +
        `<span class="bk">bit${k}</span><span class="bv">${on}</span><span class="bw">${1 << k}</span>${nm}</button>`);
    }
    $('pn-bitpop-bits').innerHTML = parts.join('');
    $('pn-bitpop-hint').innerHTML = '点 bit 翻转它；<span style="color:var(--warn)">黄框</span> = 和原值不同的位。' +
      '也可以直接在表格里敲十六进制（两边同步）。' +
      (info ? `<br>${info.name}：${info.note}` : '');
  }
}
