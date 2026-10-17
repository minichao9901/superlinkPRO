"""对同一份真实 CSV 做相位扫描；仅检查物理层/帧，不能证明执行流无损。"""
import argparse,json,hashlib
from pathlib import Path
from trace_port import extract
from tpiu_demux import demux
parser=argparse.ArgumentParser();parser.add_argument('record',type=Path);args=parser.parse_args()
rows=[]
for fraction in (.1,.2,.25,.3,.4,.5,.6,.7,.75,.8,.9):
    physical,buffers=extract(args.record/'capture.csv',0,[1,2,3,4],fraction)
    for phase,raw in enumerate(buffers):
        row=dict(fraction=fraction,phase=phase,clock=physical['clock_hz_average'],margin_ns=physical['min_data_transition_distance_ns'],sync=raw.count(b'\xff\xff\xff\x7f'))
        try:
            frames,streams=demux(raw)
            row.update(frames=frames,etm_bytes=len(streams.get(2,b'')),sha=hashlib.sha256(streams.get(2,b'')).hexdigest())
        except ValueError as e:row['error']=str(e)
        rows.append(row)
    print(fraction,[(x['phase'],x['sync'],x.get('etm_bytes'),x.get('error')) for x in rows[-2:]],flush=True)
(args.record/'phase-scan.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf8')
validation=[]
for fraction in sorted({r['fraction'] for r in rows}):
    candidates=[r for r in rows if r['fraction']==fraction and not r.get('error') and r.get('etm_bytes')]
    if len(candidates)==1:validation.append(candidates[0])
    else:validation.append(dict(fraction=fraction,error='没有唯一有效的 TPIU/ETM 流候选'))
(args.record/'phase-validation.json').write_text(json.dumps(validation,ensure_ascii=False,indent=2),encoding='utf8')
