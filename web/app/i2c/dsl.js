/**
 * 「USB→I2C」页的**命令协议**（一行一条命令 / 一行一行 C 表）。纯函数，不碰 DOM，Node 自测直接打。
 *
 * ── 为什么要自己定一套 ──────────────────────────────────────────────────
 * 桥的 HID 0x36 一次只做**一笔事务**，页面要的是"贴一整段下去跑"：器件初始化序列、
 * EEPROM 页写 + 等 tWR + 回读对账、传感器 while(1) 采样。语法刻意长得像协议本身
 * （`rd` / `wr` / `dev=` / `addr=` …），屏幕上的字和线上字段一一对应，不用记第二套词汇。
 *
 * ── 语法 ──────────────────────────────────────────────────────────────
 *   # 注释（也支持 //）到行尾；空行忽略
 *
 *   scan                                 扫 0x08..0x77，出 ACK 地址列表（14 B 位图）
 *   ping 0x50                            地址探测：只问 ACK（wr=rd=0 的那种子情况）
 *
 *   rd  <dev> <子地址> <读长>             写子地址 + repeated START 读（读寄存器最常用）
 *   rd  0x50 - 8                         子地址写 `-` = 不带子地址的**纯读**
 *   rd  0x50 0x00 256                    读长随便写（≤4096）—— **超过 54 B 自动分片**，
 *                                        日志只出一行（分片是实现细节，不该让人每次自己拆）
 *   rd  0x50 0x00 256 ptr                长读改走「地址指针自增」连续读（FIFO 型器件必须用它）
 *   wr  <dev> <子地址> <b0> <b1> …        写子地址 + 数据（单次 ≤51 B，**写不自动分片**）
 *   wr  <dev> <子地址>                    只发子地址、不带数据 = 把地址指针推到 N
 *   wr  0x50 - 11 22 33                  不带子地址的纯写（命令型器件）
 *   wrr 等价于 rd（写子地址再读），留着只为读起来顺
 *
 *   xfer dev=0x50 addr=0x00,0x10 wr=11,22 rd=0     字段名写法（key=value，与线上字段同名）
 *   xfer dev=0x50 rd=8                             所有 key 都可省（默认 dev=0/addr 空/wr 空/rd=0）
 *
 *   delay 10ms                           延时（裸数字 = µs；吃 us/µs/ms/s）
 *
 *   ── 定时读 / 定时写（那就是 while(1)）──
 *   loop 100ms                           循环块开始：里面每一行都按 100ms 周期跑
 *     rd 0x68 0x3B 14 as ax=i16be(0)/16384
 *   end                                  循环块结束
 *   loop 500ms 20 …end                   带次数：跑 20 轮就自己停
 *   every 100ms                          不给块也行：把**后面所有行**设成 100ms 周期
 *   every 100ms 50                       周期 + 最多 50 次
 *   once | every 0                       取消周期，回到一次性
 *   行尾覆盖：`rd 0x68 0x75 1 every 500ms`（这一行单独定时）
 *
 *   ── 解码（把读回的字节变成有名字的数）──
 *   行尾 `as 名字=表达式, 名字=表达式`（见 expr.js；`as hex` = 只显示原始字节）
 *
 *   ── C 表行（列序 = 线上 XFER 字段；从固件源码里直接贴过来）──
 *   {dev, addr_len, addr, wr_len, rd_len, data},
 *   {0x50, 1, 0x0000, 0, 8, NULL},
 *   {0x50, 1, 0x0010, 4, 0, (uint8_t[]){0x11, 0x22, 0x33, 0x44}},
 *   {0x00, 0, 0, 3, 0, "ABC"},              字符串字面量也吃（每字符一字节）
 *   {0x68, 1, 0x3B, 0, 14, NULL}, as ax=i16be(0)/16384 every 50ms
 *
 * ── 三个自动规则 ───────────────────────────────────────────────────────
 *   1. `rd`/`wrr` 一定带子地址相位（写了 `-` 才不带）；`wr` 同理。
 *   2. 子地址写成数字时按**大端**铺字节（`addr=0x0010`、`addr_len=2` → 线上发 `00 10`）——
 *      与 proto.h 的"addr[0] 先发"一致，也和 Python 工具 `zfill(2*addrbytes)` 一致。
 *   3. 设备地址**默认按十六进制**（I2C 惯例）：`rd 50 0 8` 里的 `50` 就是 0x50；
 *      读长/延时/周期是十进制。`0x` 前缀在任何地方都是十六进制。
 *
 * ── 校验（错在解析期就拦住，别让固件回一个 E_RANGE 让你猜）────────────────
 *   写 ≤51 B、读 ≤54 B、子地址 ≤4 B、器件地址 7 位、C 表列数/长度自洽、`as` 表达式可解析。
 *   报错带**行号 + 原行**。
 */

import { WR_MAX, RD_MAX, RD_TOTAL_MAX, ADDR_MAX, hexBytes, addr7, cBytes, hex2, planRead } from './protocol.js';
import { parseAs } from './expr.js';

export const KIND = { XFER: 'xfer', SCAN: 'scan', DELAY: 'delay' };

/** 行尾修饰关键字（出现在这些词后面的都不再算位置参数）*/
const MODIFIERS = new Set(['as', 'every', 'tag', 'note', 'ptr', 'chain', 'chunk']);
/** `chunk=ptr` / `every=100ms` 这类 `key=value` 也要认出来是修饰字（取 `=` 前面那段）*/
const modKey = t => {
  const s = String(t ?? '').toLowerCase();
  const eq = s.indexOf('=');
  return eq < 0 ? s : s.slice(0, eq);
};
const isModifierToken = t => MODIFIERS.has(modKey(t));

