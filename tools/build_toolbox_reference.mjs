/** Generate an offline, categorized inventory. Runtime describe remains authoritative. */
import {readFile,writeFile} from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import {resolve,dirname} from 'node:path';
const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const require=createRequire(resolve(root,'web/package.json'));
const {parseHTML}=require('linkedom');
const catalog=JSON.parse(execFileSync(process.env.PYTHON||'python',['-X','utf8',resolve(root,'toolbox.py'),'catalog'],{encoding:'utf8'})).value;
const html=await readFile(resolve(root,'web/index.html'),'utf8');
const {document}=parseHTML(html);
catalog.globalControls=['btn-global-reset','global-nrst-open-drain','btn-fullscreen','btn-help'].map(id=>({id,selector:'#'+id,label:document.getElementById(id)?.textContent||document.getElementById(id)?.title||''}));
const tabs=Array.from(document.querySelectorAll('#tabs [data-tab]'),e=>e.dataset.tab);
if([...new Set(tabs)].sort().join()!==catalog.pages.map(p=>p.id).sort().join())throw Error('网页分类和CLI分类不一致');
for(const p of catalog.pages){
  const source=p.id==='etm'?'etm/web/index.html':null;
  const d=source?parseHTML(await readFile(resolve(root,source),'utf8')).document:document;
  const page=source?d.getElementById('etm-app'):d.getElementById('tab-'+p.id);
  const controls=Array.from(page.querySelectorAll('input,select,textarea,button,a[download],[role="button"],[data-v]'));
  p.controls=controls.map(e=>({id:e.id||null,tag:e.tagName.toLowerCase(),type:e.getAttribute('type'),
    label:(e.getAttribute('aria-label')||e.getAttribute('title')||e.textContent||e.getAttribute('placeholder')||'').trim().replace(/\s+/g,' ').slice(0,160),
    ...(e.tagName==='SELECT'?{options:Array.from(e.querySelectorAll('option'),o=>({value:o.getAttribute('value')??o.textContent,label:o.textContent}))}:{})}));
  // A source location index, NOT inferred callable signatures. Closures, private
  // methods, generated controls and instance-only methods require live describe.
  const text=await readFile(resolve(root,p.source),'utf8');
  p.methodSourceHints=Array.from(text.matchAll(/^  (?:async )?([A-Za-z][A-Za-z0-9_]*)\s*\(/gm),m=>({name:m[1],line:text.slice(0,m.index).split('\n').length}))
    .filter(m=>!['constructor','init','onSession','subscribe'].includes(m.name));
}
catalog.inventory='静态源码清单；动态控件、当前选项、可调用方法/targets以 page describe 为准；不代表硬件验收';
const output=JSON.stringify(catalog,null,2)+'\n';
const path=resolve(root,'docs/toolbox-cli-reference.json');
if(process.argv.includes('--check')){
  if(await readFile(path,'utf8')!==output)throw Error('接口清单过期，请运行 node tools/build_toolbox_reference.mjs');
}else await writeFile(path,output);
console.log(`CLI reference: ${catalog.pages.length} pages, ${catalog.pages.reduce((n,p)=>n+p.controls.length,0)} static controls`);
