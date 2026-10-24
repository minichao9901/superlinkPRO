from pathlib import Path
import json,subprocess,sys,hashlib,time
run=Path('E:/superlinkPRO/docs/validation/20261024-0803-h743-cli-release')
data=Path('C:/Users/Administrator/.codex/validation/20261024-0803-h743-cli-release')
root=Path('C:/Users/Administrator/.codex/worktrees/etm-kvdat-fastpath/superlinkPRO')
case=run/('acceptance-'+time.strftime('%H%M%S'));case.mkdir()
out=data/case.name;out.mkdir()
cli=[sys.executable,'-X','utf8',str(root/'etm/trace.py')]
checks=[]
def call(name,args,success=True):
 index=len(checks)+1;prefix=f'{index:03}-{name}';resultfile=case/(prefix+'.json');err=case/(prefix+'.stderr.log')
 cmd=cli+['--json-out',str(resultfile)]+[str(a) for a in args]
 with (case/(prefix+'.stdout.json')).open('xb') as f,err.open('xb') as e:
  started=time.perf_counter();p=subprocess.run(cmd,cwd=root,stdout=f,stderr=e,timeout=220)
 value=json.loads(resultfile.read_text(encoding='utf8'))
 assert value==json.loads((case/(prefix+'.stdout.json')).read_text(encoding='utf8'))
 assert value['ok']==success,(prefix,value.get('error'))
 assert (p.returncode==0)==success,(prefix,p.returncode)
 checks.append(dict(name=name,command=cmd,cwd=str(root),ok=value['ok'],expectedSuccess=success,exitCode=p.returncode,seconds=time.perf_counter()-started,result=str(resultfile)))
 (case/'checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf8')
 print(prefix,'PASS',flush=True)
 return value.get('result') if success else value
record=Path('C:/Users/Administrator/.codex/validation/20261024-000104-trace-web-cli-9f57c7/entry-capture-second')
reference=Path('C:/Users/Administrator/.codex/validation/20261023-230249-trace-cli-h743-1a6f11/capture-20ms-final')
call('doctor',['doctor'])
call('elf-inspect',['elf','inspect','--elf',record/'firmware.elf'])
call('elf-resolve',['elf','resolve','--elf',record/'firmware.elf','--spec','observed_memfill'])
call('elf-odd-rejected',['elf','resolve','--elf',record/'firmware.elf','--spec','*0x0800b44d'],False)
verification=call('verify-live',['verify','--record',record,'--require-decoded'])
assert verification['extent']['complete'] and verification['extent']['actualSeconds']==.02
call('verify-reference',['verify','--record',reference,'--require-decoded'])
for view in ('summary','overview','page','event','window','timeline','issues','functions','hotspots','flame','integrity'):
 value=call('query-'+view,['query','--record',record,'--view',view,'--limit','10'])
 if view=='summary':assert value['captureExtent']['actualSeconds']==.02
call('packets',['query','--record',record,'--view','page','--table','packets','--limit','8'])
files=json.loads((reference/'sources.json').read_text(encoding='utf8'));sourcefile=next(iter(files))
call('source',['query','--record',reference,'--view','source','--file',sourcefile])
call('search',['query','--record',reference,'--view','page','--search','main','--hide-skipped','--limit','5'])
call('range',['query','--record',record,'--view','window','--from-seq','10','--to-seq','500','--bins','50'])
for mode in ('instruction','call','return','exception','issue','source','pc'):
 for direction in ('-1','1'):
  args=['query','--record',reference,'--view','navigate','--mode',mode,'--direction',direction,'--seq','100']
  if mode=='pc':args+=['--pc','0x080009e0']
  call('nav-'+mode+'-'+direction,args)
for kind in ('simplified','source','html','csv','json','events-jsonl','raw','etm','elf','record'):
 call('export-'+kind,['export','--record',reference,'--format',kind,'--out',out/('export.'+kind)])
for kind in ('simplified','source','html','csv','events-jsonl'):
 call('cropped-'+kind,['export','--record',reference,'--format',kind,'--from-seq','10','--to-seq','200','--out',out/('cropped.'+kind)])
raw_before=hashlib.sha256((out/'export.raw').read_bytes()).hexdigest()
call('no-clobber',['export','--record',reference,'--format','raw','--out',out/'export.raw'],False)
assert hashlib.sha256((out/'export.raw').read_bytes()).hexdigest()==raw_before
call('import',['import-record','--input',out/'export.record','--out',out/'imported'])
call('import-verify',['verify','--record',out/'imported','--require-decoded'])
base=call('original-summary',['query','--record',reference,'--view','summary'])
copy=call('imported-summary',['query','--record',out/'imported','--view','summary'])
assert base['summary']==copy['summary'] and base['quality']==copy['quality']
call('record-list',['record','list','--root',out])
call('record-info',['record','info','--record',out/'imported'])
call('record-config',['record','config','--record',out/'imported','--out',out/'config.json'])
new_sources=out/'additional-sources.json';new_sources.write_text(json.dumps({'cli-marker.txt':'attached explicitly'}),encoding='utf8')
attached=call('attach-sources',['record','sources','--record',out/'imported','--sources-json',new_sources])
assert Path(attached['backup']).is_file()
assert call('read-attached-source',['query','--record',out/'imported','--view','source','--file','cli-marker.txt'])['text']=='attached explicitly'
call('verify-after-source',['verify','--record',out/'imported','--require-decoded'])
call('raw-decode',['decode','--input',reference/'capture.kvdat','--elf',reference/'firmware.elf','--config',out/'config.json','--sources-json',reference/'sources.json','--out',out/'raw-decoded'])
rebuilt=call('raw-decoded-summary',['query','--record',out/'raw-decoded','--view','summary'])
assert base['summary']==rebuilt['summary'] and base['quality']==rebuilt['quality']
call('record-redecode',['decode','--record',out/'imported','--out',out/'record-decoded'])
rebuilt=call('record-decoded-summary',['query','--record',out/'record-decoded','--view','summary'])
assert base['summary']==rebuilt['summary'] and base['quality']==rebuilt['quality']
(run/'acceptance-pointer.json').write_text(json.dumps(dict(evidence=str(case),data=str(out),checks=len(checks),passed=True),indent=2),encoding='utf8')
print('ALL CHECKS PASSED',len(checks),flush=True)
