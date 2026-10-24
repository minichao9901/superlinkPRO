"""Versioned reading view: static source preparation + bounded native projection.

The JSONL mapping covers every selected raw event, including not-taken events.
Repeat templates use an exact observable instruction/state path and arithmetic
iteration ranges. No raw store, decoded state, firmware or source is rewritten.
"""
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import time

SCHEMA='etm-reading/1'
ALGORITHM='reading-v1.3'
DEFAULTS=dict(repeatThreshold=10,keepFirst=2,keepLast=1,maxBlock=128,
              foldRepeats=True,foldLibraries=True,groupUnknown=True,statements=True,macroView='annotate')


def json_text(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':')).replace('&','\\u0026').replace('<','\\u003c').replace('>','\\u003e')


def fingerprint(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1<<20),b''):h.update(block)
    return dict(bytes=Path(path).stat().st_size,sha256=h.hexdigest())


def options(values=None):
    result=dict(DEFAULTS,**(values or {}))
    if not 3<=result['repeatThreshold']<=1000 or not 1<=result['maxBlock']<=512:
        raise ValueError('重复阈值须为3～1000，最大块长为1～512')
    if min(result['keepFirst'],result['keepLast'])<1 or result['keepFirst']+result['keepLast']>result['repeatThreshold']:
        raise ValueError('保留的前后次数须为正，合计不得超过重复阈值')
    if result['macroView'] not in ('original','annotate','expanded'):raise ValueError('无效宏视图')
    return result


def prepare(store,sources,elf,config):
    from capstone import Cs,CS_ARCH_ARM,CS_MODE_THUMB,CS_MODE_MCLASS
    from decode_trace import Image
    from source_reading import SourceReading
    reader=SourceReading(sources,elf,config,store.static)
    groups={};contexts={};functions={'(未知函数)':0};static=[];rows=[]
    # The IT instruction identifies a guarded block; adjacent unknown slots in
    # other IT blocks or unknown branches are never pooled into this one.
    it={};image=Image(elf);decoder=Cs(CS_ARCH_ARM,CS_MODE_THUMB|CS_MODE_MCLASS)
    for address,size,_ in image.functions:
        remaining=0;block=0
        for pc,_,mnemonic,_ in decoder.disasm_lite(image.read(address,size),address):
            if mnemonic.startswith('it') and mnemonic!='iteq':
                if mnemonic in ('it','itt','ite','ittt','itte','itet','itee','itttt','ittte','ittet','ittee','itett','itete','iteet','iteee'):
                    remaining=len(mnemonic)-1;block=pc
            elif remaining:
                it[pc]=block;remaining-=1
    for e in store.static:
        row=reader.describe(e)
        valid=bool(row['text'])
        function=e.function or '(未知函数)'
        fn=functions.setdefault(function,len(functions))
        group=groups.setdefault((e.function,e.file,row['firstLine'],row['lastLine']),len(groups))
        context=contexts.setdefault((e.function,e.file),len(contexts))
        static.append((e.pc,e.size,int(e.predicated),int(valid),group,context,fn,
                       {'linear':0,'call':1,'return':2,'branch':3,'unknown':4}[e.flow],it.get(e.pc,0)))
        rows.append(dict(row,pc=e.pc,function=e.function,file=e.file,line=e.line,
                         mnemonic=e.mnemonic,operands=e.operands,flow=e.flow))
    return static,rows,list(functions),reader.summary()


def plan(store,entries,path,start,end,config):
    item=next(f for f in store.manifest['files'] if f['name']=='store.bin')
    flags=int(config['foldRepeats'])+2*int(config['foldLibraries'])+4*int(config['groupUnknown'])
    raw=bytearray(b'ETMPRJ1\0')
    def number(x):raw.extend(struct.pack('<Q',int(x)))
    for x in [1,start,end,store.container.generation_tag,item['bytes'],store.first_seq,store.last_seq,
              store.counts['events'],store.counts['instructions'],config['repeatThreshold'],
              config['keepFirst'],config['keepLast'],config['maxBlock'],flags]:number(x)
    text=item['sha256'].encode('ascii');number(len(text));raw.extend(text)
    number(len(entries))
    for row in entries:
        for x in row:number(x)
    number(len(store.strings));raw.extend(hashlib.sha256(raw).digest());Path(path).write_bytes(raw)
    return item


