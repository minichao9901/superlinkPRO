import test from 'node:test';
import assert from 'node:assert/strict';
import {rangeFromPixels,wheelRange} from '../../web/app/execution-window.js';
test('drag maps either direction to an inclusive execution interval, clipped at plot edges',()=>{
  assert.deepEqual(rangeFromPixels(20,60,100,1000,1099),{start:1020,end:1060});
  assert.deepEqual(rangeFromPixels(60,20,100,1000,1099),{start:1020,end:1060});
  assert.deepEqual(rangeFromPixels(-5,105,100,1000,1099),{start:1000,end:1099});
});
test('wheel zoom preserves cursor anchor and respects total and singleton ranges',()=>{
  assert.deepEqual(wheelRange({start:0,end:999},{start:0,end:999},.5,true),{start:250,end:749});
  assert.deepEqual(wheelRange({start:0,end:0},{start:0,end:0},.5,true),{start:0,end:0});
  assert.deepEqual(wheelRange({start:250,end:749},{start:0,end:999},.5,false),{start:0,end:999});
});
