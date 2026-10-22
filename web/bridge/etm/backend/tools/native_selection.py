"""Checked C++ selection/counts; Python only materializes the requested rows.

The query plan contains exact Python search matches for static instructions and
rare event strings. No language-dependent case folding or text matching moves
into C++, and no call-stack checkpoint is reused as decoder state.
"""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time

import native_export


def _run(store, mode, lo, hi, search, hide_skipped, offset=0, limit=150, bins=1600):
    engine=os.environ.get('ETM_QUERY_ENGINE','native').strip().lower()
    if engine not in ('native','python'):raise ValueError('ETM_QUERY_ENGINE 必须为 native 或 python')
    from trace_store_index import slow_mode
    if engine=='python' or slow_mode():return None
    if store.query_index is None or not (store.meta.get('seq') or {}).get('contiguous',True):return None
    binary=native_export.binary_path()
    if not binary.is_file():
        if getattr(sys,'frozen',False):raise ValueError('完整包缺少原生查询程序，请重新下载并完整解压')
        return None
    started=time.perf_counter()
    probe=subprocess.run([str(binary),'--version'],capture_output=True,timeout=15)
    try:version=json.loads(probe.stdout)
    except (ValueError,UnicodeDecodeError) as error:raise ValueError('原生查询程序握手无效') from error
    if probe.returncode or version.get('protocol')!='etm-export/1' or version.get('abi')!=1:
        raise ValueError('原生查询程序版本不匹配')
    if 'select-v1' not in version.get('features',[]):
        if getattr(sys,'frozen',False):raise ValueError('完整包的原生查询程序过旧，请更新完整包')
        return None
    needle=search.lower() if search else ''
    functions=sorted({entry.function or '(未知函数)' for entry in store.static}) or ['(未知函数)']
    function_ids={name:i for i,name in enumerate(functions)}
    statics=bytearray()
    for entry in store.static:
        item=dict(kind='instruction',function=entry.function,file=entry.file,
                  mnemonic=entry.mnemonic,operands=entry.operands,pc=entry.pc)
        statics.extend(struct.pack('<QQQQ',entry.pc,int(entry.predicated),
            int(not needle or needle in store._search_text(item)),function_ids[entry.function or '(未知函数)']))
    event_matches=bytes(int(not needle or needle in store._search_text(dict(kind='event',text=text)))
                        for text in store.strings)
    info=next(item for item in store.manifest['files'] if item['name']=='store.bin')
    plan=bytearray(b'ETMSEL1\0')
    for value in (1,mode,lo,hi,store.container.generation_tag,info['bytes'],store.first_seq,
                  store.last_seq,store.counts['events'],store.counts['instructions'],
                  min(offset,store.counts['events']),limit,int(bool(hide_skipped)),bins):
        plan.extend(struct.pack('<Q',value))
    digest=info['sha256'].encode('ascii');plan.extend(struct.pack('<Q',len(digest)));plan.extend(digest)
    plan.extend(struct.pack('<QQ',len(functions),len(store.static)));plan.extend(statics)
    plan.extend(struct.pack('<Q',len(event_matches)));plan.extend(event_matches)
    plan.extend(hashlib.sha256(plan).digest())
    prepared=time.perf_counter()
    # The plan is request-private; concurrent filters never overwrite each other.
    with tempfile.TemporaryDirectory(prefix='etm-select-') as temporary:
        path=Path(temporary)/'plan.bin';path.write_bytes(plan)
        process=subprocess.run([str(binary),'--select',str(path),str(store.container.path)],
                               capture_output=True,timeout=900)
    if process.returncode:raise ValueError('原生查询失败：'+process.stderr.decode('utf8',errors='replace')[-2000:])
    try:result=json.loads(process.stdout)
    except (ValueError,UnicodeDecodeError) as error:raise ValueError('原生查询返回无效数据') from error
    total=result.get('total')
    if result.get('protocol')!='etm-select/1' or type(total) is not int or not 0<=total<=hi-lo+1:
        raise ValueError('原生查询返回的计数无效')
    stats=dict(path='native-selection',mode=mode,
        preparationSeconds=prepared-started,nativeSeconds=time.perf_counter()-prepared,
        totalSeconds=time.perf_counter()-started)
    return result,functions,stats


