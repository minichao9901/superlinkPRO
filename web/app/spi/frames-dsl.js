/**
 * 手写多帧 —— **键值行 DSL**（一行一条帧）。纯函数，不碰 DOM，Node 自测直接打。
 *
 * 为什么要有这层：桥页那套"一个 XFER 表单 + 简单帧按钮"只够发单条命令；真正干活时
 * 要的是"一次贴一整段序列发下去"（面板初始化、NOR 的 WREN→SE→轮询→PP、四线连读…）。
 * 语法刻意长得像协议本身（`cmd=` / `addr=` / `dummy=` / `lines=`…），屏幕上的字和
 * 协议字段一一对应，不用再记第二套词汇。
 *
 * ── 语法 ────────────────────────────────────────────────────────────────
 *   # 注释（也支持 //）到行尾；空行忽略
 *
 *   0x11                            裸字节 = 一条 XFER（发 cmd 相位，最常用）
 *   xfer cmd=0x9F rx=3              通用 XFER；`xfer` 可省略（直接 cmd=… 开写）
 *   xfer cmd=0x03 addr=0 addrl=3 rx=492 cs_hold
 *   step cmd=0x11 delay=120         面板初始化步（STEP 帧；tx= 是参数）
 *   delay 120ms                     延时（裸数字 = µs；支持 us/µs/ms/s）
 *   gpio DC 1                       辅助脚写（按"有效电平"自动取反，与固件一致）
 *   cs low | cs high                片选：low = 占用（拉低），high = 释放
 *   reset 10 120                    复位脉冲：拉低 10 ms + 等 120 ms（第二个数可省，缺省 120）
 *   ping | auxin                    保活 / 读辅助输入
 *   {0x9F, 1, 0, 0x000000, 0, 3, NULL},   C 表的一行（列序 = 页面上那张命令表）
 *
 * ── XFER 的 key ─────────────────────────────────────────────────────────
 *   cmd=0x9F        命令字节（**裸数字按十六进制**，SPI 惯例；和页面 cmd 框一致）
 *   tx=00,01,02     发送数据（十六进制，分隔符 , ; : _ - 空格与 0x 前缀都吃；带空格要加引号）
 *   rx=3            接收字节数
 *   addr=0x1000     地址（0x… 十六进制，否则十进制）→ 自动带地址相位
 *   addrl=3         地址字节数 0..4（给了 addr 没给 addrl 时缺省 3）
 *   dummy=1         dummy 周期 0..4
 *   lines=1|2|4     数据相位线数（缺省 1）
 *   开关：rsp cs_hold cs_off cs_aux poll dma addrquad token dc dc1 dc0
 *
 * ── 三条自动规则（省得每行都写一遍）──────────────────────────────────────
 *   1. 写了 `cmd=` 就自动加 CMD_EN；写了 `addr=` 或 `addrl>0` 就自动加 ADDR_EN。
 *   2. `rx>0` 的帧自动加 RSP —— 固件规定"要读数据必须带 RSP"，不加就是白写。
 *   3. 整段里**一条 RSP 都没有**时，给最后一条帧自动加 RSP（否则发完不知道成没成）。
 *      你自己在任何一条上写了 `rsp`，就不再自动加。
 *
 * ── 校验（错在解析期就拦住，别让固件回一个 RANGE 让你猜）────────────────
 *   tx ≤ 492 B（单帧上限）；tx 与 rx 同时非零必须等长（全双工）；lines/dummy/addrl 范围；
 *   TE 是输入脚不能写；未知 key / 少参数 / 数字非法 → 带**行号 + 原行**报错。
 */
import {
  F, LINE, T, TC, XFER_HDR, XFER_TX_MAX, csPayload, delayPayload, gpioPayload,
  linesToTcfg, resetPayload, stepPayload, tcfgToLines, xferPayload,
} from './protocol.js';
import { parseAs } from '../core/expr.js';

/** 行首关键字（第一段不是 key=value 也不是裸字节时按关键字认）*/
const KEYWORDS = ['xfer', 'spi', 'step', 'delay', 'gpio', 'cs', 'reset', 'ping', 'auxin'];

/**
 * **块级 / 行尾修饰**关键字（与 `#i2c` 的脚本 DSL 同一套语义，学一次两边都会用）：
 *   · `loop 50ms [20]` … `end` —— 块内每行按 50 ms 周期跑（限一段块；`20` = 跑 20 轮自停）
 *   · `every 100ms [50]`        —— 不成块也行：把**后面所有行**设成周期
 *   · `once`                    —— 取消周期，回到一次性
 *   · 行尾 `as a=u8(0), b=u16be(2)` / `every 20ms` —— 只作用于这一行
 */
const BLOCK_KEYS = new Set(['loop', 'repeat', 'end', 'endloop', 'every', 'once']);
/** 行尾修饰（`buildXfer` / `parseCRow` 会把它们从参数流里摘走）*/
const MOD_KEYS = new Set(['as', 'every', 'tag']);

/** gpio 的行号写法（大小写不敏感；也吃协议里的数字 0..3）*/
const LINE_ALIAS = {
  dc: LINE.DC, rst: LINE.RST, reset: LINE.RST, csaux: LINE.CS_AUX, cs_aux: LINE.CS_AUX,
  cs辅助: LINE.CS_AUX, aux: LINE.CS_AUX, bl: LINE.BL, backlight: LINE.BL, te: LINE.TE,
  '0': LINE.DC, '1': LINE.RST, '2': LINE.CS_AUX, '3': LINE.BL, '4': LINE.TE,
};

/** 开关 key → 帧 flags */
const FLAG_KEYS = {
  rsp: F.RSP, cs_hold: F.CS_HOLD, cshold: F.CS_HOLD, cs_off: F.CS_OFF, csoff: F.CS_OFF,
  cs_aux: F.CS_AUX, csaux: F.CS_AUX, poll: F.NO_DMA, nodma: F.NO_DMA,
  dma: F.FORCE_DMA, force_dma: F.FORCE_DMA, forcedma: F.FORCE_DMA,
};
/** 开关 key → tcfg 位（dc1/dc0 是一对组合）*/
const TCFG_KEYS = {
  addrquad: TC.ADDR_QUAD, addr_quad: TC.ADDR_QUAD,
  token: TC.TOKEN_EN, dc: TC.DC_EN, dcen: TC.DC_EN,
  dc1: TC.DC_EN | TC.DC_LEVEL, dcdata: TC.DC_EN | TC.DC_LEVEL, dchigh: TC.DC_EN | TC.DC_LEVEL,
  dc0: TC.DC_EN, dccmd: TC.DC_EN,
};

