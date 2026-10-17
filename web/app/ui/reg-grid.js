/**
 * **寄存器字节网格 + 位开关板**（`#i2c` 与 `#spi` 两个寄存器面板共用）。
 *
 * 一个面板 = 一张 16 列的字节表（地址列 + 每字节一个按钮 + ASCII 列）+ 一个 `.bitpop` 位开关板。
 * 两个页面在这块上的语义必须完全一致（黄框 = 与"读回来的原值"不同的字节、点 bit 翻一位、
 * 快捷键 00/FF/取反/恢复原值、也能直接敲十六进制），所以只有这一份实现 ——
 * 各页要做的只是：给它 `base`（器件现值）与 `cur`（当前缓冲），以及两个"怎么标地址"的回调。
 *
 * DOM 契约（各页的 index.html 必须按 `prefix` 提供这些 id）：
 *   `<prefix>-body`      表格 tbody（本组件负责填）
 *   `<prefix>-head`      表头 tr（列号 0..F + ASCII）
 *   `<prefix>pop`        位开关板根（class="bitpop"，各页自己的一份，别两页共用一个 DOM）
 *   `<prefix>pop-title` / `-val` / `-bits` / `-hex` / `-hint`
 *
 * 三条纪律：
 *   1. **只改内存里的 `cur`**：本组件不发任何总线请求；什么时候写回由各页的「只写改动/整块写回」决定。
 *   2. `base` 是"读回来的器件现值"，**写回成功后由各页把它更新成 cur**（黄框随之清掉）。
 *   3. 重绘只重画变化的那一格（`paintByte`）—— 整表重绘会丢滚动位置与开关板锚点。
 */
import { $ } from './dom.js';
import { bitsOf, toggleBit } from '../core/bytes.js';

const h2 = v => (v & 0xff).toString(16).toUpperCase().padStart(2, '0');

export class RegGrid {
  /**
   * @param {object} o
   *   · `prefix`  DOM id 前缀（`i2-reg` / `sp-reg`）
   *   · `cols`    每行字节数（默认 16）
   *   · `onEdit`  改位回调 `(off, value) => void`
   *   · `labels`  `{ addr(off) => string, title(off, base, cur) => string }`
   */
  constructor({ prefix = 'i-reg', cols = 16, onEdit = null, labels = {} } = {}){
    this.prefix = prefix;
    this.cols = cols;
    this.onEdit = onEdit;
    this.labels = labels;
    this.base = new Uint8Array(0);
    this.cur = new Uint8Array(0);
    this.cell = null;            // 开关板当前编辑的 { off, anchor }
    this._key = e => { if (e.key === 'Escape') this.closePop(); };
    this._follow = () => { if (this.popOpen) this.reposition(); };
  }

  /**
   * 拼 DOM id。⚠️ 命名有个历史怪癖：表格是 `<prefix>-body` / `<prefix>-head`（带横线），
   * 而位开关板是 `<prefix>pop` / `<prefix>pop-bits`（**不带**横线）—— 前者是 I2C 页原来就有的
   * id（页面自测按它断言），后者是从 `#pn-bitpop` 那套沿下来的。别"顺手统一"，会打断既有自测。
   */
  id(suffix){
    return suffix.startsWith('pop') ? `${this.prefix}${suffix}` : `${this.prefix}-${suffix}`;
  }
  get popOpen(){ return this.cell != null; }
  /** 当前正在编辑的偏移（自测/页面脚本用）*/
  get editing(){ return this.cell ? this.cell.off : null; }