// ============================================================================
// 小工具（与 SPI 侧同款写法；默认进制按 I2C 的习惯重定）
// ============================================================================

/** 去掉行尾注释（引号里的 # / // 不算）*/
export function stripComment(line){
  let q = null;
  for (let i = 0; i < line.length; i++){
    const c = line[i];
    if (q){ if (c === q) q = null; continue; }
    if (c === '"' || c === "'"){ q = c; continue; }
    if (c === '#') return line.slice(0, i);
    if (c === '/' && line[i + 1] === '/') return line.slice(0, i);
  }
  return line;
}

/** 空白分词；`"…"` / `'…'` 里的空格保留 */
export function tokenize(s){
  const out = [];
  let i = 0;
  while (i < s.length){
    while (i < s.length && /\s/.test(s[i])) i++;
    if (i >= s.length) break;
    let tok = '';
    while (i < s.length && !/\s/.test(s[i])){
      const c = s[i];
      if (c === '"' || c === "'"){
        i++;
        while (i < s.length && s[i] !== c) tok += s[i++];
        if (s[i] !== c) throw new Error('引号没有配对');
        i++;
      } else tok += s[i++];
    }
    out.push(tok);
  }
  return out;
}

/**
 * 数字：`0x…` 一律十六进制；否则看 `defHex`。
 * I2C 里**器件地址与子地址默认十六进制**（`50` = 0x50），长度/时长默认十进制。
 * @returns {{ok:boolean, v?:number, why?:string}}
 */
export function parseNum(tok, defHex = false){
  const t = String(tok ?? '').trim();
  if (t === '') return { ok: false, why: '空值' };
  const m = /^(0x|0X)?([0-9a-fA-F]+)$/.exec(t);
  if (!m) return { ok: false, why: `"${t}" 不是数字` };
  const hex = !!m[1] || defHex;
  if (!hex && !/^[0-9]+$/.test(t)) return { ok: false, why: `"${t}" 不是十进制数字（十六进制请写 0x…）` };
  const v = parseInt(m[2], hex ? 16 : 10);
  if (!Number.isFinite(v)) return { ok: false, why: `"${t}" 解析失败` };
  return { ok: true, v };
}

/** 十六进制字节串：`11,22,33` / `0x11 0x22`（带空格要引号）/ `112233` */
export function parseHexBytesLoose(tok){
  const t = String(tok ?? '').replace(/0x/gi, '').replace(/[\s,;:_-]/g, '');
  if (t === '') return [];
  if (!/^[0-9a-fA-F]+$/.test(t)) throw new Error('十六进制里有非法字符（只吃 0-9a-f，分隔符 , ; : _ - 空格）');
  if (t.length % 2) throw new Error('十六进制要成字节（每字节两位）');
  const out = [];
  for (let i = 0; i < t.length; i += 2) out.push(parseInt(t.substr(i, 2), 16));
  return out;
}
const isHexByteList = t => /^(0x)?[0-9a-fA-F]{1,2}$/i.test(t);

/** 时长：`10ms` / `500us` / `1.5s` / 裸数字（µs） */
export function parseDuration(tok){
  const t = String(tok ?? '').trim();
  const m = /^([0-9]*\.?[0-9]+)\s*(us|µs|ms|s)?$/i.exec(t);
  if (!m) return { ok: false, why: `"${t}" 不是时长（例：10ms / 500us / 1.5s / 1000）` };
  const n = parseFloat(m[1]);
  const unit = (m[2] || 'us').toLowerCase();
  const us = unit === 's' ? n * 1e6 : unit === 'ms' ? n * 1e3 : n;
  return { ok: true, us: Math.round(us), ms: us / 1000 };
}

/** 数字 → 按大端铺成 `len` 个字节（子地址/地址用；与 proto.h 的"addr[0] 先发"一致）*/
export function numToBytes(v, len){
  const out = [];
  for (let i = len - 1; i >= 0; i--) out.push((v >>> (8 * i)) & 0xff);
  return out;
}

/** 子地址字段：`0x00` / `00 10` / `0x0010` / `-`（无子地址） */
function parseSubAddr(tok){
  const t = String(tok ?? '').trim();
  if (t === '' || t === '-' || t === '_' || t.toLowerCase() === 'none') return { ok: true, bytes: [] };
  if (t.includes(',') || /^[0-9a-fA-F]{3,}$/.test(t) && !/^0x/i.test(t)){
    // `00,10` / `0010` 这种当成字节串读更自然
    try { return { ok: true, bytes: parseHexBytesLoose(t) }; } catch (e){ return { ok: false, why: e.message }; }
  }
  const n = parseNum(t, true);
  if (!n.ok) return { ok: false, why: n.why };
  if (n.v > 0xffffffff) return { ok: false, why: `子地址 ${t} 超过 32 位` };
  // 铺几位？**按写出来的十六进制位数**（这是用户最直白的意图）：
  //   `0x00` → 2 位 → 1 B；`0x0010` → 4 位 → 2 B（发 00 10）；`3B` → 1 B；`100` → 2 B
  const digits = /^0x/i.test(t) ? t.slice(2).length : t.length;
  const len = Math.max(1, Math.ceil(Math.max(digits, 1) / 2));
  return { ok: true, bytes: numToBytes(n.v, Math.min(len, ADDR_MAX)) };
}

