/** Reserved analog HID ABI. Production HPM5301 advertises zero DAC channels. */
import { waitMs } from '../core/pace.js';
export const CMD = 0x38;
export const ACT = Object.freeze({ CAPS:1, CONFIG:2, BEGIN:3, WRITE:4, START:5, STOP:6, STATUS:7, GET_CONFIG:8 });
export const RC = Object.freeze({ OK:0, RANGE:1, BUSY:2, STATE:3, UNSUPPORTED:4 });
export const FEATURE = Object.freeze({ TABLE:1 });
export const CHUNK_POINTS = 25;
const v = b => new DataView(b.buffer,b.byteOffset,b.byteLength);
const errors = ['成功','参数超限','DAC 忙','DAC 状态或任务代数不匹配','当前固件未支持 DAC'];
export class DacError extends Error { constructor(code){super(errors[code] || `DAC 错误 ${code}`);this.code=code;} }
export function response(r, action){
  if (!(r instanceof Uint8Array) || r.length < 7 || r[0] < 8 || r[0] > 64 || r[0] > r.length+1 || r[1]!==CMD || r[2]!==action) throw Error('DAC HID 响应不完整或动作不匹配');
  const rc=v(r).getUint32(3,true); if(rc)throw new DacError(rc);
  return r.subarray(7,r[0]-1);
}
export function parseCaps(b){
  if (b.length!==16 || String.fromCharCode(...b.subarray(0,4))!=='DAC1' || b[4]!==1) throw Error('DAC 协议版本或能力长度不支持');
  const c={version:b[4],channels:b[5],bits:b[6],features:b[7],maxRate:v(b).getUint32(8,true),maxPoints:v(b).getUint16(12,true),fullScale:v(b).getUint16(14,true)/1000};
  if(c.channels>8 || (c.features & ~FEATURE.TABLE))throw Error('DAC 能力范围不支持');
  c.supported=!!c.channels && !!(c.features & FEATURE.TABLE);
  if(c.channels && (![8,10,12,16].includes(c.bits) || !c.maxRate || c.maxRate>10000000 || c.maxPoints<8 || !c.fullScale))throw Error('DAC 固件能力无效');
  return c;
}
const channel = n => { if(!Number.isInteger(n)||n<0||n>7)throw Error('DAC 通道无效'); };
const token = n => { if(!Number.isInteger(n)||n<=0||n>0xFFFFFFFF)throw Error('DAC 任务代数无效'); };
export function configData({channel:ch,bits,rate,idleCode=0}){
  channel(ch);if(![8,10,12,16].includes(bits)||!Number.isInteger(rate)||rate<1||rate>10000000||!Number.isInteger(idleCode)||idleCode<0||idleCode>=2**bits)throw Error('DAC 配置无效');
  const b=new Uint8Array(8);b[0]=ch;b[1]=bits;v(b).setUint32(2,rate,true);v(b).setUint16(6,idleCode,true);return b;
}
export function parseConfig(b){
  if(b.length!==8)throw Error('DAC 配置应答长度错误');
  const c={channel:b[0],bits:b[1],rate:v(b).getUint32(2,true),idleCode:v(b).getUint16(6,true)};configData(c);return c;
}
export function beginData(ch,points){
  channel(ch);if(!Number.isInteger(points)||points<8||points>65535)throw Error('DAC 波形表需为 8–65535 点');
  const b=new Uint8Array(3);b[0]=ch;v(b).setUint16(1,points,true);return b;
}
export function writeData(ch,generation,offset,codes){
  channel(ch);token(generation);
  if(!Number.isInteger(offset)||offset<0||offset>65535||!codes.length||codes.length>CHUNK_POINTS||offset+codes.length>65535||Array.from(codes).some(n=>!Number.isInteger(n)||n<0||n>65535))throw Error('DAC 波形分片无效');
  const b=new Uint8Array(8+2*codes.length);b[0]=ch;v(b).setUint32(1,generation,true);v(b).setUint16(5,offset,true);b[7]=codes.length;
  codes.forEach((n,i)=>v(b).setUint16(8+2*i,n,true));return b;
}
export function startData(ch,generation,cycles=0){
  channel(ch);token(generation);if(!Number.isInteger(cycles)||cycles<0||cycles>0xFFFFFFFF)throw Error('DAC 周期次数无效');
  const b=new Uint8Array(9);b[0]=ch;v(b).setUint32(1,generation,true);v(b).setUint32(5,cycles,true);return b;
}
export function stopData(ch,generation){channel(ch);token(generation);const b=new Uint8Array(5);b[0]=ch;v(b).setUint32(1,generation,true);return b;}
export function parseStatus(b){
  if(b.length!==20||b[1]>1||(b[3]&~1))throw Error('DAC 状态长度或标志错误');
  channel(b[0]);if(![8,10,12,16].includes(b[2]))throw Error('DAC 状态位宽无效');
  const s={channel:b[0],running:!!b[1],cleanup:!!(b[3]&1),bits:b[2],token:v(b).getUint32(4,true),loaded:v(b).getUint16(8,true),position:v(b).getUint16(10,true),cycles:v(b).getUint32(12,true),actualRate:v(b).getUint32(16,true)};
  if(s.running&&(!s.token||!s.loaded||s.position>=s.loaded||!s.actualRate))throw Error('DAC 运行状态无效');return s;
}
export class DacClient {
  constructor(xfer){this.xfer=xfer;this.caps=null;this.channel=null;this.token=0;this.owned=false;this.stopError=null;}
  async command(action,args=new Uint8Array()){return response(await this.xfer(CMD,Uint8Array.of(action,...args)),action);}
  async capabilities(){
    try {this.caps=parseCaps(await this.command(ACT.CAPS));}
    catch(e){if(!(e instanceof DacError)||![RC.RANGE,RC.UNSUPPORTED].includes(e.code))throw e;this.caps={version:0,channels:0,supported:false};}
    return this.caps;
  }
  requireChannel(ch){if(!this.caps?.supported)throw new DacError(RC.UNSUPPORTED);channel(ch);if(ch>=this.caps.channels)throw Error('DAC 通道超出固件能力');}
  async status(ch=this.channel){this.requireChannel(ch);const s=parseStatus(await this.command(ACT.STATUS,Uint8Array.of(ch)));if(s.channel!==ch)throw Error('DAC 状态通道不匹配');return s;}
  async getConfig(ch){this.requireChannel(ch);const c=parseConfig(await this.command(ACT.GET_CONFIG,Uint8Array.of(ch)));if(c.channel!==ch)throw Error('DAC 配置通道不匹配');return c;}
  async startLut(config,codes,{cycles=0,signal}={}){
    const alive=()=>{if(signal?.aborted){const e=Error('DAC 启动已取消');e.name='AbortError';throw e;}};alive();
    this.requireChannel(config.channel);const c=this.caps;
    if(this.owned)throw Error('先停止当前 DAC 波形');
    if(config.bits!==c.bits||config.rate>c.maxRate||codes.length<8||codes.length>c.maxPoints||Array.from(codes).some(n=>!Number.isInteger(n)||n<0||n>=2**c.bits))throw Error('DAC 波形超出固件能力');
    const args=configData(config);
    const accepted=parseConfig(await this.command(ACT.CONFIG,args));
    if(accepted.channel!==config.channel||accepted.bits!==config.bits||accepted.rate>config.rate||accepted.idleCode!==(config.idleCode||0))throw Error('DAC 配置应答不匹配');
    alive();
    this.channel=config.channel;this.beginUncertain=true;this.owned=true;
    try {
      let b;
      try{b=await this.command(ACT.BEGIN,beginData(config.channel,codes.length));}
      catch(e){if(e instanceof DacError){this.beginUncertain=false;this.owned=false;}throw e;}
      if(b.length!==4||!v(b).getUint32(0,true))throw Error('DAC 波形预备应答错误');
      this.token=v(b).getUint32(0,true);this.beginUncertain=false;
      for(let off=0;off<codes.length;off+=CHUNK_POINTS){
        alive();
        const chunk=codes.slice(off,off+CHUNK_POINTS),r=await this.command(ACT.WRITE,writeData(this.channel,this.token,off,chunk));
        if(r.length!==2||v(r).getUint16(0,true)!==off+chunk.length)throw Error('DAC 波形上传进度不匹配');
      }
      alive();
      const r=await this.command(ACT.START,startData(this.channel,this.token,cycles));
      if(r.length!==4||v(r).getUint32(0,true)!==accepted.rate)throw Error('DAC 实际更新率与配置不匹配');
      alive();return {actualRate:accepted.rate,points:codes.length};
    } catch(e){try{await this.stop();}catch(stop){throw new AggregateError([e,stop],'DAC 上传/启动失败且停止未确认，请重试停止');}throw e;}
  }
  async stop(){
    if(!this.owned)return;
    try {
      if(this.beginUncertain){
        const s=await this.status();
        // BEGIN never starts playback. Do not stop an unexpected running task.
        if(s.running||s.cleanup)throw Error('DAC BEGIN 状态未确认，保留占用');
        if(!s.token){this.owned=false;this.beginUncertain=false;this.stopError=null;return;}
        this.token=s.token;this.beginUncertain=false;
      }
      let s=parseStatus(await this.command(ACT.STOP,stopData(this.channel,this.token)));
      const deadline=performance.now()+3000;
      for(;;){
        if(s.channel!==this.channel||s.token!==this.token)throw Error('DAC 停止任务身份不匹配');
        if(!s.running&&!s.cleanup)break;
        if(performance.now()>=deadline)throw Error('DAC 停止未确认');
        await waitMs(5);s=await this.status();
      }
      this.owned=false;this.stopError=null;
    }catch(e){this.stopError=e;throw e;}
  }
}
