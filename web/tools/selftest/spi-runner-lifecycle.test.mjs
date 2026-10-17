import assert from 'node:assert/strict';
import { SpiRunner } from '../../app/spi/runner.js';
const gate = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const tick = () => new Promise(r => setImmediate(r));
const script = (count, period = 1, group = 1) => ({ errors: [], items: [{ period, group, count, type: 1, payload: new Uint8Array() }] });
for (const failOld of [false, true]){
  const entered = gate(), old = gate(), firstNew = gate(), doneNew = gate(), events = [];
  let calls = 0, newCalls = 0, oldStop;
  const runner = new SpiRunner({ async sendFrames(items, opts){
    if (++calls === 1){ oldStop = opts.shouldStop; entered.resolve(); await old.promise; if (failOld) throw Error('old failure'); }
    else newCalls++;
    return { sent: 1, failed: 0, rsps: [] };
  } }, { onEvent(e){
    events.push(e); if (e.type === 'tick' && newCalls === 1) firstNew.resolve();
    if (e.type === 'stop' && /跑完 5 轮/.test(e.reason)) doneNew.resolve();
  } });
  await runner.start(script(1)); await entered.promise; runner.stop();
  assert.equal(oldStop(), true);
  await runner.start(script(5, 10)); await firstNew.promise;
  const ticksBefore = runner.stat.ticks; old.resolve(); await tick();
  assert.equal(runner.running, true); assert.equal(runner.stat.ticks, ticksBefore);
  assert.equal(runner.stat.errors, 0); assert.equal(oldStop(), true, 'restart cannot revive old stop predicate');
  await doneNew.promise; assert.equal(newCalls, 5); assert.equal(runner.stat.ticks, 5);
  assert.equal(runner._activeGroups.size, 0); assert.equal(runner.timers.size, 0);
}
const onceEntered = gate(), onceRelease = gate();
const once = new SpiRunner({ async sendFrames(){ onceEntered.resolve(); await onceRelease.promise; return { sent: 1 }; } });
const oldStart = once.start(script(0, 0)); await onceEntered.promise; once.stop();
await once.start(script(3, 1000)); onceRelease.resolve(); assert.equal(await oldStart, false);
assert.equal(once.running, true); once.stop();
console.log('spi-runner-lifecycle: old success/error/one-shot cannot publish, stop or revive a new acquisition PASS');