def nodes(path):
    with Path(path).open(encoding='utf8') as f:
        for line in f:
            row=json.loads(line)
            if row.get('kind')!='header':yield row


def record_diagnostics(store,result):
    """Keep sparse packet diagnostics without materialising an unbounded table."""
    from trace_store import BLK_ISSUES
    packet=[];deferred=False
    if (result.get('quality') or {}).get('packetIssues'):
        blocks=[store.container.blocks[i] for i in store.container.of_type(BLK_ISSUES)]
        size=sum(b.count if b.flags&1 else b.length for b in blocks)
        if size<=4*1024*1024:
            for item in store.issues_list:
                if item.get('category')=='packet':
                    if len(packet)>=1000:deferred=True;break
                    packet.append(item)
        else:deferred=True
    return dict(packetIssues=packet,deferred=deferred,
                note='全记录诊断；所选区间的事件见range/statistics。超过诊断预算时保留原表，请用query --view issues分页读取。')


def validate(path,start,end,summary):
    """Independent host count/coverage audit before publishing any export."""
    cursor=start;counts=dict(instructions=0,unknown=0,notTaken=0,nodes=0,repeatBlocks=0,hiddenInstructions=0)
    for n in nodes(path):
        if n['seq']!=cursor or n['endSeq']<n['seq']:raise ValueError('阅读投影存在重叠/遗漏')
        span=n['endSeq']-n['seq']+1;cursor=n['endSeq']+1
        if n['kind']=='repeat':
            total=n['totalIterations'];p=n['pattern'];stride=n['stride']
            if total<=summary['repeatThreshold'] or stride*total!=span or p[0]['seq']!=n['seq'] or p[-1]['endSeq']-p[0]['seq']+1!=stride:
                raise ValueError('重复模板/阈值/区间不自洽')
            pos=p[0]['seq']
            for part in p:
                allowed=part['kind'] in ('code','not-taken') or part['kind']=='library' and not part['partial'] and part['returnObserved'] and not part['unknown'] and len(part['path'])==part['instructions']
                if part['seq']!=pos or not allowed:raise ValueError('重复跨越未允许边界')
                pos=part['endSeq']+1
            for key in ('instructions','unknown','notTaken'):
                if n[key]!=sum(x[key] for x in p)*total:raise ValueError('重复计数不一致')
            if n['hiddenInstructionCount']!=n['instructions']//total*n['hiddenIterations']:raise ValueError('隐藏指令数不一致')
            counts['repeatBlocks']+=1;counts['hiddenInstructions']+=n['hiddenInstructionCount']
        elif n['kind']=='event':
            if span!=1 or n['instructions']:raise ValueError('边界事件不自洽')
        elif n['instructions']!=span:raise ValueError('指令片段计数不自洽')
        if n['kind']=='library' and n['unknown']!=len(n['unknownPositions']):raise ValueError('无源码片段的未知指令定位不全')
        if n['kind'] not in ('event','library'):
            for part in n.get('pattern',[n]):
                if len(part['path'])!=part['instructions']:raise ValueError('指令路径长度不一致')
        counts['nodes']+=1
        for key in ('instructions','unknown','notTaken'):counts[key]+=n[key]
    if cursor!=end+1:raise ValueError('阅读投影未覆盖完整选择区间')
    for key,value in counts.items():
        if summary.get(key)!=value:raise ValueError('原生投影与独立计数不符：'+key)
    if summary['matchedEvents']!=end-start+1:raise ValueError('原始事件计数不一致')
    return counts


def display(n,static,events,functions):
    kind=n['kind']
    if kind=='repeat':return f"重复执行块：总计 {n['totalIterations']} 次，隐藏 {n['hiddenIterations']} 次（{n['hiddenInstructionCount']} 条指令）。路径相同不表示运行时取值相同。"
    if kind=='event':return events[n['entry']]
    e=static[n['entry']]
    if kind=='code':return e['text']
    if kind=='unknown':return f"条件未证实（{n['unknown']} 条指令）"
    if kind=='not-taken':return f"未成立条件：{n['notTaken']} 条指令"
    if kind=='library':
        names=' → '.join(functions[x] or '(未知符号)' for x in n['functions'])
        return f"无源码片段：{names}；{n['instructions']} 条指令，条件未知 {n['unknown']} 条；"+('部分片段，入口/此前调用关系未证实' if n['partial'] else '已观察到调用入口')+'；'+('已观察到返回调用方' if n['returnObserved'] else '此片段未确认返回')
    return '无配套源码：'+(e['function'] or '(未知符号)')


