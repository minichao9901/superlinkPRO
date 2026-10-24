import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import vm from 'node:vm';
import {parseHTML} from 'linkedom';

const root=fileURLToPath(new URL('../../../',import.meta.url));
const source='#define MASK 0x20U\n/* block\nif ("still comment") */\nuint32_t call(void) {\n  const char *s = "<script>alert(1)</script>";\n  return MASK; // trailing comment\n}\n';
const code='if (value & MASK/*0x20*/) call("a<b");';
function reader(learning=false){
  const program=`import json,sys\nsys.path.insert(0,'etm/tools')\nfrom source_export import _html_document\nfrom reading_view import document\npayload=json.loads(sys.stdin.read())\ntext=json.dumps(payload).replace('<','\\\\u003c')\nprint(document().replace('__READING_PAYLOAD__',text) if ${learning?'True':'False'} else _html_document(text,None))`;
  const payload={sources:{'main.c':source},rows:[{kind:'code',seq:1,endSeq:2,pc:256,file:'main.c',line:4,text:code}]};
  const html=execFileSync('python',['-X','utf8','-c',program],{cwd:root,input:JSON.stringify(payload),encoding:'utf8'});
  const {document}=parseHTML(html);const script=document.querySelector('script:not([type])').textContent;
  const context=vm.createContext({document,setTimeout,clearTimeout});vm.runInContext(script,context,{timeout:2000});
  return {document,context};
}

test('both HTML exports highlight code without changing its text or interpreting markup',()=>{
  for(const learning of [false,true]){
    const {document}=reader(learning),pre=document.querySelector('#list pre');
    assert.equal(pre.textContent,code);
    for(const name of ['keyword','constant','comment','function','string'])assert.ok(pre.querySelector('.c-'+name),name);
    assert.equal(document.querySelectorAll('script').length,2);
    const lines=[...document.querySelectorAll('#source .text')].map(e=>e.textContent);
    assert.deepEqual(lines,source.split('\n'));
    assert.ok(document.querySelector('#source .c-preprocessor'));
    assert.ok(document.querySelector('#source .c-type'));
    const comment=document.querySelector('#source .src[data-line="3"] .text');
    assert.equal(comment.querySelector('.c-comment').textContent,'if ("still comment") */');
    assert.equal(comment.querySelector('.c-keyword'),null);
  }
});

test('continued strings, comments, escaped quotes and numeric literals retain token boundaries',()=>{
  const {context}=reader();
  const parts=text=>JSON.parse(vm.runInContext('JSON.stringify([...cParts('+JSON.stringify(text)+')])',context));
  const text='u8"a\\"b" /* multi\ncomment */ 0x20UL 1.5e-2f .5f // if\nuint32_t x;';
  const result=parts(text);assert.equal(result.map(x=>x[0]).join(''),text);
  assert.ok(result.some(([t,c])=>t==='/* multi\ncomment */'&&c==='comment'));
  assert.deepEqual(result.filter(x=>x[1]==='number').map(x=>x[0]),['0x20UL','1.5e-2f','.5f']);
  assert.deepEqual(parts('/* open\nstill open'),[['/* open\nstill open','comment']]);
  assert.deepEqual(parts('"a\\\nb"'),[['"a\\\nb"','string']]);
});