// ============================================================================
// 解析
// ============================================================================

/**
 * 解析整段脚本。
 * @param {string} text
 * @returns {{items:Array<object>, errors:Array<{line:number,text:string,msg:string}>,
 *            warns:Array<{line:number,text:string,msg:string}>, stats:object}}
 */
export function parseScript(text){
  const src = String(text ?? '').split(/\r?\n/);
  const items = [], errors = [], warns = [];
  /**
   * 解析期的"当前状态"（loop/every 会改它）。
   * `group` 是**定时任务的边界**：同一个 group 里的命令会被当作一段不可拆的序列整体定时执行
   * （理由见 runner.js 顶部：ADS1115 的"启动转换 → 等 10ms → 读结果"拆开就错）。
   */
  const st = { period: 0, count: 0, group: 0, loops: [] };
  let gid = 0;
  const newGroup = () => ++gid;

  const err = (line, raw, msg) => errors.push({ line, text: raw.trim(), msg });
  const warn = (line, raw, msg) => warns.push({ line, text: raw.trim(), msg });

  for (let i = 0; i < src.length; i++){
    const lineNo = i + 1;
    const raw = src[i];
    let body = stripComment(raw).trim();
    if (!body) continue;

    // ---- C 表行：以 { 开头 ----
    if (body.startsWith('{')){
      const r = parseCRow(body);
      if (!r.ok){ err(lineNo, body, r.why); continue; }
      if (r.item){
        finishLine(r.item, r.rest, lineNo, body, st, err, warn);
        items.push(r.item);
      }
      continue;
    }

    let toks;
    try { toks = tokenize(body); }
    catch (e){ err(lineNo, body, e.message); continue; }
    if (!toks.length) continue;
    const head = toks[0].toLowerCase();

    // ---- 块 / 周期状态 ----
    if (head === 'loop' || head === 'repeat'){
      if (toks.length < 2){ err(lineNo, body, 'loop 后面要跟周期，例如 `loop 100ms`'); continue; }
      const d = parseDuration(toks[1]);
      if (!d.ok){ err(lineNo, body, d.why); continue; }
      let count = 0;
      if (toks[2] != null){
        const n = parseNum(toks[2], false);
        if (!n.ok || n.v < 0){ err(lineNo, body, `次数 "${toks[2]}" 不合法（0 或省略 = 无限）`); continue; }
        count = n.v;
      }
      st.loops.push({ period: st.period, count: st.count, group: st.group });
      st.period = d.ms; st.count = count; st.group = newGroup();
      continue;
    }
    if (head === 'end' || head === 'endloop'){
      const prev = st.loops.pop();
      if (!prev) { err(lineNo, body, '这里没有对应的 loop（多了一个 end）'); continue; }
      st.period = prev.period; st.count = prev.count; st.group = prev.group;
      continue;
    }
    if (head === 'every' || head === 'once'){
      if (head === 'once' || toks[1] === '0' || toks[1] === 'off'){
        st.period = 0; st.count = 0; st.group = 0;
        continue;
      }
      const d = parseDuration(toks[1]);
      if (!d.ok){ err(lineNo, body, d.why); continue; }
      let count = 0;
      if (toks[2] != null){
        const n = parseNum(toks[2], false);
        if (!n.ok || n.v < 0){ err(lineNo, body, `次数 "${toks[2]}" 不合法（0 或省略 = 无限）`); continue; }
        count = n.v;
      }
      // 换周期 = 换任务（后面这些行是另一段循环体）
      st.period = d.ms; st.count = count; st.group = newGroup();
      continue;
    }

    // ---- 单行命令 ----
    let item = null, rest = [];
    const isMod = isModifierToken;
    const cutAt = (from) => { const k = toks.findIndex((t, idx) => idx >= from && isMod(t)); return k < 0 ? toks.length : k; };
    if (head === 'scan'){
      item = { kind: KIND.SCAN, dev: null, addr: [], wr: [], rd: 0, label: '扫描 0x08..0x77' };
      rest = toks.slice(1);
      if (st.period) warn(lineNo, body, '把「扫描」放进定时循环会把总线占得很满，而且 DAP/CDC 会跟着顿 —— 建议扫描只做一次');
    } else if (head === 'ping'){
      const dev = parseNum(toks[1], true);
      if (!dev.ok){ err(lineNo, body, `ping 后面要跟 7 位从机地址：${dev.why}`); continue; }
      item = { kind: KIND.XFER, dev: dev.v & 0x7f, addr: [], wr: [], rd: 0, label: `探测 ${addr7(dev.v)}` };
      rest = toks.slice(2);
      if (dev.v > 0x7f) warn(lineNo, body, `地址 ${toks[1]} 超过 7 位，已按 0x${(dev.v & 0x7f).toString(16)} 处理`);
    } else if (head === 'delay' || head === 'wait' || head === 'sleep'){
      const d = parseDuration(toks[1]);
      if (!d.ok){ err(lineNo, body, `delay 后面要跟时长：${d.why}`); continue; }
      // 延时项也带上 addr/wr/rd（恒空）—— 让下游不用为每种 kind 特判字段存在性
      item = { kind: KIND.DELAY, us: d.us, ms: d.ms, addr: [], wr: [], rd: 0, dev: null, label: `延时 ${d.ms} ms` };
      rest = toks.slice(2);
    } else if (head === 'rd' || head === 'read' || head === 'wrr' || head === 'wr' || head === 'write'){
      const cut = cutAt(2);
      const r = parseRdWr(head, toks.slice(0, cut), toks.slice(cut));
      if (!r.ok){ err(lineNo, body, r.why); continue; }
      item = r.item; rest = r.rest;
    } else if (head === 'xfer'){
      const cut = cutAt(1);
      const r = parseXferKV(toks.slice(0, cut), toks.slice(cut));
      if (!r.ok){ err(lineNo, body, r.why); continue; }
      item = r.item; rest = r.rest;
    } else {
      // 裸字节也算一条写（`00 01 02` —— 少见但和 SPI 侧一致，方便手快）
      if (toks.every(isHexByteList)){
        err(lineNo, body, '直接写字节的那条缺 `dev=`（不知道发给哪个器件）—— 写成 `wr 0x50 0x00 00 01 02`');
        continue;
      }
      err(lineNo, body, `看不懂这一行（开头是 "${toks[0]}"）。可用：scan / ping / rd / wr / wrr / xfer / delay / loop / every / end / C 表行`);
      continue;
    }
    finishLine(item, rest, lineNo, body, st, err, warn);
    items.push(item);
  }

  if (st.loops.length) errors.push({ line: src.length, text: '', msg: `有 ${st.loops.length} 个 loop 没有对应的 end` });

  const oneShots = items.filter(x => !x.period).length;
  const timed = items.filter(x => x.period).length;
  const groups = new Set(items.filter(x => x.period && x.group).map(x => x.group)).size +
                 items.filter(x => x.period && !x.group).length;
  return {
    items, errors, warns,
    stats: { total: items.length, oneShots, timed, tasks: groups, lines: src.length, loopDepth: st.loops.length },
  };
}