def visible_rows(mapping,meta):
    """Old compact row contract, with representative iterations and comments."""
    from source_reading import inline_constants
    clean=lambda x:str(x).replace('*/','* /')
    def row(n,shift=0):
        e=meta['static'][n['entry']] if n['kind']!='event' else {}
        text=inline_constants(e['text'],e.get('inlineConstants',{})) if n['kind']=='code' else '/* '+clean(display(n,meta['static'],meta['events'],meta['functions']))+' */'
        if n['kind']=='code' and meta.get('options',{}).get('macroView')=='expanded':
            for expansion in e.get('expansions',[]):
                text+='\n/* '+clean(expansion)+' */'
        return dict(kind=n['kind'],seq=n['seq']+shift,endSeq=n['endSeq']+shift,
                    text=text,file=e.get('file',''),line=e.get('firstLine',0),pc=e.get('pc'),
                    function=e.get('function',''),records=n['instructions'])
    for n in nodes(mapping):
        if n['kind']=='not-taken':continue
        if n['kind']!='repeat':yield row(n);continue
        for iteration in range(n['keepFirst']):
            for part in n['pattern']:
                if part['kind']!='not-taken':yield row(part,iteration*n['stride'])
        # This comment covers ONLY the hidden middle. Representatives before
        # and after it retain their real raw sequence numbers without overlap.
        yield dict(kind='repeat',seq=n['seq']+n['keepFirst']*n['stride'],
                   endSeq=n['seq']+(n['totalIterations']-n['keepLast'])*n['stride']-1,
                   text='/* 此执行块共重复 '+str(n['totalIterations'])+' 次；省略中间 '+str(n['hiddenIterations'])+' 次 */',
                   repeat=dict(baseSeq=n['seq'],stride=n['stride'],pattern=[row(p) for p in n['pattern']]))
        for iteration in range(n['totalIterations']-n['keepLast'],n['totalIterations']):
            for part in n['pattern']:
                if part['kind']!='not-taken':yield row(part,iteration*n['stride'])


def c_output(path,mapping,meta):
    clean=lambda x:str(x).replace('*/','* /')
    files={name:i+1 for i,name in enumerate(sorted({e['file'] for e in meta['static'] if e['file']}))}
    with Path(path).open('w',encoding='utf8') as out:
        out.write('/* Trace 阅读执行流：不是可编译程序；语句是源码上下文。\n * 序号映射：'+clean(meta['mappingName'])+'\n * 宏值为编译时常量；相同路径不表示运行时取值相同。\n */\n')
        for file,identifier in files.items():out.write('/* F'+str(identifier)+' = '+clean(file)+' */\n')
        quality=meta['recordQuality']
        out.write('/* 全记录质量：包边界/问题 '+str(quality.get('packetIssues',0))+'，未处理字节 '+str(quality.get('unprocessedBytes',0))+'；完整质量和诊断见配套清单。 */\n')
        for r in visible_rows(mapping,meta):
            if r['kind']=='code':
                out.write('/* #'+str(r['seq'])+'～#'+str(r['endSeq'])+' '+clean(r['function'])+' · F'+str(files[r['file']])+':'+str(r['line'])+' */\n')
                out.write(r['text']+'\n')
            else:
                out.write('/* #'+str(r['seq'])+'～#'+str(r['endSeq'])+' '+r['text'][3:]+'\n')


def html_output(path,mapping,meta,sources):
    from reading_view import document
    with Path(path).open('w',encoding='utf8') as out:
        before,after=document(dict(start=meta['startSeq'],end=meta['endSeq'])).split('__READING_PAYLOAD__')
        out.write(before);out.write('{"sources":'+json_text(sources)+',"rows":[')
        first=True
        for r in visible_rows(mapping,meta):
            if not first:out.write(',')
            out.write(json_text(r));first=False
        out.write(']}');out.write(after)


