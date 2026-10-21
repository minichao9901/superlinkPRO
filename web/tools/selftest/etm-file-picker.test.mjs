import test from 'node:test';import assert from 'node:assert/strict';
import {bindFilePicker} from '../../../etm/web/app/file-picker.js';

function fixture({fullscreen=true,modern=true,restoreFailure=false}={}){
 const events=[],errors=[],file={name:'history.elf'},input={files:[file],value:'selected',addEventListener(type,fn){this[type]=fn;},click(){events.push('legacy-picker');}},button={};
 const doc={fullscreenElement:null};const root={isConnected:true,async requestFullscreen(){events.push('restore');if(restoreFailure)throw Error('activation denied');doc.fullscreenElement=root;}};
 if(fullscreen)doc.fullscreenElement=root;
 const win={document:{fullscreenElement:null},top:{document:doc}};
 if(modern)win.showOpenFilePicker=async()=>{events.push('native-picker');doc.fullscreenElement=null;return [{getFile:async()=>{events.push('file-io');return file;}}];};
 let blocked=false;
 bindFilePicker(button,input,{win,id:'test',types:[],blocked:()=>blocked,consume:async value=>{assert.equal(value,file);events.push('consume');},error:e=>errors.push(e.message)});
 return {events,errors,file,input,button,doc,root,win,block:()=>{blocked=true;}};
}
test('same-origin iframe restores parent fullscreen before IO or upload',async()=>{
 const f=fixture();await f.button.onclick();assert.deepEqual(f.events,['native-picker','restore','file-io','consume']);assert.equal(f.doc.fullscreenElement,f.root);
});
test('ordinary selection keeps legacy input; late result obeys operation guard',async()=>{
 const f=fixture({fullscreen:false});await f.button.onclick();f.block();await f.input.onchange();assert.deepEqual(f.events,['legacy-picker']);assert.equal(f.input.value,'');assert.match(f.errors[0],/等待/);
});
test('cancel restores once without consuming, and rejected restoration never prevents file loading',async()=>{
 const f=fixture();f.win.showOpenFilePicker=async()=>{f.doc.fullscreenElement=null;throw Object.assign(Error('cancel'),{name:'AbortError'});};
 await f.button.onclick();assert.deepEqual(f.events,['restore']);assert.equal(f.errors.length,0);
 const g=fixture({restoreFailure:true});await g.button.onclick();assert.deepEqual(g.events,['native-picker','restore','file-io','consume']);assert.match(g.errors[0],/恢复全屏/);
});
test('legacy change and cancel restore captured fullscreen without forcing normal pages fullscreen',async()=>{
 const f=fixture({modern:false});await f.button.onclick();f.doc.fullscreenElement=null;await f.input.onchange();assert.deepEqual(f.events,['legacy-picker','restore','consume']);
 const g=fixture({modern:false});await g.button.onclick();g.doc.fullscreenElement=null;await g.input.cancel();assert.deepEqual(g.events,['legacy-picker','restore']);
});
