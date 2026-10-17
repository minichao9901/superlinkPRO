/**
 * 「寄存器」面板（`#i2c` 页的第 3 个 dock tab）——**读一段 → 逐位改 → 写回**。
 *
 * 为什么要有它：调试 I2C 器件时 90% 的动作是"把某个寄存器的某一位打开/关掉，看看效果"。
 * 命令表/脚本能做，但每一步都要人肉把 `0x55` 拆成 8 个位、数第几位、再手拼十六进制 ——
 * 这里把这套动作压成：**读一次（默认 128 B）→ 表里点字节 → 点那一位 → 只写改动**。
 *
 * 表格与位开关板在 `app/ui/reg-grid.js`（**与 `#spi` 的寄存器面板共用一份**）；
 * 本文件只管 I2C 特有的部分：参数（器件/起始/长度/地址宽度/读法）、
 * 长读长写（`session.readLong` / `writeLong`，即 `protocol.planRead/planWrite`）与摘要。
 *
 * 三条语义纪律：
 *   1. **改的是内存里这份缓冲（`cur`），不碰器件**；只有点「只写改动 / 整块写回」才发 I2C。
 *   2. `base` 是"读回来的原值"，**写回成功后才更新** —— 黄框标的就是"与器件现状不同的字节"。
 *   3. 读完之后改过器件/起始/地址宽度就**拒绝写回**（`readKey` 指纹）—— 否则会把旧地址读回来的
 *      数据写到新地址上（对 EEPROM 尤其致命）。
 */
import { $ } from '../ui/dom.js';
import { store } from '../core/store.js';
import { RegGrid } from '../ui/reg-grid.js';
import * as P from './protocol.js';
import * as R from './registers.js';

const h2 = v => (v & 0xff).toString(16).toUpperCase().padStart(2, '0');
/** 日志里给一小段十六进制做证据（只取头部，别把整屏刷满）*/
const hexHead = (u8, n = 16) => Array.from(u8.subarray(0, n), h2).join(' ') + (u8.length > n ? ' …' : '');

export class RegView {
  /** @param {{session:object, onOp?:(op:object)=>void}} opts */
  constructor({ session, onOp = null } = {}){
    this.session = session;
    this.onOp = onOp;
    this.dev = 0x50;
    this.start = [0x00];
    this.addrLen = 1;
    this.len = R.REG_LEN_DFT;
    this.chunk = 'reset';
    this.wrChunk = P.WR_MAX;
    this.gapMs = 0;
    this.pageSize = 0;                   // 器件写页大小（0 = 不限）；见 registers.PAGE_CHOICES 的 🚨
    this.verify = true;                  // 写完自动回读对账
    this.busy = false;
    this.connected = false;
    this.readKey = null;                 // 「这份数据是从哪儿读来的」指纹（写回前对账）
    this.grid = new RegGrid({
      prefix: 'i2-reg', cols: R.REG_COLS,
      onEdit: (off, v) => this._onBitEdit(off, v),
      labels: {
        addr: off => R.addrLabel(this.start, off, this.addrLen),
        title: (off, b, c) => `偏移 +${off}（${R.addrLabel(this.start, off, this.addrLen)}）` +
          ` · 器件现值 0x${b != null ? h2(b) : '--'}` +
          (b != null && b !== c ? ` → 改成 0x${h2(c)}` : '') + '　点开改 bit',
        popTitle: off => `偏移 ${R.addrLabel(this.start, off, this.addrLen)}`,
      },
    });
  }

  // `base` / `cur` 是"这份数据"的两个版本；页面自测直接读写它们（因此要有 setter）
  get base(){ return this.grid.base; }
  set base(v){ this.grid.base = v instanceof Uint8Array ? v : new Uint8Array(v || 0); }
  get cur(){ return this.grid.cur; }
  set cur(v){ this.grid.cur = v instanceof Uint8Array ? v : new Uint8Array(v || 0); }