/** 收尾：把行尾修饰（as / every / tag）挂到 item 上，并算出默认周期 */
function finishLine(item, rest, lineNo, raw, st, err, warn){
  item.line = lineNo;
  item.text = raw;
  item.as = null;
  item.asText = '';
  item.tag = item.note || '';
  item.period = st.period;
  item.count = st.count;
  item.group = st.group;
  if (item.kind === KIND.XFER && item.chunk == null) item.chunk = 'reset';
  const basePeriod = st.period;

  // 从 rest 里剥修饰：as 的值可能被空格拆成多个 token，拼回去
  const toks = rest.slice();
  for (let i = 0; i < toks.length; i++){
    const raw = toks[i];
    const k = modKey(raw);
    // `ptr` / `chain` / `chunk=ptr`：长读改走"地址指针自增"（FIFO 型器件必须用它）
    if (k === 'ptr' || k === 'chain'){
      item.chunk = 'ptr';
      continue;
    }
    if (k === 'chunk'){
      const v = raw.includes('=') ? raw.split('=').slice(1).join('=') : toks[i + 1];
      if (!/^(ptr|chain|reset)$/i.test(String(v || ''))){ err(lineNo, raw, `chunk 只能是 ptr 或 reset（收到 "${v}"）`); return; }
      item.chunk = /^reset$/i.test(v) ? 'reset' : 'ptr';
      if (!raw.includes('=')) i++;
      continue;
    }
    if (k === 'as'){
      const val = [];
      let j = i + 1;
      for (; j < toks.length; j++){
        const kk = toks[j].toLowerCase().replace(/=$/, '');
        if (MODIFIERS.has(kk) && kk !== 'as') break;
        val.push(toks[j]);
      }
      if (!val.length){ err(lineNo, raw, '`as` 后面没东西（要么删掉它，要么写 `as 名字=表达式`）'); return; }
      const spec = parseAs(val.join(''));
      if (!spec.ok){ err(lineNo, raw, `as 子句：${spec.why}`); return; }
      item.as = spec; item.asText = val.join('');
      i = j - 1;
      continue;
    }
    if (k === 'every'){
      if (toks[i].includes('=')){
        const v = toks[i].split('=').slice(1).join('=');
        const d = parseDuration(v);
        if (!d.ok){ err(lineNo, raw, `every=${v}：${d.why}`); return; }
        item.period = d.ms;
        continue;
      }
      const d = parseDuration(toks[i + 1]);
      if (!d.ok){ err(lineNo, raw, `every 后面要跟周期：${d.why}`); return; }
      item.period = d.ms;
      let j = i + 2;
      if (toks[j] != null && !/^(as|tag|note)/i.test(toks[j])){
        const n = parseNum(toks[j], false);
        if (n.ok){ item.count = n.v; j++; }
      }
      i = j - 1;
      continue;
    }
    if (k === 'tag' || k === 'note'){
      const val = [];
      let j = i + 1;
      for (; j < toks.length; j++){
        const kk = toks[j].toLowerCase().replace(/=$/, '');
        if (MODIFIERS.has(kk) && kk !== 'tag' && kk !== 'note') break;
        val.push(toks[j]);
      }
      item.tag = val.join(' ');
      i = j - 1;
      continue;
    }
    if (/^every=/i.test(toks[i])) continue;
    // 剩下的位置参数：rd/wr 已经吃过，多出来的多半是打错的修饰字 —— 明说，别静默吞掉
    warn(lineNo, raw, `多出来的参数 "${toks[i]}" 被忽略了（修饰字只有 as / every / tag）`);
  }

  // 行尾 `every` 把这一行单独定了周期 → 它自己成一个任务（不跟着所在块走）
  if (item.period && item.period !== basePeriod) item.group = 0;

  // 探测行（wr=rd=0）在定时循环里没意义，提醒一下
  if (item.kind === KIND.XFER && item.period && !item.addr.length && !item.wr.length && !item.rd){
    warn(lineNo, raw, '这是一条「只探测地址」的事务，放进定时循环里去扫地址意义不大');
  }
  if (!item.label){
    item.label = item.kind === KIND.SCAN ? '扫描 0x08..0x77'
      : item.kind === KIND.DELAY ? `延时 ${item.ms} ms`
      : describeItem(item);
  }
}