def page(store, offset, limit, search, hide_skipped, lo, hi):
    if hi<lo:return None # Keep the existing invalid filtered-range error contract.
    answer=_run(store,0,lo,hi,search,hide_skipped,offset,limit)
    if answer is None:return None
    result,_,stats=answer;total=result['total']
    actual=min(offset,max(0,((total-1)//limit)*limit)) if total else 0
    seqs=result.get('seqs')
    if (result.get('offset')!=actual or not isinstance(seqs,list)
            or len(seqs)!=min(limit,max(0,total-actual))
            or any(type(s) is not int or not lo<=s<=hi for s in seqs)
            or any(a>=b for a,b in zip(seqs,seqs[1:]))):
        raise ValueError('原生查询返回的分页范围无效')
    started=time.perf_counter();rows=[];groups=[]
    for seq in seqs:
        if groups and seq-groups[-1][-1]<=64 and seq-groups[-1][0]<=8192:groups[-1].append(seq)
        else:groups.append([seq])
    needle=search.lower() if search else ''
    for group in groups:
        selected=set(group)
        for item in store.events(group[0],group[-1]):
            if item['seq'] not in selected:continue
            if ((hide_skipped and item.get('executionState')=='not-taken')
                    or (needle and needle not in store._search_text(item))):
                raise ValueError('原生查询与还原记录的筛选条件不符')
            rows.append(item)
    if [row['seq'] for row in rows]!=seqs:raise ValueError('原生查询分页记录缺失')
    stats['materializeSeconds']=time.perf_counter()-started
    stats['totalSeconds']+=stats['materializeSeconds']
    store.last_query_stats=stats
    return dict(rows=rows,total=total,offset=actual,limit=limit)


def function_timeline(store,start,end,search,hide_skipped,bins,max_functions):
    lo=store.first_seq if start is None else max(store.first_seq,min(store.last_seq,int(start)))
    hi=store.last_seq if end is None else max(store.first_seq,min(store.last_seq,int(end)))
    if hi<lo:
        from trace_store import TraceStoreError
        raise TraceStoreError('区间结束序号不能小于起点')
    bins=max(240,min(2400,int(bins)));max_functions=max(1,min(10,int(max_functions)))
    answer=_run(store,1,lo,hi,search,hide_skipped,bins=bins)
    if answer is None:return None
    result,functions,stats=answer
    def checked(values):
        if not isinstance(values,list) or len(values)!=bins or any(type(n) is not int or n<0 for n in values):
            raise ValueError('原生查询返回的时间线分箱无效')
        return values
    ranked=[];seen=set()
    for identifier,values in result['functions']:
        if type(identifier) is not int or not 0<=identifier<len(functions) or identifier in seen:
            raise ValueError('原生查询返回的函数编号无效')
        seen.add(identifier);ranked.append((functions[identifier],checked(values)))
    ranked.sort(key=lambda item:(-sum(item[1]),item[0]))
    labels=[dict(key='fn:'+name,name=name,kind='function',records=sum(values),bins=values)
            for name,values in ranked[:max_functions]]
    other=[sum(values[i] for _,values in ranked[max_functions:]) for i in range(bins)]
    if sum(other):labels.append(dict(key='other',name='其他函数',kind='other',records=sum(other),bins=other))
    unknown=checked(result['unknownBins']);not_taken=checked(result['notTakenBins'])
    if sum(unknown):labels.append(dict(key='unknown',name='执行状态未知',kind='unknown',records=sum(unknown),bins=unknown))
    if sum(not_taken) and not hide_skipped:
        labels.append(dict(key='not-taken',name='未成立路径（未证实执行）',kind='not-taken',records=sum(not_taken),bins=not_taken))
    if (sum(sum(v) for _,v in ranked)!=result['executed'] or sum(unknown)!=result['unknown']
            or sum(not_taken)!=result['notTaken']
            or result['total']!=result['executed']+result['unknown']+result['notTaken']+result['markerCount']):
        raise ValueError('原生查询返回的时间线计数不符')
    store.last_query_stats=stats
    return dict(range=dict(start=lo,end=hi,totalStart=store.first_seq,totalEnd=store.last_seq),
        lanes=labels,binCount=bins,markers=result['markers'],markerCount=result['markerCount'],
        summary=dict(executed=result['executed'],unknown=result['unknown'],notTaken=result['notTaken'],
                     filteredRecords=result['total'],markers=result['markerCount']),
        timing=dict(axis='execution-order',verifiedTime=False,
            reason='当前记录没有已验收的逐指令时间戳，横轴只表示执行序号'))
