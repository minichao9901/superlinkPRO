"""只诊断本次 KingstVIS 3.6.6 / <2^32 点 KVDAT；格式必须逐字段校验。"""
import argparse,json,struct,sys
from collections import Counter
from pathlib import Path
from trace_port import read_tracks
def inspect(folder):
    raw=(folder/'capture.kvdat').read_bytes()
    offset=raw.index(b'kvdat\0\0\0')+8
    depth,rate,trigger,channels=struct.unpack_from('<4Q',raw,offset);offset+=32
    if not (0<depth<2**32 and 0<rate<=500000000 and 0<channels<=16):raise ValueError('不支持的 KVDAT 头')
    initial,tracks,start,end=read_tracks(folder/'capture.csv')
    report=dict(depth=depth,rate=rate,trigger=trigger,channels={})
    for _ in range(channels):
        magic,ch,level,reserved,count=struct.unpack_from('<IBBHQ',raw,offset);offset+=16
        if magic!=0x442323 or reserved or level not in (0,1) or count>depth+1:raise ValueError('不支持的通道块')
        positions=[]
        for _ in range(count):
            pos,flag=struct.unpack_from('<IB',raw,offset);offset+=5
            if flag or pos>depth:raise ValueError('不支持的边沿标记')
            positions.append(pos)
        if not positions or positions[-1]!=depth or positions!=sorted(set(positions)):raise ValueError('无效的边沿位置')
        if ch in tracks:
            csvpos=[round(t*rate+trigger) for t,_ in tracks[ch]]
            report['channels'][ch]=dict(kvdatEdges=len(positions)-1,csvEdges=len(csvpos),identical=csvpos==positions[:-1],
                kvdatPulseSamples=dict(Counter(b-a for a,b in zip(positions[:-1],positions[1:-1]))),
                csvPulseSamples=dict(Counter(b-a for a,b in zip(csvpos,csvpos[1:]))))
    if offset!=len(raw):raise ValueError('未解析的 KVDAT 尾部')
    (folder/'waveform-validation.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    return report
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('record',type=Path);args=parser.parse_args()
    print(json.dumps(inspect(args.record),indent=2))
