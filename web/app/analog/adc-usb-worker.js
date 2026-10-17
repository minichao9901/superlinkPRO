/* WebUSB reader for ADC. USB requests and replenishment run away from the UI thread. */
let device = null, iface = null, active = false;
/* 上限要盖得住一次被遗弃的会话排下的全部块：END 是"每笔原生读一个"，最大 32 笔。 */
const RETIRE_IN_LIMIT = 40, RETIRE_IN_TIMEOUT_MS = 30;
/* IN 退场超时留下的那一笔读。它仍然占着端点，ADC 的第一个包会被它吃掉，所以不能丢 ——
 * 留着交给 pump 当**第一笔**读，顺序才不会错位。 */
let pendingRead = null;

function reply(id, value){ self.postMessage({ id, ...value }); }
function sameDevice(d, wanted){
  return d.vendorId === wanted.vendorId && d.productId === wanted.productId &&
    (!wanted.serialNumber || d.serialNumber === wanted.serialNumber);
}
function findInterface(d, wanted){
  const config = d.configurations.find(c => c.configurationValue === 1) || d.configurations[0];
  const entry = config?.interfaces.find(i => i.alternates.some(a =>
    a.endpoints.some(e => e.endpointNumber === 11 && e.direction === 'in' && e.type === 'bulk') &&
    a.endpoints.some(e => e.endpointNumber === 11 && e.direction === 'out' && e.type === 'bulk')));
  if (!entry) throw Error('当前板卡没有 SPI/ADC 共享数据接口');
  if (wanted.interfaceNumber != null && entry.interfaceNumber !== wanted.interfaceNumber)
    throw Error('ADC USB 接口描述与页面保留的接口不一致');
  return entry.interfaceNumber;
}
async function openUsb(wanted){
  if (!navigator.usb?.getDevices){
    const e = Error('当前浏览器的 Dedicated Worker 未开放 WebUSB'); e.code = 'WORKER_USB_UNSUPPORTED'; throw e;
  }
  const matches = (await navigator.usb.getDevices()).filter(d => sameDevice(d, wanted));
  if (matches.length !== 1) throw Error(matches.length ? 'Worker 找到多台同 VID/PID 探针' : 'Worker 看不到已授权的探针');
  const d = matches[0], number = findInterface(d, wanted);
  try {
    if (!d.opened) await d.open();
    if (d.configuration === null) await d.selectConfiguration(1);
    await d.claimInterface(number);
  } catch (error){
    try { if (d.opened) await d.close(); } catch {}
    throw error;
  }
  device = d; iface = number;
  navigator.usb.addEventListener?.('disconnect', event => {
    if (sameDevice(event.device,wanted)) self.postMessage({ event:'fault', message:'探针 USB 已断开' });
  });
  return { interfaceNumber: number };
}
function read(){
  const started=performance.now();
  try { return Promise.resolve(device.transferIn(11,4096)).then(result => ({ result,started,at:performance.now() }), error => ({ error,started,at:performance.now() })); }
  catch(error){ return Promise.resolve({ error,started,at:performance.now() }); }
}
function acquireRead(){
  if (pendingRead){ const p = pendingRead; pendingRead = null; return p; }
  return read();
}
function isEnd(view, token){
  return view.byteLength >= 32 && view.getUint8(0) === 0x41 && view.getUint8(1) === 0x44 &&
    view.getUint8(2) === 0x53 && view.getUint8(3) === 0x32 && view.getUint8(5) === 2 &&
    view.getUint32(8,true) === token;
}
async function pump({ token, inFlight }){
  if (active) throw Error('上一轮 ADC Worker 读取尚未退场');
  active = true;
  const queue = Array.from({ length: inFlight }, () => acquireRead());
  let ends = 0, failed = false, faultSent = false, reads = 0, maxReadMs = 0, lastAt = 0, maxGapMs = 0;
  try {
    while(queue.length){
      const { result, error, started, at:completed } = await queue.shift();reads++;
      maxReadMs=Math.max(maxReadMs,completed-started);
      if(lastAt)maxGapMs=Math.max(maxGapMs,completed-lastAt);
      lastAt=completed;
      if (error || result?.status !== 'ok' || !result.data?.byteLength){
        failed = true;
        if (!faultSent){ faultSent = true; self.postMessage({ event:'fault', message:error?.message || `ADC USB 读取失败：${result?.status}` }); }
        continue;
      }
      const data = result.data;
      const source = new Uint8Array(data.buffer,data.byteOffset,data.byteLength);
      const end = isEnd(new DataView(data.buffer,data.byteOffset,data.byteLength),token);
      if (end) ends++;
      if (!ends && !failed) queue.push(acquireRead());
      // Copy into a transferable buffer; WebUSB owns the original response buffer.
      const frame = source.slice();
      self.postMessage({ event:'packet', buffer:frame.buffer, completedAt:completed },[frame.buffer]);
    }
    self.postMessage({ event:'drained', complete:ends === inFlight, ends, expected:inFlight,
      metrics:{nativeReads:reads,nativePeak:inFlight,maxReadAwaitMs:maxReadMs,maxCompletionGapMs:maxGapMs} });
  } catch(error){
    self.postMessage({ event:'fault', message:error?.message || String(error) });
    self.postMessage({ event:'drained', complete:false, ends, expected:inFlight,
      metrics:{nativeReads:reads,nativePeak:inFlight,maxReadAwaitMs:maxReadMs,maxCompletionGapMs:maxGapMs} });
    } finally { active = false; }
}
/* SPI/QSPI 桥与 ADC 复用同一个 bulk IN 端点。关桥之后端点上可能还挂着一笔已武装的读，
 * 或者环里还排着几笔应答 —— 它们会作为 ADC 流的**第一个包**到达，把流判成
 * 「ADC DMA 数据包格式或任务代数错误」。所以开流之前先把 IN 侧退场：一直读到没有数据。
 * 这一步与 OUT 侧的 ZLP 退场（retireSpiOut）成对，缺一个共享缓冲的交接就不完整。 */
