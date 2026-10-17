import assert from 'node:assert/strict';
import {
  debugCommands,
  featureCommand,
  getRandomFlowBoard,
  makeRandomFlowPlan,
  randomFlowResultPath,
} from './hw-random-flow-plan.mjs';

const planA = makeRandomFlowPlan(20261005, 3);
const planB = makeRandomFlowPlan(20261005, 3);
assert.deepEqual(planA, planB, 'fixed seed reproduces the same scenario sequence');
assert.deepEqual(planA.map(x => x.step), [1, 2, 3]);
assert.equal(planA.filter(x => x.kind === 'feature').length, 2);
assert.equal(planA.filter(x => x.kind === 'debug').length, 1);
assert.ok(planA.filter(x => x.kind === 'feature').every(x => x.cycles === 1 && [0, 1].includes(x.alt)));
assert.throws(() => getRandomFlowBoard('unknown'), /只支持/);

const h743Feature = featureCommand('h743', { step: 1, cycles: 1, alt: 0 }, 'unit-run');
assert.equal(h743Feature.script, 'tools/selftest/hw-campaign.mjs');
assert.ok(h743Feature.args.includes('--board=h743'));
assert.ok(h743Feature.args.includes('--local'));
assert.ok(h743Feature.args.includes('--out=tmp/hw-random-flow-h743-unit-run-step-1-campaign.json'));

const hpmFeature = featureCommand('6800evk', { step: 2, cycles: 1, alt: 1 }, 'unit-run');
assert.equal(hpmFeature.script, 'tools/selftest/hw-campaign-hpm.mjs');
assert.ok(!hpmFeature.args.some(x => x.startsWith('--board=')), 'HPM campaign has a dedicated RISC-V entry point');
assert.ok(hpmFeature.args.includes('--alt=1'));

const h743Debug = debugCommands('h743', { step: 2 }, 'unit-run');
assert.equal(h743Debug[0].script, 'tools/selftest/flash-elf.mjs');
assert.ok(h743Debug[0].args.includes('--board=h743'));
assert.equal(h743Debug[1].script, 'tools/selftest/dbg-hw-stress.mjs');
assert.ok(h743Debug[1].args.includes('--board=h743'));
assert.ok(h743Debug[1].args.some(x => x.startsWith('--out=tmp/hw-random-flow-h743-unit-run-step-2-debug.json')));

const hpmDebug = debugCommands('6800evk', { step: 3 }, 'unit-run');
assert.ok(hpmDebug[0].args.includes('--board=6800evk'));
assert.equal(hpmDebug[1].script, 'tools/selftest/dbg-hw-riscv.mjs');
assert.ok(!hpmDebug[1].args.some(x => x.startsWith('--board=')), 'RISC-V stress uses its dedicated board profile');
assert.equal(randomFlowResultPath('f103cb'), 'tmp/hw-random-flow-result.json', 'keep the F103CB result path used by the first run');
assert.equal(randomFlowResultPath('h743'), 'tmp/hw-random-flow-h743-result.json');
assert.equal(randomFlowResultPath('6800evk'), 'tmp/hw-random-flow-6800evk-result.json');

console.log('hw-random-flow: seeded plans and F103CB/H743/HPM command routing PASS');