/** `rd <dev> <子地址> <读长>` / `wr <dev> <子地址> <数据…>`（`toks` 已切到修饰字之前）*/
function parseRdWr(head, toks, rest){
  const isRead = head === 'rd' || head === 'read' || head === 'wrr';
  if (toks.length < 2) return { ok: false, why: `${head} 后面要跟器件地址，例如 \`${head} 0x50 0x00 ${isRead ? '8' : '11 22'}\`` };
  const dv = parseNum(toks[1], true);
  if (!dv.ok) return { ok: false, why: `器件地址：${dv.why}` };
  if (dv.v > 0x7f) return { ok: false, why: `器件地址 ${toks[1]} 超过 7 位（0x00..0x7F）` };
  const sa = parseSubAddr(toks[2]);
  if (!sa.ok) return { ok: false, why: `子地址：${sa.why}` };
  if (sa.bytes.length > ADDR_MAX) return { ok: false, why: `子地址 ${sa.bytes.length} B 超过 ${ADDR_MAX} B` };
  const item = { kind: KIND.XFER, dev: dv.v, addr: sa.bytes, wr: [], rd: 0, chunk: 'reset', label: '' };
  if (isRead){
    if (toks[3] == null) return { ok: false, why: '读命令缺长度，例如 `rd 0x50 0x00 8`' };
    const n = parseNum(toks[3], false);
    if (!n.ok) return { ok: false, why: `读长：${n.why}` };
    if (n.v <= 0) return { ok: false, why: '读长必须 > 0（只想探测地址就写 `ping 0x50`）' };
    if (n.v > RD_TOTAL_MAX){
      return { ok: false, why: `读长 ${n.v} 超过一次逻辑读的上限 ${RD_TOTAL_MAX} B` +
        `（超过 ${RD_MAX} B 会**自动分片**，不用自己拆；但要更大的块请分几次读）` };
    }
    item.rd = n.v;
    if (toks.length > 4){
      return { ok: false, why: `读命令多了一个参数 "${toks[4]}"（读命令只有 dev / 子地址 / 读长三个位置参数）。` +
        `⚠️ 多字节子地址要写成**一个整体**：\`rd 0x50 0x0010 2\` 或 \`rd 0x50 00,10 2\` —— ` +
        `写成 \`00 10\` 会被当成两个参数` };
    }
  } else {
    // `wr <dev> <子地址>`（**没有数据**）= 只把子地址发出去，用来把器件的地址指针推到 N。
    // 线上 = `START + dev+W + 子地址 + STOP`，一个数据字节都不写 —— EEPROM 分片读就靠它。
    if (toks.length < 4) return { ok: true, item, rest };
    try { item.wr = parseHexBytesLoose(toks.slice(3).join(',')); }
    catch (e){ return { ok: false, why: `写数据：${e.message}` }; }
    if (!item.wr.length) return { ok: false, why: '写数据是空的' };
    if (item.wr.length > WR_MAX) return { ok: false, why: `写数据 ${item.wr.length} B 超过单次上限 ${WR_MAX} B（**写不自动分片**：EEPROM 页写跨页会绕回页首，自动拆是危险的 —— 请自己按页/按寄存器块拆）` };
  }
  return { ok: true, item, rest };
}

/** `xfer dev=0x50 addr=0x00,0x10 wr=11,22 rd=0`（`toks` 已切到修饰字之前）*/
function parseXferKV(toks, rest){
  const item = { kind: KIND.XFER, dev: 0, addr: [], wr: [], rd: 0, chunk: 'reset', label: '' };
  let addrLenHint = null;
  for (let i = 1; i < toks.length; i++){
    const t = toks[i];
    const eq = t.indexOf('=');
    if (eq < 0) return { ok: false, why: `"${t}" 不是 key=value（xfer 用字段名写法）` };
    const key = t.slice(0, eq).toLowerCase();
    const val = t.slice(eq + 1);
    switch (key){
      case 'dev': case 'addr7': case 'device': {
        const n = parseNum(val, true);
        if (!n.ok) return { ok: false, why: `dev=${val}：${n.why}` };
        if (n.v > 0x7f) return { ok: false, why: `dev=${val} 超过 7 位` };
        item.dev = n.v; break;
      }
      case 'addr': case 'sub': case 'reg': {
        const sa = parseSubAddr(val);
        if (!sa.ok) return { ok: false, why: `addr=${val}：${sa.why}` };
        item.addr = sa.bytes; break;
      }
      case 'wr': case 'tx': case 'data': {
        try { item.wr = parseHexBytesLoose(val); }
        catch (e){ return { ok: false, why: `wr=${val}：${e.message}` }; }
        break;
      }
      case 'rd': case 'rx': case 'len': {
        const n = parseNum(val, false);
        if (!n.ok) return { ok: false, why: `rd=${val}：${n.why}` };
        item.rd = n.v; break;
      }
      case 'chunk': {
        if (!/^(ptr|chain|reset)$/i.test(val)) return { ok: false, why: `chunk=${val} 只能是 ptr 或 reset` };
        item.chunk = /^reset$/i.test(val) ? 'reset' : 'ptr'; break;
      }
      case 'addrl': case 'addr_len': {
        const n = parseNum(val, false);
        if (!n.ok) return { ok: false, why: `addr_len=${val}：${n.why}` };
        if (n.v < 0 || n.v > ADDR_MAX) return { ok: false, why: `addr_len=${n.v} 越界（0..${ADDR_MAX}）` };
        addrLenHint = n.v; break;
      }
      case 'flags':
        if (parseNum(val, false).v !== 0) return { ok: false, why: 'flags 是保留位，必须 0' };
        break;
      default:
        return { ok: false, why: `xfer 不认识的字段 "${key}"（可用 dev / addr / addrl / wr / rd）` };
    }
  }
  if (addrLenHint != null && item.addr.length && addrLenHint !== item.addr.length){
    return { ok: false, why: `addr_len=${addrLenHint} 与 addr 的 ${item.addr.length} B 对不上` };
  }
  if (addrLenHint != null && !item.addr.length && addrLenHint > 0) item.addr = numToBytes(0, addrLenHint);
  if (item.wr.length > WR_MAX) return { ok: false, why: `写数据 ${item.wr.length} B 超过 ${WR_MAX} B（**写不自动分片**：EEPROM 页写跨页会绕回页首，自动拆是危险的）` };
  if (item.rd > RD_TOTAL_MAX) return { ok: false, why: `读长 ${item.rd} 超过一次逻辑读的上限 ${RD_TOTAL_MAX} B` };
  if (item.rd < 0) return { ok: false, why: '读长不能是负的' };
  return { ok: true, item, rest };
}

