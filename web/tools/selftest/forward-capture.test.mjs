/**
 * 「RTT 转发」验收采集接口的离线回归（不需要浏览器、探针、目标板或真实串口）。
 *
 * 被测对象：`web/app/hid/stream.js` 的 `captureForwardWindow()` 与 `forwardPortOptions()`。
 * 覆盖验收 runner 真正依赖的四类边界：
 *   1. STOP 迟到数据 —— STOP 返回后 CDC/USB 里的在途字节还在到，必须收齐到最终 moved 才结束，
 *      且**尾部字节不能混进 bytesPerSecond**（测速只算 START→STOP 完成这段）；
 *   2. 未确认 STOP —— 生产 HidView.stop() 抛「停止超时」时不吞异常，如实报 stopError/
 *      stop.running=true/canRestart=false，并且**不关串口、不关 HID**；
 *   3. 限额 —— 参数区间直接拒绝，超 maxMiB 只落截断的 .bin 并报 limitExceeded；
 *   4. 连续起停 —— 同一页同一端口连续两轮 START/STOP 各自完整，每轮都能 canRestart。
 * 另外核对 `forwardPortOptions()` 只返回 getInfo() 有真实 VID/PID 的端口、排除演示串口。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { parseHTML } from 'linkedom';
import { Bus } from '../../app/core/bus.js';
import { RttCdcStreamView } from '../../app/hid/stream.js';
import { createWorkbenchAutomation } from '../../app/automation/workbench.js';

/** 下载拦截：download() 会 createObjectURL + <a>.click()，离线环境里换成记账 */
function interceptDownloads(){
  const blobs = [];
  const saved = {
    create: URL.createObjectURL, revoke: URL.revokeObjectURL,
    doc: globalThis.document, tools: globalThis.__tools, location: globalThis.location,
  };
  URL.createObjectURL = b => { blobs.push(b); return 'blob:offline/' + blobs.length; };
  URL.revokeObjectURL = () => {};
  globalThis.document = {
    createElement: () => ({ href: '', download: '', click(){}, remove(){} }),
    body: { appendChild(){} },
  };
  return {
    blobs,
    async read(i = 0){ return new Uint8Array(await blobs[i].arrayBuffer()); },
    restore(){
      URL.createObjectURL = saved.create;
      URL.revokeObjectURL = saved.revoke;
      globalThis.document = saved.doc;
      globalThis.__tools = saved.tools;
      if (saved.location === undefined) delete globalThis.location; else globalThis.location = saved.location;
    },
  };
}

/**
 * 假环境：Bus 当 SerialSession（已由"上游"打开），假 HidView 提供 start()/stop()。
 * `plan.steps` 按时间推进窗口内的字节与探针 moved；`plan.late` 模拟 STOP 之后才到的在途字节。
 */
function harness(plan = {}){
  const session = new Bus();
  session.isOpen = true;
  session.port = { getInfo: () => ({ usbVendorId: 0x1209, usbProductId: 0x0001 }) };
  let closeCalls = 0, disconnectCalls = 0;
  session.close = async () => { closeCalls++; };
  const hid = {
    dev: { connected: plan.hidConnected !== false },
    last: null, mock: null, _bridgeRequested: false, _statusError: null,
    starts: 0, stops: 0,
    async start(){
      hid.starts++;
      hid._bridgeRequested = true;
      hid.last = { running: true, moved: 0, rdErr: 0, wrErr: 0, cbAddr: 0x20000000,
        upAddr: 0x20000000, channel: 0, chunkBytes: 512, targetType: 2, swdMhz: 20,
        transfers: 0, polls: 0, emptyRing: 0, dapYield: 0, rescans: 0, startRc: 0, discard: false };
      if (plan.noRun) hid.last = { ...hid.last, running: false, startRc: -100 };
      for (const step of plan.steps || []){
        setTimeout(() => {
          if (step.emit) session.emit('data', new Uint8Array(step.emit).fill(step.byte ?? 0x5a), new Date());
          if (step.moved) hid.last = { ...hid.last, moved: hid.last.moved + step.moved };
        }, step.at);
      }
      return plan.startResult;                 // 成功的生产语义就是 undefined（见 HidView.start）
    },
    async stop(){
      hid.stops++;
      if (plan.stopError){ throw Error(plan.stopError); }
      hid._bridgeRequested = false;
      hid.last = { ...hid.last, running: false, cbAddr: 0, upAddr: 0 };
      for (const step of plan.late || []){
        setTimeout(() => {
          if (step.emit) session.emit('data', new Uint8Array(step.emit).fill(step.byte ?? 0x33), new Date());
          if (step.moved) hid.last = { ...hid.last, moved: hid.last.moved + step.moved };
        }, step.at);
      }
      return undefined;
    },
    async disconnect(){ disconnectCalls++; },
  };
  globalThis.__tools = { hid };
  const view = new RttCdcStreamView(session);
  view.$ = () => null;                       // 离线：没有 DOM 状态行
  return { view, hid, session, calls: () => ({ closeCalls, disconnectCalls }) };
}

