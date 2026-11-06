import test from 'node:test';import assert from 'node:assert/strict';
import {downloadHref} from '../../app/download/view.js';
const info={version:'1.1.0-web.3',file:'superlinkPRO-win64.zip'};
test('portable and development downloads remain same-origin',()=>{
 assert.equal(downloadHref(info,'http://127.0.0.1:8878/#download'),'http://127.0.0.1:8878/downloads/superlinkPRO-win64.zip');
});
test('Pages accepts only the exact repository release/version/filename',()=>{
 const expected='https://github.com/minichao9901/superlinkPRO/releases/download/workbench-v1.1.0-web.3/superlinkPRO-win64.zip';
 assert.equal(downloadHref({...info,downloadUrl:expected},'https://minichao9901.github.io/superlinkPRO/'),expected);
 for(const downloadUrl of ['javascript:alert(1)','http://github.com/evil','https://example.com/file.zip',expected.replace('web.3','web.2'),expected+'?redirect=1',expected.replace('superlinkPRO/releases','other/releases')])assert.throws(()=>downloadHref({...info,downloadUrl:downloadUrl},'https://minichao9901.github.io/superlinkPRO/'),/不匹配/);
});
