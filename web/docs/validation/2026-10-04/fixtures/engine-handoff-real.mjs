import assert from 'node:assert/strict';
import {nativeProbe} from './native-adapter.mjs';
import {AkaLinkHid} from '../../app/hid/probe.js';
import {ScopeView} from '../../app/scope/view.js';
import {RttCdcView} from '../../app/hid/view.js';
import {DebugSession} from '../../app/dbg/session.js';
import {I2cSession} from '../../app/i2c/session.js';
import {installProbeManager} from '../../app/core/probe-users.js';
const nodes=new Map();
globalThis.document={getElementById:id=>{
 if(id==='h-target')return {value:'swd'};
 if(!id.startsWith('sc-'))return null;
 if(!nodes.has(id))nodes.set(id,{value:id==='sc-clock'?'10000':'',checked:id==='sc-batch',textContent:'',className:''});
 return nodes.get(id);
}};
globalThis.window={};
const n=await nativeProbe(),control=new AkaLinkHid(),scope=new ScopeView();
const bridge=Object.create(RttCdcView.prototype),dbg=new DebugSession(),i2c=new I2cSession();
Object.assign(scope,{selected:[{name:'RTT magic',addr:0x2000000c,size:4,scalar:'u32'}],
 renderer:{setStore(){},setTrigger(){},clearMarks(){},fitAll(){}},
 periodUs:()=>10,seconds:()=>2,updatePlan:()=>({spans:[{}]}),applyTrigger(){},_applyBackendUi(){},benchFresh:()=>false});
Object.assign(bridge,{dev:new AkaLinkHid(),mock:false,
 params:()=>({addr:0x2000000c,size:4096,channel:0,clockHz:10000000}),
 persist(){},render(r){if(r?.error)console.log('RTT_ERROR',r.error);},_noteProgress(){}});
const tools={scope,hid:bridge,dbg:{session:dbg,disconnect:()=>dbg.disconnect()},i2c:{session:i2c}};
const m=installProbeManager(tools,{locks:null});
let baseline,identityReads=0;
const owners=()=>[...m.leases.keys()].sort();
const eeprom=async()=>{
 const r=await i2c.readLong({dev:0x50,addr:[0],rd:256},{quiet:true});
 assert.equal(r.err,0);assert.equal(r.data.length,256);
 if(baseline)assert.deepEqual(r.data,baseline);return r.data;
};
try{
 await control.open(n.hd);await control.stop();await control.setTargetType(false);
 assert.equal(await i2c.connect(false,{enable:true}),true);i2c.stopPoll();baseline=await eeprom();
 const i2cHid=i2c.hid;
 for(let k=0;k<10;k++){
  await scope.start();assert.ok(scope.running,scope.state);
  assert.deepEqual(owners(),['i2c','scope']);
  await Promise.all([eeprom(),new Promise(r=>setTimeout(r,40))]);
  assert.ok(scope.store.count>20,`scope count ${scope.store.count}`);
  assert.equal(scope.decodeErr,0);assert.equal(scope.lost,0);
  for(let j=0;j<scope.store.count;j++)assert.equal(scope.store.channels[0].data[j],0x47474553);
  await bridge.start();assert.ok(bridge.last.running,JSON.stringify(bridge.last));
  assert.equal(bridge.last.cbAddr,0x2000000c);assert.deepEqual(owners(),['hid','i2c']);
  assert.equal(scope.running,false);assert.equal(scope.transport,null);assert.equal(scope.hid,null);
  await eeprom();
  await m.run('dbg',()=>dbg.connect({clockKhz:1000,stopBridge:false}));
  assert.deepEqual(owners(),['dbg','i2c']);
  const status=await control.status();assert.equal(status.status.running,false);assert.equal(bridge._bridgeRequested,false);
  const b=await dbg.probe.readMem(0xe0042000,4);identityReads++;
  assert.equal(new DataView(b.buffer,b.byteOffset,4).getUint32(0,true),0x20036410);
  await eeprom();assert.equal(i2c.hid,i2cHid);assert.ok(i2c.enabled);assert.equal(m.failures.size,0);
  assert.equal(n.stats.resets,0);
  console.log(`PASS engine handoff ${k+1}/10 Scope->RTT/CDC->Debug; real samples and F103CB identity; confirmed STOP; same live I2C session and unchanged 256-byte EEPROM; resets=0`);
 }
 await m.releaseOthers(null,'verification complete');
 assert.equal(m.leases.size,0);assert.equal(m.failures.size,0);assert.equal(n.stats.resets,0);
 console.log('PASS final cleanup',JSON.stringify({owners:owners(),failures:m.failures.size,identityReads,usb:n.stats}));
}finally{
 clearInterval(bridge._timer);
 try{if(i2c.connected)await i2c.disconnect();}catch(e){console.error('CLEANUP I2C',e.message);}
 try{if(dbg.connected)await dbg.disconnect();m.forget('dbg');}catch(e){console.error('CLEANUP DAP',e.message);}
 try{if(scope.hid||scope.transport)await scope.releaseProbe('cleanup');}catch(e){console.error('CLEANUP scope',e.message);}
 try{await bridge.stop();await bridge.dev.close();}catch(e){console.error('CLEANUP RTT',e.message);}
 await control.stop();await control.configure({clockHz:60000000});await control.close();n.close();
}
