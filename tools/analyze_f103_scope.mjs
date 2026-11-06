/** Decode captured hardware bytes with the production J-Scope protocol. */
import fs from 'node:fs/promises';import * as P from '../web/app/scope/protocol.js';
const folder=process.argv[2];if(!folder)throw Error('Pass a capture directory');
const record=JSON.parse(await fs.readFile(folder+'/result.json','utf8')),raw=new Uint8Array(await fs.readFile(folder+'/capture.bin'));
const stream=new P.PacketStream(),seq=new P.SeqTracker(),counts={},frames=[];let definition,samples=0,first,last,stat;
for(const packet of stream.push(raw)){
 seq.note(packet.seq);counts[packet.kindName]=(counts[packet.kindName]||0)+1;
 if(packet.kind===P.KIND.DEF)definition=P.parseDef(packet.payload,packet.version);
 if(packet.kind===P.KIND.STAT)stat=P.parseStat(packet.payload,packet.version);
 if(packet.kind===P.KIND.DATA){
  if(!definition)throw Error('DATA before DEF');
  const values=P.decodeSamples(definition.vars,packet.payload,packet.n);
  if(values.some(v=>!Number.isFinite(v)))throw Error('Invalid decoded sample');
  if(!first)first={time:packet.tUs,values:values.slice(0,definition.nvars)};
  last={time:packet.tUs,values:values.slice(-definition.nvars)};samples+=packet.n;
 }
}
const reply=Uint8Array.from(Buffer.from(record.statusReply,'hex')),status=P.parseScopeStatus(reply.subarray(4));
const result={definition,counts,samples,first,last,seq,stat,status,trailingBytes:stream.buf.length,resyncs:stream.resyncs,tailDifference:status.produced-samples,rateHz:first&&last&&last.time>first.time?(samples-1)*1e6/(last.time-first.time):0};
result.passed=!!samples&&!status.running&&seq.gaps===0&&seq.dup===0&&seq.reordered===0&&stream.resyncs===0&&status.swdErr===0&&status.usbErr===0;
await fs.writeFile(folder+'/decoded.json',JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));if(!result.passed)process.exitCode=1;