test('captureForwardWindow: late bytes after STOP are drained, not counted in bytesPerSecond', async () => {
  const io = interceptDownloads();
  const { view, hid, session, calls } = harness({
    steps: [
      { at: 5, emit: 100, moved: 100 },
      { at: 30, emit: 100, moved: 100 },
      { at: 50, moved: 100 },             // 已搬进 CDC 缓冲、还没到浏览器
    ],
    late: [{ at: 350, emit: 100 }],       // A slow but valid in-flight tail after STOP
  });
  try {
    const r = await view.captureForwardWindow({ seconds: 0.1, maxMiB: 1, drainMs: 3000 });
    assert.equal(r.started, true, 'bridge START confirmed');
    assert.equal(r.complete, true, 'window drained to the probe final moved');
    assert.equal(r.bytes, 300); assert.equal(r.observedBytes, 300);
    assert.equal(r.stop.running, false);
    assert.equal(r.stop.moved, 300);
    assert.equal(r.stop.rdErr, 0); assert.equal(r.stop.wrErr, 0);
    // 真实桥状态字段（Python 用来和 COM 侧对账）
    assert.equal(r.start.cbAddr, 0x20000000);
    assert.equal(r.stop.cbAddr, 0);
    assert.equal(r.stop.chunkBytes, 512);
    assert.equal(r.stop.targetType, 2);
    assert.equal(r.stop.swdMhz, 20);
    assert.equal(r.canRestart, true);
    // 测速只覆盖实收窗口：180ms 才到的那 100 B 只进 drainBytes
    assert.ok(r.windowBytes >= 100 && r.windowBytes <= 200, `window bytes ${r.windowBytes}`);
    assert.ok(r.drainBytes >= 100, `drain bytes ${r.drainBytes}`);
    assert.equal(r.preWindowBytes + r.windowBytes + r.drainBytes, r.observedBytes);
    assert.ok(Math.abs(r.bytesPerSecond * r.seconds - r.windowBytes) < 1e-6,
      'bytesPerSecond uses the window phase only');
    assert.ok(r.bytesPerSecond * r.seconds < r.observedBytes,
      'drain tail bytes are not mixed into bytesPerSecond');
    assert.ok(r.bytesPerSecond > 0);
    assert.match(r.name, /^rtt-forward-raw-\d{8}-\d{6}\.bin$/);
    // 独立 raw .bin：窗口 + 迟到字节原样落盘
    assert.equal(io.blobs.length, 1);
    const raw = await io.read(0);
    assert.equal(raw.length, 300);
    assert.ok(raw.slice(0, r.windowBytes).every(b => b === 0x5a));
    assert.ok(raw.slice(r.windowBytes).every(b => b === 0x33));
    // 不接管/不关闭任何别的东西
    assert.deepEqual(calls(), { closeCalls: 0, disconnectCalls: 0 });
    assert.equal(session.isOpen, true);
    assert.equal(hid.starts, 1); assert.equal(hid.stops, 1);
  } finally { io.restore(); }
});

test('captureForwardWindow: 60-second window leaves STOP until the window ends', async t => {
  const io = interceptDownloads();
  t.mock.timers.enable({apis:['setTimeout']});
  const { view, hid } = harness({steps:[{at:5,emit:26,moved:26}]});
  try {
    const task = view.captureForwardWindow({seconds:60,maxMiB:64});
    await Promise.resolve();
    t.mock.timers.tick(59_999);
    assert.equal(hid.stops, 0);
    t.mock.timers.tick(1);
    const out = await task;
    assert.equal(hid.stops, 1);
    assert.equal(out.complete, true);
    assert.equal(out.bytes, out.stop.moved);
  } finally {
    io.restore();
  }
});

test('captureForwardWindow: unconfirmed STOP keeps running state and still reports evidence', async () => {
  const io = interceptDownloads();
  const { view, hid, session, calls } = harness({
    steps: [{ at: 5, emit: 300, moved: 300 }],
    stopError: '停止 RTT 转发超时（探针还在运行）',
  });
  try {
    const r = await view.captureForwardWindow({ seconds: 0.1, maxMiB: 1, drainMs: 400 });
    assert.equal(r.complete, false, 'incomplete when STOP is not confirmed');
    assert.match(r.stopError, /停止 RTT 转发超时/);
    assert.equal(r.stop.running, true, 'probe still running after the failed STOP');
    assert.equal(r.stop.moved, 300);
    assert.equal(r.canRestart, false, 'cannot restart while the bridge may still run');
    assert.equal(r.bytes, 300, 'raw evidence is still returned');
    assert.equal(io.blobs.length, 1);
    assert.deepEqual(calls(), { closeCalls: 0, disconnectCalls: 0 });
    assert.equal(session.isOpen, true);
    assert.equal(hid.starts, 1); assert.equal(hid.stops, 1);
  } finally { io.restore(); }
});

