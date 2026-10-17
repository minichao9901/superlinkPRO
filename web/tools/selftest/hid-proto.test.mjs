/**
 * akaLinkPro 自定义 HID 协议的纯 Node 自测（不需要浏览器/硬件）：
 *   node tools/selftest/hid-proto.test.mjs
 * 覆盖：组包（长度/命令/字段小端）、状态字解析（位域与有符号返回码）、
 *       返回码文案、以及"假探针"的完整流程（启动 → 排队 → 状态 → 运行中 → 停止）。
 */
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const app = join(here, '..', '..', 'app');
const load = p => import('file://' + p.replace(/\\/g, '/'));

const {
  CMD, RTT_ACT, PAYLOAD, buildRequest, rttData, rttConfigData,
  parseStatus, startRcText, ascii, USAGE_PAGE, VID, PID, AkaLinkHid,
} = await load(join(app, 'hid', 'probe.js'));
const { MockAkaLinkHid } = await load(join(app, 'hid', 'mock.js'));

let pass = 0, fail = 0;
const ok = (cond, name, extra = '') => {
  if (cond){ pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${extra ? '  → ' + extra : ''}`); }
};

console.log('== 1. 组包（对齐协议文档的字节位）==');
{
  const p = buildRequest(CMD.RTT, rttData(RTT_ACT.START, 0x20000000, 0x10000, 3));
  ok(p.length === 63, `payload 63 字节（Report ID 由 WebHID 单独传），实际 ${p.length}`);
  ok(p[0] === 11, `Data Length = 1+10 = 11，实际 ${p[0]}`);
  ok(p[1] === 0x31, 'Command = 0x31');
  ok(p[2] === 1, 'action = 1（START）');
  const dv = new DataView(p.buffer);
  ok(dv.getUint32(3, true) === 0x20000000, '目标地址小端 @payload[3..6]');
  ok(dv.getUint32(7, true) === 0x10000, '搜索长度小端 @payload[7..10]');
  ok(p[11] === 3, '通道号 @payload[11]');
  ok(p[12] === 0 && p[62] === 0, '其余字节补 0');

  const q = buildRequest(CMD.RTT, rttData(RTT_ACT.STATUS));
  ok(q[0] === 11 && q[2] === 2, '查状态也是 10 字节 data（action=2）');

  const c = buildRequest(CMD.RTT, rttConfigData({ clockHz: 60000000, chunkBytes: 1024, discard: true }));
  ok(c[2] === 7 && c[0] === 10, `Action 7（调参）data 同样以 action 开头，Data Length = 1+9 = 10（实际 ${c[0]}）`);
  const cdv = new DataView(c.buffer);
  ok(cdv.getUint32(3, true) === 60000000, 'SWD 时钟 Hz @payload[3..6]');
  ok(cdv.getUint16(7, true) === 1024, '块读字节 @payload[7..8]');
  ok(c[9] === 1 && c[10] === 0xff, '标志位(bit0=丢弃) / delay 覆盖=0xFF');

  const m = buildRequest(CMD.MODEL);
  ok(m[0] === 1 && m[1] === 0x10 && m[2] === 0, '简单查询：Data Length = 1，无数据');
}

console.log('== 2. 状态字解析 ==');
{
  const w = new Uint32Array(12);
  w[0] = 0x01 | (2 << 8) | (1 << 16) | (7 << 24);   // 运行 | 通道 2 | SWD 就绪 | clock_delay 7
  w[1] = 0x2000012c; w[2] = 0x20000144; w[3] = 123456;
  w[4] = 37 | (12 << 16);                            // 轮询 37 / 搬运 12
  w[5] = 3 | (4 << 16);                              // 读错 3 / RdOff 错 4
  w[6] = 512 | (5 << 16);                            // 上次搬运 512 / 空环 5
  w[7] = 9 | (1 << 16);                              // 让路 9 / 重扫 1
  w[8] = 0x02; w[9] = 0xdeadbeef;
  w[10] = 0xfffffffd;                                // -3 没找到控制块（符号扩展）
  w[11] = 512 | (1 << 16) | (60 << 24);              // 块读 512 | 丢弃 | 档位 60MHz
  const st = parseStatus(new Uint8Array(w.buffer));
  ok(st.running && st.channel === 2 && st.swdReady && st.clockDelay === 7, 'word[0] 的四个位域');
  ok(st.cbAddr === 0x2000012c && st.upAddr === 0x20000144 && st.moved === 123456, '控制块/上行缓冲/已搬运');
  ok(st.polls === 37 && st.transfers === 12, '轮询次数 / 搬运次数');
  ok(st.rdErr === 3 && st.wrErr === 4, '读错误 / RdOff 写错误');
  ok(st.lastChunk === 512 && st.emptyRing === 5, '上次搬运字节 / 空环次数');
  ok(st.dapYield === 9 && st.rescans === 1, '让路次数 / 重扫次数');
  ok(st.lastCmdId === 2 && st.lastResp === 0xdeadbeef, '最近 DAP 命令/响应');
  ok(st.startRc === -3, `返回码符号扩展（低 8 位 -3），实际 ${st.startRc}`);
  ok(st.chunkBytes === 512 && st.discard === true && st.swdMhz === 60, '块读字节 / 丢弃模式 / SWD 档位');

  ok(startRcText(0) === '正常', 'rc=0 文案');
  ok(/没找到 RTT 控制块/.test(startRcText(-3)), 'rc=-3 文案');
  ok(/SWD 时钟/.test(startRcText(-1)) && /SWD 初始化/.test(startRcText(-2)), 'rc=-1/-2 文案');
  /**
   * 同一串返回码，两条后端要分开说人话（2026-10 真机：-2 被一律写成"SWD 初始化失败"，
   * 结果一条 JTAG/DMI 故障被当成接线问题查了半天）。
   */
  ok(/SWD 时钟/.test(startRcText(-1, false)) && /JTAG 时序/.test(startRcText(-1, true)), 'rc=-1 文案按后端分家');
  ok(/SWD 初始化/.test(startRcText(-2, false)) && /JTAG\/DMI/.test(startRcText(-2, true)), 'rc=-2 文案按后端分家');
  ok(/AXI SRAM/.test(startRcText(-3, false)) && /_SEGGER_RTT/.test(startRcText(-3, true)), 'rc=-3 文案按后端分家');
  ok(/该档位链路不可用$/.test(startRcText(-4, false)) && /DMI 无应答/.test(startRcText(-4, true)), 'rc=-4 文案按后端分家');
}

console.log('== 3. 字符串回包 ==');
{
  const buf = new Uint8Array([14, CMD.MODEL, ...'akaLink CMSIS-DAP'.split('').map(c => c.charCodeAt(0)), 0, 0]);
  ok(ascii(buf.subarray(2)) === 'akaLink CMSIS-DAP', `型号解析：${ascii(buf.subarray(2))}`);
}

console.log('== 4. 假探针：完整流程（没硬件也能验面板逻辑）==');
{
  const m = new MockAkaLinkHid();
  ok(m.connected && m.label.includes('mock'), 'mock 就绪');
  const info = await m.info();
  ok(/mock/.test(info.model) && info.sn === 'MOCK-0001', 'info() 有型号/SN');

  const a = await m.start({ addr: 0x24000000, size: 0x80000, channel: 0 });
  ok(a.rc === 0 && !a.status.running, 'START 只是排队：第一次查还没 running（与真固件一致）');
  ok(a.status.startRc === -100, `排队期间的返回码 = -100（固件哨兵值，不是错误），实际 ${a.status.startRc}`);
  ok(/启动中/.test(startRcText(-100)), 'rc=-100 的文案是「启动中」而不是报错');
  const b = await m.status();
  ok(b.status.running && b.status.cbAddr === 0x24000000, `下一拍就起来了，控制块 ${'0x' + b.status.cbAddr.toString(16)}`);
  ok(b.status.swdMhz === 45, '默认档位 45 MHz');

  await m.configure({ clockHz: 60000000, chunkBytes: 2048 });
  const c = await m.status();
  ok(c.status.swdMhz === 60 && c.status.chunkBytes === 2048, '运行时调参生效（60MHz / 2048B）');

  const d = await m.stop();
  ok(!d.status.running, '停止后不再运行');

  m.failNextStart = -3;
  await m.start({ addr: 0x20000000 });
  const e = await m.status();
  ok(!e.status.running && e.status.startRc === -3, '注入失败：rc=-3 且没跑起来（用于测错误提示）');
  ok(/没找到 RTT 控制块/.test(startRcText(e.status.startRc)), '错误文案能对上');
}

console.log('== 5. 常量与设备匹配 ==');
{
  ok(USAGE_PAGE === 0xff00, 'HID usage page = 0xFF00');
  ok(VID === 0x0d28 && PID === 0x0204, `VID:PID = ${VID.toString(16)}:${PID.toString(16)}（akaLinkPro 复合设备）`);
  ok(PAYLOAD === 63, 'payload = 64 - 1（Report ID）');
  ok(CMD.RTT === 0x31 && CMD.MODEL === 0x10 && CMD.FW_VER === 0x13 && CMD.DFU === 0xff, '命令码抽查');
  const acts = [RTT_ACT.STOP, RTT_ACT.START, RTT_ACT.STATUS, RTT_ACT.AUTOSTART, RTT_ACT.CONFIG, RTT_ACT.BENCH_RESULT];
  ok(acts.join(',') === '0,1,2,3,7,9', '动作码与 api_param.c 一致：' + acts.join(','));
}

console.log('== 6. 已授权设备里挑探针（别挑到触摸板）==');
{
  // 真机踩过：Synaptics 触摸板的 collections 里也有 vendor-defined 0xFF00，
  // 而且排在列表前面 —— 只按 usage page 找就会打开触摸板，然后 sendReport 报
  // "Failed to write the report"（看着像探针坏了）。必须**先按 VID/PID 精确匹配**。
  const dev = (productName, vendorId, productId, pages) => ({
    productName, vendorId, productId, collections: pages.map(usagePage => ({ usagePage })),
  });
  const touchpad = dev('HID Miniport Device', 0x06cb, 0x000f, [0x0d, 0xff00]);
  const probe = dev('akaLinkPro CMSIS-DAP', 0x0d28, 0x0204, [0xff00]);
  const other = dev('Some Keyboard', 0x1234, 0x5678, [0xff00]);
  ok(AkaLinkHid.pick([touchpad, probe, other]) === probe, '列表里有触摸板 → 仍然挑中 akaLinkPro');
  ok(AkaLinkHid.pick([probe]) === probe, '只有探针时正常挑中');
  ok(AkaLinkHid.pick([touchpad, other]) === touchpad, '没有探针时退回"按 usage page"（上层会提示可能选错）');
  ok(AkaLinkHid.pick([]) === null && AkaLinkHid.pick(undefined) === null, '列表为空/未定义 → null（不抛）');
  const p = new AkaLinkHid();
  p.device = probe;
  ok(p.isProbe === true, 'isProbe：探针 → true');
  p.device = touchpad;
  ok(p.isProbe === false, 'isProbe：触摸板 → false（上层据此发警告）');
}

console.log('== 7. xfer 超时定时器：旧请求的残雷不许打掉在飞的新请求 ==');
{
  // 代码审查复现过的 bug：响应到达后 setTimeout 不清、超时回调又按 cmd 匹配 ——
  // 早先那条请求的残雷触发时 reject 落在旧 promise 上（无效），却把 this._pending 一起打掉，
  // **当前在飞的那条请求就永久挂起**（既不 resolve 也不 reject，迟到真回包也被丢）。
  // 高频同命令轮询（RTT 转发每 2 s 的 status、J-Scope 启动每 120 ms）撞上就是"点按钮没反应也不报错"。
  const hid = new AkaLinkHid();
  const listeners = {};
  const fake = {
    opened: true,
    addEventListener: (ev, fn) => { (listeners[ev] ||= []).push(fn); },
    removeEventListener: () => {},
    sendReport: async () => {},
  };
  // 不走 open()：它要 navigator.hid 的 disconnect 监听（Node 里没有）。这里只挂 inputreport。
  hid.device = fake;
  fake.addEventListener('inputreport', e => hid._handleInput(e));
  const input = payload => { for (const fn of listeners.inputreport || []) fn({ device: fake, data: { buffer: payload.buffer } }); };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const reply = Uint8Array.of(0, 0x32, 0, 0);          // payload[1] = cmd 0x32

  const r1 = hid.xfer(0x32, Uint8Array.of(0), 100).then(() => 'ok1', e => 'err1:' + e.message);
  await sleep(10);
  input(reply);                                         // 第一条 10 ms 就正常回包（它的定时器必须被清掉）
  const out1 = await r1;
  await sleep(20);
  const r2 = hid.xfer(0x32, Uint8Array.of(0), 5000).then(() => 'ok2', e => 'err2:' + e.message);
  const t = setTimeout(() => input(reply), 250);         // 第二条要 250 ms 才回（比第一条的 100 ms 残雷晚）
  const out2 = await Promise.race([r2, sleep(3000).then(() => 'HANG')]);
  clearTimeout(t);
  ok(out1 === 'ok1', `第一条正常返回（${out1}）`);
  ok(out2 === 'ok2', `第二条没被旧请求的超时残雷误杀（${out2}）`);
  ok(hid._pending === null, '请求结束后 _pending 已清空');
  // 反向：真超时仍然要报错，并且把在飞状态清干净（不能卡住后续请求）
  const r3 = hid.xfer(0x32, Uint8Array.of(0), 60).then(() => 'ok3', e => 'err3');
  const out3 = await r3;
  ok(out3 === 'err3', `真超时仍会报错（${out3}）`);
  ok(hid._pending === null, '超时后 _pending 清空（通道等待迟到响应重新同步）');
  input(reply); // Drain the timed-out command before sending another command with the same opcode.
  const r4 = hid.xfer(0x32, Uint8Array.of(0), 1000).then(() => 'ok4', e => 'err4:' + e.message);
  setTimeout(() => input(reply), 10);
  ok(await r4 === 'ok4', '超时之后还能继续正常请求');
}

console.log(`\n${fail ? 'FAIL' : 'OK'}  ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