// ──────────────────────────────────────────────────────────── 小工具

/** 去掉行尾注释（引号里的 # / // 不算）*/
export function stripComment(line){
  let q = false;
  for (let i = 0; i < line.length; i++){
    const c = line[i];
    if (c === '"') q = !q;
    else if (!q && c === '#') return line.slice(0, i);
    else if (!q && c === '/' && line[i + 1] === '/') return line.slice(0, i);
  }
  return line;
}

/** 空白分词；`"…"` 里的空格保留（tx="00 01 02"，引号可以在 token 中间）*/
export function tokenize(s){
  const out = [];
  let i = 0;
  while (i < s.length){
    while (i < s.length && /\s/.test(s[i])) i++;
    if (i >= s.length) break;
    let tok = '';
    while (i < s.length && !/\s/.test(s[i])){
      if (s[i] === '"'){
        i++;
        while (i < s.length && s[i] !== '"') tok += s[i++];
        if (s[i] !== '"') throw new Error('引号没有配对');
        i++;
      } else {
        tok += s[i++];
      }
    }
    out.push(tok);
  }
  return out;
}

const isBareHexByte = t => /^(0x)?[0-9a-fA-F]{1,2}$/i.test(t);

/**
 * 数字：`0x…` 一律十六进制；否则默认十进制（`defHex` 为真时默认十六进制 —— cmd 用）。
 * @returns {{ok:boolean, v?:number, why?:string}}
 */
export function parseNum(tok, defHex = false){
  const t = String(tok ?? '').trim();
  if (t === '') return { ok: false, why: '空值' };
  const m = /^(0x|0X)?([0-9a-fA-F]+)$/.exec(t);
  if (m){
    const hex = !!m[1] || defHex;
    if (!hex && !/^[0-9]+$/.test(t)) return { ok: false, why: `"${t}" 不是十进制数字（十六进制请写 0x…）` };
    const v = parseInt(m[2], hex ? 16 : 10);
    if (!Number.isFinite(v)) return { ok: false, why: `"${t}" 解析失败` };
    return { ok: true, v };
  }
  // （2）位运算表达式：`0x80|0x3B`、`0x40|(0x2<<1)`、`0xFF&0x0F`
  //     寄存器面板那套 "opcode | 寄存器号" 直接写进脚本就行，不必先自己算成 0xBB
  if (/^[0-9a-fA-FxX\s|&<>+\-()]+$/.test(t)){
    try {
      return { ok: true, v: evalBitExpr(t, defHex) };
    } catch (e){
      return { ok: false, why: `"${t}" 不是数字（位运算表达式：${e?.message || e}）` };
    }
  }
  return { ok: false, why: `"${t}" 不是数字` };
}

/**
 * 位运算小计算器：支持 `| & << >> + -` 与括号。
 * **刻意从简：从左到右、不分优先级**（`0x80|0x3B` 才是常见写法，没人会写混合优先级的式子）；
 * 数字按 `defHex` 决定裸写法是十进制还是十六进制（`cmd=` 那颗是十六进制）。
 */
export function evalBitExpr(src, defHex = false){
  const s = String(src).replace(/\s+/g, '');
  let i = 0;
  const atom = () => {
    if (s[i] === '('){
      i++;
      const v = expr();
      if (s[i] !== ')') throw new Error('括号没配对');
      i++;
      return v;
    }
    const m = /^(0[xX][0-9a-fA-F]+|[0-9a-fA-F]+)/.exec(s.slice(i));
    if (!m) throw new Error(`第 ${i + 1} 个字符处应该是数字`);
    i += m[1].length;
    const hex = /^0[xX]/.test(m[1]) || defHex;
    if (!hex && !/^[0-9]+$/.test(m[1])) throw new Error(`"${m[1]}" 不是十进制（十六进制请写 0x…）`);
    return parseInt(m[1].replace(/^0[xX]/, ''), hex ? 16 : 10);
  };
  const expr = () => {
    let v = atom();
    for (;;){
      const op = /^(<<|>>|[|&+\-])/.exec(s.slice(i));
      if (!op) break;
      i += op[1].length;
      const r = atom();
      v = op[1] === '|' ? (v | r)
        : op[1] === '&' ? (v & r)
        : op[1] === '<<' ? (v << r)
        : op[1] === '>>' ? (v >>> r)
        : op[1] === '+' ? (v + r) : (v - r);
    }
    return v;
  };
  const v = expr();
  if (i !== s.length) throw new Error(`第 ${i + 1} 个字符起看不懂`);
  return v;
}

