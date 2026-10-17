"""核对真实 SPI 执行流：直接分支落点、闭合发送的 TXDR 写入次数与包覆盖。"""
import argparse,json,re
from collections import Counter
from pathlib import Path
parser=argparse.ArgumentParser();parser.add_argument('record',type=Path);args=parser.parse_args()
r=json.loads((args.record/'decoded.json').read_text(encoding='utf8'));es=r['events']
assert r['status']['lastErrorCode']==0
assert all(r['quality'][k]==0 for k in ('overlapBytes','unaccountedBytes','packetIssues','overflowPackets'))
branches=0
for e,n in zip(es,es[1:]):
    if e['kind']!='instruction' or n['kind']!='instruction':continue
    if e['flow'] not in ('call','branch') or e.get('executed') is None:continue
    target=re.search(r'(?:^|, )#(0x[0-9a-f]+)$',e['operands'])
    if not target:continue
    expected=int(target[1],16) if e['executed'] else e['pc']+e['size']
    assert n['pc']==expected,(e,n,expected)
    branches+=1
counts=[];active=None;calls=0
for i,e in enumerate(es[:-1]):
    if e['kind']!='instruction':continue
    n=es[i+1]
    if e['flow']=='call' and e.get('executed') is True and n['kind']=='instruction' and n['function']=='HAL_SPI_Transmit':
        assert active is None
        active=0;calls+=1
    if e['function']!='HAL_SPI_Transmit':continue
    # This verified build uses the byte TXDR store at HAL SPI line 959.
    if e['mnemonic']=='strb.w' and e['operands']=='r3, [r2, #0x20]' and e['line']==959:
        assert e['executed'] is True
        if active is not None:active+=1
    if e['flow']=='return' and e.get('executed') is True and active is not None:
        counts.append(active);active=None
assert counts and set(counts)=={100},Counter(counts)
report=dict(record=args.record.name,branchTransitionsChecked=branches,
            observedSpiCalls=calls,closedSpiCalls=len(counts),bytesPerClosedTransfer=100,
            partialTransferBytes=active,unknownPredicates=r['quality']['unknownExecution'],
            scope='只核对此版本真实捕获中的控制流与发送次数；不证明所有变量值或绝对无损')
(args.record/'spi-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
print(json.dumps(report,ensure_ascii=False),flush=True)