test('captureForwardWindow: argument limits are rejected before any bridge action', async () => {
  const io = interceptDownloads();
  const { view, hid } = harness();
  try {
    for (const args of [{ seconds: 0.05 }, { seconds: 61 }, { maxMiB: 0 }, { maxMiB: 65 },
      { maxMiB: 1.5 }, { drainMs: -1 }, { drainMs: 30001 }]){
      await assert.rejects(view.captureForwardWindow(args), /采集窗口|缓存|排空超时/, JSON.stringify(args));
    }
    assert.equal(hid.starts, 0, 'no START on invalid arguments');
    assert.equal(io.blobs.length, 0, 'no download on invalid arguments');
  } finally { io.restore(); }
});

test('captureForwardWindow: maxMiB truncates the .bin and reports limitExceeded', async () => {
  const io = interceptDownloads();
  const MiB = 1024 * 1024;
  const { view } = harness({
    steps: [{ at: 5, emit: 512 * 1024, moved: 512 * 1024 },
      { at: 15, emit: 512 * 1024, moved: 512 * 1024 },
      { at: 25, emit: 512 * 1024, moved: 512 * 1024 }],
  });
  try {
    const r = await view.captureForwardWindow({ seconds: 0.1, maxMiB: 1, drainMs: 400 });
    assert.equal(r.bytes, MiB);
    assert.equal(r.observedBytes, 1.5 * MiB);
    assert.equal(r.limitExceeded, true);
    assert.equal(r.complete, false, 'truncated capture is not complete');
    const raw = await io.read(0);
    assert.equal(raw.length, MiB, 'raw file is capped, observedBytes keeps the truth');
  } finally { io.restore(); }
});

test('captureForwardWindow: bytes beyond final moved are evidence of a bad window', async () => {
  const io = interceptDownloads();
  const { view } = harness({ steps: [{ at: 5, emit: 65, moved: 64 }] });
  try {
    const r = await view.captureForwardWindow({ seconds: 0.1, maxMiB: 1, drainMs: 300 });
    assert.equal(r.stop.moved, 64);
    assert.equal(r.bytes, 65);
    assert.equal(r.complete, false);
    assert.equal((await io.read()).length, 65);
  } finally { io.restore(); }
});

test('captureForwardWindow: refuses to take over a bridge that is already running', async () => {
  const io = interceptDownloads();
  const { view, hid } = harness();
  hid.last = { running: true, moved: 1024 };
  try {
    await assert.rejects(view.captureForwardWindow({ seconds: 0.1 }), /不接管已有桥/);
    assert.equal(hid.starts, 0); assert.equal(hid.stops, 0);
  } finally { io.restore(); }
});

test('captureForwardWindow: consecutive start/stop rounds each stay complete and restartable', async () => {
  const io = interceptDownloads();
  const { view, hid } = harness({
    steps: [{ at: 5, emit: 64, moved: 64 }, { at: 20, emit: 64, moved: 64 }],
  });
  try {
    for (let round = 1; round <= 3; round++){
      const r = await view.captureForwardWindow({ seconds: 0.1, maxMiB: 1, drainMs: 1000 });
      assert.equal(r.started, true, `round ${round} START`);
      assert.equal(r.complete, true, `round ${round} complete`);
      assert.equal(r.bytes, 128);
      assert.equal(r.stop.moved, 128, 'moved restarts from zero every START');
      assert.equal(r.canRestart, true, `round ${round} canRestart`);
      assert.equal(r.stop.running, false);
    }
    assert.equal(hid.starts, 3); assert.equal(hid.stops, 3);
    assert.equal(io.blobs.length, 3, 'one independent raw .bin per round');
  } finally { io.restore(); }
});

test('captureForwardWindow: START failure is cleaned up by STOP and reported without throwing', async () => {
  const io = interceptDownloads();
  const { view, hid } = harness({ startResult: false });
  hid._statusError = 'SWD 初始化失败（查接线 / 目标供电 / 复位）';
  try {
    const r = await view.captureForwardWindow({ seconds: 0.1, maxMiB: 1, drainMs: 300 });
    assert.equal(r.started, false);
    assert.match(r.startError, /SWD 初始化失败/, 'bridge failure text is preserved');
    assert.equal(r.complete, false);
    assert.equal(r.bytes, 0);
    assert.equal(hid.stops, 1, 'production STOP still runs as cleanup');
    assert.equal(io.blobs.length, 1, 'raw evidence file is still produced');
    assert.equal(r.canRestart, true, 'bridge is not running, so a retry is allowed');
  } finally { io.restore(); }
});

