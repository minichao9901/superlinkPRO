/**
 * 纯 Node 自测：命令协议（DSL + C 表）+ 表格互转 + 四个模块示例（不需要浏览器/硬件）
 *   node tools/selftest/i2c-dsl.test.mjs          （等价：make test-i2c-dsl）
 *
 * 这里咬四件事：
 *   ① 逐条命令的**字段落位**（尤其子地址按原序铺字节、`-` = 无子地址、`wr dev addr` 无数据 = 设指针）；
 *   ② `loop … end` / `every` 的**任务边界**：ADS1115 的"写配置 → 等 → 读结果"必须是一个不可拆的整段，
 *      拆开就会读到上一次的结果 —— 这是本页最容易做错、也最难在真机上发现的地方，所以钉死在 buildTasks 上；
 *   ③ **错误必须在解析期带行号拦住**（否则固件回一个 E_RANGE，用户只能猜）；
 *   ④ **示例脚本必须零错误**：示例写错比功能写错更丢人（和 SPI 侧同一条纪律）。
 */
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const app = join(here, '..', '..', 'app');
const url = p => 'file://' + join(app, p).replace(/\\/g, '/');

const D = await import(url('i2c/dsl.js'));
const X = await import(url('i2c/expr.js'));
const { buildTasks } = await import(url('i2c/runner.js'));
const { PRESETS, DEFAULT_PRESET } = await import(url('i2c/presets.js'));
const V = await import(url('i2c/view.js'));
const P = await import(url('i2c/protocol.js'));

