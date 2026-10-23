from pathlib import Path
import json,subprocess,sys,re,hashlib,time
run=Path('E:/superlinkPRO/docs/validation/20261024-000104-trace-web-cli-9f57c7')
data=Path('C:/Users/Administrator/.codex/validation/20261024-000104-trace-web-cli-9f57c7')
root=Path('C:/Users/Administrator/.codex/worktrees/etm-kvdat-fastpath/superlinkPRO')
cli=[sys.executable,'-X','utf8',str(root/'etm/trace.py'),'--token-file',str(data/'cli-token')]
def call(name,commands):
 script=run/(name+'.commands.txt');script.write_text('\n'.join(commands),encoding='utf8')
 out=run/(name+'.json');err=run/(name+'.stderr.log')
 with out.open('xb') as f,err.open('xb') as e:
  p=subprocess.run(cli+['web','command','--script',str(script)],cwd=root,stdout=f,stderr=e,timeout=210)
 v=json.loads(out.read_text(encoding='utf8'))
 if p.returncode or not v['ok']:raise RuntimeError(str(v.get('error')))
 return v['result']
size=2*1024*1024
for round in (3,):
 result=call('backup-flash-%d'%round,['halt']+['md 0x%x 4096'%(0x08000000+n) for n in range(0,size,4096)])
 image=bytearray()
 for command in result['results']:
  if not command['command'].startswith('md '):continue
  before=len(image)
  for row in command['lines']:
   match=re.match(r'^0x([0-9a-fA-F]{8})  ([0-9A-Fa-f ]+)  \|',row['t'])
   if match:
    address=int(match[1],16)
    if address!=0x08000000+len(image):raise RuntimeError('non-contiguous read')
    image.extend(bytes.fromhex(match[2]))
  if len(image)-before!=4096:raise RuntimeError('short block')
 if len(image)!=size:raise RuntimeError('short image')
 file=data/('original-flash-%d.bin'%round)
 with file.open('xb') as f:f.write(image)
 print(round,len(image),hashlib.sha256(image).hexdigest(),flush=True)
a=(data/'original-flash-1.bin').read_bytes();b=(data/'original-flash-3.bin').read_bytes()
assert a==b
call('flash-after-resume',['c'])
(run/'flash-after-summary.json').write_text(json.dumps(dict(bytes=len(b),sha256=hashlib.sha256(b).hexdigest(),identicalToBefore=True),indent=2),encoding='utf8')