  init(){
    const bits = $(this.id('pop-bits'));
    if (!bits) return false;                       // 这一页没有开关板 DOM（不该发生，防御一下）
    bits.addEventListener('click', e => {
      const b = e.target.closest('button.bit');
      if (b && this.popOpen) this._emitEdit(this.cell.off, toggleBit(this.value(), +b.dataset.k));
    });
    const pop = $(this.id('pop'));
    pop.addEventListener('click', e => {
      const btn = e.target.closest('button[data-bit]');
      if (!btn || !this.popOpen) return;
      const act = btn.dataset.bit;
      const v = this.value();
      if (act === 'zero') this._emitEdit(this.cell.off, 0x00);
      else if (act === 'ones') this._emitEdit(this.cell.off, 0xff);
      else if (act === 'inv') this._emitEdit(this.cell.off, ~v & 0xff);
      else if (act === 'orig') this._emitEdit(this.cell.off, this.original() ?? v);
      else if (act === 'close') this.closePop();
    });
    const hex = $(this.id('pop-hex'));
    hex.addEventListener('input', e => {
      if (!this.popOpen) return;
      const t = e.target.value.trim().replace(/^0[xX]/, '');
      if (!/^[0-9a-fA-F]{1,2}$/.test(t)){ e.target.classList.add('bad'); return; }
      e.target.classList.remove('bad');
      this._emitEdit(this.cell.off, parseInt(t, 16));
    });
    hex.addEventListener('keydown', e => { if (e.key === 'Enter') this.closePop(); });
    document.addEventListener('pointerdown', e => {
      if (!this.popOpen) return;
      if (pop.contains(e.target) || e.target.closest?.('button.rb')) return;   // 点另一个字节 = 换一格
      this.closePop();
    }, true);
    document.addEventListener('keydown', this._key);
    window.addEventListener('scroll', this._follow, { passive: true, capture: true });
    window.addEventListener('resize', this._follow, { passive: true });
    $(this.id('body')).addEventListener('click', e => {
      const b = e.target.closest('button.rb');
      if (b) this.openPop(+b.dataset.off, b);
    });
    return true;
  }

  /** 换一份数据（读回后调）*/
  setData({ base, cur } = {}){
    this.base = base instanceof Uint8Array ? base : new Uint8Array(base || 0);
    this.cur = cur instanceof Uint8Array ? cur : (base ? this.base.slice() : new Uint8Array(0));
  }

  /** 原地改一个字节（各页从别处改数据时用）*/
  setByte(off, v){
    const next = this.cur.slice();
    next[off] = v & 0xff;
    this.cur = next;
    this.paintByte(off);
  }

  get length(){ return this.cur.length; }
  /** 有改动的字节下标（写回「只写改动」用它）*/
  changedOffsets(){
    const out = [];
    const n = Math.min(this.base.length, this.cur.length);
    for (let i = 0; i < n; i++) if (this.base[i] !== this.cur[i]) out.push(i);
    return out;
  }

  /** 丢弃改动：回到"器件现值"*/
  discard(){
    if (!this.base.length) return;
    this.cur = this.base.slice();
    this.render();
    this.renderPop();
  }

  value(){ return this.cell ? (this.cur[this.cell.off] ?? 0) : 0; }
  original(){ return this.cell && this.base.length ? (this.base[this.cell.off] ?? null) : null; }

  openPop(off, anchor){
    if (!anchor) return;
    this.cell = { off, anchor };
    const pop = $(this.id('pop'));
    pop.hidden = false;
    this.renderPop();
    this.reposition();
  }
  closePop(){
    if (!this.popOpen) return;
    this.cell = null;
    const pop = $(this.id('pop'));
    if (pop) pop.hidden = true;
  }
  /** 表格重绘后把锚点找回来（重绘会换掉 DOM 节点）*/
  reattach(){
    if (!this.popOpen) return;
    const el = document.querySelector(`#${this.id('body')} button.rb[data-off="${this.cell.off}"]`);
    if (el) this.cell.anchor = el; else this.closePop();
  }
  reposition(){
    if (!this.popOpen) return;
    const pop = $(this.id('pop'));
    if (!this.cell.anchor?.isConnected){ this.closePop(); return; }
    const r = this.cell.anchor.getBoundingClientRect();
    const left = Math.min(Math.max(6, r.left - 40), window.innerWidth - pop.offsetWidth - 8);
    let top = r.bottom + 6;
    if (top + pop.offsetHeight > window.innerHeight - 8) top = Math.max(8, r.top - pop.offsetHeight - 6);
    pop.style.left = Math.round(left) + 'px';
    pop.style.top = Math.round(top) + 'px';
  }

  _emitEdit(off, v){
    this.setByte(off, v);
    this.onEdit?.(off, v & 0xff);
    if (this.popOpen) this.renderPop();
  }

