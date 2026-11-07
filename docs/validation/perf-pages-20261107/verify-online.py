import concurrent.futures,datetime,hashlib,json,time
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request,urlopen

ROOT=Path.cwd();BASE='https://minichao9901.github.io/superlinkPRO/';HEAD='4406462'
import subprocess
HEAD=subprocess.check_output(['git','rev-parse',HEAD],cwd=ROOT,text=True).strip()
expected=json.loads((ROOT/'dist/manifest.json').read_text())
def request(url,method='GET'):
    return urlopen(Request(url,method=method,headers={'User-Agent':'superlinkPRO-release-verification','Cache-Control':'no-cache'}),timeout=60)
run=None
for attempt in range(40):
    with request('https://api.github.com/repos/minichao9901/superlinkPRO/actions/workflows/pages.yml/runs?per_page=5&check='+str(time.time_ns())) as response:runs=json.load(response)['workflow_runs']
    run=next((r for r in runs if r['head_sha']==HEAD),None)
    if run and run['status']=='completed':break
    print('Waiting for Pages web.4 deployment',flush=True);time.sleep(10)
else:raise TimeoutError('Pages did not finish')
assert run['conclusion']=='success',run['html_url']
for attempt in range(12):
    with request(BASE+'downloads/manifest.json') as response:manifest=json.load(response)
    if manifest==expected:break
    print('Waiting for current manifest at normal URL',flush=True);time.sleep(5)
else:raise AssertionError('Normal URL manifest is stale')
def asset(path):
    with request(BASE+quote(path)+'?verify='+HEAD) as response:data=response.read();status=response.status
    local=(ROOT/'web'/('index.html' if not path else path)).read_bytes()
    canonical=(lambda b:b) if Path(path).suffix in ['.mp4','.png','.zip','.bmp'] else (lambda b:b.replace(b'\r\n',b'\n'))
    assert canonical(data)==canonical(local),path
    return {'path':path or 'index.html','http':status,'bytes':len(data),'sha256':hashlib.sha256(canonical(data)).hexdigest(),'matchesCurrentSource':True}
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    assets=list(pool.map(asset,['','app/site/main.js','app/core/build.js','app/download/view.js','app/app.css','etm/index.html','etm/app/analysis-views.js','samples/anim/花园.mp4']))
url=BASE+'downloads/'+manifest['file'];headInfo=None
with request(url,'HEAD') as response:headInfo={'http':response.status,'bytes':int(response.headers.get('Content-Length','0'))}
assert headInfo['bytes']==manifest['bytes'],headInfo
digest=hashlib.sha256();received=0
with request(url+'?sha256='+manifest['sha256']) as response:
    status=response.status
    while block:=response.read(1048576):received+=len(block);digest.update(block)
assert received==manifest['bytes'] and digest.hexdigest()==manifest['sha256']
result={'checkedAt':datetime.datetime.now(datetime.timezone.utc).isoformat(),'deployment':{k:run[k] for k in ['id','head_sha','status','conclusion','html_url']},'manifest':manifest,'assets':assets,'plainZipHead':headInfo,'fullZip':{'url':url+'?sha256='+manifest['sha256'],'http':status,'bytes':received,'sha256':digest.hexdigest(),'matchesLocalDistribution':True},'passed':True}
(ROOT/'docs/validation/perf-pages-20261107/online.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
