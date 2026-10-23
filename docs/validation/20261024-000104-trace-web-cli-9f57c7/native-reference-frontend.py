from pathlib import Path
import json,sys,time,hashlib
root=Path('C:/Users/Administrator/.codex/worktrees/etm-kvdat-fastpath/superlinkPRO')
sys.path.insert(0,str(root/'etm/tools'))
from trace_frontend import Frontend
folder=Path('C:/Users/Administrator/.codex/validation/20261024-000104-trace-web-cli-9f57c7/entry-capture-final')
start=time.perf_counter();value=Frontend(folder/'capture.kvdat',dict(clock=0,data=[1,2,3,4],fraction=.25,glitch_ns=4,traceid=2)).run()
result=dict(seconds=time.perf_counter()-start,record=str(folder),tpiuEqual=value['tpiu']==(folder/'tpiu.bin').read_bytes(),etmEqual=value['streams'][2]==(folder/'etm.bin').read_bytes(),framingEqual=value['framing']==json.loads((folder/'framing.json').read_text(encoding='utf8')),inputSha256=hashlib.sha256((folder/'capture.kvdat').read_bytes()).hexdigest())
assert result['tpiuEqual'] and result['etmEqual'] and result['framingEqual']
p=Path('E:/superlinkPRO/docs/validation/20261024-000104-trace-web-cli-9f57c7/native-reference-frontend.json')
with p.open('x',encoding='utf8') as stream:json.dump(result,stream,indent=2)
print(json.dumps(result))
