"""Exact window views from optional compact metrics stored in the derived index.

JSONL remains the evidence and export source. Unsupported records retain the
ordinary JSONL implementation; counters use integers, never float-weight sums.
"""
import json
import math
import os


def window(db,lo,hi,total_start,total_end,bins,event_loader):
    if os.environ.get('ETM_PYTHON_WINDOW')=='1':return None
    try:import numpy as np
    except ImportError:return None
    info=db.execute("SELECT value FROM meta WHERE key='metrics-v1'").fetchone()
    if info is None:return None
    info=json.loads(info[0])
    if not info['first']<=lo<=hi<info['first']+info['count']:return None
    dtype=np.dtype([('instruction','<u4'),('path','<u4'),('cycles','<u8'),('state','u1'),('call','u1'),('reserved','<u2')])
    pieces=[];expected=lo
    for first,last,body in db.execute('SELECT first,last,data FROM metric_blocks WHERE first<=? AND last>=? ORDER BY first',(hi,lo)):
        if len(body)!=(last-first+1)*dtype.itemsize:raise ValueError('浏览统计块大小无效，请重建索引')
        begin=max(first,lo);end=min(last,hi)
        if begin!=expected:raise ValueError('浏览统计块不连续，请重建索引')
        pieces.append(np.frombuffer(body,dtype=dtype)[begin-first:end-first+1]);expected=end+1
    if expected!=hi+1:raise ValueError('浏览统计块缺失，请重建索引')
    data=pieces[0] if len(pieces)==1 else np.concatenate(pieces)
    if np.any(data['reserved']) or np.any(data['state']>3):raise ValueError('浏览统计块标记无效')
    templates={row[0]:dict(kind=row[1],pc=row[2],function=row[3] or '(未知函数)',file=row[4],line=row[5],flow=row[6],type=row[7])
               for row in db.execute('SELECT id,kind,pc,fn,file,line,flow,type FROM event_templates')}
    paths={row[0]:json.loads(row[1]) for row in db.execute('SELECT id,path FROM metric_paths')}
    if any(not isinstance(path,list) or not path or not all(isinstance(x,str) for x in path) for path in paths.values()):return None
    mask=data['state']!=3;tid=data['instruction'];pid=data['path'];state=data['state'];cycles=data['cycles']
    ids=tid[mask];states=state[mask];cc=cycles[mask];path_ids=pid[mask]
    if len(data)!=hi-lo+1 or any(int(x) not in templates for x in np.unique(tid)):raise ValueError('浏览统计模板无效')
    if any(int(x) not in paths for x in np.unique(path_ids)):raise ValueError('浏览调用路径无效')
    length=max(templates,default=-1)+1
    count=np.bincount(ids,minlength=length)
    executed=np.bincount(ids[states==1],minlength=length)
    unknown=np.bincount(ids[states==2],minlength=length)
    skipped=np.bincount(ids[states==0],minlength=length)
    totals=np.zeros(length,dtype=np.uint64);np.add.at(totals,ids,cc)
    order,positions=np.unique(ids,return_index=True);order=order[np.argsort(positions)]
    functions={};hotspots={}
    for identifier in order:
        identifier=int(identifier);entry=templates[identifier];fn=entry['function'];n=int(count[identifier]);c=int(totals[identifier])
        stat=functions.get(fn)
        if stat is None:
            stat=dict(function=fn,instructions=0,knownExecuted=0,unknownExecution=0,notTaken=0,calls=0,interruptEntries=0,selfCycles=0);functions[fn]=stat
        stat['instructions']+=n;stat['knownExecuted']+=int(executed[identifier]);stat['unknownExecution']+=int(unknown[identifier]);stat['notTaken']+=int(skipped[identifier]);stat['selfCycles']+=c
        key=(fn,entry['file'],entry['line']);hot=hotspots.get(key)
        if hot is None:
            hot=dict(function=fn,file=entry['file'],line=entry['line'],records=0,unknown=0,cycles=0,pc=entry['pc']);hotspots[key]=hot
        hot['records']+=n;hot['unknown']+=int(unknown[identifier]);hot['cycles']+=c
    call_positions=np.flatnonzero(mask[1:]&mask[:-1]&(data['call'][:-1]!=0)&(state[:-1]==1))+1
    types=np.full(length,-1,dtype=np.int64)
    for identifier,entry in templates.items():
        if entry['type'] is not None:types[identifier]=entry['type']
    irq_positions=np.flatnonzero(mask[1:]&~mask[:-1]&(types[tid[:-1]]==9))+1
    for indices,key in [(call_positions,'calls'),(irq_positions,'interruptEntries')]:
        unique,counts=np.unique(tid[indices],return_counts=True)
        for identifier,n in zip(unique,counts):functions[templates[int(identifier)]['function']][key]+=int(n)
    root=dict(name='所选执行区间',value=int(mask.sum()),self=0,selfCycles=0,valueCycles=int(cc.sum()),children={})
    inclusive={}
    unique,firsts=np.unique(path_ids,return_index=True)
    path_count=np.bincount(path_ids,minlength=max(paths,default=-1)+1)
    path_cycles=np.zeros(len(path_count),dtype=np.uint64);np.add.at(path_cycles,path_ids,cc)
    for identifier in unique[np.argsort(firsts)]:
        identifier=int(identifier);path=paths[identifier];n=int(path_count[identifier]);c=int(path_cycles[identifier]);node=root
        for name in path:
            child=node['children'].get(name)
            if child is None:
                child=dict(name=name,value=0,self=0,selfCycles=0,valueCycles=0,children={});node['children'][name]=child
            node=child;node['value']+=n;node['valueCycles']+=c;inclusive[name]=inclusive.get(name,0)+c
        node['self']+=n;node['selfCycles']+=c
    def serialize(node):return {**node,'children':[serialize(value) for value in node['children'].values()]}
    step=max(1,math.ceil((hi-lo+1)/bins));timeline=[]
    for begin in range(0,len(data),step):
        end=min(len(data),begin+step);selected=mask[begin:end];part=pid[begin:end][selected]
        common=None;mixed=False
        if len(part):
            path_order,path_first=np.unique(part,return_index=True)
            for identifier in path_order[np.argsort(path_first)]:
                path=paths[int(identifier)]
                if common is None:common=list(path)
                elif common!=path:
                    limit=0
                    for a,b in zip(common,path):
                        if a!=b:break
                        limit+=1
                    common=common[:limit];mixed=True
        common=common or []
        if mixed:common.append('多段调用（放大查看）')
        timeline.append(dict(startSeq=lo+begin,endSeq=lo+end-1,firstSeq=lo+begin,path=common,
                             records=int(selected.sum()),unknown=int((state[begin:end]==2).sum()),
                             notTaken=int((state[begin:end]==0).sum()),mixed=mixed,events=[]))
    for event in event_loader():
        timeline[(event['seq']-lo)//step]['events'].append(dict(seq=event['seq'],type=event['type'],text=event.get('text','')))
    total_cycles=int(cc.sum());known=int((states==1).sum())
    for stat in functions.values():
        stat['totalCycles']=inclusive.get(stat['function'],0)
        stat['cyclePercent']=100*stat['selfCycles']/total_cycles if total_cycles else 0
        stat['cpi']=(stat['selfCycles']/stat['instructions']) if stat['instructions'] and stat['selfCycles'] else None
    cycle_functions=[dict(function=s['function'],selfCycles=s['selfCycles'],cyclePercent=s['cyclePercent'],instructions=s['instructions'],cpi=s['cpi'],calls=s['calls'])
                     for s in sorted((x for x in functions.values() if x['selfCycles']),key=lambda x:x['selfCycles'],reverse=True)]
    first_indices=np.flatnonzero(mask)
    return dict(range=dict(start=lo,end=hi,totalStart=total_start,totalEnd=total_end),firstSeq=lo+int(first_indices[0]) if len(first_indices) else None,
                timing=dict(axis='execution-order',verifiedTime=False,reason='当前记录没有已验收的逐指令时间戳，执行序号不能换算为微秒'),
                timeline=timeline,resolution=step,
                analysis=dict(flame=serialize(root),functions=sorted(functions.values(),key=lambda x:x['instructions'],reverse=True),
                    hotspots=sorted(hotspots.values(),key=lambda x:(x['cycles'],x['records']),reverse=True),observedCalls=len(call_positions),
                    timing=dict(totalCycles=total_cycles,cycleRanges=len(cycle_functions),executedInstructions=known,instructions=len(ids),
                                cpi=total_cycles/known if known and total_cycles else None,functions=cycle_functions,timeFlameAvailable=bool(total_cycles),
                                basis='所选区间的周期计数（区间末指令记录，可求和）' if total_cycles else '所选区间没有周期计数数据（目标未启用或 ETMv3 不支持）'),
                    weight='所选区间的路径记录数；周期列才是耗时' if total_cycles else '所选区间的路径记录数（含未成立/未知状态），不是耗时'))