test('captureForwardWindow: a queued START that never runs is reported as not started', async () => {
  const io = interceptDownloads();
  const { view, hid } = harness({ noRun: true });
  try {
    const r = await view.captureForwardWindow({ seconds: 0.1, maxMiB: 1, drainMs: 200 });
    assert.equal(r.started, false);
    assert.match(r.startError, /返回码 -100/, 'START_PENDING is evidence, not a silent success');
    assert.equal(r.complete, false);
    assert.equal(hid.stops, 1);
  } finally { io.restore(); }
});

test('forwardPortOptions: only real authorized ports with USB ids, demo excluded', () => {
  const io = interceptDownloads();
  const { view } = harness();
  view.ports = [
    { getInfo: () => ({ usbVendorId: 0x1209, usbProductId: 0x0001 }) },
    { getInfo: () => ({}) },                                    // 无 USB 信息 → 不能匹配
    { getInfo: () => { throw Error('no usb'); } },               // 读不到 → 跳过
    { getInfo: () => ({ usbVendorId: 0x0483, usbProductId: 0x5740 }) },
  ];
  try {
    assert.deepEqual(view.forwardPortOptions(), [
      { index: 0, vendorId: 0x1209, productId: 0x0001 },
      { index: 3, vendorId: 0x0483, productId: 0x5740 },
    ], 'index keeps the #c-port option position');
    globalThis.location = { search: '?demo=serial' };
    assert.deepEqual(view.forwardPortOptions(), [], 'demo ports never count as real');
  } finally { io.restore(); }
});

test('workbench CLI path: page rttcdc invoke captures forwardWindow and returns the raw .bin download', async () => {
  const { view, hid } = harness({
    steps: [{ at: 5, emit: 40, moved: 40 }, { at: 20, emit: 24, moved: 24 }],
  });
  view.ports = [{ getInfo: () => ({ usbVendorId: 0x1209, usbProductId: 0x0001 }) }];
  const { document, window } = parseHTML('<html><body><section id="tab-rttcdc"></section></body></html>');
  const saved = { doc: globalThis.document, url: globalThis.URL, tools: globalThis.__tools };
  // 用原型链借 linkedom 的 window（它是个 Proxy，spread 会丢掉 URL/HTMLAnchorElement）
  const win = Object.create(window);
  win.document = document;
  win.CSS = { escape: s => s };
  win.location = { hash: '' };
  win.atob = v => Buffer.from(v, 'base64').toString('binary');
  win.btoa = v => Buffer.from(v, 'binary').toString('base64');
  globalThis.document = document;
  globalThis.URL = win.URL;                  // 与窗口同一个 URL 对象，workbench 才能接管下载
  const automation = createWorkbenchAutomation({ stream: view, hid, errors: [] }, win);
  try {
    // runner 先 describe 拿方法表，再 invoke —— 两个新方法都必须是公开方法
    const described = await automation.dispatch({ op: 'describe', page: 'rttcdc' });
    const names = described.methods.map(m => m.name);
    assert.ok(names.includes('captureForwardWindow'), 'captureForwardWindow is public');
    assert.ok(names.includes('forwardPortOptions'), 'forwardPortOptions is public');

    const invoked = await automation.dispatch({ op: 'invoke', page: 'rttcdc', method: 'forwardPortOptions', args: [] });
    assert.deepEqual(invoked.value, [{ index: 0, vendorId: 0x1209, productId: 0x0001 }]);

    const run = await automation.dispatch({ op: 'invoke', page: 'rttcdc', method: 'captureForwardWindow',
      args: [{ seconds: 0.1, maxMiB: 1, drainMs: 2000 }] });
    assert.equal(run.returned, true);
    assert.equal(run.value.complete, true);
    assert.equal(run.value.bytes, 64);
    assert.equal(run.value.stop.moved, 64);
    assert.equal(run.value.canRestart, true);
    assert.equal(typeof run.value.bytesPerSecond, 'number');

    const list = await automation.dispatch({ op: 'downloads', page: 'rttcdc' });
    assert.equal(list.length, 1, 'exactly one independent raw download');
    assert.match(list[0].name, /^rtt-forward-raw-.*\.bin$/);
    assert.equal(list[0].bytes, 64);
    const got = await automation.dispatch({ op: 'download', page: 'rttcdc', id: list[0].id, offset: 0, length: 4096 });
    assert.equal(Buffer.from(got.base64, 'base64').length, 64);
  } finally {
    globalThis.document = saved.doc;
    globalThis.URL = saved.url;
    globalThis.__tools = saved.tools;
  }
});
