import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { Cdp, sleep } from './cdp-lib.mjs';
const c = new Cdp();
const dir = process.argv[2];
const corpus = process.argv[3];
assert.ok(dir, 'Usage: node dbg-cfg-page.test.mjs INPUT_OUTPUT_DIRECTORY [OPENOCD_TCL_DIRECTORY]');
const fixtures = 'web/tools/selftest/fixtures/dbg-cfg';
const results = {};
async function waitFor(expr){
  const until = Date.now()+12000;
  do { if(await c.eval(`return !!(${expr});`).catch(()=>false)) return; await sleep(45); } while(Date.now()<until);
  throw Error(expr+'\n'+await c.eval("return document.querySelector('#d-out').innerText.slice(-2800);"));
}
async function upload(selector,path){
  const {root}=await c.send('DOM.getDocument');
  const {nodeId}=await c.send('DOM.querySelector',{nodeId:root.nodeId,selector});
  await c.send('DOM.setFileInputFiles',{nodeId,files:[resolve(path)]});
}
async function importCfg(name, input = dir){
  await upload('#d-cfg-file',`${input}/${name}`);
  await waitFor(`!__tools.dbg._cfgImporting && __tools.dbg._activeCfg()?.name===${JSON.stringify(name)}`);
  return await c.eval('return __tools.dbg._activeCfg();');
}
async function importFromDirectory(directory, entry){
  await upload('#d-cfg-dir', directory);
  await waitFor(`!__tools.dbg._cfgImporting && __tools.dbg.cfgDirectory?.has(${JSON.stringify(entry)})`);
  await c.eval(`
    const select = document.querySelector('#d-cfg-entry');
    select.value = ${JSON.stringify(entry)};
    select.dispatchEvent(new Event('change'));
  `);
  await waitFor(`!__tools.dbg._cfgImporting && __tools.dbg._activeCfg()?.name===${JSON.stringify(entry)}`);
  return await c.eval('return __tools.dbg._activeCfg();');
}
async function command(line,expected){
  await c.eval(`document.querySelector('#d-cmd').value=${JSON.stringify(line)};document.querySelector('#d-run').click();`);
  await waitFor(`document.querySelector('#d-out').innerText.includes(${JSON.stringify(expected)}) && !__tools.dbg.session._opBusy`);
}
try{
  await c.connect();
  const run = String(Date.now());
  await c.send('Page.navigate',{url:`http://127.0.0.1:8899/?modules=source&cfg-e2e=${run}#dbg`});
  await waitFor(`new URLSearchParams(location.search).get('cfg-e2e')===${JSON.stringify(run)} &&
    !!window.__tools?.dbg && !window.__useWebBundle && document.querySelector("#d-arm-target option[value=stm32f103]")`);
  if(corpus){
    results.atmel = await importFromDirectory(corpus, 'board/atmel_sam4e_ek.cfg');
    assert.equal(results.atmel.target,'SAM4E16E.cpu');
    assert.deepEqual(results.atmel.sourced,['target/at91sam4sXX.cfg','target/at91sam4XXX.cfg']);
  }
  results.nordic = await importCfg('nrf52.cfg');
  results.nordicBoard = await importCfg('nordic_nrf52_dk.cfg');
  assert.equal(results.nordic.dpidr,0x2ba01477);
  assert.equal(results.nordicBoard.family,'nrf52');
  results.stm32 = await c.eval(`
    const sel=document.querySelector('#d-arm-target');sel.value='stm32h7';sel.dispatchEvent(new Event('change'));
    return {selected:sel.value,stm32Options:[...sel.options].filter(o=>o.value.startsWith('stm32')).length,
      activeCfg:__tools.dbg._activeCfg(),keptCfg:__tools.dbg.openOcdConfig.name,manualClockVisible:!document.querySelector('#d-clock-row').hidden,
      selectedTarget:__tools.sessionContext.target.id};
  `);
  assert.equal(results.stm32.selected,'stm32h7');assert.equal(results.stm32.selectedTarget,'stm32h7');
  assert.equal(results.stm32.activeCfg,null);assert.equal(results.stm32.keptCfg,'nordic_nrf52_dk.cfg');
  assert.ok(results.stm32.manualClockVisible && results.stm32.stm32Options >= 8);
  results.gd32 = await importCfg('gd32vf103.cfg');
  assert.equal(results.gd32.arch,'riscv');assert.equal(results.gd32.tapIdcode,0x1000563d);
  results.generic = await importFromDirectory(fixtures, 'rv_board.cfg');
  assert.deepEqual(results.generic.sourced,['rv_sim.cfg']);
  assert.equal(results.generic.hart,2);
  const image = (await readFile(`${dir}/rv_ram.elf`)).toString('base64');
  await c.eval(`
    if((await navigator.usb.getDevices()).length) throw Error('Use an isolated browser profile without USB authorization');
    const {SimTarget}=await import('/tools/selftest/hpm-sim.mjs');
    const {WebUsbDapProbe:P}=await import('/app/rtt/dap-webusb.js');
    const {AkaLinkHid}=await import('/app/hid/probe.js');
    const {DAP}=await import('/app/flash/hpm/jtag.js');
    const sim=window.cfgTarget=new SimTarget({idcode:0x12345679,ramBase:0x20000000});
    sim.dtmcs=0x6061;
    let tdata1=0x21800000,tdata2=0;
    const abstract=sim._onAbstract.bind(sim);
    sim._onAbstract=function(cmd){
      const reg=cmd&0xffff,write=!!(cmd&(1<<16));
      if(reg===0x7a0){if(!write)this.dm.data0=0;return;}
      if(reg===0x7a1){if(write)tdata1=this.dm.data0||0x21800000;else this.dm.data0=tdata1;return;}
      if(reg===0x7a2){if(write)tdata2=this.dm.data0;else this.dm.data0=tdata2;return;}
      abstract(cmd);
    };
    sim._onResume=function(){
      for(let i=0;i<4;i++){
        if((tdata1&4)&&this.pc===tdata2){this.halted=true;return;}
        if(this._loadWord(this.pc)!==0x00150513) throw Error('Unexpected RISC-V fixture instruction');
        this.regs[10]++;this.pc+=4;
        if(this.dcsr&4){this.halted=true;return;}
      }
      throw Error('Fixture did not stop');
    };
    const updateIR=sim._onUpdateIR.bind(sim),updateDR=sim._onUpdateDR.bind(sim);
    sim._onUpdateIR=s=>{if(s.irBits!==6)throw Error('Wrong JTAG IR width '+s.irBits);updateIR(s);};
    sim._onUpdateDR=s=>{if((s.ir&0x1f)===0x11&&s.drBits!==40)throw Error('Wrong DMI scan width '+s.drBits);updateDR(s);};
    const device={opened:false};
    P.authorized=async()=>[device];
    P.open=async (_device,opts)=>{
      if(!opts.skipTargetInit)throw Error('RISC-V attached through SWD');device.opened=true;
      return {device,disconnect:async()=>{device.opened=false;},setClock:async hz=>{sim.requestedHz=hz;},async _ctrl(cmd,body){
        if(cmd===DAP.CONNECT)return Uint8Array.of(body[0]);
        if(cmd===DAP.JTAG_CONFIGURE){sim.configure=[...body];return Uint8Array.of(0,1);}
        if(cmd!==DAP.JTAG_SEQUENCE)throw Error('Unexpected DAP command '+cmd);
        const seqs=[];let offset=1;
        for(let i=0;i<body[0];i++){
          const info=body[offset++],clocks=(info&63)||64,size=Math.ceil(clocks/8);
          seqs.push({info,clocks,tdi:body.subarray(offset,offset+size),captureBytes:info&128?size:0});offset+=size;
        }
        if(offset!==body.length)throw Error('Invalid DAP packet');
        return Uint8Array.of(0,...(await sim.jtagSequences(seqs)).flatMap(x=>[...x]));
      }};
    };
    for(const m of ['reconnect','xfer','riscvStop','stop','close'])AkaLinkHid.prototype[m]=async()=>{};
    const root=await navigator.storage.getDirectory(), folder=await root.getDirectoryHandle('cfg-rv-'+Date.now(),{create:true});
    const file=await folder.getFileHandle('rv_ram.elf',{create:true});const stream=await file.createWritable();
    await stream.write(Uint8Array.from(atob(${JSON.stringify(image)}),ch=>ch.charCodeAt(0)));await stream.close();
    window.showDirectoryPicker=async()=>folder;
    document.querySelector('#d-connect').click();
  `);
  await waitFor('__tools.dbg.session.connected && !__tools.dbg._connecting && __tools.dbg.session.halted');
  results.connected = await c.eval(`const s=__tools.dbg.session;return {tap:s.idcode,ir:cfgTarget.configure[1],hart:s.dm.hart,dmiBits:s.dm.dmiBits,tck:cfgTarget.requestedHz,halted:s.halted};`);
  assert.deepEqual(results.connected,{tap:0x12345679,ir:6,hart:2,dmiBits:40,tck:1500000,halted:true});
  await c.eval("document.querySelector('#d-image-pick').click();");
  await waitFor('!!__tools.dbg.imageFiles.root');
  await command('load_image rv_ram.elf','downloaded 12 bytes');
  await upload('#d-elf-file',`${dir}/rv_ram.elf`);
  await waitFor("__tools.dbg.sym?.find('main')?.addr===0x20000100");
  await upload('#d-src-dir',fixtures);
  await waitFor('__tools.dbg.src.ready');
  await command('r pc 0x20000100','→  main+0x0');
  await waitFor('__tools.dbg.session.pc===0x20000100 && __tools.dbg.srcCur?.line===7');
  const snap=()=>c.eval(`const d=__tools.dbg,s=d.session;return {pc:s.pc,loc:d.sym.at(s.pc)?.line,a0:cfgTarget.regs[10],halted:s.halted,highlight:Number(document.querySelector('#d-src .srcrow.cur')?.dataset.line),source:document.querySelector('#d-src').innerText.includes('addi')};`);
  results.main=await snap();
  for(const line of [9,11]){
    await c.eval("document.querySelector('#d-step-over').click();");
    await waitFor(`!__tools.dbg.session.busy && __tools.dbg.sym.at(__tools.dbg.session.pc)?.line===${line}`);
    (results.steps??=[]).push(await snap());
  }
  assert.deepEqual(results.steps.map(x=>[x.loc,x.a0,x.halted]),[[9,1,true],[11,2,true]]);
  assert.ok([results.main,...results.steps].every(x=>x.source && x.highlight===x.loc));
  results.memory=await c.eval(`return {code:[...cfgTarget.ram.slice(0x100,0x10c)],sbaWrites:cfgTarget.stats.sbaWrites,flashPristine:cfgTarget.flash.every(x=>x===0xff),targetResets:cfgTarget.havereset};`);
  assert.deepEqual(results.memory.code,[0x13,0x05,0x15,0x00,0x13,0x05,0x15,0x00,0x6f,0x00,0x00,0x00]);
  assert.ok(results.memory.sbaWrites>0 && results.memory.flashPristine && !results.memory.targetResets);
  await c.eval("document.querySelector('#d-disconnect').click();");
  await waitFor('!__tools.dbg.session.connected && !__tools.dbg._disconnecting');
  results.passed=true;
} catch(e){ results.error=e.message;process.exitCode=1; }
finally{ await writeFile(`${dir}/browser.json`,JSON.stringify(results,null,2)); console.log(JSON.stringify(results,null,2));c.close(); }