let pass = 0, fail = 0;
const ok = (cond, name, extra = '') => {
  if (cond){ pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name} ${extra}`); }
};
const eq = (a, b, name) => ok(a === b, name, `期望 ${JSON.stringify(b)}，实际 ${JSON.stringify(a)}`);
const hex = a => [...a].map(x => x.toString(16).padStart(2, '0')).join(' ');
const parse1 = text => {
  const r = D.parseScript(text);
  if (r.errors.length) throw new Error('本不该有错：' + JSON.stringify(r.errors));
  return r.items[0];
};
const errOf = text => {
  const r = D.parseScript(text);
  return r.errors.length ? r.errors[0] : null;
};

// ==================================================================== 1
console.log('== 1. 小工具：注释 / 分词 / 数字 / 时长 / 子地址铺字节 ==');
{
  eq(D.stripComment('rd 0x50 0 8 # 注释').trim(), 'rd 0x50 0 8', 'stripComment：# 到行尾');
  eq(D.stripComment('rd 0x50 0 8 // 注释').trim(), 'rd 0x50 0 8', 'stripComment：// 到行尾');
  eq(D.stripComment('{0x00,0,0,3,0,"A#B"}, # 尾注').trim(), '{0x00,0,0,3,0,"A#B"},', 'stripComment：引号里的 # 不算注释');
  eq(D.tokenize('wr 0x50 0x00 "11 22 33"').length, 4, 'tokenize：引号里的空格不断词');
  let threw = false;
  try { D.tokenize('a "b'); } catch { threw = true; }
  ok(threw, 'tokenize：引号没配对 → 抛错');

  // I2C 惯例：器件地址 / 子地址**默认十六进制**，长度/时长十进制
  eq(D.parseNum('50', true).v, 0x50, 'parseNum：器件地址默认十六进制（50 = 0x50）');
  eq(D.parseNum('0x50', false).v, 0x50, 'parseNum：0x 前缀一律十六进制');
  eq(D.parseNum('54', false).v, 54, 'parseNum：长度默认十进制');
  eq(D.parseNum('54ab', false).ok, false, 'parseNum：十进制模式下非 0x 的十六进制字母被拒');
  eq(D.parseNum('', false).ok, false, 'parseNum：空值被拒');

  eq(D.parseDuration('10ms').ms, 10, '时长 10ms');
  eq(D.parseDuration('500us').us, 500, '时长 500us');
  eq(D.parseDuration('500µs').us, 500, '时长 500µs（带 µ）');
  eq(D.parseDuration('1.5s').ms, 1500, '时长 1.5s');
  eq(D.parseDuration('1000').us, 1000, '时长裸数字 = µs');
  eq(D.parseDuration('abc').ok, false, '时长非法被拒');

  eq(hex(D.parseHexBytesLoose('11,22,33')), '11 22 33', 'hex 字节：逗号分隔');
  eq(hex(D.parseHexBytesLoose('0x11 0x22')), '11 22', 'hex 字节：0x 前缀 + 空格');
  eq(hex(D.parseHexBytesLoose('112233')), '11 22 33', 'hex 字节：连写');
  threw = false;
  try { D.parseHexBytesLoose('112'); } catch { threw = true; }
  ok(threw, 'hex 字节：奇数位被拒');

  eq(hex(D.numToBytes(0x0010, 2)), '00 10', 'numToBytes 大端铺字节');
}

// ==================================================================== 2
console.log('== 2. 基本命令：scan / ping / rd / wr / delay ==');
{
  eq(parse1('scan').kind, D.KIND.SCAN, 'scan');
  const ping = parse1('ping 0x50');
  eq(ping.kind, D.KIND.XFER, 'ping 是一条 XFER');
  eq(ping.dev, 0x50, 'ping：器件地址');
  eq(ping.addr.length + ping.wr.length + ping.rd, 0, 'ping：wr=rd=0（只问 ACK）');
  eq(ping.label, '探测 0x50', 'ping：标签');

  const rd = parse1('rd 0x50 0x00 8');
  eq(rd.dev, 0x50, 'rd：器件');
  eq(hex(rd.addr), '00', 'rd：子地址 1 B');
  eq(rd.rd, 8, 'rd：读长');
  eq(rd.wr.length, 0, 'rd：不写数据');
  eq(rd.label, '读 0x50[00] × 8', 'rd：标签');

  const rdNo = parse1('rd 0x50 - 8');
  eq(rdNo.addr.length, 0, 'rd 的 `-` = 不带子地址（纯读）');
  eq(rdNo.label, '读 0x50 × 8', '纯读的标签');

  const rd16 = parse1('rd 0x50 0x0010 2');
  eq(hex(rd16.addr), '00 10', '子地址 0x0010 → 线上发 00 10（按写出来的位数铺字节）');
  const rd2 = parse1('rd 0x50 00,10 2');
  eq(hex(rd2.addr), '00 10', '子地址「00,10」按字节串读也对');
  ok(errOf('rd 0x50 00 10 2').msg.includes('一个整体'), '子地址拆成两个 token 时，报错要说清"要写成一个整体"');

  const wr = parse1('wr 0x50 0x00 A5 5A DE AD');
  eq(hex(wr.wr), 'a5 5a de ad', 'wr：写数据');
  eq(wr.rd, 0, 'wr：不读');
  eq(wr.label, '写 0x50[00] ← A5 5A DE AD', 'wr：标签');

  const wrPtr = parse1('wr 0x50 0x00');
  eq(wrPtr.wr.length, 0, 'wr 不给数据 = 只发子地址');
  eq(hex(wrPtr.addr), '00', '……而且子地址还在');
  eq(wrPtr.label, '设地址指针 0x50[00]', '……标签写清了它在干什么');

  const wrNo = parse1('wr 0x50 - 11 22');
  eq(wrNo.addr.length, 0, 'wr 的 `-` = 不带子地址的纯写');
  eq(hex(wrNo.wr), '11 22', '纯写的数据');

  const dl = parse1('delay 10ms');
  eq(dl.kind, D.KIND.DELAY, 'delay');
  eq(dl.ms, 10, 'delay：毫秒');

  const wrr = parse1('wrr 0x68 0x3B 14');
  eq(wrr.rd, 14, 'wrr 等价于 rd（读起来顺一点）');
}

// ==================================================================== 3
console.log('== 3. xfer 字段名写法 ==');
{
  const a = parse1('xfer dev=0x50 addr=0x00 rd=8');
  eq(a.dev, 0x50, 'xfer：dev=');
  eq(hex(a.addr), '00', 'xfer：addr=');
  eq(a.rd, 8, 'xfer：rd=');
  const b = parse1('xfer dev=0x50 addr=0x00,0x10 wr=11,22 rd=0');
  eq(hex(b.addr), '00 10', 'xfer：addr 多字节');
  eq(hex(b.wr), '11 22', 'xfer：wr 多字节');
  const c = parse1('xfer dev=50 rd=1');
  eq(c.dev, 0x50, 'xfer：dev 默认十六进制');
  const d = parse1('xfer dev=0x50 addrl=2 rd=1');
  eq(hex(d.addr), '00 00', 'xfer：只给 addrl=2 → 子地址两个 0 字节');
  ok(errOf('xfer dev=0x50 qq=1') !== null, 'xfer：不认识的字段报错');
  ok(errOf('xfer dev=0x50 flags=1') !== null, 'xfer：flags 非 0 报错');
  ok(errOf('xfer dev=0x50 addr=0x00 addrl=2 rd=1').msg.includes('对不上'), 'xfer：addr_len 与 addr 对不上报错');
}

// ==================================================================== 4
console.log('== 4. C 表行 ==');
{
  const r1 = parse1('{0x50, 1, 0x0000, 0, 8, NULL},');
  eq(r1.dev, 0x50, 'C 表：dev');
  eq(hex(r1.addr), '00', 'C 表：addr_len=1 addr=0x0000 → 发 00');
  eq(r1.rd, 8, 'C 表：rd_len');
  eq(r1.wr.length, 0, 'C 表：NULL = 没有写数据');

  const r2 = parse1('{0x50, 1, 0x0010, 4, 0, (uint8_t[]){0x11, 0x22, 0x33, 0x44}},');
  eq(hex(r2.addr), '10', 'C 表：addr 0x0010 + addr_len 1 → 只发最低字节 10');
  eq(hex(r2.wr), '11 22 33 44', 'C 表：(uint8_t[]){…} 数据');

  const r3 = parse1('{0x50, 2, 0x0010, 4, 0, {0x11, 0x22, 0x33, 0x44}},');
  eq(hex(r3.addr), '00 10', 'C 表：addr_len=2 → 发 00 10');
  eq(hex(r3.wr), '11 22 33 44', 'C 表：裸 {…} 也吃');

  const r4 = parse1('{0x00, 0, 0, 3, 0, "ABC"},');
  eq(hex(r4.wr), '41 42 43', 'C 表：字符串字面量 → 每字符一字节');
  eq(r4.addr.length, 0, 'C 表：addr_len=0 → 不带子地址');

  const r5 = parse1('{0x68, 1, 0x3B, 0, 14, NULL}, as ax=i16be(0)/16384 every 50ms');
  eq(r5.rd, 14, 'C 表 + 行尾修饰：读长');
  eq(r5.period, 50, 'C 表 + 行尾修饰：every 50ms');
  eq(r5.as.fields[0].name, 'ax', 'C 表 + 行尾修饰：as 解码');

  ok(errOf('{0x50, 1, 0x00, 4, 0, NULL},') !== null, 'C 表：wr_len=4 却写 NULL → 报错');
  ok(errOf('{0x50, 1, 0x00, 4, 0, (uint8_t[]){1,2}},') !== null, 'C 表：数据长度与 wr_len 对不上 → 报错');
  ok(errOf('{0x50, 1, 0x00, 52, 0, NULL},') !== null, 'C 表：wr_len 超上限 → 报错');
  ok(errOf('{0x50, 1, 0x00, 0, 55, NULL},') !== null, 'C 表：rd_len 超上限 → 报错');
  ok(errOf('{0x80, 0, 0, 0, 0, NULL},') !== null, 'C 表：dev 超 7 位 → 报错');
  ok(errOf('{0x50, 0, 0, 0}') !== null, 'C 表：列数不足 → 报错');
  ok(errOf('{0x50, 0, 0, 0, 0, NULL') !== null, 'C 表：少右花括号 → 报错');
}

// ==================================================================== 5
console.log('== 5. 定时（while(1)）：loop / every / 任务边界 ==');
{
  const txt = [
    'wr 0x68 0x6B 0x00',            // 一次性
    'loop 100ms',
    '  wr 0x48 0x01 C3 83',
    '  delay 10ms',
    '  rd 0x48 0x00 2',
    'end',
    'rd 0x50 0x00 8 every 500ms',   // 单行定时
  ].join('\n');
  const r = D.parseScript(txt);
  eq(r.errors.length, 0, '这段没有语法错');
  eq(r.items.length, 5, '共 5 条命令');
  eq(r.stats.oneShots, 1, '一次性 1 条');
  eq(r.stats.timed, 4, '定时 4 条');
  const { once, tasks } = buildTasks(r.items);
  eq(once.length, 1, '一次性段 1 条');
  eq(tasks.length, 2, '两个定时任务（loop 块 + 单行 every）');
  eq(tasks[0].period, 100, 'loop 块周期 100 ms');
  eq(tasks[0].items.length, 3, '🚨 loop 块里的 3 条是**一个不可拆的整段**（写配置→等→读结果）');
  eq(tasks[0].items.map(i => i.kind).join(','), 'xfer,delay,xfer', '……而且顺序原样保留');
  eq(tasks[1].period, 500, '单行 every 自成任务，周期 500 ms');
  eq(tasks[1].items.length, 1, '……只有 1 条');

  // 次数
  const r2 = D.parseScript('loop 500ms 20\n  rd 0x50 0x00 8\nend');
  const t2 = buildTasks(r2.items).tasks;
  eq(t2[0].count, 20, 'loop 带次数 → 跑 20 轮自停');
  const r3 = D.parseScript('every 100ms 5\nrd 0x50 0x00 1');
  eq(buildTasks(r3.items).tasks[0].count, 5, 'every 带次数');

  // 状态式 every：换周期 = 换任务
  const r4 = D.parseScript('every 100ms\nrd 0x50 0x00 1\nrd 0x50 0x01 1\nevery 500ms\nrd 0x50 0x02 1');
  const t4 = buildTasks(r4.items).tasks;
  eq(t4.length, 2, '换周期会切出第二个任务');
  eq(t4[0].items.length, 2, '第一段 2 条');
  eq(t4[0].period, 100, '第一段 100 ms');
  eq(t4[1].period, 500, '第二段 500 ms');
  eq(buildTasks(D.parseScript('every 100ms\nrd 0x50 0x00 1\nonce\nrd 0x50 0x01 1').items).tasks.length, 1, '`once` 之后回到一次性');

  // 行尾 every 覆盖块周期 → 自成任务
  const r5 = D.parseScript('loop 100ms\n  rd 0x50 0x00 1 every 1000ms\n  rd 0x50 0x01 1\nend');
  const t5 = buildTasks(r5.items).tasks;
  eq(t5.length, 2, '行尾 every 覆盖周期的那一行自成任务');
  eq(t5[0].period, 1000, '……周期是它自己的 1000 ms');

  ok(errOf('loop 100ms\nrd 0x50 0x00 1').msg.includes('end'), '少了 end → 报错');
  ok(errOf('end').msg.includes('loop'), '多了 end → 报错');
  ok(errOf('loop abc').msg.includes('时长'), 'loop 的周期非法 → 报错');
  const warnScan = D.parseScript('loop 100ms\nscan\nend').warns;
  ok(warnScan.length === 1 && warnScan[0].msg.includes('扫描'), '把扫描塞进循环会给个告警');
}

// ==================================================================== 6
console.log('== 6. as 解码表达式 ==');
{
  const spec = X.parseAs('ax=i16be(0)/16384, ay=i16be(2)/16384, az=i16be(4)/16384');
  ok(spec.ok, '解析三轴');
  const bytes = Uint8Array.from([0x40, 0x00, 0xC0, 0x00, 0xFF, 0xFF]);   // 16384, -16384, -1
  const a = X.applyAs(spec, bytes);
  eq(a.values[0].value, 1, 'ax = 0x4000/16384 = 1 g');
  eq(a.values[1].value, -1, 'ay = 0xC000/16384 = -1 g（有符号）');
  eq(a.values[2].value, -1 / 16384, 'az = 0xFFFF → -1/16384');

  const t = X.parseAs('t=i16be(0)/340+36.53');
  const tv = X.applyAs(t, Uint8Array.from([0x30, 0x39])).values[0].value;   // 12345
  ok(Math.abs(tv - (12345 / 340 + 36.53)) < 1e-9, '温度：raw/340+36.53（严格从左到右）', String(tv));

  const v = X.parseAs('v=i16be(0)*0.000125');
  eq(X.applyAs(v, Uint8Array.from([0x20, 0x00])).values[0].value, 8192 * 0.000125, '电压：raw*125µV');

  const bit = X.parseAs('mux=u16be(0)>>12&7');
  eq(X.applyAs(bit, Uint8Array.from([0xC3, 0x83])).values[0].value, 4, '位段：>>12&7 = 4（从左到右，注意没有优先级）');

  const le = X.parseAs('w=u16le(0)');
  eq(X.applyAs(le, Uint8Array.from([0x34, 0x12])).values[0].value, 0x1234, 'u16le 小端');
  const f = X.parseAs('f=f32be(0)');
  eq(X.applyAs(f, Uint8Array.from([0x3F, 0x80, 0x00, 0x00])).values[0].value, 1, 'f32be');
  const i8 = X.parseAs('x=i8(0)');
  eq(X.applyAs(i8, Uint8Array.from([0xFF])).values[0].value, -1, 'i8 有符号');

  ok(X.parseAs('hex').hexOnly, 'as hex = 只显示原始字节');
  ok(!X.parseAs('').fields.length, '空 as = 只显示原始字节');
  ok(!X.parseAs('ax=qqq(0)').ok, '未知函数被拒');
  ok(!X.parseAs('ax=i16be(0)/0').ok, '除以 0 被拒');
  ok(!X.parseAs('ax').ok, '少 = 被拒');
  ok(!X.parseAs('1ax=u8(0)').ok, '变量名非法被拒');
  ok(!X.parseAs('ax=u8(0),ax=u8(1)').ok, '变量名重复被拒');

  // 越界提示：读 2 B 却按 i16be(4) 解码
  const warnOut = X.applyAs(X.parseAs('z=i16be(4)'), Uint8Array.from([0, 0]));
  ok(warnOut.warn && warnOut.warn.includes('第 6 字节'), '读不到的偏移会给告警', warnOut.warn);

  eq(X.fmtValue(1), '1', 'fmtValue：整数原样');
  eq(X.fmtValue(1.5), '1.5', 'fmtValue：小数不留尾零');
  eq(X.fmtValue(0.000125), '0.000125', 'fmtValue：小值给足有效位');
  eq(X.fmtValue(1234.5678), '1234.57', 'fmtValue：大值收两位');
}

// ==================================================================== 7
console.log('== 7. 错误必须带行号拦住 ==');
{
  const e1 = errOf('scan\nrd 0x50 0x00 99999\n');
  ok(e1 && e1.line === 2, '一次逻辑读超上限：报在第 2 行', JSON.stringify(e1));
  ok(e1.msg.includes('4096'), '……并说清上限是多少（4096）', e1.msg);
  ok(errOf('rd 0x80 0x00 1').msg.includes('7 位'), '器件地址超 7 位被拒');
  ok(errOf('rd 0x50 0x00').msg.includes('缺长度'), '读命令缺长度被拒');
  ok(errOf('rd 0x50 0x00 0').msg.includes('必须 > 0'), '读长 0 被拒（要探测就写 ping）');
  eq(parse1('wr 0x50 0x00 11 22 33 44 55').wr.length, 5, '写 5 B 是合法的（上限 51）');
  ok(errOf(`wr 0x50 0x00 ${new Array(52).fill('11').join(' ')}`).msg.includes('51'), '写 52 B 被拒');
  ok(errOf(`wr 0x50 0x00 ${new Array(52).fill('11').join(' ')}`).msg.includes('不自动分片'), '……并说清"写不自动分片"（EEPROM 跨页会绕回页首）');
  ok(errOf('rd 0x50 0x00 1 2 3').msg.includes('多了一个参数'), '读命令多余参数被拒');
  ok(errOf('qqq 1 2').msg.includes('看不懂'), '未知关键字被拒');
  ok(errOf('00 01 02').msg.includes('dev='), '裸字节缺 dev 被拒并给出正确写法');
  ok(errOf('rd 0x50 0x00 8 as ax').msg.includes('='), 'as 少 = 被拒');
  const warns = D.parseScript('scan qq').warns;
  ok(warns.length === 1 && warns[0].msg.includes('多出来'), '多余的位置参数给告警（不静默吞掉）', JSON.stringify(warns));
}

// ==================================================================== 8
console.log('== 8. 导出与往返 ==');
{
  const text = [
    'scan',
    'rd 0x50 0x00 8',
    'wr 0x50 0x10 A5 5A',
    'delay 10ms',
    '{0x68, 1, 0x3B, 0, 14, NULL}, as ax=i16be(0)/16384 every 50ms',
  ].join('\n');
  const r = D.parseScript(text);
  const c = D.toCTable(r.items);
  ok(c.includes('{0x50, 1, 0x00, 0, 8, NULL}'), 'C 表导出：读那一行', c);
  ok(c.includes('{0x50, 1, 0x10, 2, 0, (uint8_t[]){0xA5, 0x5A}}'), 'C 表导出：写那一行', c);
  ok(c.includes('delay 10ms'), 'C 表导出：延时');
  ok(c.includes('scan'), 'C 表导出：扫描');
  ok(c.includes('as ax=i16be(0)/16384') && c.includes('every 50ms'), 'C 表导出：带上 as 与周期');

  // 导出 → 再解析，字段必须逐条一致（往返测试）
  const back = D.parseScript(c);
  eq(back.errors.length, 0, '导出的 C 表能原样解析回来');
  eq(back.items.length, r.items.length, '……条数一致');
  const key = it => [it.kind, it.dev ?? '', hex(it.addr), hex(it.wr), it.rd, it.ms ?? '', it.period, it.count].join('|');
  eq(back.items.map(key).join('\n'), r.items.map(key).join('\n'), '……每条字段都一致');

  const j = D.toJson(r.items);
  const fromJ = D.fromJson(j);
  eq(fromJ.length, r.items.length, 'JSON 往返：条数');
  eq(fromJ.map(key).join('\n'), r.items.map(key).join('\n'), 'JSON 往返：字段一致');
  eq(D.fromJson('{"a":1}'), null, '不是本站形状的 JSON 返回 null（上层按普通文本处理）');
  ok(D.toText(r.items).split('\n').filter(Boolean).length === r.items.length, '文本导出一行一条');
}

// ==================================================================== 9
console.log('== 9. 命令表 ⇄ 脚本（表格与脚本区走同一套解析）==');
{
  eq(V.periodText(''), '', '周期空格 → 空');
  eq(V.periodText('100'), 'every 100ms', '周期裸数字按 ms');
  eq(V.periodText('100ms'), 'every 100ms', '周期 100ms');
  eq(V.periodText('100ms×50'), 'every 100ms 50', '周期 100ms×50');
  eq(V.periodText('100msx50'), 'every 100ms 50', '周期 100msx50（小写 x 也认）');
  eq(V.periodText('500us'), 'every 500us', '周期 500us');
  eq(V.periodText('once'), '', 'once = 一次性');

  const rows = [
    { op: 'rd', dev: '0x50', addr: '0x00', data: '', rd: '8', as: '', period: '' },
    { op: 'wr', dev: '0x50', addr: '0x10', data: 'A5 5A', rd: '', as: '', period: '' },
    { op: 'ping', dev: '0x68', addr: '', data: '', rd: '', as: '', period: '' },
    { op: 'delay', dev: '', addr: '', data: '10ms', rd: '', as: '', period: '' },
    { op: 'rd', dev: '', addr: '', data: '', rd: '', as: '', period: '' },        // 空器件 → 跳过
    { op: 'rd', dev: '0x68', addr: '0x3B', data: '', rd: '14', as: 'ax=i16be(0)/16384', period: '50ms×100' },
  ];
  const script = V.tableToScript(rows);
  const r = D.parseScript(script);
  eq(r.errors.length, 0, '表格生成的脚本没有语法错：' + JSON.stringify(r.errors.map(e => e.msg)));
  eq(r.items.length, 5, '空器件那一行被跳过（5 条）');
  eq(r.items[0].rd, 8, '表格 → 脚本：读长');
  eq(hex(r.items[1].wr), 'a5 5a', '表格 → 脚本：写数据');
  eq(r.items[2].label, '探测 0x68', '表格 → 脚本：探测');
  eq(r.items[3].ms, 10, '表格 → 脚本：延时');
  eq(r.items[4].period, 50, '表格 → 脚本：周期');
  eq(r.items[4].count, 100, '表格 → 脚本：次数');
  eq(r.items[4].asText, 'ax=i16be(0)/16384', '表格 → 脚本：as 解码');

  // 反向：脚本 → 表格
  const rows2 = V.rowsFromItems(r.items);
  eq(rows2.length, 5, '脚本 → 表格：行数');
  eq(rows2[0].op, 'rd', '……第一行是读');
  eq(rows2[1].op, 'wr', '……第二行是写');
  eq(rows2[2].op, 'ping', '……第三行是探测');
  eq(rows2[3].op, 'delay', '……第四行是延时');
  eq(rows2[3].data, '10ms', '……延时行的时长放在「数据」格');
  eq(rows2[4].period, '50ms×100', '……周期带次数');
  eq(rows2[4].as, 'ax=i16be(0)/16384', '……as 带上');

  // 表格 → 脚本 → 表格，应当稳定（不然改一格就漂）
  const again = V.rowsFromItems(D.parseScript(V.tableToScript(rows2)).items);
  const pick = x => [x.op, x.dev, x.addr, x.data, x.rd, x.as, x.period].join('|');
  eq(again.map(pick).join('\n'), rows2.map(pick).join('\n'), '表格 → 脚本 → 表格 稳定不漂');
}

// ==================================================================== 10
console.log('== 10. 示例脚本必须零错误（示例写错比功能写错更丢人）==');
{
  for (const p of PRESETS){
    const r = D.parseScript(p.text);
    ok(r.errors.length === 0, `示例「${p.name}」解析零错误` + (r.errors.length ? '：' + JSON.stringify(r.errors) : ''));
    ok(r.warns.length === 0, `示例「${p.name}」解析零告警` + (r.warns.length ? '：' + JSON.stringify(r.warns) : ''));
    const { once, tasks } = buildTasks(r.items);
    ok(r.items.length > 0, `示例「${p.name}」有 ${r.items.length} 条命令（一次性 ${once.length} / 定时任务 ${tasks.length}）`);
  }
  eq(DEFAULT_PRESET.id, 'quick', '默认示例是「快速上手」');
  eq(new Set(PRESETS.map(p => p.id)).size, PRESETS.length, '示例 id 不重复');

  // 四个模块的示例都在，而且都带 as 解码
  for (const id of ['at24c02-read', 'at24c02-write', 'mpu6050', 'ads1115', 'si5351']){
    ok(PRESETS.some(p => p.id === id), `收录了示例 ${id}`);
  }
  const mpu = D.parseScript(PRESETS.find(p => p.id === 'mpu6050').text);
  const mpuTask = buildTasks(mpu.items).tasks[0];
  eq(mpuTask.items.length, 1, 'MPU6050 的 while(1)：一个任务一条命令');
  eq(mpuTask.items[0].rd, 14, '……一次读 14 B（加速度 6 + 温度 2 + 陀螺 6）');
  eq(mpuTask.period, 50, '……周期 50 ms');
  const names = mpuTask.items[0].as.fields.map(f => f.name);
  eq(names.join(','), 'ax,ay,az,tC,gx,gy,gz', '……解出 7 个命名变量');

  const ads = D.parseScript(PRESETS.find(p => p.id === 'ads1115').text);
  const adsTask = buildTasks(ads.items).tasks[0];
  eq(adsTask.items.length, 12, 'ADS1115 的 while(1)：四通道 × (写配置+等+读) = 12 条');
  eq(adsTask.items.map(x => x.kind).slice(0, 3).join(','), 'xfer,delay,xfer',
     '🚨 每通道的「写配置 → 等 10ms → 读结果」是一个不可拆的整段');
  eq(adsTask.period, 200, '……周期 200 ms');

  // 会改动内容的示例必须在标题里写明（AT24C02 有写寿命）
  const w = PRESETS.find(p => p.id === 'at24c02-write');
  ok(w.name.includes('⚠'), '写 EEPROM 的示例标题里带警告标记', w.name);
  ok(w.text.includes('AT24C02 · 读'), '……并提示先跑只读那份留底');

  // 🚨 示例里的十六进制注释也是**给人抄的** —— 2026-10 手册对账抓出的两处错钉在这里，别再漂回去
  const adsText = PRESETS.find(p => p.id === 'ads1115').text;
  ok(adsText.includes('0xC583'), 'ADS1115 给出了"单端 ±2.048V"的正确配置字 0xC583');
  ok(!adsText.includes('0x8383 = 同上'), '……不再把 0x8383 说成"同上但 PGA=010(±2.048V)"');
  ok(adsText.includes('**AIN0-AIN1 差分** · ±4.096V'), '……0x8383 的真实含义写对了（MUX=000 差分 · ±4.096V）');
  const siText = PRESETS.find(p => p.id === 'si5351').text;
  ok(siText.includes('0x03 输出使能'), 'Si5351 写明 0x03 是输出使能（低有效，写 1 = 关掉那一路）');
  ok(siText.includes('0x10 CLK0 控制'), '……CLK0 控制在 0x10');
  ok(!siText.includes('0x03 CLK0 控制'), '……不再把 CLK0 控制写成 0x03');
  ok(!siText.includes('00 01 0A 00'), '……参数块 8 B 不再漏掉 P1[17:16] 那一位');

  // Si5351 现在是"**真机验过的三路配置**"：字节、OEB 掩码、PLL 复位写法都得在
  ok(siText.includes('00 01 00 04 00 00 00 00') && siText.includes('00 01 00 16 00 00 00 00')
     && siText.includes('00 01 01 2A 00 00 00 00'), 'Si5351 三路参数块字节齐全（50 / 12.5 / 1 MHz）');
  ok(siText.includes('00 01 01 2A'), '……MS2 那 8 B 带上了 P1[17:16]（P1 = 0x012A00，÷600）');
  ok(siText.includes('wr 0x60 0x10 0F 0F 0F'), '……三路控制字一次写完（0x10 ← 0F 0F 0F）');
  ok(siText.includes('wr 0x60 0x09 FF'), '……带上 OEB 掩码那一笔（0x09 = 0xFF，输出交给 0x03 管）');
  ok(siText.includes('0xB1 A0'), '……PLL 软复位写 0xA0（不是流程图的 0xAC）');
  ok(siText.includes('0xAC') && siText.includes('残留 0x0C'), '……并说明为什么不照抄 0xAC（实测会留下 Reserved 位）');

  // 标了"会写器件"就得写清楚：标题带 ⚠、正文写明 RAM/掉电、空白件没有上电频率
  const si = PRESETS.find(p => p.id === 'si5351');
  ok(si.name.includes('⚠') && si.note.includes('真机验证'), 'Si5351 示例标题带 ⚠、注明字节已真机验证');
  ok(si.text.includes('no boot-up frequency') && si.text.includes('掉电'), '……写明空白件上电没频率、配置在 RAM 掉电即失');
  const siParsed = D.parseScript(si.text);
  const siLoop = buildTasks(siParsed.items).tasks[0];
  eq(siLoop.count, 20, 'Si5351 的状态监视循环带次数（跑 20 轮自停）');
}

// ==================================================================== 11
console.log('== 11. 长读自动分片（实现细节，不该让人自己拆）==');
{
  // 一条 `rd … 256` 就是**一条逻辑读**：解析成一项、rd=256、默认 chunk=reset
  const big = parse1('rd 0x50 0x00 256');
  eq(big.rd, 256, '读长 256 直接收下（不再要求 ≤54）');
  eq(big.chunk, 'reset', '长读默认 chunk=reset（每片重发子地址 —— EEPROM/寄存器型全都对）');
  ok(big.label.includes('自动分片'), '标签里写明了会自动分片', big.label);
  eq(parse1('rd 0x50 0x00 8').chunk, 'reset', '短读也带 chunk 字段（恒 reset，下游不用特判）');

  // ptr / chain / chunk= 三种写法
  eq(parse1('rd 0x50 0x00 256 ptr').chunk, 'ptr', '行尾 ptr → 连续读');
  eq(parse1('rd 0x50 0x00 256 chain').chunk, 'ptr', 'chain 是 ptr 的别名');
  eq(parse1('rd 0x50 0x00 256 chunk=ptr').chunk, 'ptr', 'chunk=ptr 也认');
  eq(parse1('rd 0x50 0x00 256 chunk=reset').chunk, 'reset', 'chunk=reset 显式写也认');
  eq(parse1('xfer dev=0x50 addr=0x00 rd=256 chunk=ptr').chunk, 'ptr', 'xfer 字段名写法也支持 chunk=');
  ok(errOf('rd 0x50 0x00 256 chunk=qqq') !== null, 'chunk 只认 ptr / reset');
  eq(parse1('rd 0x50 0x00 256 ptr').rd, 256, '……ptr 不吃掉读长');
  eq(parse1('rd 0x50 0x00 256 ptr').addr.length, 1, '……也不吃掉子地址');
  eq(parse1('rd 0x50 0x00 256 ptr as v=u8(0)').as.fields[0].name, 'v', '……和 as 共存');

  // 上限与边界
  ok(errOf('rd 0x50 0x00 99999') !== null, `一次逻辑读超过 ${4096} B 被拒`);
  eq(parse1('rd 0x50 0x00 4096').rd, 4096, '恰好 4096 可以');
  eq(parse1('rd 0x50 0x00 55').rd, 55, '刚过 54 也走自动分片（两笔）');
  ok(parse1('rd 0x50 0x00 55').label.includes('自动分片'), '……标签直接说"2 笔"', parse1('rd 0x50 0x00 55').label);

  // 写**不**自动分片（EEPROM 跨页会绕回页首，自动拆是危险的）
  const wrErr = errOf(`wr 0x50 0x00 ${new Array(52).fill('11').join(' ')}`);
  ok(wrErr.msg.includes('不自动分片'), '写超过 51 B 明确拒绝并说明原因', wrErr.msg);

  // 导出的 C 表必须**展开成线上真实发出的每一笔** —— rd_len 是线上字段（一个字节，最大 54）
  const script = [
    'rd 0x50 0x00 256',
    'rd 0x68 0x00 120 ptr',
  ].join('\n');
  const r = D.parseScript(script);
  const c = D.toCTable(r.items);
  const rows = c.split('\n').filter(l => l.startsWith('{'));
  eq(rows.length, 5 + 4, 'reset 模式 256 B → 5 行；ptr 模式 120 B → 1 笔设指针 + 3 片 = 4 行，合计 9');
  ok(rows.every(l => /, ([0-9]+), (NULL|\(uint8_t)/.test(l)), '……每一行的 rd_len 都在线上范围内', rows.join(' | '));
  ok(c.includes('rd_len 是线上字段'), '……并留了注释说明为什么被展开');
  ok(c.includes('导出 JSON'), '……并指向无损的 JSON 导出');
  eq(D.parseScript(c).errors.length, 0, '展开后的 C 表能原样解析回来（零错误）');

  // 展开后的**字节流**必须与 planRead 算出来的一致
  const plan = P.planRead([0x00], 256, { mode: 'reset' });
  const parsedRows = D.parseScript(c).items;
  eq(parsedRows.length, plan.cmds.length + 4, '展开后的条数 = 分片数 + ptr 那条的设指针笔 + …');
  eq(parsedRows[0].rd, plan.cmds[0].rd, '第一片的读长一致');
  eq(hex(parsedRows[0].addr), hex(plan.cmds[0].addr), '第一片的子地址一致');
  eq(parsedRows[4].rd, plan.cmds[4].rd, '末片读长一致（40 B）');
  eq(hex(parsedRows[4].addr), hex(plan.cmds[4].addr), '末片子地址一致（0x00+216=0xD8）');

  // JSON 是无损的：chunk 与 as 都保住
  const json = D.toJson(r.items);
  const back = D.fromJson(json);
  eq(back[0].chunk, 'reset', 'JSON 往返：chunk 保住');
  eq(back[1].chunk, 'ptr', 'JSON 往返：ptr 保住');
  eq(back[0].rd, 256, 'JSON 往返：读长保住');

  // 命令表：「读长」格写 `256` 或 `256 ptr` 都能走通（这样才能无损往返）
  eq(V.parseRdCell('256').n, 256, '读长格：256');
  eq(V.parseRdCell('256').chunk, 'reset', '读长格：默认每片重发子地址');
  eq(V.parseRdCell('256 ptr').chunk, 'ptr', '读长格：256 ptr');
  eq(V.parseRdCell('256ptr').chunk, 'ptr', '读长格：256ptr 连写也认');
  eq(V.parseRdCell(''), null, '读长格：空 → null（交给上层给默认值）');
  eq(V.parseRdCell('abc'), null, '读长格：非数字 → null');

  const rowsIn = [{ op: 'rd', dev: '0x50', addr: '0x00', data: '', rd: '256', as: '', period: '' }];
  const line = V.rowToLine(rowsIn[0]);
  eq(line, 'rd 0x50 0x00 256', '表格 → 脚本：长读原样写成一条', line);
  const rowsPtr = [{ op: 'rd', dev: '0x50', addr: '0x00', data: '', rd: '256 ptr', as: '', period: '' }];
  eq(V.rowToLine(rowsPtr[0]), 'rd 0x50 0x00 256 ptr', '表格 → 脚本：ptr 也带出去');

  // 脚本 → 表格：长读装回一格
  const rowsBack = V.rowsFromItems(D.parseScript('rd 0x50 0x00 256\nrd 0x68 0x00 120 ptr').items);
  eq(rowsBack[0].rd, '256', '脚本 → 表格：长读回到「读长」格');
  eq(rowsBack[1].rd, '120 ptr', '脚本 → 表格：ptr 长读带上标记');
  const rowsBack2 = V.rowsFromItems(D.parseScript('rd 0x50 0x00 8').items);
  eq(rowsBack2[0].rd, '8', '短读不带多余标记');
}

console.log(`\n== 结果：${pass} 项通过 / ${fail} 项失败 ==`);
process.exit(fail ? 1 : 0);
