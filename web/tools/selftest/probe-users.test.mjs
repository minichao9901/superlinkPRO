import assert from 'node:assert/strict';
import {releaseLocalProbeUsers} from '../../app/core/probe-users.js';
const events=[];globalThis.__tools={flash:{busy:true}};
await assert.rejects(releaseLocalProbeUsers('scope'),/烧录器/);
await releaseLocalProbeUsers('flash');
globalThis.__tools={dbg:{session:{connected:true},disconnect:async()=>events.push('debug')},rtt:{probe:{},disconnect:async()=>events.push('viewer')},scope:{running:true,releaseProbe:async()=>events.push('scope')},hid:{last:{running:true},stop:async()=>events.push('bridge')}};
await releaseLocalProbeUsers('hid');assert.deepEqual(events,['debug','viewer','scope'],'bridge startup releases competing readers first');
events.length=0;await releaseLocalProbeUsers('scope');assert.deepEqual(events,['debug','viewer','bridge']);
events.length=0;globalThis.__tools.hid.stop=async()=>{throw new Error('STOP not complete');};await assert.rejects(releaseLocalProbeUsers('flash'),/STOP not complete/);
delete globalThis.__tools;console.log('probe-users: busy flash rejects takeover; same-page target readers stop; failed STOP aborts handoff PASS');
globalThis.__tools={hid:{last:{running:false},dev:{connected:true},stop:()=>assert.fail('idle HID handle does not own the global bridge')}};
await releaseLocalProbeUsers(null);delete globalThis.__tools;
console.log('probe-users: passive HID connections cannot stop an engine owned by another page PASS');
const {createProbeManager}=await import('../../app/core/probe-users.js');
{
 const events=[];
 const t={scope:{running:false,releaseProbe:async()=>{events.push('scope');t.scope.running=false;}},
  spiSession:{connected:false,teardown:async()=>{events.push('spi');t.spiSession.connected=false;}},
  dbg:{session:{connected:false},disconnect:async()=>{events.push('dbg');t.dbg.session.connected=false;}}};
 const m=createProbeManager(t,{locks:null});
 await m.run('scope',async()=>{t.scope.running=true;});await m.run('spi',async()=>{t.spiSession.connected=true;});
 assert.deepEqual(m.summary().owners,['scope','spi']);assert.deepEqual(events,[]);
 assert.throws(()=>m.assertUsbResetAllowed('scope',{}),/spi/);
 await m.run('dbg',async()=>{t.dbg.session.connected=true;});assert.deepEqual(events,['scope']);
 assert.deepEqual(m.summary().owners,['spi','dbg'],'debug handoff preserves independent SPI');
 t.session={isOpen:true,port:{getInfo:()=>({usbVendorId:0xd28,usbProductId:0x204})}};
 assert.throws(()=>m.assertUsbResetAllowed('dap',{vendorId:0xd28,productId:0x204}),/CDC/);
}
console.log('probe-users: independent SPI coexists with target users; engine remains exclusive; reset guards cover CDC PASS');
