/** Decode captured hardware bytes with the production J-Scope protocol. */
import fs from 'node:fs/promises';import * as P from '../web/app/scope/protocol.js';
const folder=process.argv[2];if(!folder)throw Error('Pass a capture directory');
const contractArg=process.argv[3],contractIndex=contractArg?.startsWith('--u-hi-index=')?Number(contractArg.split('=')[1]):null;
if(contractArg&&(contractIndex===null||!Number.isInteger(contractIndex)||contractIndex<0))throw Error('Pass --u-hi-index=N for a known fixture channel');
const record=JSON.parse(await fs.readFile(folder+'/result.json','utf8')),raw=new Uint8Array(await fs.readFile(folder+'/capture.bin'));
const stream=new P.PacketStream(),seq=new P.SeqTracker(),counts={},contract={checked:0,bad:0,backwards:0};let definition,samples=0,first,last,stat,previous;
for(const packet of stream.push(raw)){
 seq.note(packet.seq);counts[packet.kindName]=(counts[packet.kindName]||0)+1;
 if(packet.kind===P.KIND.DEF)definition=P.parseDef(packet.payload,packet.version);
 if(packet.kind===P.KIND.STAT)stat=P.parseStat(packet.payload,packet.version);
 if(packet.kind===P.KIND.DATA){
  if(!definition)throw Error('DATA before DEF');
  const values=P.decodeSamples(definition.vars,packet.payload,packet.n);
  if(values.some(v=>!Number.isFinite(v)))throw Error('Invalid decoded sample');
  if(contractIndex!==null){if(contractIndex>=definition.nvars)throw Error('Contract channel missing');for(let i=contractIndex;i<values.length;i+=definition.nvars){const value=values[i]>>>0;contract.checked++;if((value>>>16)!==0x1000)contract.bad++;if(previous!==undefined&&((value-previous)&0xffff)>32768)contract.backwards++;previous=value;}}
  if(!first)first={time:packet.tUs,values:values.slice(0,definition.nvars)};
  last={time:packet.tUs,values:values.slice(-definition.nvars)};samples+=packet.n;
 }
}
const reply=Uint8Array.from(Buffer.from(record.statusReply,'hex')),status=P.parseScopeStatus(reply.subarray(4));
const invalidSamples=record.finalMetrics?.invalidSamples??0;
const produced=record.finalMetrics?.produced??status.produced;
const result={definition,counts,samples,first,last,seq,stat,status,trailingBytes:stream.buf.length,resyncs:stream.resyncs,tailDifference:produced-samples-invalidSamples,rateHz:first&&last&&last.time>first.time?(samples-1)*1e6/(last.time-first.time):0,contract:contractIndex===null?null:contract};
result.passed=!!samples&&!status.running&&seq.gaps===0&&seq.dup===0&&seq.reordered===0&&stream.resyncs===0&&status.swdErr===0&&status.usbErr===0;
if(record.tailDrain){
 result.passed=result.passed&&result.tailDifference===0&&invalidSamples===0&&!status.stopping&&!status.stopAborted&&
   record.finalMetrics?.usb===0&&record.finalMetrics?.errors===0;
}
if(contractIndex!==null)result.passed=result.passed&&contract.checked===samples&&!contract.bad&&!contract.backwards;
await fs.writeFile(folder+'/decoded.json',JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));if(!result.passed)process.exitCode=1;
