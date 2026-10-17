/** Shared ADC DMA HID ABI; pure parsing, no session or resource policy. */
export const ADC_ACT=Object.freeze({CAPS:9,OPEN:10,END:11,CLOSE:12,START:13,STATUS:14,PIPELINE:15});

export function decodeReply(r,action){
  if(!(r instanceof Uint8Array)||r.length<7||r[0]<8||r[0]>64||r[0]>r.length+1||r[1]!==0x38||r[2]!==action)throw Error('ADC DMA 固件响应无效');
  const rc=new DataView(r.buffer,r.byteOffset,r.byteLength).getUint32(3,true);
  if(rc){const e=Error(`ADC DMA 控制失败：${rc}`);e.code=rc;throw e;}
  return r.subarray(7,r[0]-1);
}
export function streamCaps(b,{allowUnsupported=false}={}){
  if(b.length!==20||String.fromCharCode(...b.subarray(0,4))!=='ADB2'||b[4]!==0x8b||b[5]!==2||b[6]!==15||b[7]!==6)
    throw Error('需要 SPI/ADC 共享 DMA 配套固件');
  const v=new DataView(b.buffer,b.byteOffset,b.byteLength),maxRate=v.getUint32(8,true),reference=v.getUint16(18,true)/1000;
  if(v.getUint16(12,true)!==4096||v.getUint16(14,true)!==4096||maxRate<1||maxRate>2000000||reference<=0)throw Error('ADC DMA 能力信息无效');
  if(![0,1].includes(b[17])||(b[16]&~3))throw Error('ADC DMA 能力标志无效');
  if(!b[17]&&!allowUnsupported)throw Error('当前板卡未支持共享 SPI 端点的 ADC DMA；此版本支持 HPM5301EVKLite PB14');
  return {supported:!!b[17],channel:6,nativeBits:16,gain:1,reference,maxRate,flags:b[16]};
}
