import assert from 'node:assert/strict';
import { mkdir, readFile, rmdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { Cdp, sleep } from './cdp-lib.mjs';
import { parseImage } from '../../app/dbg/image.js';
const c=new Cdp(process.env.CDP,40000);
const [out, board, mode] = process.argv.slice(2);
assert.ok(out && board && (!mode || mode === '--webusb-only'),
  'Usage: node dbg-hpm6800-cfg-hw.mjs OUTPUT_DIR BOARD_CFG [--webusb-only]');
const lock=resolve('../web-etm-trace-coordination/hardware.lock');
const elf=resolve(out,'hpm6800_ram.elf'), input=await readFile(elf);
const base=0x18000, end=0x18014;
const {sections}=parseImage(input,'elf');
assert.deepEqual(sections.map(s=>[s.addr,s.data.length]),[[base,end-base]]);
const expected=[0x13,0x05,0x00,0x00,0x6f,0x00,0x40,0x00,0x13,0x05,0x15,0x00,0x13,0x05,0x15,0x00,0x6f,0x00,0x00,0x00];
assert.deepEqual([...sections[0].data],expected);
const report={bundle:true,writesRestrictedToIlm:[base,end]};
let locked=false;
async function waitFor(expr,timeout=12000){
  const until=Date.now()+timeout;
  do { if(await c.eval(`return !!(${expr});`).catch(()=>false))return;await sleep(60); } while(Date.now()<until);
  throw Error(expr+'\n'+await c.eval("return document.querySelector('#d-out').innerText.slice(-2200);"));
}
async function upload(selector,path){
  const {root}=await c.send('DOM.getDocument');
  const {nodeId}=await c.send('DOM.querySelector',{nodeId:root.nodeId,selector});
  await c.send('DOM.setFileInputFiles',{nodeId,files:[resolve(path)]});
}
try{
  await mkdir(lock);locked=true;
  await c.connect();
  if (mode === '--webusb-only'){
    await c.send('Page.addScriptToEvaluateOnNewDocument', {
      source: "Object.defineProperty(navigator,'hid',{value:undefined,configurable:true});",
    });
  }
  const nonce=Date.now();
  await c.send('Page.navigate',{url:`http://127.0.0.1:8899/?modules=bundle&hpm-hw=${nonce}#dbg`});
  await waitFor(`location.search.includes('hpm-hw=${nonce}') && !!window.__tools?.dbg && window.__useWebBundle`);
  report.webHidUnavailable = await c.eval('return !navigator.hid;');
  if (mode === '--webusb-only') assert.ok(report.webHidUnavailable);
  await upload('#d-cfg-file',board);
  await waitFor('window.__tools.dbg._activeCfg()?.boardId=== "hpm6800evk" && !window.__tools.dbg._cfgImporting');
  report.cfg=await c.eval('return window.__tools.dbg._activeCfg();');
  await c.eval(`
    const devices=(await navigator.usb.getDevices()).filter(d=>d.vendorId===0x0d28 && d.productId===0x0204);
    if(devices.length!==1)throw Error('Exactly one already authorized akaLinkPro required');
    document.querySelector('#d-connect').click();
  `);
  await waitFor('window.__tools.dbg.session.connected && !window.__tools.dbg._connecting && window.__tools.dbg.session.idcode===0x1000563d');
  report.backup=await c.eval(`
    const d=__tools.dbg,s=d.session;d._stopWatch();
    return await s.exclusive(async()=>{
      if(s.port.chipId!=='hpm6880'||s.probe.device.nativeService||s.clockHz!==8000000)throw Error('Expected HPM6880 imported profile over WebUSB at 8 MHz');
      const h=window.__hpmBackup={wasHalted:await s._pollHalted(),writes:[],mutated:false};
      if(!h.wasHalted)await s.halt();
      h.regs={pc:await s.readReg('pc'),a0:await s.readReg('a0'),dcsr:await s.readReg('dcsr')};
      h.ram=await s.memRead(${base},${end-base});
      const check=await s.memRead(${base},${end-base});
      if(h.ram.length!==${end-base} || !h.ram.every((b,i)=>b===check[i]))throw Error('Unstable ILM backup');
      const write=s.dm.writeMem.bind(s.dm);
      s.dm.writeMem=async(addr,data,...rest)=>{
        if(addr<${base} || addr+data.length>${end})throw Error('Blocked write outside approved ILM interval');
        h.mutated=true;h.writes.push({addr,length:data.length});
        return await write(addr,data,...rest);
      };
      return {wasHalted:h.wasHalted,regs:h.regs,ram:[...h.ram],tap:s.idcode,dtmcs:s.dm.lastDtmcs,dmstatus:s.dm.lastDmstatus,
        sbcs:await s.dm.dmiRead(0x38),misa:await s.dm.readReg(0x301),mvendorid:await s.dm.readReg(0xf11),marchid:await s.dm.readReg(0xf12),mimpid:await s.dm.readReg(0xf13),
        clockHz:s.clockHz,sbaFences:s.port.memory.sbaReadForbidden};
    });
  `);
  assert.equal(report.backup.misa>>>30,1,'RV32 MXL');
  await writeFile(`${out}/hardware-backup.json`,JSON.stringify(report.backup,null,2)+'\n',{flag:'wx'});
  await upload('#d-src-dir','web/tools/selftest/fixtures/dbg-cfg');
  await waitFor('window.__tools.dbg.src.ready');
  await upload('#d-elf-file',elf);
  await waitFor("window.__tools.dbg.sym?.find('main')?.addr===0x18008");
  await c.eval(`
    const root=await navigator.storage.getDirectory(),dir=await root.getDirectoryHandle('hpm-ram-e2e-'+Date.now(),{create:true});
    const f=await dir.getFileHandle('hpm6800_ram.elf',{create:true}),w=await f.createWritable();
    await w.write(Uint8Array.from(atob(${JSON.stringify(input.toString('base64'))}),ch=>ch.charCodeAt(0)));await w.close();
    window.showDirectoryPicker=async()=>dir;
    document.querySelector('#d-image-pick').click();
  `);
  await waitFor('!!window.__tools.dbg.imageFiles.root');
  report.download=await c.eval(`
    const d=__tools.dbg,s=d.session;const result=await d.runLine('load_image hpm6800_ram.elf');
    if(result.error)throw Error(result.error);
    const bytes=await s.exclusive(()=>s.memRead(${base},${end-base}));
    const {PROGBUF_FENCE}=await import('/app/legacy/flash/hpm/jtag.js');
    await s.exclusive(()=>s.dm.execProgbuf(PROGBUF_FENCE));
    return {bytes:[...bytes],lines:result.lines.map(l=>l.t)};
  `);
  assert.deepEqual(report.download.bytes,expected);
  const snapshot=()=>c.eval(`const d=__tools.dbg,s=d.session;return await s.exclusive(async()=>({
    pc:await s.readReg('pc'),a0:await s.readReg('a0'),halted:await s._pollHalted(),
    source:document.querySelector('#d-src-pos').textContent,highlight:Number(document.querySelector('#d-src .srcrow.cur')?.dataset.line),
    sourceText:document.querySelector('#d-src').innerText.includes('addi a0')
  }));`);
  await c.eval(`const d=__tools.dbg;for(const cmd of ['r pc 0x18000','rc main']){const result=await d.runLine(cmd);if(result.error)throw Error(result.error);}`);
  report.main=await snapshot();
  assert.deepEqual([report.main.pc,report.main.a0,report.main.highlight],[0x18008,0,14]);
  report.steps=[];
  for(const [pc,a0,line] of [[0x1800c,1,16],[0x18010,2,18]]){
    await c.eval("document.querySelector('#d-step-over').click();");
    await waitFor('!window.__tools.dbg.session.busy');
    const step=await snapshot();assert.deepEqual([step.pc,step.a0,step.highlight],[pc,a0,line]);report.steps.push(step);
  }
  assert.ok([report.main,...report.steps].every(s=>s.halted&&s.sourceText));
  report.passed=true;
} catch(e){report.error=e.message;report.passed=false;process.exitCode=1;}
finally{
  if(c.ws){
    try{report.cleanup=await c.eval(`
      const d=__tools?.dbg,s=d?.session,h=window.__hpmBackup;
      if(!s?.connected)return {connected:false};
      const result=await s.exclusive(async()=>{
        if(!h)return {};
        const r={wasHalted:h.wasHalted};
        if(h.mutated){
          if(!await s._pollHalted())await s.halt();
          await s.bpClear();await s.memWrite(${base},h.ram);
          const check=await s.memRead(${base},h.ram.length);
          if(check.length!==h.ram.length || !h.ram.every((b,i)=>b===check[i]))throw Error('ILM restoration mismatch');
          const {PROGBUF_FENCE}=await import('/app/legacy/flash/hpm/jtag.js');
          await s.dm.execProgbuf(PROGBUF_FENCE);
          for(const reg of ['a0','dcsr','pc'])await s.writeReg(reg,h.regs[reg]);
          for(const reg of ['a0','pc'])if(await s.readReg(reg)!==h.regs[reg])throw Error('Register restoration mismatch: '+reg);
          const dcsr=await s.readReg('dcsr');
          if((dcsr^h.regs.dcsr)&~0x1c0)throw Error('DCSR restoration mismatch outside read-only cause');
          r.dcsrCauseBefore=(h.regs.dcsr>>>6)&7;r.dcsrCauseAfter=(dcsr>>>6)&7;
          r.ramRestored=r.registersRestored=true;
        }
        if(!h.wasHalted){await s.run();if(!await s._waitResumed(h.regs?.pc))throw Error('Could not verify original firmware resumed');}
        r.writes=h.writes;r.restoredHaltState=(await s._pollHalted())===h.wasHalted;
        return r;
      });
      await d.disconnect();result.disconnected=!s.connected;return result;
    `);
    if(report.passed)assert.ok(report.cleanup.ramRestored&&report.cleanup.registersRestored&&report.cleanup.restoredHaltState&&report.cleanup.disconnected);
    }catch(e){report.cleanupError=e.message;report.passed=false;process.exitCode=1;}
    c.close();
  }
  if(locked&&!report.cleanupError)await rmdir(lock);
  await writeFile(`${out}/hardware.json`,JSON.stringify(report,null,2)+'\n',{flag:'wx'});
  console.log(JSON.stringify(report,null,2));
}
