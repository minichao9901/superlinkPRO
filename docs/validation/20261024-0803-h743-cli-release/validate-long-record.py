"""Frozen CLI against an existing physical H743 1 s capture; no new acquisition."""
from pathlib import Path
import hashlib,json,os,subprocess,time

run=Path(__file__).resolve().parent
data=Path('C:/Users/Administrator/.codex/validation')/run.name
home=Path(json.loads((run/'portable-validation.json').read_text(encoding='utf8'))['home'])
fixture=Path('C:/Users/Administrator/.codex/validation/20261023-codex-h743-live-19d7d53-db17fe80/portable-1s-import.etmrecord')
out=data/'long-record';out.mkdir()
env=dict(os.environ,PATH=str(Path(os.environ['SystemRoot'])/'System32')+';'+os.environ['SystemRoot'],PYTHONNOUSERSITE='1')
for key in ('PYTHONPATH','PYTHONHOME','WEB_ETM_RECORDS'):env.pop(key,None)
checks=[]
def call(name,args):
    report=run/('long-'+name+'.json');command=[str(home/'etm-bridge.exe'),'--cli','--json-out',str(report),*map(str,args)]
    started=time.perf_counter()
    p=subprocess.run(command,cwd=out,env=env,capture_output=True,text=True,encoding='utf8',timeout=180)
    value=json.loads(p.stdout)
    (run/('long-'+name+'.stderr.log')).write_text(p.stderr,encoding='utf8')
    assert json.loads(report.read_text(encoding='utf8'))==value
    assert p.returncode==0 and value['ok'],(name,value)
    checks.append(dict(name=name,seconds=time.perf_counter()-started,exitCode=p.returncode,command=command))
    print(name,'PASS',round(checks[-1]['seconds'],3),flush=True)
    return value['result']
call('import',['import-record','--input',fixture,'--out',out/'imported'])
before=call('before',['query','--record',out/'imported','--view','overview'])
info=call('extent',['record','info','--record',out/'imported'])
assert info['captureExtent']['actualSeconds']==1 and info['captureExtent']['complete']
decoded=call('redecode',['decode','--record',out/'imported','--out',out/'rebuilt'])
assert decoded['verification']['passed']
after=call('after',['query','--record',out/'rebuilt','--view','overview'])
for key in ('summary','quality','analysis'):assert before[key]==after[key],key
for offset in [0,after['eventCount']//2,max(0,after['eventCount']-150)]:
    page=call('page-'+str(offset),['query','--record',out/'rebuilt','--view','page','--offset',offset,'--limit','150'])
    assert page['rows'] and page['total']==after['eventCount']
call('csv',['export','--record',out/'rebuilt','--format','csv','--from-seq','10000','--to-seq','10100','--out',out/'range.csv'])
result=dict(passed=True,hardware='not-run; existing physical capture',fixture=str(fixture),fixtureSha256=hashlib.sha256(fixture.read_bytes()).hexdigest(),actualSeconds=1,summary=after['summary'],decodeSeconds=decoded['wallSeconds'],checks=checks)
(run/'long-record-validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
print(json.dumps({k:v for k,v in result.items() if k!='checks'},ensure_ascii=False,indent=2))
