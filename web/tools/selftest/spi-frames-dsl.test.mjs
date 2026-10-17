/**
 * 纯 Node 自测：手写多帧 DSL 的解析器（不需要浏览器、不需要硬件）
 *   node tools/selftest/spi-frames-dsl.test.mjs      （等价：make test-dsl）
 *
 * 这里咬的是"解析对了没有"：字段落位、三条自动规则（cmd/addr 自动开相位、rx 自动带 RSP、
 * 末帧补 RSP）、以及**错误必须在解析期带行号拦住**（否则固件回一个 RANGE，用户只能猜）。
 * 另外把面板上的示例片段当回归用例 —— 示例写错比功能写错更丢人。
 */
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const app = join(here, '..', '..', 'app');
const url = p => 'file://' + join(app, p).replace(/\\/g, '/');

const P = await import(url('spi/protocol.js'));
const D = await import(url('spi/frames-dsl.js'));

let pass = 0, fail = 0;
const ok = (cond, name, extra = '') => {
  if (cond){ pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name} ${extra}`); }
};
const hexOf = a => [...a].map(x => x.toString(16).padStart(2, '0')).join(' ');
const parse1 = text => {
  const r = D.parseFrames(text);
  if (r.errors.length) throw new Error('本不该有错：' + JSON.stringify(r.errors));
  return r.items[0];
};
const xferOf = it => {
  const p = it.payload, dv = new DataView(p.buffer, p.byteOffset, p.byteLength);
  return {
    cmd: p[0], tcfg: p[1], addrLen: p[2], dummy: p[3],
    txLen: dv.getUint16(4, true), rxLen: dv.getUint16(6, true), addr: dv.getUint32(8, true),
    tx: p.subarray(P.XFER_HDR),
  };
};
const errOf = text => {
  const r = D.parseFrames(text);
  return r.errors.length ? r.errors[0] : null;
};

// ==================================================================== 1
console.log('== 1. 小工具：去注释 / 分词 / 数字 / 时长 / 十六进制 ==');
{
  ok(D.stripComment('0x11 # 注释') === '0x11 ', 'stripComment：# 到行尾');
  ok(D.stripComment('0x11 // 注释') === '0x11 ', 'stripComment：// 到行尾');
  ok(D.stripComment('tx="00 # 11" # 尾注') === 'tx="00 # 11" ', 'stripComment：引号里的 # 不算注释');
  ok(D.tokenize('tx="00 01"  rx=2').length === 2, 'tokenize：引号里的空格不断词');
  ok(D.tokenize('  a   b  ').join('|') === 'a|b', 'tokenize：多余空白吃掉');
  let threw = false;
  try { D.tokenize('tx="00'); } catch { threw = true; }
  ok(threw, 'tokenize：引号没配对 → 抛错');

  ok(D.parseNum('0x9F', true).v === 0x9f && D.parseNum('9F', true).v === 0x9f, 'parseNum：cmd 那颗默认十六进制');
  ok(D.parseNum('492').v === 492, 'parseNum：默认十进制（rx=492 是 492 不是 0x492）');
  ok(D.parseNum('492', false).v === 492 && D.parseNum('0x1EC', false).v === 492, 'parseNum：0x 前缀一律十六进制');
  ok(D.parseNum('12ab', false).ok === false, 'parseNum：非 0x 的十六进制字母在十进制模式下被拒');
  ok(D.parseNum('', false).ok === false, 'parseNum：空值被拒');

  ok(D.parseDuration('120ms').us === 120000, '时长 120ms');
  ok(D.parseDuration('500us').us === 500 && D.parseDuration('500µs').us === 500, '时长 500us / 500µs');
  ok(D.parseDuration('1.5s').us === 1500000, '时长 1.5s');
  ok(D.parseDuration('1000').us === 1000, '时长裸数字 = µs');
  ok(D.parseDuration('abc').ok === false, '时长非法被拒');

  ok(hexOf(D.parseHexBytesLoose('00,01,02')) === '00 01 02', 'tx：逗号分隔');
  ok(hexOf(D.parseHexBytesLoose('0x00 0x01')) === '00 01', 'tx：0x 前缀 + 空格');
  ok(hexOf(D.parseHexBytesLoose('000102')) === '00 01 02', 'tx：连写');
  ok(D.parseHexBytesLoose('').length === 0, 'tx：空 = 零字节');
  threw = false;
  try { D.parseHexBytesLoose('0'); } catch { threw = true; }
  ok(threw, 'tx：奇数位十六进制被拒');
}

// ==================================================================== 2
console.log('== 2. 裸字节行 = 一条 cmd 帧 ==');
{
  const it = parse1('0x11');
  ok(it.type === P.T.XFER, '裸字节 → XFER');
  const x = xferOf(it);
  ok(x.cmd === 0x11 && x.tcfg === P.TC.CMD_EN, 'cmd=0x11 且自动开 cmd 相位');
  ok(x.txLen === 0 && x.rxLen === 0 && x.addrLen === 0, '没有数据/地址相位');
  ok(it.flags & P.F.RSP, '整段只有一条 → 自动带 RSP（发完要知道成没成）');

  ok(xferOf(parse1('29')).cmd === 0x29, '裸字节按十六进制（29 → 0x29 而不是十进制 29）');
  const shorthand = xferOf(parse1('0x9F rx=3'));
  ok(shorthand.cmd === 0x9f && shorthand.rxLen === 3, '裸字节后面可以跟 key=value（0x9F rx=3）');
  const e = errOf('0x11 extra');
  ok(e && /不认识的 key/.test(e.msg), '裸字节后面跟不认识的东西 → 报错');
  const dup = errOf('0x11 cmd=0x22');
  ok(dup && /不用再写 cmd=/.test(dup.msg), '行首字节与 cmd= 重复 → 报错');
}

// ==================================================================== 3
console.log('== 3. XFER 的 key：字段落位与自动开相位 ==');
{
  const x = xferOf(parse1('cmd=0x9F rx=3'));
  ok(x.cmd === 0x9f && x.rxLen === 3 && (x.tcfg & P.TC.CMD_EN), 'cmd= + rx= → cmd 相位 + 读长度');

  const a = xferOf(parse1('cmd=0x03 addr=0x1000 addrl=3'));
  ok(a.addr === 0x1000 && a.addrLen === 3 && (a.tcfg & P.TC.ADDR_EN), '给 addr+addrl → 自动开地址相位');
  const a2 = xferOf(parse1('cmd=0x03 addr=0x1000'));
  ok(a2.addrLen === 3 && (a2.tcfg & P.TC.ADDR_EN), '只给 addr → 地址字节数缺省 3（NOR 的常识）');

  const q = xferOf(parse1('xfer cmd=0x6B addr=0 addrl=3 dummy=1 lines=4 rx=492'));
  ok((q.tcfg & P.TC.LINES_MASK) === P.TC.LINES_4 && P.tcfgToLines(q.tcfg) === 4, 'lines=4 落进 tcfg 低两位');
  ok(q.dummy === 1, 'dummy 落位');
  const t = xferOf(parse1('cmd=0x02 tx=aa,bb addrl=3 addr=0x10'));
  ok(hexOf(t.tx) === 'aa bb' && t.txLen === 2, 'tx 数据 + 长度一致');

  const fl = parse1('cmd=0x0B addr=0 addrl=3 dummy=1 rx=16 cs_hold dma addrquad token');
  ok((fl.flags & P.F.CS_HOLD) && (fl.flags & P.F.FORCE_DMA), 'flags：cs_hold / dma');
  const tcfg = xferOf(fl).tcfg;
  ok((tcfg & P.TC.ADDR_QUAD) && (tcfg & P.TC.TOKEN_EN), 'tcfg：addrquad / token');

  const dc = xferOf(parse1('cmd=0x2C tx=00,00 addrl=4 addr=0x2c dc1'));
  ok((dc.tcfg & P.TC.DC_EN) && (dc.tcfg & P.TC.DC_LEVEL), 'dc1 = DC_EN|DC_LEVEL（数据）');
  const dc0 = xferOf(parse1('cmd=0x2A dc'));
  ok((dc0.tcfg & P.TC.DC_EN) && !(dc0.tcfg & P.TC.DC_LEVEL), 'dc = 只开 DC_EN（命令）');

  const noCmd = xferOf(parse1('lines=4 rx=492'));
  ok(noCmd.cmd === 0 && !(noCmd.tcfg & P.TC.CMD_EN) && noCmd.rxLen === 492, '不发 cmd 的续读帧（cmd_en=0）');

  const short = parse1('cmd=0x05 rx=1') && xferOf(parse1('cmd=0x05 rx=1'));
  ok(short.rxLen === 1, 'rx=1 的短读');
}

// ==================================================================== 4
console.log('== 4. 其余关键字帧 ==');
{
  const d = parse1('delay 120ms');
  ok(d.type === P.T.DELAY && new DataView(d.payload.buffer).getUint32(0, true) === 120000, 'delay 120ms → DELAY 帧（µs）');
  const g = parse1('gpio DC 1');
  ok(g.type === P.T.GPIO && g.payload[0] === P.LINE.DC && g.payload[1] === 1, 'gpio DC 1');
  ok(parse1('gpio rst 0').payload[0] === P.LINE.RST, 'gpio 线名大小写不敏感');
  ok(parse1('gpio 3 1').payload[0] === P.LINE.BL, 'gpio 也吃协议里的数字线号');
  ok(parse1('cs low').payload[0] === 1, 'cs low = 占用（payload 1）');
  ok(parse1('cs high').payload[0] === 0, 'cs high = 释放（payload 0）');
  ok(parse1('reset 10 120').payload.length === 4, 'reset 10 120');
  const r2 = parse1('reset 5');
  ok(new DataView(r2.payload.buffer).getUint16(2, true) === 120, 'reset 只给一个数 → post 缺省 120ms');
  ok(parse1('ping').type === P.T.PING && parse1('auxin').type === P.T.AUX_IN, 'ping / auxin');

  const s = parse1('step cmd=0x11 delay=120');
  const sd = new DataView(s.payload.buffer, s.payload.byteOffset, s.payload.byteLength);
  ok(s.type === P.T.STEP && s.payload[0] === 0x11 && s.payload[1] === 0 && sd.getUint16(2, true) === 120, 'step = cmd/nparams/delay_ms');
  const s2 = parse1('step cmd=0x36 tx=00,01 delay=10');
  ok(s2.payload[1] === 2 && hexOf(s2.payload.subarray(4)) === '00 01', 'step 的 tx 就是参数');
}

// ==================================================================== 5
console.log('== 5. 三条自动规则（RSP 归谁）==');
{
  const r1 = D.parseFrames('0x05 rx=1\n0x05 rx=1');
  ok((r1.items[0].flags & P.F.RSP) && (r1.items[1].flags & P.F.RSP), '两条读帧都各自带 RSP（rx>0 必带）');

  const r2 = D.parseFrames('0x11\n0x29\ndelay 10ms');
  ok(!(r2.items[0].flags & P.F.RSP) && !(r2.items[1].flags & P.F.RSP), '没人要应答时不乱加 RSP');
  ok(r2.items[2].flags & P.F.RSP, '整段零 RSP → 末帧补一个（哪怕是 DELAY）');

  const r3 = D.parseFrames('0x11 rsp\n0x29');
  ok((r3.items[0].flags & P.F.RSP) && !(r3.items[1].flags & P.F.RSP), '自己写了 rsp → 不再自动补末帧');

  const r4 = D.parseFrames('0x11\n0x29');
  ok(r4.items[1].autoRsp === true && r4.items[0].autoRsp === undefined, 'autoRsp 标记只落在被自动补的那条上');
}

// ==================================================================== 6
console.log('== 6. 错误必须在解析期带行号拦住 ==');
{
  const cases = [
    ['xfer cmd=0x02 tx=00,01 addrl=3 addr=0 rx=3', /等长/, '全双工不等长'],
    ['xfer lines=3 rx=1', /lines/, 'lines 只吃 1/2/4'],
    ['xfer cmd=0x03 addr=0 addrl=5', /addrl/, 'addrl 超范围'],
    ['xfer cmd=0x03 addr=0 addrl=0', /addrl=0/, '给了 addr 却 addrl=0'],
    ['xfer cmd=0x03 dummy=9', /dummy/, 'dummy 超范围'],
    ['xfer cmd=0x03 bogus=1', /不认识的 key/, '未知 key'],
    ['xfer cmd=0x03 cmd=0x04', /写了两次/, '同名 key 重复'],
    ['xfer cmd=0x03 rx=-1', /不是数字/, '负数/非法数字'],
    ['xfer tx=00,0', /成对/, '奇数位十六进制'],
    ['xfer', /什么都没干/, '空帧'],
    ['xfer cmd="0x03', /引号/, '引号没配对'],
    ['gpio TE 1', /输入脚/, 'TE 是输入不能写'],
    ['gpio XX 1', /认不出线名/, '线名不认识'],
    ['gpio DC', /要跟/, 'gpio 少参数'],
    ['gpio DC 2', /只吃 0 \/ 1/, 'gpio 电平非法'],
    ['cs maybe', /low \/ high/, 'cs 值非法'],
    ['delay', /要跟且只跟一个时长/, 'delay 少参数'],
    ['delay 10ms 20ms', /要跟且只跟一个时长/, 'delay 多参数'],
    ['reset', /要跟低电平毫秒数/, 'reset 少参数'],
    ['reset 10 20 30', /要跟低电平毫秒数/, 'reset 多参数'],
    ['gpio DC 1 1', /要跟「线 电平」/, 'gpio 多参数'],
    ['ping x', /不带参数/, 'ping 多参数'],
    ['foo bar', /认不出这一行/, '整行认不出'],
    ['step delay=10', /少了 cmd=/, 'step 缺 cmd'],
    ['step cmd=0x11 dc1', /不认识的 key/, 'STEP 没有 dc 开关'],
    ['xfer cmd=0x03 tx="00 01" rx=1', /等长/, '引号里的 tx 也参与等长校验'],
  ];
  for (const [src, re, name] of cases){
    const e = errOf(src);
    ok(e && re.test(e.msg), `${name} → 报错`, e ? `（收到：${e.msg}）` : '（没报错）');
  }

  const e = errOf('# 注释\n\n0x11\nxfer bogus=1');
  ok(e && e.line === 4, `报错行号对准源文件（第 ${e?.line} 行，前面有注释和空行）`, JSON.stringify(e));
  ok(e && e.text === 'xfer bogus=1', '错误里带上原行文本');

  const big = 'xfer cmd=0x02 tx=' + 'aa,'.repeat(493).slice(0, -1);
  const eb = errOf(big);
  ok(eb && /超过单帧上限/.test(eb.msg), 'tx 超过 492 B → 报错（不静默截断）');

  const many = D.parseFrames('0x11\nbogus=1\n0x29');
  ok(many.errors.length === 1 && many.items.length === 2, '一行错不影响其它行（错误与有效帧分开返回）');
}

// ==================================================================== 7
console.log('== 7. 整段解析：注释、空行、统计、描述 ==');
{
  const r = D.parseFrames(`
    # 一段混合序列
    0x11
    delay 120ms
    gpio DC 0
    xfer cmd=0x2C addr=0 addrl=4 rx=4
  `);
  ok(r.errors.length === 0 && r.items.length === 4, '4 条帧全部解析成功');
  ok(r.stats.xfer === 2 && r.stats.bytes > 0, `统计：${r.stats.xfer} 条 XFER / payload 共 ${r.stats.bytes} B`);
  ok(/4 条帧/.test(D.describeParsed(r)), `摘要：${D.describeParsed(r)}`);

  const empty = D.parseFrames('# 只有注释\n\n   ');
  ok(empty.items.length === 0 && empty.errors.length === 0, '全注释/空行 → 零帧零错');
  ok(D.describeParsed(empty) === '没有可发的帧', '空段的摘要');

  // 打包器能吃下解析结果（帧构造与打包器是同一套尺寸约束）
  const packs = P.packFrames(r.items.map(i => P.frame(i.type, i.payload, { flags: i.flags, seq: 1 })));
  ok(P.checkPacks(packs).length === 0, `解析结果能干净打包（${r.items.length} 帧 → ${packs.length} 包）`);
}

// ==================================================================== 8
console.log('== 8. 面板上的示例片段必须是好的（回归）==');
{
  ok(D.DSL_SAMPLES.length >= 3, `内置 ${D.DSL_SAMPLES.length} 条示例`);
  for (const s of D.DSL_SAMPLES){
    const r = D.parseFrames(s.text);
    ok(r.errors.length === 0 && r.items.length > 0,
      `示例「${s.name}」解析干净（${r.items.length} 条帧）`,
      JSON.stringify(r.errors));
  }
  const flash = D.parseFrames(D.DSL_SAMPLES[0].text);
  const id = xferOf(flash.items[0]);
  ok(id.cmd === 0x9f && id.rxLen === 3, '示例里的 RDID：cmd=0x9F / rx=3');
  const sfdp = xferOf(flash.items[1]);
  ok(sfdp.cmd === 0x5a && sfdp.addrLen === 3 && sfdp.dummy === 1 && sfdp.rxLen === 8, '示例里的 SFDP：0x5A + 3 B 地址 + 1 dummy + 8 B');

  const quad = D.parseFrames(D.DSL_SAMPLES[1].text);
  ok(quad.items.length === 3, '四线连读示例 = 3 条帧');
  ok(P.tcfgToLines(xferOf(quad.items[0]).tcfg) === 4 && (quad.items[0].flags & P.F.CS_HOLD), '首帧：4 线 + CS_HOLD');
  ok(!(xferOf(quad.items[1]).tcfg & P.TC.CMD_EN) && (quad.items[1].flags & P.F.CS_HOLD), '续读帧：不发 cmd + CS_HOLD');
  ok(quad.items[2].flags & P.F.CS_OFF, '末帧：CS_OFF 释放');
}

// ==================================================================== 9
console.log('== 9. C 表行（列序 = 页面命令表）与导出/回灌 ==');
{
  const x = xferOf(parse1('{0x9F, 1, 0, 0x000000, 0, 3, NULL},'));
  ok(x.cmd === 0x9f && x.addrLen === 0 && x.rxLen === 3 && x.txLen === 0, 'C 行：cmd/rx 落位，tx=NULL');
  ok((x.tcfg & P.TC.CMD_EN) && !(x.tcfg & P.TC.ADDR_EN), 'C 行：cmd 相位开、地址相位关');

  const w = xferOf(parse1('{0x02, 1, 3, 0x001000, 0, 0, (uint8_t[]){0xAA, 0xBB}},'));
  ok(w.addr === 0x1000 && w.addrLen === 3 && hexOf(w.tx) === 'aa bb', 'C 行：地址 + (uint8_t[]){…} 数据');
  ok(P.tcfgToLines(xferOf(parse1('{0x03, 4, 3, 0x100, 1, 492, NULL}')).tcfg) === 4, 'C 行：4 线');
  ok(xferOf(parse1('{0x05, 1, 0, 0, 0, 1, "EE"}')).rxLen === 1, 'C 行：tx 也能写成字符串（这里是读帧，tx 忽略）');

  const cases = [
    ['{0x9F, 1, 0, 0, 0}', /6~7 列/, '列数不够'],
    ['{0x9F, 3, 0, 0, 0, 3, NULL}', /lines/, 'lines 非法'],
    ['{0x9F, 1, 9, 0, 0, 3, NULL}', /addr_len/, 'addr_len 超范围'],
    ['{0x9F, 1, 0, 0, 0, 505, NULL}', /rx_len/, 'rx_len 超范围'],
    ['{0x11, 1, 0, 0, 0, 0, (uint8_t[]){0xAA}}', /tx/, 'tx 非法…（这一条其实是合法的 0xAA）'],
  ];
  for (const [src, re, name] of cases.slice(0, 4)){
    const e = errOf(src);
    ok(e && re.test(e.msg), `${name} → 报错`, e ? `（${e.msg}）` : '（没报错）');
  }
  const e = errOf('{0x11, 1, 0, 0, 0, 0, (uint8_t[]){0xGG}}');
  ok(e && /tx/.test(e.msg), 'tx 里有非法十六进制 → 报错');

  // 导出 C 表 → 再解析回来：帧必须一模一样
  const src = D.parseFrames('0x9F rx=3\nxfer cmd=0x03 addr=0x1000 addrl=3 rx=64\nxfer cmd=0x02 addr=0x2000 addrl=3 tx="aa bb"');
  const c = D.itemsToC(src.items);
  ok(/^\{0x9f, 1, 0, 0x000000, 0, 3, NULL\},/m.test(c), `导出的 C 表列序正确：${c.split('\n')[1]}`);
  ok(/\(uint8_t\[\]\)\{0xaa, 0xbb\}/.test(c), '导出的 tx 是 (uint8_t[]){…}');
  const back = D.parseFrames(c);
  ok(back.errors.length === 0 && back.items.length === src.items.length, `导出的 C 表能再解析（${back.items.length} 条）`);
  ok(back.items.every((it, i) => hexOf(it.payload) === hexOf(src.items[i].payload)), 'C 表往返后每帧字节完全一致');

  // 导出的 DSL 文本 → 再解析：也要一致（含各开关）
  const mix = D.parseFrames('xfer cmd=0x6B addr=0 addrl=3 dummy=1 lines=4 rx=492 cs_hold\nxfer lines=4 rx=100 cs_off\ndelay 120ms\ngpio DC 1\ncs low\nreset 5 20\nping');
  const dsl = D.itemsToDsl(mix.items);
  const mix2 = D.parseFrames(dsl);
  ok(mix2.errors.length === 0 && mix2.items.length === mix.items.length, `DSL 导出能再解析（${mix2.items.length} 条）`);
  ok(mix2.items.every((it, i) => it.type === mix.items[i].type && hexOf(it.payload) === hexOf(mix.items[i].payload) && it.flags === mix.items[i].flags),
    'DSL 往返后每帧的 type/payload/flags 完全一致（含 cs_hold / cs_off）');

  // JSON 保真 + 回灌
  const j = D.itemsToJson(mix.items);
  const arr = JSON.parse(j);
  ok(arr.length === mix.items.length && arr[0].cmd === 0x6b && arr[0].lines === 4, `JSON 导出含全部帧（${arr.length} 条）`);
  ok(arr.some(o => o.t === 'delay' && o.us === 120000) && arr.some(o => o.t === 'gpio'), 'JSON 里 delay/gpio 都在');
  const fromJson = D.parseFrames(D.jsonToDsl(j));
  ok(fromJson.errors.length === 0 && fromJson.items.length === mix.items.length, 'JSON 回灌成 DSL 后条数一致');
  ok(D.jsonToDsl('不是 json') === null && D.jsonToDsl('{}') === null, '不是我们的 JSON 形状 → 返回 null（调用方原样载入）');

  // 非 XFER 帧在 C 表里写成注释（不能静默丢）
  const withDelay = D.itemsToC(D.parseFrames('0x11\ndelay 10ms').items);
  ok(/^\/\/ delay 10000us\s+← 不是 XFER/m.test(withDelay), `C 表里用注释标出放不下的帧：${withDelay.split('\n').filter(l => l.startsWith('//')).pop()}`);
}

console.log(`\n${fail ? '❌' : '✅'} spi-frames-dsl.test: ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
