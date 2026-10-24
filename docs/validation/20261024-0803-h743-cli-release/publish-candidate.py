"""Publish an explicitly marked candidate without replacing the stable download."""
from pathlib import Path
import argparse,hashlib,json,subprocess,urllib.request

run=Path(__file__).resolve().parent
data=Path('C:/Users/Administrator/.codex/validation')/run.name
parser=argparse.ArgumentParser();parser.add_argument('--revision',required=True);args=parser.parse_args()
tag='etm-cli-v1.0.14-native.14-rc.1'
manifest=json.loads((data/'package-final/manifest.json').read_text(encoding='utf8'))
archive=data/'package-final'/manifest['file']
assert hashlib.sha256(archive.read_bytes()).hexdigest()==manifest['sha256']
assert json.loads((run/'portable-validation.json').read_text(encoding='utf8'))['passed']
assert json.loads((run/'long-record-validation.json').read_text(encoding='utf8'))['passed']
r=subprocess.run(['git','credential','fill'],input='protocol=https\nhost=github.com\n\n',capture_output=True,text=True,timeout=30,check=True)
credential=dict(line.split('=',1) for line in r.stdout.splitlines() if '=' in line)
headers={'Authorization':'Bearer '+credential['password'],'User-Agent':'superlinkpro-release','Accept':'application/vnd.github+json'}
api='https://api.github.com/repos/minichao9901/superlinkPRO'
def call(path,value=None,method=None):
    body=json.dumps(value,ensure_ascii=False).encode() if value is not None else None
    request=urllib.request.Request(api+path,data=body,headers={**headers,'Content-Type':'application/json'},method=method)
    with urllib.request.urlopen(request,timeout=40) as response:return json.load(response)
body=f'''H743 ETM、调试与烧录 CLI 候选版。**本版不是最终真机验收通过的正式版。**

完整 ZIP 含 `etm-trace.cmd` / `etm-bridge.exe --cli`、Python、Node.js、固定 USB 适配依赖、网页原有调试/烧录算法和 C++ 解码工具；无需另装 Python/Node/npm，不依赖 pyOCD。解压后先运行 `etm-trace.cmd doctor`，中文方法见包内 `Trace-CLI-中文说明.md`。

源码 CLI 65 条离线端到端、便携 CLI 65 条端到端与真实 H743 历史 1s 记录的完整重解对照通过。所有包内文件逐项 SHA256 核对；System32-only PATH 与中文空格目录验证通过。

**限制**：当前环境的执行工具在创建启动进程前返回 `blocked by policy`，没有具体理由；最终运行时启动/退出和 H743 新硬件回归未完成。此前早期 worker 的短窗/断点/原镜像烧录结果不等于本包已通过板测。暂未验收源码 DWARF 调试、200/500/1000ms 新采集及异常拔插；F103ZE/H7B0 不在本轮范围。正式站点下载保留 native.13，本候选版不替换它。

ZIP：{manifest['bytes']} bytes；SHA256 `{manifest['sha256']}`。
源码：`{manifest['sourceRevision']}`；发布提交：`{args.revision}`。
验证报告：仓库 `docs/validation/{run.name}/package-report.md`。
'''
release=call('/releases',dict(tag_name=tag,target_commitish=args.revision,name='H743 Trace CLI · 1.0.14-native.14-rc.1（候选版）',body=body,draft=True,prerelease=True,make_latest='false'))
(run/'draft-release-receipt.json').write_text(json.dumps({k:release[k] for k in ['id','html_url','tag_name','draft','prerelease']},ensure_ascii=False,indent=2),encoding='utf8')
uploaded=[]
for path,ctype in [(archive,'application/zip'),(data/'package-final/manifest.json','application/json')]:
    url=release['upload_url'].split('{')[0]+'?name='+path.name
    request=urllib.request.Request(url,data=path.read_bytes(),headers={**headers,'Content-Type':ctype},method='POST')
    with urllib.request.urlopen(request,timeout=600) as response:asset=json.load(response)
    uploaded.append({k:asset[k] for k in ['id','name','size','browser_download_url','state']})
    (run/'uploaded-assets.json').write_text(json.dumps(uploaded,indent=2),encoding='utf8')
    assert asset['state']=='uploaded' and asset['size']==path.stat().st_size
    print('Uploaded',path.name,path.stat().st_size,flush=True)
published=call('/releases/'+str(release['id']),dict(draft=False,prerelease=True,make_latest='false'),method='PATCH')
receipt={k:published[k] for k in ['id','html_url','tag_name','draft','prerelease','published_at']}
receipt['assets']=uploaded
(run/'release-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf8')
print(json.dumps(receipt,ensure_ascii=False,indent=2))