async function retireIn(){
  if (!device) throw Error('ADC Worker USB 尚未打开');
  if (active) throw Error('ADC Worker 仍有 USB 读取');
  const idle = () => new Promise(resolve => setTimeout(() => resolve(null), RETIRE_IN_TIMEOUT_MS));
  let retired = 0;
  while (retired < RETIRE_IN_LIMIT){
    /* 先接手上一次退场留下的那笔读：丢掉它等于让一笔没人管的读占着端点，
     * ADC 的第一个包会被它吃掉，流就从 seq=1 开始 → 判成"数据块不连续"。 */
    const pending = acquireRead();
    const outcome = await Promise.race([pending, idle()]);
    if (outcome === null){ pendingRead = pending; break; }   /* 端点空了：这一笔留给 pump */
    if (outcome.error) throw outcome.error;
    if (outcome.result?.status !== 'ok') throw Error('SPI IN 退场失败');
    if (!outcome.result.data?.byteLength) break;
    retired++;
  }
  return { retired };
}
async function closeUsb(){
  if (active) throw Error('ADC Worker 仍有 USB 读取');
  pendingRead = null;
  if (!device) return { closed:true };
  try { if (device.opened) await device.releaseInterface(iface); }
  finally { try { if (device.opened) await device.close(); } finally { device = null; iface = null; } }
  return { closed:true };
}

self.onmessage = async ({ data }) => {
  const { id, action } = data || {};
  try {
    let value = {};
    if (action === 'open') value = await openUsb(data.device);
    else if (action === 'start') {
      // Submit all reads synchronously before acknowledging; the page then sends ADC START over HID.
      const task = pump(data);
      reply(id,{ ok:true });
      await task;
      return;
    } else if (action === 'in') value = await retireIn();
    else if (action === 'out') {
      if (!device) throw Error('ADC Worker USB 尚未打开');
      const result = await device.transferOut(11,new Uint8Array());
      value = { status:result.status };
    } else if (action === 'close') value = await closeUsb();
    else throw Error(`未知 ADC Worker 操作：${action}`);
    reply(id,{ ok:true,...value });
  } catch(error){
    reply(id,{ ok:false, code:error?.code || null, error:error?.message || String(error) });
  }
};
