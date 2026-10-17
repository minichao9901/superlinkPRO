import assert from 'node:assert/strict';
import {signalLevels,waveform,dacTable,WAVES} from '../../app/analog/model.js';
assert.deepEqual(signalLevels({min:.5,max:2.5},'range'),{amplitude:1,offset:1.5,min:.5,max:2.5,vpp:2});
assert.deepEqual(signalLevels({vpp:2,offset:1.5},'vpp'),signalLevels({amplitude:1,offset:1.5}));
assert.throws(()=>signalLevels({min:2,max:1},'range'),/最大/);
assert.throws(()=>signalLevels({vpp:-1,offset:1},'vpp'),/非负/);
assert.throws(()=>signalLevels({amplitude:NaN,offset:1}),/非负/);
const options={shape:'sine',rate:10000,frequency:99,amplitude:1,offset:1.65,bits:12,reference:3.3,phase:90};
const caps={bits:12,fullScale:3.3,maxRate:100000,maxPoints:4096};
const table=dacTable(options,caps);assert.equal(table.codes.length,101);assert.equal(table.actualFrequency,10000/101);
assert.deepEqual(table.codes,table.rows.map(r=>r.code),'preview/CSV use exactly the uploaded LUT');
assert.equal(table.rows[0].volts,2.65);
assert.deepEqual(waveform({...options,phase:-270}),waveform({...options,phase:90}));
for(const shape of Object.keys(WAVES)){
 const t=dacTable({...options,shape,frequency:100,points:64,duty:25},caps);
 assert.ok(t.codes.every(n=>n>=0&&n<=4095));
 assert.equal(t.actualFrequency,['dc','noise'].includes(shape)?null:100);
}
const pulse=dacTable({...options,shape:'pulse',phase:0,frequency:100,duty:25},caps);
assert.equal(pulse.rows.filter(r=>r.volts===2.65).length,25);
assert.throws(()=>dacTable({...options,amplitude:2},caps),/不自动削顶/);
assert.throws(()=>dacTable({...options,frequency:1},caps),/点数/);
console.log('DAC generator: linked min/max/Vp/Vpp/common-mode, phase, duty, all presets, coherent preview/upload, capability/clipping bounds PASS');
