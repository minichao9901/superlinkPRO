import assert from 'node:assert/strict';
import {nativeProbe} from './native-adapter.mjs';
import {AkaLinkHid} from '../../app/hid/probe.js';
import {SpiSession} from '../../app/spi/session.js';
import * as F from '../../app/spi/flash.js';
import {TC} from '../../app/spi/protocol.js';
const n=await nativeProbe(),h=new AkaLinkHid(),s=new SpiSession();
const read=async(items)=>{const r=await s.sendFrames(items,{quiet:true});for(const p of r.rsps){assert.ok(p);assert.equal(p.status,0);}return Buffer.concat(r.rsps.map(p=>Buffer.from(p.data)));};
try{
 await h.open(n.hd);await h.stop();assert.equal(await s.connectHid(false),true);clearInterval(s.pollTimer);s.pollTimer=null;
 assert.equal(await s.connectUsb(false),true);await s.applyConfig({...s.cfg,sclkHz:1000000,mode:0,bits:8,csPolicy:0});
 await s.applyProfile({...s.profile,profile:0,defLines:1});await s.setEnabled(true);
 for(const addr of [0,0x7f0000]){
  const base=await read([F.readFrame({opcode:3,addr,addrLen:3,dummy:0,lines:1,rx:256})]);
  console.log('BASE',addr.toString(16),base.subarray(0,32).toString('hex'),'nonFF',base.reduce((n,b)=>n+(b!==255),0));
  for(const [opcode,lines,quadAddr] of [[0x0b,1,false],[0x3b,2,false],[0x6b,4,false],[0xeb,4,false],[0xeb,4,true]])for(const dummy of [0,1,2,3,4]){
   const it=F.readFrame({opcode,addr,addrLen:3,dummy,lines,rx:256});if(quadAddr)it.payload[1]|=TC.ADDR_QUAD;
   let bad=0,sample;
   for(let k=0;k<5;k++){const b=await read([it]);if(!b.equals(base))bad++;sample=b.subarray(0,16).toString('hex');}
   console.log('MODE',JSON.stringify({addr,opcode:opcode.toString(16),lines,quadAddr,dummy,bad,iterations:5,sample}));
  }
 }
}finally{if(s.connected||s.transport)await s.teardown();await h.close();n.close();}
