import { UsbLease } from '../core/usb-device.js';
import { withTimeout } from '../rtt/dap-webusb.js';
export const ADC_EP=0x8b, ADC_BLOCK_BYTES=4096;
export const ADC_MAX_INFLIGHT=32;
/** 共享缓冲 IN 侧退场的上限与单笔等待时间（端点空闲时只花一次超时）。
 *  上限要盖得住一次被遗弃的会话排下的全部块：END 是"每笔原生读一个"，
 *  最大 32 笔，外加尾块。 */
const RETIRE_IN_LIMIT=40, RETIRE_IN_TIMEOUT_MS=30;
const LITTLE_ENDIAN=new Uint8Array(Uint16Array.of(1).buffer)[0]===1;
export function decodeAdcPacket(bytes,token){
  const v=new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength);
  if(bytes.length<32 || String.fromCharCode(...bytes.subarray(0,4))!=='ADS2' ||
    bytes[4]!==2 || ![1,2].includes(bytes[5]) || ![8,10,12,16].includes(bytes[6]) ||
    bytes[7]!==0 || v.getUint32(8,true)!==token || bytes[26]!==6 || bytes[27]!==2)
    throw Error('ADC DMA 数据包格式或任务代数错误');
  const count=v.getUint16(24,true),rate=v.getUint32(20,true);
  if(count>2031 || bytes.length!==32+2*count || (bytes[5]===2?count!==0:!count) ||
    (count && (!rate || rate>2000000))) throw Error('ADC DMA 数据长度或速率无效');
  // WebUSB owns a distinct buffer per completed request. Keep its u16 view;
  // the bounded scope history copies it once, without another decode allocation.
  const aligned=(bytes.byteOffset+32)%2===0;
  const codes=LITTLE_ENDIAN&&aligned?new Uint16Array(bytes.buffer,bytes.byteOffset+32,count):new Uint16Array(count);
  if(!LITTLE_ENDIAN||!aligned||bytes[6]!==16)for(let i=0;i<count;i++){
    if(!LITTLE_ENDIAN||!aligned)codes[i]=v.getUint16(32+2*i,true);
    if(bytes[6]!==16&&codes[i]>=2**bytes[6])throw Error('ADC DMA 码值超出位宽');
  }
  return {codes,bits:bytes[6],rate,seq:v.getUint32(12,true),first:v.getUint32(16,true),
    fault:v.getUint32(28,true),done:bytes[5]===2};
}
class AdcWorkerTransport {
  constructor(device,iface){
    this.device=device;this.lease=new UsbLease(device,'analog');this.iface=iface;
    this.done=false;this.error=null;this.pending=null;this._rpcId=0;this._rpcPending=new Map();
    this.worker=new Worker(new URL('./adc-usb-worker.js',import.meta.url),{type:'module'});
    this.worker.onmessage=event=>this._message(event.data);
    this.worker.onerror=event=>this._workerError(event.message||'ADC Worker 异常');
  }
  _rpc(action,args={}, {native=false}={}){
    const id=++this._rpcId;
    return new Promise((resolve,reject)=>{
      // A native OUT timeout must not discard its eventual completion.
      const timer=native?null:setTimeout(()=>{this._rpcPending.delete(id);reject(Error(`ADC Worker ${action} 超时`));},10000);
      this._rpcPending.set(id,{resolve,reject,timer});this.worker.postMessage({id,action,...args});
    });
  }
  _message(message){
    if(message.id){
      const p=this._rpcPending.get(message.id);if(!p)return;
      this._rpcPending.delete(message.id);clearTimeout(p.timer);
      if(message.ok)p.resolve(message);else{const e=Error(message.error||'ADC Worker 请求失败');e.code=message.code;p.reject(e);}
      return;
    }
    if(message.event==='fault'){this.usbFailed=true;this._fault(Error(message.message||'ADC Worker USB 失败'));return;}
    if(message.event==='packet'){this._packet(new Uint8Array(message.buffer),message.completedAt);return;}
    if(message.event==='drained'){
      this.done=!!message.complete;
      this.metrics=message.metrics||null;
      if(!this.done&&!this.error)this._fault(Error(`ADC USB 读取只退场 ${message.ends}/${message.expected} 笔`));
      this._settled=true;const resolve=this._resolvePending;this._resolvePending=null;this.pending=null;resolve?.();
    }
  }
  _workerError(message){
    this.usbFailed=true;this._fault(Error(`ADC Worker 失败：${message}`));this.done=false;
    this._settled=true;const resolve=this._resolvePending;this._resolvePending=null;this.pending=null;resolve?.();
    for(const [id,p] of this._rpcPending){clearTimeout(p.timer);p.reject(Error(message));this._rpcPending.delete(id);}
  }
  static async request(device){
    const config=device.configurations.find(c=>c.configurationValue===1)||device.configurations[0];
    const iface=config?.interfaces.find(i=>i.alternates.some(a=>
      a.endpoints.some(e=>e.endpointNumber===11&&e.direction==='in'&&e.type==='bulk')&&
      a.endpoints.some(e=>e.endpointNumber===11&&e.direction==='out'&&e.type==='bulk')));
    if(!iface)throw Error('当前板卡没有 SPI/ADC 共享数据接口');
    let t;
    try{t=new AdcWorkerTransport(device,iface.interfaceNumber);}catch{return null;}
    try{
      await t.lease.claimExternal(iface.interfaceNumber,[0x0b,ADC_EP]);
      await t._rpc('open',{device:{vendorId:device.vendorId,productId:device.productId,
        serialNumber:device.serialNumber,interfaceNumber:iface.interfaceNumber}});
      t.workerOwned=true;return t;
    }catch(e){
      t.worker.terminate();try{await t.lease.close();}catch(cleanup){t.lease.abandon();e.message+=`；USB 清理失败：${cleanup.message}`;}
      if(e.code==='WORKER_USB_UNSUPPORTED')return null;
      throw e;
    }
  }
  async retireSpiOut(){
    if(this.flush)throw Error('上一笔 SPI OUT 退场请求尚未结束');
    this.flush=this._rpc('out',{}, {native:true}).then(r=>{if(r.status!=='ok')throw Error('SPI OUT 退场失败');})
      .finally(()=>{this.flush=null;});
    return await withTimeout(this.flush,2000,'退场 SPI OUT');
  }
  /** SPI/QSPI 桥与 ADC 复用同一个 bulk IN 端点：开流之前必须把它的残留应答读干净，
   *  否则第一个包会是垃圾（「数据包格式或任务代数错误」）。与 retireSpiOut 成对。 */
  async retireSpiIn(){
    if(this.flushIn)throw Error('上一笔共享缓冲 IN 退场请求尚未结束');
    this.flushIn=this._rpc('in').finally(()=>{this.flushIn=null;});
    return await this.flushIn;
  }
  start(token,{bits,inFlight=1,onBlock,onFault}={}){
    if(this.pending&&!this.done)throw Error('上一轮 ADC USB 读取尚未结束');
    if(!Number.isInteger(inFlight)||inFlight<1||inFlight>ADC_MAX_INFLIGHT)throw Error('ADC USB 接收深度必须为 1–32');
    this.token=token;this.expectedBits=bits;this.onBlock=onBlock;this.onFault=onFault;
    this.done=false;this.error=null;this.seq=0;this.index=0;this.rate=null;this.ends=0;
    this.inFlight=inFlight;this.usbFailed=false;this.discard=false;this._settled=false;
    this.pending=new Promise(resolve=>{this._resolvePending=resolve;});
    this._rpc('start',{token,inFlight}).catch(e=>{this._fault(e);this.done=false;this._settled=true;
      const resolve=this._resolvePending;this._resolvePending=null;this.pending=null;resolve?.();});
  }
  _fault(e){if(!this.error){this.error=e;try{this.onFault?.(e);}catch{}}}
  _packet(bytes,completedAt){
    let p,valid=false;
    try{
      p=decodeAdcPacket(bytes,this.token);
      if(p.done)this.ends++;
      if(this.ends&&!p.done)throw Error('ADC END 后仍收到样本');
      if(p.bits!==this.expectedBits||p.seq!==this.seq||p.first!==this.index||
        (this.rate!==null&&p.rate!==this.rate))throw Error('ADC DMA 数据块不连续或配置变化');
      this.seq=(this.seq+1)>>>0;this.index=(this.index+p.codes.length)>>>0;
      if(p.codes.length)this.rate=p.rate;valid=true;
    }catch(e){this.discard=true;this._fault(e);}
    if(valid){p.completedAt=completedAt;p.mainReceivedAt=performance.now();}
    if(valid&&p.codes.length&&!this.discard){try{this.onBlock?.(p);}catch(e){this.discard=true;this._fault(e);}}
    if(valid&&p.fault)this._fault(Error(`ADC 已停止：${p.fault===5?'DMA 缓冲满':`硬件故障 ${p.fault}`}，未静默覆盖`));
  }
  async drain(){
    if(this.usbFailed&&!this.pending)throw this.error;
    if(!this.pending&&!this.done){
      this.onBlock=null;this.start(this.token,{bits:this.expectedBits,inFlight:this.inFlight});
    }
    if(this.pending)await withTimeout(this.pending,3000,'等待 ADC END');
    if(!this.done)throw this.error||Error('ADC 数据流停止未确认');
  }
  async close(){
    if(this.pending&&!this.done||this.flush)throw Error('ADC USB 请求尚未退出，请先停止或拔插探针');
    try{await this._rpc('close');}finally{this.worker.terminate();}
    await this.lease.close();
  }
}
export class AdcTransport {
  constructor(device){this.lease=new UsbLease(device,'analog');this.device=this.lease.device;this.done=false;}
  static async request(hidDevice){
    if(!globalThis.navigator?.usb)throw Error('ADC 需要桌面 Chrome/Edge WebUSB');
    const same=d=>d.vendorId===hidDevice.vendorId&&d.productId===hidDevice.productId&&
      (!hidDevice.serialNumber||d.serialNumber===hidDevice.serialNumber);
    const devices=(await navigator.usb.getDevices()).filter(same);
    const device=devices.length===1?devices[0]:await navigator.usb.requestDevice({filters:[{
      vendorId:hidDevice.vendorId,productId:hidDevice.productId,
      ...(hidDevice.serialNumber?{serialNumber:hidDevice.serialNumber}:{})}]});
    if(!same(device))throw Error('HID 和 ADC 不是同一台探针');
    if(typeof Worker==='function'){
      let workerTransport=null;
      workerTransport=await AdcWorkerTransport.request(device);
      if(workerTransport)return workerTransport;
    }
    const t=new AdcTransport(device);
    try{
      await t.lease.open();
      const iface=t.device.configuration.interfaces.find(i=>i.alternates.some(a=>
        a.endpoints.some(e=>e.endpointNumber===11&&e.direction==='in'&&e.type==='bulk')&&
        a.endpoints.some(e=>e.endpointNumber===11&&e.direction==='out'&&e.type==='bulk')));
      if(!iface)throw Error('当前板卡没有 SPI/ADC 共享数据接口');
      await t.lease.claim(iface.interfaceNumber,[0x0b,ADC_EP]);return t;
    }catch(e){try{await t.lease.close();}catch(cleanup){t.lease.abandon();e.message+=`；USB 清理失败：${cleanup.message}`;}throw e;}
  }
  async retireSpiOut(){
    if(this.flush)throw Error('上一笔 SPI OUT 退场请求尚未结束');
    // Only called when HID CAPS reports disabled SPI with exactly one OUT still armed.
    const native=Promise.resolve().then(()=>this.device.transferOut(11,new Uint8Array()));
    this.flush=native.finally(()=>{this.flush=null;});this.flush.catch(()=>{});
    const r=await withTimeout(this.flush,2000,'退场 SPI OUT');
    if(r.status!=='ok')throw Error('SPI OUT 退场失败');
  }
  /** 同 AdcWorkerTransport.retireSpiIn：把共享 IN 端点上残留的应答读掉。
   *  超时的那一笔读**不能丢**：它已经占着端点，ADC 的第一个包会被它吃掉，
   *  所以留着当 pump 的第一笔读（`_takeRead`）。 */
  async retireSpiIn(){
    if(this.flushIn)throw Error('上一笔共享缓冲 IN 退场请求尚未结束');
    const task=(async()=>{
      const settleIdle=()=>new Promise(resolve=>setTimeout(()=>resolve(null),RETIRE_IN_TIMEOUT_MS));
      for(let retired=0;retired<RETIRE_IN_LIMIT;retired++){
        // 先接手上一次退场留下的那笔读：丢掉它等于让一笔没人管的读占着端点，
        // ADC 的第一个包会被它吃掉，流就从 seq=1 开始 → 判成「数据块不连续」。
        const pending=this._takeRead();
        const outcome=await Promise.race([pending,settleIdle()]);
        if(outcome===null){this._leftover=pending;return;}
        if(outcome.error)throw outcome.error;
        if(outcome.result?.status!=='ok')throw Error('SPI IN 退场失败');
        if(!outcome.result.data?.byteLength)return;
      }
    })().finally(()=>{this.flushIn=null;});
    this.flushIn=task;await task;
  }
  _takeRead(){const pending=this._leftover;this._leftover=null;return pending||this._read();}
  start(token,{bits,inFlight=1,onBlock,onFault}={}){
    if(this.pending||(this.token!=null&&!this.done))throw Error('上一轮 ADC USB 读取尚未结束');
    if(!Number.isInteger(inFlight)||inFlight<1||inFlight>ADC_MAX_INFLIGHT)throw Error('ADC USB 接收深度必须为 1–32');
    this.token=token;this.expectedBits=bits;this.onBlock=onBlock;this.onFault=onFault;
    this.done=false;this.error=null;this.seq=0;this.index=0;this.rate=null;
    this.inFlight=inFlight;this.usbFailed=false;this.discard=false;
    this._launch();
  }
  _fault(e){if(!this.error){this.error=e;try{this.onFault?.(e);}catch{ /* Keep draining native IN requests. */ }}}
  _launch(){this.pending=this._pump().catch(e=>this._fault(e)).finally(()=>{this.pending=null;});}
  _read(){
    // Attach rejection handlers at submission, including requests completed out
    // of order. Process results in submission order, never Promise.race order.
    try{return Promise.resolve(this.device.transferIn(11,ADC_BLOCK_BYTES)).then(result=>({result}),error=>({error}));}
    catch(error){return Promise.resolve({error});}
  }
  async _pump(){
    const queue=Array.from({length:this.inFlight},()=>this._takeRead());
    let ends=0;
    while(queue.length){
      const {result:r,error}=await queue.shift();
      if(error||r.status!=='ok'||!r.data?.byteLength){
        this.usbFailed=true;this._fault(error||Error(`ADC USB 读取失败：${r.status}`));
        continue; // Retire other native requests before relinquishing ownership.
      }
      let p,valid=false;
      try{
        p=decodeAdcPacket(new Uint8Array(r.data.buffer,r.data.byteOffset,r.data.byteLength),this.token);
        if(p.done)ends++;
        if(ends&&!p.done)throw Error('ADC END 后仍收到样本');
        if(p.bits!==this.expectedBits || p.seq!==this.seq || p.first!==this.index ||
          (this.rate!==null&&p.rate!==this.rate))throw Error('ADC DMA 数据块不连续或配置变化');
        this.seq=(this.seq+1)>>>0;this.index=(this.index+p.codes.length)>>>0;
        if(p.codes.length)this.rate=p.rate;
        valid=true;
      }catch(e){
        this.discard=true;this._fault(e);
      }
      // Refill before history/UI callbacks. Stop refilling at the first matched
      // END; firmware sends one sequenced END per negotiated native reader.
      if(!ends&&!this.usbFailed)queue.push(this._takeRead());
      if(valid&&p.codes.length&&!this.discard){
        try{this.onBlock?.(p);}catch(e){this.discard=true;this._fault(e);}
      }
      // A hardware fault does not invalidate sequenced tail samples. Deliver
      // them first and continue retaining subsequent valid DATA until END.
      if(valid&&p.fault)this._fault(Error(`ADC 已停止：${p.fault===5?'DMA 缓冲满':`硬件故障 ${p.fault}`}，未静默覆盖`));
    }
    this.done=ends===this.inFlight;
    if(!this.done)throw Error('ADC USB 请求退场后未收到完整 END');
  }
  async drain(){
    if(this.usbFailed&&!this.pending)throw this.error;
    if(!this.pending&&!this.done){this.onBlock=null;this._launch();}
    if(this.pending)await withTimeout(this.pending,3000,'等待 ADC END');
    if(!this.done)throw Error('ADC 数据流停止未确认');
  }
  async close(){if(this.pending||this.flush||this.flushIn)throw Error('ADC USB 请求尚未退出，请先停止或拔插探针');this._leftover=null;await this.lease.close();}
}