/** 十六进制字节串：`00,01,02` / `0x00 0x01`（带空格要引号）/ `000102` */
export function parseHexBytesLoose(tok){
  const t = String(tok ?? '').replace(/0x/gi, '').replace(/[\s,;:_-]/g, '');
  if (t === '') return new Uint8Array(0);
  if (!/^[0-9a-fA-F]+$/.test(t)) throw new Error('十六进制里有非法字符（只吃 0-9a-f，分隔符 , ; : _ - 空格）');
  if (t.length % 2) throw new Error('十六进制要成对（每字节两位）');
  const out = new Uint8Array(t.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(t.substr(i * 2, 2), 16);
  return out;
}

/** 时长：`120ms` / `500us` / `1.5s` / 裸数字（µs） */
export function parseDuration(tok){
  const t = String(tok ?? '').trim();
  const m = /^([0-9]*\.?[0-9]+)\s*(us|µs|ms|s)?$/i.exec(t);
  if (!m) return { ok: false, why: `"${t}" 不是时长（例：120ms / 500us / 1.5s / 1000）` };
  const n = parseFloat(m[1]);
  const unit = (m[2] || 'us').toLowerCase();
  const us = unit === 's' ? n * 1e6 : unit === 'ms' ? n * 1e3 : n;
  return { ok: true, us: Math.round(us) };
}

// ──────────────────────────────────────────────────────────── 解析器

/**
 * 解析整段文本 → 帧列表。
 * @param {string} text
 * @param {{maxTx?:number}} [opts]
 * @returns {{items:Array<{type:number,payload:Uint8Array,flags:number,label:string,line:number,kind:string}>,
 *            errors:Array<{line:number,text:string,msg:string}>, warns:string[], stats:object}}
 */
export function parseFrames(text, opts = {}){
  const maxTx = opts.maxTx ?? XFER_TX_MAX;
  const src = String(text ?? '').split(/\r?\n/);
  const items = [], errors = [], warns = [];
  /** 解析期的块状态（loop / every 会改它）—— 与 `#i2c` 的脚本区同一套口径 */
  const st = { period: 0, count: 0, group: 0, loops: [], declared: [] };

  for (let i = 0; i < src.length; i++){
    const lineNo = i + 1;
    const raw = stripComment(src[i]);
    if (!raw.trim()) continue;
    const text0 = src[i].trim();
    /**
     * 嗅行首关键字**只用正则**（不 tokenize）：`tokenize` 会对没配对的引号抛错，
     * 而这个嗅探发生在 try 之外 —— 用 tokenize 的话 `xfer cmd="0x03` 这种行会**直接抛穿**
     * 整个解析（原本应该作为"第 N 行报错"收集起来）。踩过：自测里那条"引号没配对"就是这么红的。
     */
    const headM = /^([A-Za-z_][A-Za-z0-9_]*)/.exec(raw.trim());
    const head = (headM ? headM[1] : '').toLowerCase();
    try {
      // ── 块级关键字：loop / end / every / once（不进帧列表，只改周期状态）
      if (BLOCK_KEYS.has(head)){
        const rest = tokenize(raw.trim()).slice(1);
        if (head === 'loop' || head === 'repeat'){
          if (st.loops.length) throw new Error('loop 不能嵌套（一段脚本只支持一个 loop 块）');
          if (!rest.length) throw new Error('loop 后面要跟周期，例如 `loop 50ms`');
          const d = parseDuration(rest[0]);
          if (!d.ok) throw new Error(`loop 的周期有问题：${d.why}`);
          if (d.us <= 0) throw new Error('loop 的周期必须是正数');
          const n = rest[1] != null ? parseNum(rest[1]) : null;
          if (n && (!n.ok || n.v <= 0)) throw new Error('loop 的次数必须是正整数（省略 = 一直跑）');
          st.period = Math.max(1, Math.round(d.us / 1000));
          st.count = n ? n.v : 0;
          st.group++;
          st.loops.push({ line: lineNo, period: st.period, count: st.count, group: st.group });
          st.declared.push({ line: lineNo, period: st.period, count: st.count, group: st.group });
          continue;
        }
        if (head === 'end' || head === 'endloop'){
          if (!st.loops.length) throw new Error('这里没有对应的 loop（多了一个 end）');
          st.loops.pop();
          st.period = 0; st.count = 0;
          continue;
        }
        if (head === 'once'){
          st.period = 0; st.count = 0;
          continue;
        }
        // every：不成块也行
        if (!rest.length) throw new Error('every 后面要跟周期，例如 `every 100ms`');
        const d = parseDuration(rest[0]);
        if (!d.ok) throw new Error(`every 的周期有问题：${d.why}`);
        if (d.us <= 0) throw new Error('every 的周期必须是正数');
        const n = rest[1] != null ? parseNum(rest[1]) : null;
        if (n && (!n.ok || n.v <= 0)) throw new Error('every 的次数必须是正整数');
        st.period = Math.max(1, Math.round(d.us / 1000));
        st.count = n ? n.v : 0;
        st.group++;
        continue;
      }
      const it = parseLine(raw.trim(), text0, lineNo, maxTx, warns);
      if (it){
        // 行内没写 every → 跟着块/全局的周期走
        if (it.period == null && st.period){
          it.period = st.period;
          it.count = st.count;
          it.group = st.group;
        }
        items.push(it);
      }
    } catch (e){
      errors.push({ line: lineNo, text: text0, msg: e?.message || String(e) });
    }
  }
  if (st.loops.length) errors.push({ line: src.length, text: '', msg: `有 ${st.loops.length} 个 loop 没有对应的 end` });

  // 规则 3：整段一条 RSP 都没有 → 给最后一条帧补上
  if (items.length && !items.some(x => x.flags & F.RSP)){
    items[items.length - 1].flags |= F.RSP;
    items[items.length - 1].autoRsp = true;
  }

  const stats = {
    frames: items.length,
    xfer: items.filter(x => x.type === T.XFER).length,
    bytes: items.reduce((n, x) => n + x.payload.length + 8, 0),
    kinds: [...new Set(items.map(x => x.kind))],
    /** 定时采集：有周期的帧（分组按 group）、解码变量名、一次性帧数 —— 见 app/spi/runner.js */
    oneShots: items.filter(x => !x.period).length,
    timed: items.filter(x => x.period).length,
    groups: [...new Set(items.filter(x => x.period).map(x => x.group))].length,
    loops: st.declared.map(l => ({ ...l })),
    vars: items.filter(x => x.as).flatMap(x => x.as.map(a => a.name)),
  };
  return { items, errors, warns, stats };
}

/** 解析一行（不 export 给页面用都一样，留着方便单测单独打）*/
export function parseLine(line, text0, lineNo, maxTx, warns = []){
  const toks = tokenize(line);
  if (!toks.length) return null;
  const head = toks[0];
  const kw = head.toLowerCase();

  // ── 关键字行
  if (KEYWORDS.includes(kw)){
    const rest = toks.slice(1);
    switch (kw){
      case 'ping':
        if (rest.length) throw new Error('ping 不带参数');
        return mk(T.PING, new Uint8Array(0), 0, 'PING', 'ping');
      case 'auxin':
        if (rest.length) throw new Error('auxin 不带参数');
        return mk(T.AUX_IN, new Uint8Array(0), 0, 'AUX_IN', 'auxin');
      case 'delay': {
        if (rest.length !== 1) throw new Error('delay 要跟且只跟一个时长（例：delay 120ms）');
        const d = parseDuration(rest[0]);
        if (!d.ok) throw new Error(d.why);
        if (d.us <= 0) throw new Error('延时必须是正数');
        return mk(T.DELAY, delayPayload(d.us), 0, `DELAY ${d.us}µs`, 'delay');
      }
      case 'cs': {
        if (rest.length !== 1) throw new Error('cs 要跟且只跟 low / high（low = 占用/拉低，high = 释放）');
        const v = rest[0].toLowerCase();
        const assert = ['low', '↓', '0', 'assert', 'on'].includes(v) ? true
                     : ['high', '↑', '1', 'release', 'off'].includes(v) ? false : null;
        if (assert === null) throw new Error(`cs 只吃 low / high（收到 "${rest[0]}"）`);
        return mk(T.CS, csPayload(assert), 0, `CS ${assert ? '↓ 占用' : '↑ 释放'}`, 'cs');
      }
      case 'gpio': {
        if (rest.length !== 2) throw new Error('gpio 要跟「线 电平」，例：gpio DC 1');
        const ln = LINE_ALIAS[rest[0].toLowerCase()];
        if (ln === undefined) throw new Error(`认不出线名 "${rest[0]}"（DC / RST / CSAUX / BL）`);
        if (ln === LINE.TE) throw new Error('TE 是输入脚，不能写（要读它用 auxin）');
        const lv = parseNum(rest[1]);
        if (!lv.ok) throw new Error(lv.why);
        if (lv.v !== 0 && lv.v !== 1) throw new Error('gpio 电平只吃 0 / 1');
        const name = ['DC', 'RST', 'CS辅助', 'BL'][ln];
        return mk(T.GPIO, gpioPayload(ln, lv.v), 0, `GPIO ${name}=${lv.v}`, 'gpio');
      }
      case 'reset': {
        if (rest.length < 1 || rest.length > 2) throw new Error('reset 要跟低电平毫秒数（可再跟一个等待毫秒数），例：reset 10 120');
        const low = parseNum(rest[0]);
        if (!low.ok) throw new Error(low.why);
        let post = 120;
        if (rest.length > 1){
          const p = parseNum(rest[1]);
          if (!p.ok) throw new Error(p.why);
          post = p.v;
        }
        if (low.v < 0 || post < 0) throw new Error('reset 的毫秒数不能是负数');
        if (low.v > 65535 || post > 65535) throw new Error('reset 的毫秒数上限 65535');
        return mk(T.RESET, resetPayload(low.v, post), 0, `RESET ${low.v}+${post}ms`, 'reset');
      }
      case 'step': {
        const kv = readKV(rest);
        const cmd = kv.num('cmd', { hex: true, required: true });
        const params = kv.bytes('tx', { fallback: kv.bytes('params') });
        const delayMs = kv.num('delay', { def: 0 });
        // STEP 的 DC 行为由面板档（profile）决定，这里没有 dc 开关可给
        kv.done(['cmd', 'tx', 'params', 'delay']);
        if (params.length > 255) throw new Error('STEP 参数最多 255 字节');
        const flags = kv.flags;
        return mk(T.STEP, stepPayload({ cmd, params, delayMs }), flags,
          `STEP cmd=0x${cmd.toString(16).padStart(2, '0')} n=${params.length} delay=${delayMs}ms`, 'step');
      }
      case 'xfer':
      case 'spi':
        return buildXfer(rest, text0, maxTx, warns);
      default: break;
    }
  }

  // ── key=value 开头（省略 xfer 关键字）
  if (head.includes('=')) return buildXfer(toks, text0, maxTx, warns);

  // ── 裸字节 = 一条 cmd 帧（后面还能跟 key=value：`0x9F rx=3`）
  if (isBareHexByte(head)){
    const cmd = parseInt(head.replace(/^0x/i, ''), 16) & 0xff;
    return buildXfer(toks.slice(1), text0, maxTx, warns, cmd);
  }

  // ── C 表的一行：{cmd, lines, addr_len, addr, dummy, rx_len, tx}（花括号后还能跟 `as … every …`）
  if (line.trimStart().startsWith('{')){
    const close = line.lastIndexOf('}');
    const row = close > 0 ? line.slice(0, close + 1) : line;
    const tail = close > 0 ? line.slice(close + 1).replace(/^[\s,]+/, '') : '';
    const it = parseCRow(row, maxTx);
    return tail ? applyModifiers(it, extractModifiers(tokenize(tail))) : it;
  }

  throw new Error(`认不出这一行："${text0}"（裸字节 / xfer / step / delay / gpio / cs / reset / ping / auxin / {C 表行}）`);
}

/** 按分隔符切**顶层**（不切进 {} () [] 与引号里）*/
export function splitTopLevel(s, sep = ','){
  const out = [];
  let cur = '', depth = 0, q = false;
  for (let i = 0; i < s.length; i++){
    const c = s[i];
    if (c === '"'){ q = !q; cur += c; continue; }
    if (!q){
      if ('{(['.includes(c)) depth++;
      else if ('})]'.includes(c)) depth--;
      else if (c === sep && depth === 0){ out.push(cur); cur = ''; continue; }
    }
    cur += c;
  }
  out.push(cur);
  return out.map(x => x.trim());
}

/** tx 字段的几种写法：NULL / 0 / - / (uint8_t[]){0xAA,0xBB} / {0xAA,0xBB} / "AA BB" / AA,BB */
export function parseTxField(s){
  const t = String(s ?? '').trim();
  if (!t || /^(null|none|-|\{\s*\})$/i.test(t)) return new Uint8Array(0);
  const quoted = /^"(.*)"$/.exec(t);
  if (quoted) return parseHexBytesLoose(quoted[1]);
  const braced = /\{([\s\S]*)\}\s*$/.exec(t);
  if (braced) return parseHexBytesLoose(braced[1]);
  return parseHexBytesLoose(t);
}

/**
 * C 表的一行（列序与页面上那张命令表一致）：
 *   {cmd, lines, addr_len, addr, dummy, rx_len, tx}
 * 例：
 *   {0x9F, 1, 0, 0x000000, 0, 3, NULL},
 *   {0x02, 1, 3, 0x001000, 0, 0, (uint8_t[]){0xAA, 0xBB}},
 */
export function parseCRow(line, maxTx = XFER_TX_MAX){
  const s = String(line).trim().replace(/^\{/, '').replace(/\}\s*,?\s*$/, '');
  const f = splitTopLevel(s, ',');
  if (f.length < 6) throw new Error(`C 表行要 6~7 列（cmd, lines, addr_len, addr, dummy, rx_len[, tx]），收到 ${f.length} 列`);
  const num = (tok, name, defHex = false) => {
    const r = parseNum(tok, defHex);
    if (!r.ok) throw new Error(`${name} 有问题：${r.why}`);
    return r.v;
  };
  const cmd = num(f[0], 'cmd', true);
  const lines = num(f[1], 'lines');
  const addrLen = num(f[2], 'addr_len');
  const addr = num(f[3], 'addr');
  const dummy = num(f[4], 'dummy');
  const rx = num(f[5], 'rx_len');
  let tx;
  try { tx = f.length > 6 ? parseTxField(f[6]) : new Uint8Array(0); }
  catch (e){ throw new Error(`tx 有问题：${e.message}`); }
  if (![1, 2, 4].includes(lines)) throw new Error(`lines 只吃 1 / 2 / 4（收到 ${lines}）`);
  if (addrLen < 0 || addrLen > 4) throw new Error(`addr_len 范围 0..4（收到 ${addrLen}）`);
  if (dummy < 0 || dummy > 4) throw new Error(`dummy 范围 0..4（收到 ${dummy}）`);
  if (rx < 0 || rx > 504) throw new Error(`rx_len 范围 0..504（收到 ${rx}）`);
  if (tx.length > maxTx) throw new Error(`tx 有 ${tx.length} B，超过单帧上限 ${maxTx} B`);
  if (tx.length && rx && tx.length !== rx) throw new Error(`全双工要求收发等长（tx=${tx.length} rx=${rx}）`);
  if (cmd < 0 || cmd > 255) throw new Error('cmd 范围 0..255');
  let tcfg = linesToTcfg(lines) | TC.CMD_EN;
  if (addrLen > 0) tcfg |= TC.ADDR_EN;
  const flags = rx > 0 ? F.RSP : 0;
  const label = `XFER cmd=0x${cmd.toString(16).padStart(2, '0')}` +
    (addrLen ? ` addr=0x${(addr >>> 0).toString(16)}/${addrLen}B` : '') +
    (dummy ? ` dummy=${dummy}` : '') + (lines !== 1 ? ` ${lines}线` : '') +
    (tx.length ? ` tx=${tx.length}` : '') + (rx ? ` rx=${rx}` : '');
  return mk(T.XFER, xferPayload({ cmd, tcfg, addrLen, dummy, addr: addr >>> 0, tx, rxLen: rx }), flags, label, 'xfer');
}

// ──────────────────────────────────────────────────────────── 导出（与解析器互为逆运算）

const hexByte = v => '0x' + v.toString(16).padStart(2, '0');
const hexBytes = b => [...b].map(hexByte).join(', ');

/** items → 归一化 DSL 文本（经它再解析一次应得到同样的帧）*/
export function itemsToDsl(items){
  return (items || []).map(it => {
    const p = it.payload || new Uint8Array(0);
    const dv = new DataView(p.buffer, p.byteOffset, p.byteLength);
    switch (it.type){
      case T.XFER: {
        const tcfg = p[1], txLen = dv.getUint16(4, true), rxLen = dv.getUint16(6, true);
        const parts = [];
        if (tcfg & TC.CMD_EN) parts.push(`cmd=${hexByte(p[0])}`);
        if (tcfg & TC.ADDR_EN) parts.push(`addrl=${p[2]}`, `addr=0x${(dv.getUint32(8, true)).toString(16)}`);
        if (p[3]) parts.push(`dummy=${p[3]}`);
        const lines = tcfgToLines(tcfg);
        if (lines !== 1) parts.push(`lines=${lines}`);
        if (txLen) parts.push(`tx="${hexBytes(p.subarray(XFER_HDR, XFER_HDR + txLen))}"`);
        if (rxLen) parts.push(`rx=${rxLen}`);
        if (tcfg & TC.ADDR_QUAD) parts.push('addrquad');
        if (tcfg & TC.TOKEN_EN) parts.push('token');
        if (tcfg & TC.DC_EN) parts.push(tcfg & TC.DC_LEVEL ? 'dc1' : 'dc0');
        for (const [name, bit] of [['cs_hold', F.CS_HOLD], ['cs_off', F.CS_OFF], ['cs_aux', F.CS_AUX], ['poll', F.NO_DMA], ['dma', F.FORCE_DMA]])
          if (it.flags & bit) parts.push(name);
        return 'xfer ' + parts.join(' ');
      }
      case T.STEP: return `step cmd=${hexByte(p[0])}` + (p[1] ? ` tx="${hexBytes(p.subarray(4, 4 + p[1]))}"` : '') +
        (dv.getUint16(2, true) ? ` delay=${dv.getUint16(2, true)}` : '');
      case T.DELAY: return `delay ${dv.getUint32(0, true)}us`;
      case T.GPIO: return `gpio ${['DC', 'RST', 'CSAUX', 'BL', 'TE'][p[0]] || p[0]} ${p[1] ? 1 : 0}`;
      case T.CS: return p[0] ? 'cs low' : 'cs high';
      case T.RESET: return `reset ${dv.getUint16(0, true)} ${dv.getUint16(2, true)}`;
      case T.PING: return 'ping';
      case T.AUX_IN: return 'auxin';
      default: return `# 不认识帧类型 0x${(it.type || 0).toString(16)}`;
    }
  }).join('\n') + '\n';
}

/**
 * items → C 表（列序 = 页面上那张命令表：cmd, lines, addr_len, addr, dummy, rx_len, tx）。
 * 只有 XFER 能进表；别的帧类型写成注释（免得导出后静默丢东西）。
 */
export function itemsToC(items){
  const lines = [], notes = [];
  for (const it of items || []){
    if (it.type !== T.XFER){
      const dsl = itemsToDsl([it]).trim();
      notes.push(`// ${dsl}    ← 不是 XFER，C 表里放不下`);
      continue;
    }
    const p = it.payload, dv = new DataView(p.buffer, p.byteOffset, p.byteLength);
    const tcfg = p[1], txLen = dv.getUint16(4, true), rxLen = dv.getUint16(6, true);
    const tx = txLen ? `(uint8_t[]){${hexBytes(p.subarray(XFER_HDR, XFER_HDR + txLen))}}` : 'NULL';
    lines.push(`{${hexByte(p[0])}, ${tcfgToLines(tcfg)}, ${p[2]}, 0x${(dv.getUint32(8, true) >>> 0).toString(16).padStart(6, '0')}, ${p[3]}, ${rxLen}, ${tx}},`);
  }
  return (lines.length ? '// {cmd, lines, addr_len, addr, dummy, rx_len, tx}\n' + lines.join('\n') + '\n' : '') +
    (notes.length ? '\n' + notes.join('\n') + '\n' : '');
}

/** items → JSON（保真：所有帧类型都在）*/
export function itemsToJson(items){
  const out = (items || []).map(it => {
    const p = it.payload || new Uint8Array(0);
    const dv = new DataView(p.buffer, p.byteOffset, p.byteLength);
    const flags = F_BITS_LIST.filter(([, b]) => it.flags & b).map(([n]) => n);
    switch (it.type){
      case T.XFER: return { t: 'xfer', cmd: p[0], lines: tcfgToLines(p[1]), addrLen: p[2], addr: dv.getUint32(8, true), dummy: p[3],
        rx: dv.getUint16(6, true), tx: [...p.subarray(XFER_HDR, XFER_HDR + dv.getUint16(4, true))], flags };
      case T.STEP: return { t: 'step', cmd: p[0], tx: [...p.subarray(4, 4 + p[1])], delay: dv.getUint16(2, true), flags };
      case T.DELAY: return { t: 'delay', us: dv.getUint32(0, true), flags };
      case T.GPIO: return { t: 'gpio', line: p[0], level: p[1] ? 1 : 0, flags };
      case T.CS: return { t: 'cs', assert: p[0] ? 1 : 0, flags };
      case T.RESET: return { t: 'reset', low: dv.getUint16(0, true), post: dv.getUint16(2, true), flags };
      case T.PING: return { t: 'ping', flags };
      case T.AUX_IN: return { t: 'auxin', flags };
      default: return { t: 'unknown', type: it.type, flags };
    }
  });
  return JSON.stringify(out, null, 1) + '\n';
}

const F_BITS_LIST = [
  ['rsp', F.RSP], ['cs_hold', F.CS_HOLD], ['cs_off', F.CS_OFF],
  ['cs_aux', F.CS_AUX], ['poll', F.NO_DMA], ['dma', F.FORCE_DMA],
];

/** 导出的 JSON 文本 → DSL 文本（回灌用；不是我们的形状就返回 null，让调用方原样载入）*/
export function jsonToDsl(text){
  let arr;
  try { arr = JSON.parse(text); }
  catch { return null; }
  if (!Array.isArray(arr) || !arr.length || !arr.every(x => x && typeof x === 'object' && typeof x.t === 'string')) return null;
  const lines = arr.map(o => {
    const fl = (o.flags || []).length ? ' ' + o.flags.join(' ') : '';
    const tx = (o.tx || []).length ? ` tx="${hexBytes(Uint8Array.from(o.tx))}"` : '';
    switch (o.t){
      case 'xfer': {
        const parts = [];
        if (o.cmd !== undefined) parts.push(`cmd=${hexByte(o.cmd)}`);
        if (o.addrLen) parts.push(`addrl=${o.addrLen}`, `addr=0x${(o.addr >>> 0).toString(16)}`);
        if (o.dummy) parts.push(`dummy=${o.dummy}`);
        if (o.lines && o.lines !== 1) parts.push(`lines=${o.lines}`);
        if (tx) parts.push(tx.trim());
        if (o.rx) parts.push(`rx=${o.rx}`);
        return 'xfer ' + parts.join(' ') + fl;
      }
      case 'step': return `step cmd=${hexByte(o.cmd)}${tx}${o.delay ? ` delay=${o.delay}` : ''}`;
      case 'delay': return `delay ${o.us}us`;
      case 'gpio': return `gpio ${['DC', 'RST', 'CSAUX', 'BL', 'TE'][o.line] || o.line} ${o.level ? 1 : 0}`;
      case 'cs': return o.assert ? 'cs low' : 'cs high';
      case 'reset': return `reset ${o.low} ${o.post}`;
      case 'ping': return 'ping';
      case 'auxin': return 'auxin';
      default: return `# 不认识：${JSON.stringify(o)}`;
    }
  });
  return lines.join('\n') + '\n';
}

function mk(type, payload, flags, label, kind){
  return { type, payload, flags, label, kind };
}

/**
 * 把**行尾修饰**从参数流里摘出来：`as a=u8(0), b=u16be(2)` / `every 20ms [50]` / `tag 名字`。
 *
 * 为什么要单独摘：`as` 的值会被空格切开（`as ax=i16be(0)/16384, az=…`），直接丢给 `readKV`
 * 会被当成"不认识的 key"报错。摘出来之后：
 *   · `as` 的值交给 `core/expr.js` 的 `parseAs` 解析（语法错**在解析期**就带行号报出来，而不是等发送时）
 *   · `every` 只作用于这一行（覆盖块级周期）
 * @returns {{core:Array<string>, asText:string, everyText:string, countText:string, tag:string}}
 */
export function extractModifiers(toks){
  const core = [];
  let asText = '', everyText = '', countText = '', tag = '';
  const keyOf = t => { const low = t.toLowerCase(); const eq = low.indexOf('='); return eq >= 0 ? low.slice(0, eq) : low; };
  for (let i = 0; i < toks.length; i++){
    const t = toks[i];
    const key = keyOf(t);
    if (!MOD_KEYS.has(key)){ core.push(t); continue; }
    const eq = t.indexOf('=');
    const inline = eq >= 0 ? t.slice(eq + 1) : '';
    if (key === 'as'){
      const parts = inline ? [inline] : [];
      while (i + 1 < toks.length && !MOD_KEYS.has(keyOf(toks[i + 1]))) parts.push(toks[++i]);
      if (!parts.length) throw new Error('as 后面要跟表达式，例：as ax=u16be(0)/16384, az=u16be(4)/16384');
      asText += (asText ? ',' : '') + parts.join('');
    } else if (key === 'every'){
      if (inline) everyText = inline;
      else if (i + 1 < toks.length && !MOD_KEYS.has(keyOf(toks[i + 1]))) everyText = toks[++i];
      if (!everyText) throw new Error('every 后面要跟周期，例：every 20ms');
      // 可选次数：紧跟的纯数字
      if (i + 1 < toks.length && /^\d+$/.test(toks[i + 1])) countText = toks[++i];
    } else if (key === 'tag'){
      tag = inline || (i + 1 < toks.length ? toks[++i] : '');
    }
  }
  return { core, asText, everyText, countText, tag };
}

/** 修饰 → 挂到 item 上（`as` 在这里就解析，语法错带行号抛出去）*/
function applyModifiers(it, mods){
  if (!it) return it;
  if (mods.asText){
    const r = parseAs(mods.asText);
    if (!r.ok) throw new Error(`as 有问题：${r.why}`);
    it.asText = mods.asText;
    it.as = r.fields;                        // [{name, ast, text}] —— runner 按它解码
  }
  if (mods.everyText){
    const d = parseDuration(mods.everyText);
    if (!d.ok) throw new Error(`every 的周期有问题：${d.why}`);
    if (d.us <= 0) throw new Error('every 的周期必须是正数');
    it.period = Math.max(1, Math.round(d.us / 1000));
    it.count = mods.countText ? parseInt(mods.countText, 10) : 0;
  }
  if (mods.tag) it.tag = mods.tag;
  return it;
}

/**
 * 收集 key=value 与**裸开关**（`cs_hold dma` 这种不带值的写法）。
 * unknown 交给调用方 done() 校验（裸开关也算 key —— 写错了要报出来，不能静默忽略）。
 */
function readKV(toks){
  const map = new Map();
  const switches = new Set();
  for (const t of toks){
    const eq = t.indexOf('=');
    if (eq < 0){
      switches.add(t.toLowerCase().replace(/-/g, '_'));
      continue;
    }
    if (eq === 0) throw new Error(`"${t}" 的 = 左边没有 key`);
    const k = t.slice(0, eq).toLowerCase().replace(/-/g, '_');
    const v = t.slice(eq + 1);
    if (map.has(k)) throw new Error(`key "${k}" 写了两次`);
    map.set(k, v);
  }
  let flags = 0;
  let tcfgBits = 0;
  for (const k of [...map.keys(), ...switches]){
    if (FLAG_KEYS[k] !== undefined) flags |= FLAG_KEYS[k];
    if (TCFG_KEYS[k] !== undefined) tcfgBits |= TCFG_KEYS[k];
  }
  return {
    switches: () => [...switches],
    has: k => map.has(k) || switches.has(k),
    raw: k => map.get(k),
    flags, tcfgBits,
    num(k, { hex = false, def, required = false } = {}){
      if (!map.has(k)){
        if (required) throw new Error(`少了 ${k}=`);
        if (switches.has(k)) throw new Error(`${k} 要写成 ${k}=值`);
        return def;
      }
      const r = parseNum(map.get(k), hex);
      if (!r.ok) throw new Error(`${k}= 有问题：${r.why}`);
      return r.v;
    },
    bytes(k, { fallback } = {}){
      if (!map.has(k)) return fallback ?? new Uint8Array(0);
      try { return parseHexBytesLoose(map.get(k)); }
      catch (e){ throw new Error(`${k}= 有问题：${e.message}`); }
    },
    /** 收尾：把没被认领的 key / 裸开关都报出来 */
    done(known){
      const ok = new Set(known);
      for (const k of map.keys()) if (!ok.has(k)) throw new Error(`不认识的 key "${k}"（可用：${known.join(' ')}）`);
      for (const k of switches) if (!ok.has(k)) throw new Error(`不认识的 key "${k}"（开关要单独写：${known.filter(x => FLAG_KEYS[x] || TCFG_KEYS[x]).join(' ')}）`);
    },
  };
}

function buildXfer(toks, text0, maxTx, warns, presetCmd = null){
  // 行尾修饰（as / every / tag）先摘走，剩下的才是帧参数 —— 见 extractModifiers 的注释
  const mods = extractModifiers(toks);
  const kv = readKV(mods.core);
  const known = ['cmd', 'tx', 'rx', 'addr', 'addrl', 'addrlen', 'alen', 'dummy', 'lines',
    'rsp', 'cs_hold', 'cshold', 'cs_off', 'csoff', 'cs_aux', 'csaux', 'poll', 'nodma',
    'dma', 'force_dma', 'forcedma', 'addrquad', 'addr_quad', 'token', 'dc', 'dcen', 'dc1',
    'dcdata', 'dchigh', 'dc0', 'dccmd'];
  kv.done(known);
  if (presetCmd !== null && kv.has('cmd')) throw new Error('行首已经是命令字节了，不用再写 cmd=');

  const hasCmd = presetCmd !== null || kv.has('cmd');
  const cmd = presetCmd !== null ? presetCmd : kv.num('cmd', { hex: true, def: 0 });
  const tx = kv.bytes('tx');
  const rx = kv.num('rx', { def: 0 });
  const addrlKey = kv.has('addrl') ? 'addrl' : kv.has('addrlen') ? 'addrlen' : kv.has('alen') ? 'alen' : null;
  let addrLen = addrlKey ? kv.num(addrlKey, { def: 0 }) : (kv.has('addr') ? 3 : 0);
  const addr = kv.num('addr', { def: 0 });
  const dummy = kv.num('dummy', { def: 0 });
  const lines = kv.num('lines', { def: 1 });

  if (tx.length > maxTx) throw new Error(`tx 有 ${tx.length} B，超过单帧上限 ${maxTx} B（拆成多条，或让固件侧改 max_frame）`);
  if (rx < 0 || rx > 504) throw new Error(`rx 范围 0..504（收到 ${rx}）`);
  if (tx.length && rx && tx.length !== rx) throw new Error(`全双工要求收发等长（tx=${tx.length} rx=${rx}），不然拆成两条帧`);
  if (addrLen < 0 || addrLen > 4) throw new Error(`addrl 范围 0..4（收到 ${addrLen}）`);
  if (addrLen === 0 && kv.has('addr')) throw new Error('给了 addr 但 addrl=0：把 addrl 写成 3（或 4）');
  if (dummy < 0 || dummy > 4) throw new Error(`dummy 范围 0..4（收到 ${dummy}）`);
  if (![1, 2, 4].includes(lines)) throw new Error(`lines 只吃 1 / 2 / 4（收到 ${lines}）`);
  if (!hasCmd && !tx.length && !rx && addrLen === 0) throw new Error('这条帧什么都没干（cmd / tx / rx / addr 至少要有一个）');
  if (lines > 1 && !tx.length && !rx) warns.push(`lines=${lines} 但这条帧没有数据相位（线数只在数据相位生效）`);

  let tcfg = linesToTcfg(lines) | kv.tcfgBits;
  if (hasCmd) tcfg |= TC.CMD_EN;
  if (addrLen > 0) tcfg |= TC.ADDR_EN;
  let flags = kv.flags;
  if (rx > 0) flags |= F.RSP;      // 规则 2

  const body = [];
  if (hasCmd) body.push(`cmd=0x${cmd.toString(16).padStart(2, '0')}`);
  else if (tx.length || rx) body.push('cmd_en=0');
  if (addrLen) body.push(`addr=0x${(addr >>> 0).toString(16)}/${addrLen}B`);
  if (dummy) body.push(`dummy=${dummy}`);
  if (lines !== 1) body.push(`${lines}线`);
  if (tx.length) body.push(`tx=${tx.length}`);
  if (rx) body.push(`rx=${rx}`);
  const it = mk(T.XFER, xferPayload({ cmd, tcfg, addrLen, dummy, addr, tx, rxLen: rx }), flags,
    `XFER ${body.join(' ')}`, 'xfer');
  return applyModifiers(it, mods);
}

/**
 * 解析结果的一句话摘要（日志/状态栏用）。
 * @param {{items:Array,errors:Array,stats:object}} r
 */
export function describeParsed(r){
  if (!r.items.length) return '没有可发的帧';
  const k = r.stats.kinds;
  const kindText = k.map(x => ({ xfer: 'XFER', step: 'STEP', delay: 'DELAY', gpio: 'GPIO', cs: 'CS', reset: 'RESET', ping: 'PING', auxin: 'AUX_IN' }[x] || x)).join('/');
  return `${r.items.length} 条帧（${kindText}）· payload 合计 ${r.stats.bytes} B`;
}

/** 面板上「语法速查」折叠块的内容 */
export const DSL_HELP = `一行一条帧；# 或 // 到行尾是注释，空行忽略。

裸字节        0x11                          → 一条 cmd 帧（最常用）
通用帧        xfer cmd=0x9F rx=3             （xfer 可省略，直接 cmd=… 开写）
              xfer cmd=0x03 addr=0x1000 addrl=3 rx=492 cs_hold
C 表行        {0x9F, 1, 0, 0x000000, 0, 3, NULL},
              {0x02, 1, 3, 0x001000, 0, 0, (uint8_t[]){0xAA, 0xBB}},
              （列序与上面那张命令表一致：cmd, lines, addr_len, addr, dummy, rx_len, tx）
面板初始化步  step cmd=0x11 delay=120        （tx= 是参数）
延时          delay 120ms / 500us / 1.5s     （裸数字 = µs）
辅助脚        gpio DC 1 · gpio RST 0         （按「有效电平」自动取反，与固件一致）
片选          cs low（占用/拉低） · cs high（释放）
复位脉冲      reset 10 120                   （拉低 10 ms + 等 120 ms）
保活/读脚     ping · auxin

XFER 的 key：cmd= tx= rx= addr= addrl= dummy= lines=
开关（不带值）：rsp cs_hold cs_off cs_aux poll dma addrquad token dc dc1 dc0

定时采集（与「USB→I2C」页的脚本区**同一套语义**，学一次两边都会用）：
  loop 20ms                      块开始：里面每行都按 20 ms 周期跑
  loop 20ms 100 … end            带次数：跑 100 轮自己停（省略 = 一直跑到点「停止」）
  every 100ms                    不成块也行：把**后面所有行**设成 100 ms 周期
  once                           取消周期，回到一次性
  行尾 every 20ms                只给这一行定周期（覆盖块级）
  行尾 as ax=u16be(0)/16384, …   把读回的字节变成有名字的量（实时值面板画出曲线）
  行尾 tag 名字                 给这条帧一个短名字（日志里好认）

例（SPI 加速度计，每 20 ms 读 6 B 解成三轴 g 值）：
  xfer cmd=0x80|0x75 rx=1 as who=u8(0)        # 一次性：先读 WHO_AM_I 认片子
  loop 20ms
    xfer cmd=0x80|0x3B rx=6 as ax=i16be(0)/16384, ay=i16be(2)/16384, az=i16be(4)/16384
  end

as 表达式的偏移是**这一组读帧拼起来的整块**上的偏移（与 I2C 侧一致）；可用函数见
「USB→I2C」页的语法速查（u8/i8/u16be/u16le/i16be/i32be/f32be… + 四则与位运算）。

三条自动规则：
  1. 写了 cmd= 自动加 cmd 相位；写了 addr= / addrl>0 自动加地址相位
  2. rx>0 的帧自动带 RSP（固件规定读数据必须带）
  3. 整段一条 RSP 都没有时，末帧自动补一个

数字：0x… 十六进制，否则十进制；**cmd= 与行首字节按十六进制**。
数据：tx=00,01,02 或 tx="00 01 02"（带空格要引号）。
上限：单帧 tx ≤ 492 B、rx ≤ 504 B，超了请拆成多条。

导出：C 表 / JSON / 归一化文本 —— 导出的 C 表就是上面那种行，能直接贴回来。`;
/** 面板上「示例」按钮用的现成片段 */
export const DSL_SAMPLES = [
  {
    name: 'NOR：读 ID + SFDP + 状态',
    text: `# 外接 SPI NOR 的最小连通性检查（1 线，CS = J3[26]/PB10，SCLK = J3[13]/PB11）
0x9F rx=3                 # JEDEC ID：厂商 / 类型 / 容量
0x5A addr=0 addrl=3 dummy=1 rx=8   # SFDP 头（签名应为 "SFDP"）
0x05 rx=1                 # 状态寄存器 1（bit0 = BUSY，bit1 = WEL）`,
  },
  {
    name: 'NOR：4 线连续读测速（首帧带命令地址，后续帧只读数据）',
    text: `# 0x6B = Quad Output Fast Read；首帧发 cmd+地址，之后靠 CS_HOLD 续读
xfer cmd=0x6B addr=0 addrl=3 dummy=1 lines=4 rx=492 cs_hold
xfer lines=4 rx=492 cs_hold   # 第二帧起不发 cmd/地址，接着上一帧的地址往下读
xfer lines=4 rx=492 cs_off    # 末帧释放 CS`,
  },
  {
    name: '面板：几条命令 + 延时',
    text: `step cmd=0x11 delay=120
step cmd=0x36 tx=00
step cmd=0x3A tx=55
step cmd=0x29
delay 20ms`,
  },
  {
    name: '传感器：WHO_AM_I + 每 20ms 采三轴（loop + as）',
    text: `# 以 MPU-9250 风格（读 = 0x80|reg、无 dummy）为例：假探针切到「寄存器器件」就能跑
xfer cmd=0x80|0x75 rx=1 as who=u8(0)          # 一次性：WHO_AM_I（0x71 / 0x68）
loop 20ms
  xfer cmd=0x80|0x3B rx=6 as ax=i16be(0)/16384, ay=i16be(2)/16384, az=i16be(4)/16384
end`,
  },
  {
    name: 'ADC：MCP3008 每 50ms 读 0 通道（命令型）',
    text: `# 假探针切到「命令型 ADC」；真器件：VDD/GND + CH0 接被测电压
# 命令型器件没有"命令相位"，整条命令都放在 tx 里，且**收发必须等长**（各 3 B）
loop 50ms
  xfer tx=01,80,00 rx=3 as v=u16be(1)&0x3FF
end`,
  },
];