  /** 整表重绘（读回、丢弃改动、写回成功后调）*/
  render(){
    const body = $(this.id('body'));
    const n = this.cur.length;
    body.innerHTML = '';
    if (!n){
      const tr = document.createElement('tr');
      tr.innerHTML = `<td colspan="${this.cols + 2}" class="hint">还没读 —— 填好参数后点「读取」</td>`;
      body.appendChild(tr);
      this.renderHead();
      return;
    }
    for (let row = 0; row < Math.ceil(n / this.cols); row++){
      const tr = document.createElement('tr');
      const first = row * this.cols;
      const td = document.createElement('td');
      td.className = 'ra';
      td.textContent = this.labels.addr ? this.labels.addr(first) : '+' + first;
      tr.appendChild(td);
      for (let c = 0; c < this.cols; c++){
        const cell = document.createElement('td');
        cell.className = 'rbc';
        const off = first + c;
        if (off < n){
          const b = document.createElement('button');
          b.className = 'rb';
          b.dataset.off = String(off);
          b.textContent = h2(this.cur[off]);
          if (this.base.length && this.base[off] !== this.cur[off]) b.classList.add('chg');
          b.title = this.labels.title ? this.labels.title(off, this.base[off], this.cur[off])
                                      : `偏移 +${off} 点开改 bit`;
          cell.appendChild(b);
        }
        tr.appendChild(cell);
      }
      const asc = document.createElement('td');
      asc.className = 'rascii';
      asc.textContent = this.asciiOf(first);
      tr.appendChild(asc);
      body.appendChild(tr);
    }
    this.renderHead();
  }

  renderHead(){
    const head = $(this.id('head'));
    if (!head) return;
    head.innerHTML = '<th class="ra">地址</th>' +
      Array.from({ length: this.cols }, (_, i) => `<th class="rh">${i.toString(16).toUpperCase()}</th>`).join('') +
      '<th class="rascii">ASCII</th>';
  }

  asciiOf(first){
    const n = this.cur.length;
    let s = '';
    for (let i = first; i < Math.min(first + this.cols, n); i++){
      const v = this.cur[i];
      s += v >= 0x20 && v <= 0x7e ? String.fromCharCode(v) : '.';
    }
    return s;
  }

  /** 只重画一个字节格 + 它所在行的 ASCII 列*/
  paintByte(off){
    const btn = document.querySelector(`#${this.id('body')} button.rb[data-off="${off}"]`);
    if (!btn) return;
    const v = this.cur[off], b = this.base[off];
    btn.textContent = h2(v);
    btn.classList.toggle('chg', b != null && b !== v);
    if (this.labels.title) btn.title = this.labels.title(off, b, v);
    const asc = btn.closest('tr')?.querySelector('td.rascii');
    if (asc) asc.textContent = this.asciiOf(Math.floor(off / this.cols) * this.cols);
  }

  /** 开关板重绘（8 个方块 + 值文本 + 十六进制框）*/
  renderPop(){
    if (!this.popOpen) return;
    const off = this.cell.off;
    const v = this.value(), o = this.original();
    $(this.id('pop-title')).textContent = this.labels.popTitle ? this.labels.popTitle(off) : `偏移 +${off}`;
    $(this.id('pop-val')).textContent = `0x${h2(v)} = ${v} = 0b${bitsOf(v).slice().reverse().join('')}` +
      (o != null && o !== v ? `　（原 0x${h2(o)}）` : '');
    const bits = bitsOf(v);
    const parts = [];
    for (let k = 7; k >= 0; k--){
      const on = bits[k] ? 1 : 0;
      const was = o == null ? on : ((o >> k) & 1);
      parts.push(`<button class="bit${on ? ' on' : ''}${on !== was ? ' chg' : ''}" data-k="${k}"` +
        ` title="bit${k}（权重 ${1 << k}）—— 点一下翻转">` +
        `<span class="bk">bit${k}</span><span class="bv">${on}</span><span class="bw">${1 << k}</span></button>`);
    }
    $(this.id('pop-bits')).innerHTML = parts.join('');
    const hex = $(this.id('pop-hex'));
    if (document.activeElement !== hex){ hex.value = h2(v); hex.classList.remove('bad'); }
    const hint = $(this.id('pop-hint'));
    if (hint){
      hint.innerHTML = '点 bit 翻转它；<span style="color:var(--warn)">黄框</span> = 与<b>读回来的原值</b>不同的位。' +
        '改完点「只写改动」把这一格（以及其它改过的格）写回器件。';
    }
  }
}