  init(){
    const ids = ['i2-reg-dev', 'i2-reg-start', 'i2-reg-len', 'i2-reg-alen', 'i2-reg-chunk', 'i2-reg-wrchunk',
      'i2-reg-gap', 'i2-reg-page', 'i2-reg-verify'];
    // 写分片档位（EEPROM 页写要按页给，见 registers.WRITE_CHOICES 注释）
    const wsel = $('i2-reg-wrchunk');
    for (const [v, t] of R.WRITE_CHOICES) wsel.appendChild(new Option(t, String(v)));
    // 页大小档位：这一格才是页写回卷的防线（只设写分片挡不住起始地址不对齐）
    const psel = $('i2-reg-page');
    for (const [v, t] of R.PAGE_CHOICES) psel.appendChild(new Option(t, String(v)));
    // 恢复上次填的参数（调试时来回切页/刷新不该丢）
    $('i2-reg-dev').value = store.get('i2c.regDev', '0x50');
    $('i2-reg-start').value = store.get('i2c.regStart', '0x00');
    $('i2-reg-len').value = String(store.get('i2c.regLen', R.REG_LEN_DFT));
    $('i2-reg-alen').value = String(store.get('i2c.regAddrLen', 1));
    $('i2-reg-chunk').value = store.get('i2c.regChunk', 'reset');
    wsel.value = String(store.get('i2c.regWrChunk', P.WR_MAX));
    $('i2-reg-gap').value = String(store.get('i2c.regGap', 0));
    psel.value = String(store.get('i2c.regPage', 0));
    $('i2-reg-verify').checked = store.get('i2c.regVerify', 1) !== 0;
    for (const id of ids) $(id).addEventListener('change', () => this._saveParams());
    // 面板上的三个动作都可能抛（输入非法、USB 掉线…）——统一兜住并写进日志，
    // 别变成"未捕获的 promise 错误"（页面自测会把它当失败，用户也看不到原因）
    const guard = fn => () => fn().catch(e => this.session.log('e', '寄存器面板：' + (e?.message || e)));
    $('i2-reg-read').addEventListener('click', guard(() => this.read()));
    $('i2-reg-write').addEventListener('click', guard(() => this.write({ only: true })));
    $('i2-reg-write-all').addEventListener('click', guard(() => this.write({ only: false })));
    $('i2-reg-discard').addEventListener('click', () => this.discard());
    $('i2-reg-dev').addEventListener('change', () => this._renderSummary());
    this.grid.init();
    this._render();
    this._renderSummary();
  }

  /** 扫描页「选用」时把器件地址带过来（省得两头填）*/
  setDevice(text){
    const el = $('i2-reg-dev');
    if (!el) return;
    el.value = text;
    this._saveParams();
    this._renderSummary();
  }

  setEnabled(on){
    this.connected = !!on;
    this._syncButtons();
  }

  _syncButtons(){
    const on = this.connected && !this.busy;
    for (const id of ['i2-reg-read', 'i2-reg-write', 'i2-reg-write-all', 'i2-reg-discard']){
      const b = $(id);
      if (b) b.disabled = !on;
    }
    const w = $('i2-reg-write');
    if (w) w.disabled = !on || !this.grid.changedOffsets().length;
  }

  _saveParams(){
    store.set('i2c.regDev', $('i2-reg-dev').value.trim());
    store.set('i2c.regStart', $('i2-reg-start').value.trim());
    store.set('i2c.regLen', $('i2-reg-len').value.trim());
    store.set('i2c.regAddrLen', +$('i2-reg-alen').value || 0);
    store.set('i2c.regChunk', $('i2-reg-chunk').value);
    store.set('i2c.regWrChunk', +$('i2-reg-wrchunk').value || P.WR_MAX);
    store.set('i2c.regGap', Math.max(0, +$('i2-reg-gap').value || 0));
    store.set('i2c.regPage', Math.max(0, +$('i2-reg-page').value || 0));
    store.set('i2c.regVerify', $('i2-reg-verify').checked ? 1 : 0);
  }

  /** 读参数（任何一项非法就抛，错误信息直接给用户看）*/
  _params(){
    const addrLen = +$('i2-reg-alen').value || 0;
    const dev = R.parseDev($('i2-reg-dev').value);
    const start = R.parseStart($('i2-reg-start').value, addrLen);
    const len = R.parseLen($('i2-reg-len').value);
    return { dev, start, addrLen, len, chunk: $('i2-reg-chunk').value === 'ptr' ? 'ptr' : 'reset',
             wrChunk: +$('i2-reg-wrchunk').value || P.WR_MAX,
             gapMs: Math.max(0, +$('i2-reg-gap').value || 0),
             pageSize: Math.max(0, +$('i2-reg-page').value || 0),
             verify: $('i2-reg-verify').checked };
  }

  /** 「这份缓冲是从哪儿读来的」指纹（器件 + 起始地址 + 地址宽度）—— 写回前必须一致 */
  static keyOf(p){
    return `${p.dev}|${(p.start || []).join(',')}|${p.addrLen}`;
  }

  async read(){
    let p;
    try { p = this._params(); }
    catch (e){ this.session.log('e', '寄存器面板：' + e.message); return; }
    this._saveParams();
    this.busy = true; this._syncButtons();
    try {
      const r = await this.session.readLong({ dev: p.dev, addr: p.start, rd: p.len, chunk: p.chunk },
        { label: `寄存器读 ${P.addr7(p.dev)}${p.addrLen ? '[' + R.addrLabel(p.start, 0, p.addrLen) + ']' : ''} × ${p.len}` });
      if (r.err !== P.E.OK){
        this.session.log('e', `寄存器读失败：${P.errText(r.err)}${r.failNote ? '（' + r.failNote + '）' : ''}`);
        return;
      }
      this.dev = p.dev; this.start = p.start; this.addrLen = p.addrLen; this.len = p.len; this.chunk = p.chunk;
      this.readKey = RegView.keyOf(p);      // 记住"这份数据是从哪儿读来的"，写回前要对账
      // 长度对不上（器件提前 NACK 停了 / 少回几个字节）就如实说，别假装读全了
      if (r.data.length !== p.len) this.session.log('w', `寄存器读：想要 ${p.len} B，只回来 ${r.data.length} B —— 表里按实际长度画`);
      const n = r.data.length || p.len;
      const base = new Uint8Array(n);
      base.set(r.data);
      this.grid.setData({ base, cur: base.slice() });
      this._render();
    } finally {
      this.busy = false; this._syncButtons();
      this._renderSummary();
      this.grid.reattach();
    }
  }