def write(store,result,folder,path,mapping,manifest,kind,start=None,end=None,config=None,names=None):
    from native_export import binary_path
    began=time.perf_counter();config=options(config);folder=Path(folder)
    lo=store.first_seq if start is None else int(start);hi=store.last_seq if end is None else int(end)
    if not store.first_seq<=lo<=hi<=store.last_seq:raise ValueError('阅读导出序号超出范围')
    sources=result.get('sources') or store.sources
    if not sources or not any(t.strip() for t in sources.values()):raise ValueError('阅读导出需要匹配的配套源码正文')
    quality=result.get('quality') or {}
    for key in ('packetIssues','unprocessedBytes'):
        value=quality.get(key)
        if isinstance(value,bool) or not isinstance(value,int) or value<0:raise ValueError('记录缺少可核实的质量字段：'+key)
    executable=binary_path()
    version=subprocess.run([str(executable),'--version'],capture_output=True,timeout=15,check=True)
    if 'reading-v1' not in json.loads(version.stdout).get('features',[]):raise ValueError('原生导出器缺少 reading-v1，请更新完整包')
    entries,static,functions,sourceSummary=prepare(store,sources,folder/'firmware.elf',config)
    prepared=time.perf_counter();names=names or dict(mappingName=Path(mapping).name,manifestName=Path(manifest).name)
    with tempfile.TemporaryDirectory(prefix='.reading-',dir=Path(path).parent) as tmp:
        p=Path(tmp)/'plan.bin';raw=Path(tmp)/'nodes.jsonl';storeFile=plan(store,entries,p,lo,hi,config)
        completed=subprocess.run([str(executable),'--project',str(p),str(store.container.path),str(raw)],capture_output=True,timeout=900)
        if completed.returncode:raise ValueError('阅读投影失败：'+completed.stderr.decode('utf8',errors='replace')[-2000:])
        summary=json.loads(completed.stdout)
        if summary.get('protocol')!=SCHEMA or fingerprint(raw)!={k:summary[k] for k in ('bytes','sha256')}:raise ValueError('阅读投影输出指纹不符')
        audit=validate(raw,lo,hi,dict(summary,repeatThreshold=config['repeatThreshold']))
        sourceHash=hashlib.sha256(json_text(sources).encode()).hexdigest()
        meta=dict(schema=SCHEMA,algorithm=ALGORITHM,options=config,startSeq=lo,endSeq=hi,
                  contextBasis='从选择区间保守重建；采集前祖先调用栈未证实，不是现场寄存器快照',
                  limits=dict(maxCallDepth=128,maxBlock=config['maxBlock'],maxBufferedNodes=65536,maxBufferedPathWords=1048576,nestedFoldDepth=1),
                  recordId=folder.name,store=storeFile,generation=str(store.container.generation_tag),
                  recordQuality=result.get('quality') or {},recordStatus=result.get('status') or {},recordDiagnostics=record_diagnostics(store,result),
                  sourcesSha256=sourceHash,elf=fingerprint(folder/'firmware.elf'),static=static,
                  events=list(store.strings),functions=functions,statistics=summary,sourceReading=sourceSummary,**names)
        with Path(mapping).open('w',encoding='utf8') as out,raw.open(encoding='utf8') as stream:
            out.write(json_text(dict(kind='header',**meta))+'\n')
            for line in stream:out.write(line)
    projected=time.perf_counter()
    if kind=='simplified':c_output(path,mapping,meta)
    elif kind=='simplified-html':html_output(path,mapping,meta,sources)
    else:raise ValueError('阅读模式仅用于简化C/HTML')
    outcome=dict(schema=SCHEMA,algorithm=ALGORITHM,recordId=meta['recordId'],generation=meta['generation'],
                 store=storeFile,sourcesSha256=sourceHash,elf=meta['elf'],options=config,
                 range=dict(startSeq=lo,endSeq=hi),statistics=summary,audit=audit,sourceReading=sourceSummary,
                 nativeTool=dict(version=json.loads(version.stdout),**fingerprint(executable)),
                 recordQuality=meta['recordQuality'],recordStatus=meta['recordStatus'],recordDiagnostics=meta['recordDiagnostics'],
                 files=dict(output=fingerprint(path),mapping=fingerprint(mapping)),
                 seconds=dict(prepare=prepared-began,project=projected-prepared,render=time.perf_counter()-projected,total=time.perf_counter()-began))
    Path(manifest).write_text(json.dumps(outcome,ensure_ascii=False,indent=2),encoding='utf8')
    return outcome