/**
 * 一行 C 表：`{dev, addr_len, addr, wr_len, rd_len, data},` —— 列序 = 线上 XFER 字段。
 * 右花括号后面还能跟行尾修饰（`, as ax=… every 50ms`）。
 */
export function parseCRow(text){
  let s = text.trim().replace(/,\s*$/, '');
  if (!s.startsWith('{')) return { ok: false, why: '不是 C 表行' };
  // 找到与第一个 { 配对的 }
  let depth = 0, end = -1;
  let q = null;
  for (let i = 0; i < s.length; i++){
    const c = s[i];
    if (q){ if (c === q) q = null; continue; }
    if (c === '"' || c === "'"){ q = c; continue; }
    if (c === '{') depth++;
    else if (c === '}'){ depth--; if (depth === 0){ end = i; break; } }
  }
  if (end < 0) return { ok: false, why: 'C 表行少了右花括号 `}`' };
  const inner = s.slice(1, end);
  const restRaw = s.slice(end + 1).replace(/^\s*,\s*/, '');
  const fields = splitTop(inner);
  if (fields.length < 5) return { ok: false, why: `C 表行至少要 5 列（dev, addr_len, addr, wr_len, rd_len），现在只有 ${fields.length} 列` };
  if (fields.length > 6) return { ok: false, why: `C 表行最多 6 列（第 6 列是数据），现在有 ${fields.length} 列` };

  const dev = parseNum(fields[0], true);
  if (!dev.ok) return { ok: false, why: `第 1 列 dev：${dev.why}` };
  if (dev.v > 0x7f) return { ok: false, why: `第 1 列 dev=0x${dev.v.toString(16)} 超过 7 位` };
  const alen = parseNum(fields[1], false);
  if (!alen.ok) return { ok: false, why: `第 2 列 addr_len：${alen.why}` };
  if (alen.v > ADDR_MAX) return { ok: false, why: `第 2 列 addr_len=${alen.v} 超过 ${ADDR_MAX}` };
  const addrN = parseNum(fields[2], true);
  if (!addrN.ok) return { ok: false, why: `第 3 列 addr：${addrN.why}` };
  const wlen = parseNum(fields[3], false);
  if (!wlen.ok) return { ok: false, why: `第 4 列 wr_len：${wlen.why}` };
  const rlen = parseNum(fields[4], false);
  if (!rlen.ok) return { ok: false, why: `第 5 列 rd_len：${rlen.why}` };
  if (wlen.v > WR_MAX) return { ok: false, why: `第 4 列 wr_len=${wlen.v} 超过 ${WR_MAX}` };
  if (rlen.v > RD_MAX) return { ok: false, why: `第 5 列 rd_len=${rlen.v} 超过 ${RD_MAX}` };

  const addr = alen.v > 0 ? numToBytes(addrN.v, alen.v) : [];
  let wr = [];
  const dataField = (fields[5] ?? '').trim();
  if (dataField && !/^(null|0|nil|none)$/i.test(dataField)){
    // 吃这几种写法：`(uint8_t[]){…}` / `(const uint8_t *){…}` / `{…}` / `[…]` / `"ABC"`
    const lit = /^(?:\(\s*(?:const\s+)?uint8_t\s*(?:\[\s*\]|\*)\s*\)\s*)?\{(.*)\}$/i.exec(dataField)
             || /^\[(.*)\]$/.exec(dataField);
    if (lit){
      try { wr = parseHexBytesLoose(lit[1]); }
      catch (e){ return { ok: false, why: `第 6 列数据：${e.message}` }; }
    } else if (/^["']/.test(dataField)){
      const str = dataField.replace(/^["']|["']$/g, '');
      wr = [...str].map(ch => ch.charCodeAt(0) & 0xff);
    } else {
      try { wr = parseHexBytesLoose(dataField); }
      catch (e){ return { ok: false, why: `第 6 列数据：${e.message}` }; }
    }
    if (wr.length !== wlen.v) return { ok: false, why: `第 6 列有 ${wr.length} B，但第 4 列 wr_len=${wlen.v} —— 对不上` };
  } else if (wlen.v > 0){
    return { ok: false, why: `第 4 列 wr_len=${wlen.v}，但第 6 列是 NULL（没有数据）` };
  }
  return {
    ok: true,
    item: { kind: KIND.XFER, dev: dev.v, addr, wr, rd: rlen.v, label: '' },
    rest: restRaw ? (() => { try { return tokenize(restRaw); } catch { return []; } })() : [],
  };
}

/** 按顶层逗号切分（括号/花括号/引号里的逗号不算）*/
function splitTop(s){
  const out = [];
  let depth = 0, cur = '', q = null;
  for (const c of s){
    if (q){ cur += c; if (c === q) q = null; continue; }
    if (c === '"' || c === "'"){ q = c; cur += c; continue; }
    if (c === '{' || c === '(' || c === '['){ depth++; cur += c; continue; }
    if (c === '}' || c === ')' || c === ']'){ depth--; cur += c; continue; }
    if (c === ',' && depth === 0){ out.push(cur.trim()); cur = ''; continue; }
    cur += c;
  }
  if (cur.trim() || out.length) out.push(cur.trim());
  return out;
}

/** 一条命令 → 一句话（日志/标签用）*/
export function describeItem(it){
  if (it.kind === KIND.SCAN) return '扫描 0x08..0x77';
  if (it.kind === KIND.DELAY) return `延时 ${it.ms} ms`;
  const a = it.addr.length ? `[${hexBytes(it.addr)}]` : '';
  const big = it.rd > RD_MAX ? `（自动分片 ${Math.ceil(it.rd / RD_MAX)} 笔）` : '';
  const mode = it.rd > RD_MAX && it.chunk === 'ptr' ? '·连续读' : '';
  if (!it.addr.length && !it.wr.length && !it.rd) return `探测 ${addr7(it.dev)}`;
  if (it.addr.length && !it.wr.length && !it.rd) return `设地址指针 ${addr7(it.dev)}${a}`;
  if (it.wr.length && !it.rd) return `写 ${addr7(it.dev)}${a} ← ${hexBytes(it.wr)}`;
  if (!it.wr.length && it.rd) return `读 ${addr7(it.dev)}${a} × ${it.rd}${big}${mode}`;
  return `写 ${addr7(it.dev)}${a} ← ${hexBytes(it.wr)} → 读 × ${it.rd}${big}${mode}`;
}

// ============================================================================
// 导出
// ============================================================================

const periodText = it => (!it.period ? '' : it.count ? `every ${it.period}ms ${it.count}` : `every ${it.period}ms`);
const asText = it => (it.as?.fields?.length ? `as ${it.asText}` : '');

/**
 * 导出成 **C 表**（列序 = 线上 XFER 字段）：能直接贴回固件源码，也能贴回本页。
 *
 * ⚠️ **长读会被展开成多行**：C 表的 `rd_len` 是**线上字段**（一个字节，最大 54），
 *    塞不下 `rd 0x50 0x00 256` 这种逻辑读 —— 所以按 `planRead()` 展开成 N 行，
 *    展开后的字节流与页面上实际发出的**逐笔一致**。
 *    代价：`as` 解码表达式没法逐行表达（它是作用在**拼起来之后**的整块上的），
 *    所以被展开的那些行会丢掉 `as` —— 要无损往返请用「导出 JSON」（那个带 chunk 与 as）。
 */
export function toCTable(items, { header = true } = {}){
  const L = [];
  if (header){
    L.push('// USB→I2C 页导出：列序 = 线上 XFER 字段 (dev, addr_len, addr, wr_len, rd_len, data)');
    L.push('// addr 按大端铺成 addr_len 个字节（addr[0] 先发）；NULL = 没有数据');
  }
  let expanded = 0;
  for (const it of items){
    if (it.kind === KIND.SCAN){ L.push('scan'); continue; }
    if (it.kind === KIND.DELAY){ L.push(`delay ${it.ms}ms`); continue; }
    const tail = [asText(it), periodText(it)].filter(Boolean).join(' ');
    // 长读：展开成线上真实发出的每一笔
    if (it.rd > RD_MAX){
      const plan = planRead(it.addr, it.rd, { mode: it.chunk === 'ptr' ? 'ptr' : 'reset' });
      L.push(`// ↓ 一条 \`${describeItem({ ...it, period: 0, count: 0 })}\` 展开成 ${plan.cmds.length} 笔（C 表的 rd_len 是线上字段，最大 ${RD_MAX}）`);
      for (const c of plan.cmds) L.push(cRow(it.dev, c.addr, [], c.rd));
      // 周期属于**整条逻辑读**，展开后挂到最后一笔上才等价
      if (tail) L.push(`// 上面这组按 ${tail} 循环（周期属于整条读，不属于某一片）`);
      expanded++;
      continue;
    }
    L.push(cRow(it.dev, it.addr, it.wr, it.rd) + (tail ? ' ' + tail : ''));
  }
  if (expanded) L.push(`// （共 ${expanded} 条长读被展开；要无损往返请用「导出 JSON」）`);
  return L.join('\n') + '\n';
}

/** 一行 C 表 */
function cRow(dev, addr, wr, rd){
  const a = Array.from(addr || []);
  const addrTxt = a.length
    ? '0x' + (a.reduce((x, y) => ((x << 8) | y) >>> 0, 0) >>> 0).toString(16).toUpperCase().padStart(a.length * 2, '0')
    : '0';
  return `{${hex2(dev)}, ${a.length}, ${addrTxt}, ${(wr || []).length}, ${rd | 0}, ${cBytes(wr)}},`;
}

/** 导出成**人读文本**（每行一条 + 结果注解）*/
export function toText(items){
  const L = [];
  for (const it of items){
    const tail = [asText(it), periodText(it)].filter(Boolean).join(' ');
    L.push(describeItem(it) + (tail ? '   ' + tail : ''));
  }
  return L.join('\n') + '\n';
}

/** 导出成 JSON（带行列号，便于外部工具；也能再读回来）*/
export function toJson(items){
  return JSON.stringify({
    format: 'web-serial-rtt-tools/i2c-script',
    version: 1,
    items: items.map(it => ({
      kind: it.kind, dev: it.dev ?? null, addr: it.addr, wr: it.wr, rd: it.rd,
      ms: it.ms ?? null, periodMs: it.period || 0, count: it.count || 0, group: it.group || 0,
      chunk: it.chunk || 'reset',
      as: it.asText || '', tag: it.tag || '',
    })),
  }, null, 2) + '\n';
}

/** JSON（本站导出的形状）→ 命令项；不是这个形状就返回 null */
export function fromJson(text){
  let o;
  try { o = JSON.parse(text); } catch { return null; }
  if (!o || o.format !== 'web-serial-rtt-tools/i2c-script' || !Array.isArray(o.items)) return null;
  return o.items.map((r, i) => {
    const it = {
      // dev / ms 保持 null（而不是 0）：scan 没有器件、XFER 没有时长 —— 别把"没有"写成"是 0"
      kind: r.kind || KIND.XFER, dev: r.dev ?? null, addr: r.addr || [], wr: r.wr || [], rd: r.rd || 0,
      ms: r.ms ?? null, period: r.periodMs || 0, count: r.count || 0, group: r.group || 0,
      chunk: r.chunk === 'ptr' ? 'ptr' : 'reset',
      line: i + 1, text: '', tag: r.tag || '', asText: r.as || '', as: null, warn: null,
    };
    if (it.asText){
      const spec = parseAs(it.asText);
      if (spec.ok) it.as = spec;
    }
    it.label = it.kind === KIND.SCAN ? '扫描 0x08..0x77'
      : it.kind === KIND.DELAY ? `延时 ${it.ms} ms` : describeItem(it);
    return it;
  });
}

/** 语法速查（页面上「语法速查」那一块直接用）*/
export const SYNTAX_HELP = `一行一条命令，'#' 或 '//' 注释到行尾，空行忽略。

  scan                     扫 0x08..0x77，列出应答的地址
  ping 0x50                地址探测（只问 ACK）
  rd  0x50 0x00 8          写子地址 0x00 后 repeated START 读 8 B
  rd  0x50 0x00 256        读长随便写（≤4096）—— **超过 54 B 自动分片**，日志只出一行
  rd  0x50 0x00 256 ptr    同上，但用「地址指针自增」连续读（读一次弹一个数的 FIFO 型器件必须用它）
  rd  0x50 - 8             子地址写 '-' = 不带子地址的纯读
  wr  0x50 0x00 11 22 33   写子地址 + 数据（单次 ≤51 B，**写不自动分片**）
  wr  0x50 0x00            只发子地址、不带数据 = **把器件的地址指针推到 0x00**
                           （线上 = START + dev+W + 子地址 + STOP；EEPROM 分片读靠它）
  wr  0x50 - 11 22         不带子地址的纯写
  xfer dev=0x50 addr=0x00,0x10 wr=11,22 rd=0    字段名写法（与线上字段同名）
  delay 10ms               延时（裸数字 = µs；吃 us/µs/ms/s）

定时（这就是 while(1)）：
  loop 100ms               块开始：里面每行都按 100ms 周期跑
    rd 0x68 0x3B 14
  end
  loop 500ms 20 … end      带次数：跑 20 轮自己停（省略 = 一直跑到点「停止」）
  every 100ms              不成块也行：把后面所有行设成 100ms 周期
  every 100ms 50           周期 + 最多 50 次
  once                     取消周期，回到一次性
  行尾覆盖：rd 0x68 0x75 1 every 500ms

解码（读回的字节 → 有名字的数）：
  行尾 as 名字=表达式, 名字=表达式     例：as ax=i16be(0)/16384, az=i16be(4)/16384

C 表行（列序 = 线上 XFER 字段，从固件源码直接贴）：
  {dev, addr_len, addr, wr_len, rd_len, data},
  {0x50, 1, 0x0000, 0, 8, NULL},
  {0x50, 1, 0x0010, 4, 0, (uint8_t[]){0x11, 0x22, 0x33, 0x44}},
  {0x00, 0, 0, 3, 0, "ABC"},              字符串字面量也吃
  {0x68, 1, 0x3B, 0, 14, NULL}, as ax=i16be(0)/16384 every 50ms

进制：设备地址与子地址**默认十六进制**（50 就是 0x50）；读长/延时/周期是十进制。
上限：单次写 ≤51 B（写**不**自动分片）、一次逻辑读 ≤4096 B（>54 B 自动分片）、子地址 ≤4 B、器件地址 7 位。`;
