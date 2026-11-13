import test from 'node:test';
import assert from 'node:assert/strict';
import {scopeNextAction,scopeHasWarnings} from '../../app/ui/scope-onboarding.js';
test('empty workspace gives a concrete next step and disappears with data or sampling',()=>{
  assert.equal(scopeNextAction({}).kind,'choose');
  assert.equal(scopeNextAction({canAdopt:true}).kind,'adopt');
  assert.equal(scopeNextAction({canReuse:true}).kind,'reuse');
  assert.equal(scopeNextAction({hasElf:true}).kind,'variables');
  assert.equal(scopeNextAction({hasElf:true,selected:1}),null);
  assert.equal(scopeNextAction({hasElf:true,selected:1,hasHid:true}),null);
  assert.equal(scopeNextAction({hasElf:true,selected:1,hasHid:true,hasTransport:true}).kind,'start');
  assert.equal(scopeNextAction({mock:true}).kind,'start');
  assert.equal(scopeNextAction({hasData:true}),null);assert.equal(scopeNextAction({starting:true}),null);
});
test('nonzero loss and unavailable metrics remain prominent when detailed counters are folded',()=>{
  assert.equal(scopeHasWarnings({}),false);
  for(const flag of ['probeDropped','usbDrop','lost','readErrors','planMismatch','defMismatch','metricsError'])assert.equal(scopeHasWarnings({[flag]:1}),true);
  assert.equal(scopeHasWarnings({store:{overrun:1}}),true);assert.equal(scopeHasWarnings({stream:{resyncs:1}}),true);
});
