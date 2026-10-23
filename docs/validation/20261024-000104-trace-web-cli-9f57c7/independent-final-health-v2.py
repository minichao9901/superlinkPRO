from pathlib import Path
import json,re,subprocess,sys
run=Path('E:/superlinkPRO/docs/validation/20261024-000104-trace-web-cli-9f57c7')
data=Path('C:/Users/Administrator/.codex/validation/20261024-000104-trace-web-cli-9f57c7')
root=Path('C:/Users/Administrator/.codex/worktrees/etm-kvdat-fastpath/superlinkPRO')
state=json.loads((data/'manual-target-state.json').read_text(encoding='utf8'))['target']
cli=[sys.executable,'-X','utf8',str(root/'etm/trace.py'),'--token-file',str(data/'cli-token')]
def call(name,commands):
 script=run/(name+'.commands.txt');script.write_text('\n'.join(commands),encoding='utf8')
 with (run/(name+'.stdout.json')).open('xb') as out,(run/(name+'.stderr.log')).open('xb') as err:
  result=subprocess.run(cli+['web','command','--script',str(script)],cwd=root,stdout=out,stderr=err,timeout=60)
 value=json.loads((run/(name+'.stdout.json')).read_text(encoding='utf8'));assert result.returncode==0 and value['ok'],value
 return value['result']
def word(row):
 line=next(l['t'] for l in row['lines'] if re.match('^0x[0-9a-fA-F]{8}  ',l['t']))
 return int.from_bytes(bytes.fromhex(line.split('  ')[1]),'little')
def write32(address,value):return 'mw 0x%x '%address+' '.join('%02x'%n for n in value.to_bytes(4,'little'))
initial=call('health-v2-initial',['halt','md 0x580244e0 4'])
clock=word(initial['results'][1]);addresses=[a for a,b in state['saved']]
try:
 value=call('health-v2-read-enabled',[write32(0x580244e0,clock|0x10)]+['md 0x%x 4'%a for a in addresses if a!=0x580244e0])
finally:
 final=call('health-v2-cleanup',[write32(0x580244e0,clock),'md 0x580244e0 4','bl','c'])
actual={a:word(row) for a,row in zip([a for a in addresses if a!=0x580244e0],value['results'][1:])}
actual[0x580244e0]=word(final['results'][1]);rows=[]
for address,binding in state['saved']:
 number=actual[address];rows.append(dict(address=hex(address),mask=hex(binding['mask']),before=hex(binding['before']),after=hex(number),equal=((number^binding['before'])&binding['mask'])==0))
summary=dict(controlledMasks=len(rows),differences=[r for r in rows if not r['equal']],rows=rows,cpuRunning=not final['status']['halted'],breakpoints=final['breakpoints'],gpioClockRestored=actual[0x580244e0]==clock,note='GPIO clock was temporarily enabled while halted; gated GPIO reads are not treated as register differences.')
with (run/'independent-final-health-v2.json').open('x',encoding='utf8') as out:json.dump(summary,out,indent=2)
assert summary['cpuRunning'] and not summary['differences'] and not summary['breakpoints'] and summary['gpioClockRestored'],summary
print(json.dumps({k:v for k,v in summary.items() if k!='rows'}))