  /** 写回：`only` = 只写改动字节（相邻的合并成一片）；否则整块 */
  async write({ only = true } = {}){
    if (!this.grid.length){ this.session.log('w', '寄存器面板：还没读过，先「读取」再写（不然不知道器件现状）'); return; }
    let p;
    try { p = this._params(); }
    catch (e){ this.session.log('e', '寄存器面板：' + e.message); return; }
    /* 🚨 读完之后改过「器件 / 起始 / 地址宽度」就**不许写**：手里这份数据是从旧地址读回来的，
     *    照着新地址写下去 = 把一段数据糊到别的器件/别的寄存器区上（对 EEPROM 尤其致命）。
     *    要换地址就重新读一次 —— 这也是唯一能保证"黄框 = 与器件现状的差异"成立的用法。 */
    if (this.readKey && RegView.keyOf(p) !== this.readKey){
      this.session.log('e', '寄存器面板：器件/起始地址/地址宽度在「读取」之后被改过 —— 现在写回会把旧地址读回来的数据写到新地址上。请先重新「读取」');
      return;
    }
    const offs = only ? this.grid.changedOffsets() : null;
    if (only && !offs.length){ this.session.log('w', '寄存器面板：没有改动，不用写'); return; }
    const diff = R.diffBytes(this.grid.base, this.grid.cur);
    this.busy = true; this._syncButtons();
    try {
      const r = await this.session.writeLong(
        { dev: p.dev, addr: p.start, data: this.grid.cur, offsets: offs,
          chunkMax: p.wrChunk, pageSize: p.pageSize, gapMs: p.gapMs },
        { label: `寄存器写 ${P.addr7(p.dev)}${p.addrLen ? '[' + R.addrLabel(p.start, 0, p.addrLen) + ']' : ''}` +
                 ` × ${offs ? offs.length : this.grid.cur.length} B${offs ? '（只写改动）' : '（整块）'}` });
      if (r.err !== P.E.OK) return;
      /* 器件回了 ACK **不等于**写对了：EEPROM 页写回卷、器件把非法字段夹掉、片间 tWR 没等够，
       * 都会让它老实 ACK 却存了别的东西。所以按需回读同一段对账一次。
       * （2026-10 代码审查：旧代码这里直接 base = cur —— 黄框消失、界面显示成功，而数据其实是花的。）
       * 对不上就**不更新 base**：黄框留着，用户一眼看得见哪几个字节没进去。 */
      if (p.verify){
        const want = this.grid.cur;
        const vr = await this.session.readLong({ dev: p.dev, addr: p.start, rd: want.length, chunk: p.chunk },
          { quiet: true });
        if (vr.err !== P.E.OK){
          this.session.log('w', `写后回读失败：${P.errText(vr.err)} —— 无法确认写进去了没有，先别当成功`);
        } else if (vr.data.length !== want.length || !R.bytesEq(vr.data, want)){
          this.session.log('e', `写后回读与写入不一致 —— 器件里存的不是这份数据：` +
            `写 ${hexHead(want)} / 读 ${hexHead(vr.data)}。常见原因：① 器件有页写回卷而「页大小」没给对` +
            `（EEPROM 必填）；② 片间 tWR 等太短；③ 器件把非法字段夹掉。黄框保留，别当写成功`);
          return;
        }
      }
      // 写成功（必要时回读也一致）→ "器件现状"就是现在这份，黄框随之清掉
      this.grid.base = this.grid.cur.slice();
      this._render();
      if (diff.length) this.session.log('dim', '改动已写回：' + diff.slice(0, 8).map(d =>
        `+${d.off} 0x${h2(d.from)}→0x${h2(d.to)}`).join('、') + (diff.length > 8 ? ` …共 ${diff.length} 处` : ''));
    } finally {
      this.busy = false; this._syncButtons();
      this._renderSummary();
      this.grid.reattach();
    }
  }

  /** 丢弃改动（回到读回来的那份）*/
  discard(){
    this.grid.discard();
    this._renderSummary();
  }

  _onBitEdit(off, v){
    this._renderSummary();
    void off; void v;
  }

  /** 画表（页面自测也直接调它）*/
  _render(){ this.grid.render(); this._syncButtons(); }

  _renderSummary(){
    let p = null;
    try { p = this._params(); } catch { /* 输入还没填全时只报个大概 */ }
    const sum = $('i2-reg-sum');
    if (sum){
      sum.textContent = R.summarize({
        len: p?.len ?? this.len, base: this.grid.base, cur: this.grid.cur,
        dev: p?.dev ?? this.dev, start: p?.start ?? this.start, addrLen: p?.addrLen ?? this.addrLen,
      });
    }
    this._syncButtons();
  }
}
