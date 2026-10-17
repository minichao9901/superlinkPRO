import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {nativeProbe} from './native-adapter.mjs';
import {AkaLinkHid} from '../../app/hid/probe.js';
import {installProbeManager} from '../../app/core/probe-users.js';
import {DebugSession} from '../../app/dbg/session.js';
import {SpiSession} from '../../app/spi/session.js';
import {I2cSession} from '../../app/i2c/session.js';
import * as F from '../../app/spi/flash.js';
import * as P from '../../app/spi/protocol.js';
import * as I from '../../app/i2c/protocol.js';

// Real application sessions and central registry. Only browser transport is
// adapted to OS HID/libusb; no browser chooser or rendering is claimed here.
// External memory operations are restricted to reads and address-pointer setup.
const n=await nativeProbe(),control=new AkaLinkHid();
const dbg=new DebugSession(),spi=new SpiSession(),i2c=new I2cSession();
const view={session:dbg,disconnect:()=>dbg.disconnect()};
const tools={dbg:view,spiSession:spi,i2c:{session:i2c}};
const manager=installProbeManager(tools,{locks:null});
const hash=b=>createHash('sha256').update(b).digest('hex');
const owners=()=>[...manager.leases.keys()].sort();
let eepromAddress=0x50,baselineEeprom,baselineNor;
let debugReads=0,events=[],initialCloses=0;
const readIdentity=async()=>{
 const b=await dbg.probe.readMem(0xe0042000,4);
 assert.equal(new DataView(b.buffer,b.byteOffset,4).getUint32(0,true),0x20036410);
 debugReads++;return b;
};
const readEeprom=async()=>{
 const r=await i2c.readLong({dev:eepromAddress,addr:[0],rd:256,chunk:'reset'},{quiet:true});
 assert.equal(r.err,0,JSON.stringify(r));assert.equal(r.data.length,256);
 if(baselineEeprom)assert.deepEqual(r.data,baselineEeprom);
 return r.data;
};
const frames=async(items)=>{
 const r=await spi.sendFrames(items,{quiet:true,timeoutMs:3000});
 assert.equal(r.failed,0);
 for(const s of r.rsps){assert.ok(s,'missing SPI reply');assert.equal(s.status,P.ST.OK);}
 return Buffer.concat(r.rsps.map(s=>Buffer.from(s.data)));
};
const readNor=async()=>{
 const id=await frames(F.rdidItems());assert.equal(id.toString('hex'),'ef4017');
 // Use separate addressed transactions as a handoff control. A separate
 // diagnostic retains the failed long CS_HOLD continuation-read evidence.
 const items=Array.from({length:32},(_,k)=>F.readFrame({opcode:F.OP.READ,addr:k*128,addrLen:3,dummy:0,lines:1,rx:128}));
 const b=await frames(items);
 assert.equal(b.length,4096);if(baselineNor){
  const diffs=[];for(let k=0;k<b.length;k++)if(b[k]!==baselineNor[k])diffs.push(k);
  if(diffs.length)console.log('NOR_MISMATCH',JSON.stringify({count:diffs.length,first:diffs.slice(0,16),last:diffs.slice(-8),baselineHash:hash(baselineNor),actualHash:hash(b),sample:[...b.subarray(diffs[0],diffs[0]+16)]}));
  assert.equal(diffs.length,0,'repeated NOR read mismatch');
 }
 return b;
};
const connectI2c=async()=>{
 assert.equal(await i2c.connect(false,{enable:true}),true);i2c.stopPoll();
 assert.ok(i2c.enabled);assert.deepEqual(owners(),['dbg','i2c']);
};
const connectSpi=async()=>{
 assert.equal(await spi.connectHid(false),true);
 clearInterval(spi.pollTimer);spi.pollTimer=null;
 assert.equal(await spi.connectUsb(false),true);
 const cfg=await spi.loadCfg({quiet:true});
 const want={...cfg,sclkHz:1000000,mode:0,bits:8,csPolicy:0};
 const back=await spi.applyConfig(want);assert.equal(back.cfg.sclkHz,1000000);
 await spi.applyProfile({...spi.profile,profile:0,defLines:1});
 assert.equal(await spi.setEnabled(true),true);assert.deepEqual(owners(),['dbg','spi']);
};
const originalI2cDisconnect=i2c.disconnect.bind(i2c);
i2c.disconnect=async()=>{events.push('i2c-release-start');await originalI2cDisconnect();
 const r=await control.xfer(I.HID_CMD,I.actStatus());assert.equal(I.parseStatus(r).enabled,false);
 events.push('i2c-disabled-confirmed');};
const originalSpiTeardown=spi.teardown.bind(spi);
spi.teardown=async()=>{events.push('spi-release-start');await originalSpiTeardown();
 const r=await control.xfer(P.HID_CMD,P.hidData.status());assert.equal(P.statusWord(P.parseWordPayload(r)).enabled,false);
 events.push('spi-disabled-confirmed');};
