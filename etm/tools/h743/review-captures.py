"""只读复核 CSV 窄脉冲和已保存的 TPIU 流；不会操作探针或 LA。"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tools'))
from trace_port import read_tracks
from tpiu_demux import demux


def review(folder):
    folder=Path(folder).resolve();csv=folder/'capture.csv'
    initial,tracks,start,end=read_tracks(csv)
    digest=hashlib.sha256()
    with csv.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    result=dict(record=str(folder),csvSha256=digest.hexdigest(),spanMs=(end-start)*1000,channels={})
    for channel,edges in tracks.items():
        widths=[round((b[0]-a[0])*1e9,3) for a,b in zip(edges,edges[1:])]
        result['channels'][str(channel)]=dict(transitions=len(edges),minWidthNs=min(widths) if widths else None,
            width2ns=sum(abs(w-2)<.1 for w in widths),width4ns=sum(abs(w-4)<.1 for w in widths))
    # This checks the saved byte stream separately; it is not an assertion that
    # a cached TPIU file has been independently regenerated from this CSV.
    raw=folder/'tpiu.bin'
    if raw.exists():
        try:
            stats,streams=demux(raw.read_bytes())
            result['savedTpiu']=dict(passed=True,path=str(raw),stats=stats)
        except ValueError as error:result['savedTpiu']=dict(passed=False,path=str(raw),error=str(error))
    result['basis']='digital pulse observations; not an analog oscilloscope diagnosis or a lossless-proof'
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('records',nargs='+',type=Path)
    parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args()
    if args.out.exists():parser.error('输出已存在，请使用新的运行文件名，避免覆盖证据')
    rows=[]
    for folder in args.records:
        rows.append(review(folder));print(folder.name,rows[-1]['channels'].get('0'),flush=True)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf8')
