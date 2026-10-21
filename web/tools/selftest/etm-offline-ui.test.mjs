import test from 'node:test';import assert from 'node:assert/strict';import {ui} from './etm-ui-harness.mjs';
const config={protocol:'ETMv3',idr:0x4114f241,ctrl:0x810,ccer:0x18800,traceid:7};
async function pick(u,id,file){Object.defineProperty(u.$(id),'files',{configurable:true,value:[file]});await u.$(id).onchange();}
test('bare KVDAT imports offline with saved trace ID and physical settings, without target access',async()=>{
 const u=await ui();try{
  await u.upload();const before=u.events.length;
  const session={config,clock:5,data:[6,7,8,9],fraction:.6,glitchNs:2,elfId:'elf-previous-upload'};
  await pick(u,'offline-config-file',new File([JSON.stringify(session)],'session.json'));
  u.model.jobStates=['cancelled'];await pick(u,'capture-file',new File(['wave'],'capture.kvdat'));
  assert.equal(u.target.probe,null);assert.equal(u.events.length,before);assert.equal(u.imports.length,1);
  const form=u.imports[0];assert.deepEqual(JSON.parse(form.get('config')),config);
  assert.equal(form.get('clock'),'5');assert.deepEqual(JSON.parse(form.get('data')),[6,7,8,9]);assert.equal(form.get('fraction'),'0.6');assert.equal(form.get('glitchNs'),'2');
 }finally{u.close();}
});
test('offline import refuses missing snapshot and mismatched historical ELF even if a live target exists',async()=>{
 const u=await ui();try{
  await u.upload();await u.click('connect');u.model.jobStates=['cancelled'];
  await pick(u,'capture-file',new File(['wave'],'capture.kvdat'));assert.equal(u.imports.length,0);assert.match(u.$('job-state').textContent,/session.json/);
  await pick(u,'offline-config-file',new File([JSON.stringify({config,elfSha256:'b'.repeat(64)})],'session.json'));
  await pick(u,'capture-file',new File(['wave'],'capture.kvdat'));assert.equal(u.imports.length,0);assert.match(u.$('job-state').textContent,/ELF.*不匹配/);
 }finally{u.close();}
});
test('record packages stay self-contained; invalid config never becomes an implicit decoder default',async()=>{
 const u=await ui();try{
  await pick(u,'offline-config-file',new File(['{"config": {"traceid":2}}'],'session.json'));assert.equal(u.state.offlineConfig,null);
  u.model.jobStates=['cancelled'];await pick(u,'capture-file',new File(['zip'],'history.ETMRECORD'));
  assert.equal(u.imports.length,1);assert.equal(u.imports[0].has('config'),false);assert.equal(u.imports[0].has('elf'),false);
 }finally{u.close();}
});