try{
 await control.open(n.hd);await control.stop();await control.setTargetType(false);
 await manager.run('dbg',()=>dbg.connect({clockKhz:1000,stopBridge:false}));
 initialCloses=n.stats.closes;
 console.log('USB pre-acquisition orphan cleanup closes',initialCloses);
 await readIdentity();console.log('PASS F103CB identity=0x20036410; SWD=1 MHz; real DebugSession acquired through central registry');
 await connectI2c();
 let found=false;
 for(let a=0x50;a<=0x57;a++){
  const r=await i2c.transaction({dev:a,addr:[0],wr:[],rd:1},{quiet:true});
  if(r.err===0){eepromAddress=a;found=true;break;}
 }
 assert.ok(found,'AT24C02 address 0x50..0x57 did not acknowledge');
 baselineEeprom=await readEeprom();
 for(let k=0;k<10;k++)await Promise.all([readEeprom(),readIdentity()]);
 console.log(`PASS AT24C02 address=0x${eepromAddress.toString(16)}; 11 complete 256-byte reads; SHA256=${hash(baselineEeprom)}; concurrent DAP identity unchanged`);
 await connectSpi();assert.ok(!i2c.connected);assert.ok(events.includes('i2c-disabled-confirmed'));
 baselineNor=await readNor();
 for(let k=0;k<10;k++)await Promise.all([readNor(),readIdentity()]);
 assert.deepEqual([...baselineNor.subarray(0,256)],Array.from({length:256},(_,k)=>k));
 console.log(`PASS W25Q64 JEDEC=ef4017; 11 reads of 4096 bytes at 0 with fresh address per 128-byte frame; existing first 256 bytes verified against 00..ff pattern; SHA256=${hash(baselineNor)}; concurrent DAP identity unchanged`);
 spi.setBusy(true);assert.equal(await i2c.connect(false,{enable:true}),false);
 assert.deepEqual(owners(),['dbg','spi']);await readNor();spi.setBusy(false);
 console.log('PASS protected busy SPI refuses I2C takeover; original real SPI session remains usable');
 for(let k=0;k<20;k++){
  events=[];
  await Promise.all([connectI2c(),readIdentity()]);
  assert.deepEqual(events,['spi-release-start','spi-disabled-confirmed']);
  await Promise.all([readEeprom(),readIdentity()]);
  events=[];
  await Promise.all([connectSpi(),readIdentity()]);
  assert.deepEqual(events,['i2c-release-start','i2c-disabled-confirmed']);
  await Promise.all([readNor(),readIdentity()]);
  console.log('USB handoff stats',JSON.stringify(n.stats));
  assert.equal(manager.failures.size,0);assert.equal(n.stats.resets,0);assert.equal(n.stats.closes,initialCloses);
  console.log(`PASS handoff ${k+1}/20 SPI->I2C->SPI; confirmed firmware disable; EEPROM/NOR bytes unchanged; DAP peer kept alive`);
 }
 await connectI2c();
 const xfer=i2c.hid.xfer.bind(i2c.hid);
 i2c.hid.xfer=async(cmd,data,...rest)=>cmd===I.HID_CMD&&data[0]===I.ACT.ENABLE?Uint8Array.of(0):xfer(cmd,data,...rest);
 assert.equal(await spi.connectHid(false),false);
 assert.ok(manager.failures.has('i2c'));assert.ok(manager.leases.has('i2c'));assert.ok(!spi.connected);
 await readIdentity();
 i2c.hid.xfer=xfer;await i2c.disconnect();
 await connectSpi();await readNor();await readIdentity();
 console.log('PASS fault injection on real I2C disable reply: conflicting SPI setup blocked, DAP preserved; explicit recovery restores reads');
 assert.equal(n.stats.resets,0);assert.equal(n.stats.closes,initialCloses);
 await spi.teardown();await view.disconnect();manager.forget('dbg');
 assert.equal(manager.leases.size,0);assert.equal(manager.failures.size,0);
 assert.equal(n.stats.closes,initialCloses+1);assert.equal(n.stats.resets,0);
 console.log(`PASS final cleanup: no leases/failures; DAP identity reads=${debugReads}; USB closes exactly once after final owner; resets=0; no external memory erase/program`);
}finally{
 spi.setBusy(false);
 try{if(spi.connected||spi.transport)await spi.teardown();}catch(e){console.error('CLEANUP SPI',e.message);}
 try{if(i2c.connected)await i2c.disconnect();}catch(e){console.error('CLEANUP I2C',e.message);}
 try{if(dbg.connected)await dbg.disconnect();}catch(e){console.error('CLEANUP DAP',e.message);}
 await control.close();n.close();
}
