"""Small-input Python oracle for etm-reading/1; never used for production export."""
from copy import deepcopy


def project(events,entries,strings,options):
    if len(events)>200000:raise ValueError('参考投影仅接受20万事件以内的小样本')
    names=('pc','size','predicated','valid','group','context','function','flow','it')
    static=[dict(zip(names,e)) for e in entries];byPC={e['pc']:i for i,e in enumerate(static)}
    normalized=[];frames=[];contexts={};previous=None;pending=None;frameId=0
    def context(parent,site,fn):return contexts.setdefault((parent,site,fn),len(contexts)+1)
    def flush():
        nonlocal pending
        if pending is not None:normalized.append(pending);pending=None
    def reset(fn):
        nonlocal frames,frameId
        flush();normalized.append(None);frameId+=1
        frames=[dict(function=fn,returnPC=None,context=context(0,0,fn),frame=frameId,known=False)]
    def node(kind,seq,entry,frame=None):
        return dict(kind=kind,seq=seq,endSeq=seq,entry=entry,instructions=0,unknown=0,notTaken=0,
            context=frame['context'] if frame else 0,depth=len(frames) if frame else 0,
            partial=False,returnObserved=False,path=[],functions=[],unknownPositions=[],
            _frame=frame['frame'] if frame else 0,_safe=True)
    for raw in events:
        seq=raw['seq']
        if raw['kind']!='instruction':
            flush();n=node('event',seq,strings.index(raw['text']));n['path']=[raw['type']];normalized.extend([None,n]);frames=[];previous=None;continue
        identifier=byPC[raw['pc']];e=static[identifier];state={False:0,True:1,None:2}[raw['executed']];call=False;boundary=False
        if not frames:reset(e['function'])
        if previous is not None:
            oldId,oldState=previous;old=static[oldId];boundary=oldState==1 and old['flow']!=0
            if oldState==1 and old['flow']==1:
                if pending is None or pending['kind']!='library':flush()
                if len(frames)>=128:raise ValueError('参考调用层级超过128')
                frameId+=1;frames.append(dict(function=e['function'],returnPC=old['pc']+old['size'],context=context(frames[-1]['context'],old['pc'],e['function']),frame=frameId,known=True));call=True
            elif oldState==1 and old['flow'] in (2,3):
                matches=[j for j in range(len(frames)-1,0,-1) if frames[j]['returnPC']==e['pc']]
                if matches:
                    found=matches[0]
                    if pending and pending['kind'] in ('library','missing') and pending['depth']>=found+1:pending['returnObserved']=True;flush()
                    frames=frames[:found]
                elif old['function']!=e['function']:
                    if old['flow']==3 and old['function'] and e['function']:
                        if pending is None or pending['kind']!='library':flush()
                        frames[-1]['function']=e['function'];frames[-1]['context']=context(frames[-2]['context'] if len(frames)>1 else 0,old['pc'],e['function'])
                    else:reset(e['function'])
            elif old['function']!=e['function']:reset(e['function'])
        if frames[-1]['function']!=e['function']:reset(e['function'])
        frame=frames[-1]
        if options['foldLibraries'] and not e['valid']:
            if pending and len(pending['unknownPositions'])>=4096:pending['partial']=True;flush()
            extend=pending and pending['kind'] in ('library','missing') and len(frames)>=pending['depth']
            if not extend:
                flush();pending=node('library',seq,identifier,frame);pending['partial']=not call or not frame['known']
            if len(pending['path'])<4096:pending['path'].append(identifier*3+state)
            if e['flow']==4:pending['_safe']=False
            if state==2:pending['unknownPositions'].append([seq,identifier])
            if e['function'] not in pending['functions']:pending['functions'].append(e['function'])
        else:
            if pending and pending['kind']=='library':pending['partial']=True;flush()
            kind='unknown' if state==2 else 'not-taken' if state==0 else 'code' if e['valid'] else 'missing'
            same=pending and pending['kind']==kind and pending['_frame']==frame['frame'] and len(pending['path'])<4096
            if same:
                first=static[pending['entry']]
                same=options['groupUnknown'] and e['it'] and e['it']==first['it'] if kind=='unknown' else e['group']==first['group'] and e['context']==first['context'] and not boundary and e['pc']>static[previous[0]]['pc']
            if not same:flush();pending=node(kind,seq,identifier,frame)
            pending['path'].append(identifier*3+state)
            if e['flow']==4:pending['_safe']=False
        pending['endSeq']=seq;pending['instructions']+=1;pending['unknown']+=state==2;pending['notTaken']+=state==0
        previous=identifier,state
    flush()
    return fold(normalized,options)


def fold(nodes,p):
    """Deterministic bounded suffix search, with exact path comparisons."""
    output=[];buffer=[];pattern=[];partial=[];iterations=0;lastEnd=0
    def public(n):return {k:deepcopy(v) for k,v in n.items() if not k.startswith('_')}
    def same(a,b):
        return all(a[k]==b[k] for k in ('kind','entry','context','depth','instructions','unknown','notTaken','path','partial','returnObserved')) and a['endSeq']-a['seq']==b['endSeq']-b['seq']
    def end_repeat():
        nonlocal pattern,iterations
        ins=sum(n['instructions'] for n in pattern);skipped=sum(n['notTaken'] for n in pattern);hidden=iterations-p['keepFirst']-p['keepLast']
        output.append(dict(kind='repeat',seq=pattern[0]['seq'],endSeq=lastEnd,totalIterations=iterations,
            shownIterations=p['keepFirst']+p['keepLast'],keepFirst=p['keepFirst'],keepLast=p['keepLast'],
            hiddenIterations=hidden,hiddenInstructionCount=hidden*ins,instructions=iterations*ins,unknown=0,
            notTaken=iterations*skipped,stride=pattern[-1]['endSeq']-pattern[0]['seq']+1,pattern=[public(n) for n in pattern]))
        pattern=[];iterations=0
    def finish():
        nonlocal partial,buffer
        while pattern:
            extra=partial;partial=[];end_repeat()
            for n in extra:add(n)
        output.extend(public(n) for n in buffer);buffer=[]
    def add(n):
        nonlocal buffer,pattern,partial,iterations,lastEnd
        closed=n and n['kind']=='library' and not n['partial'] and n['returnObserved'] and not n['unknown'] and len(n['path'])==n['instructions']
        if n is None or not p['foldRepeats'] or not n['_safe'] or n['kind'] not in ('code','not-taken') and not closed:
            finish()
            if n is not None:output.append(public(n))
            return
        if pattern:
            if (partial or n['_frame']==pattern[0]['_frame']) and same(n,pattern[len(partial)]):
                partial.append(n)
                if len(partial)==len(pattern):iterations+=1;lastEnd=partial[-1]['endSeq'];partial=[]
                return
            extra=partial;partial=[];end_repeat()
            for part in extra:add(part)
            add(n);return
        buffer.append(n)
        for length in range(1,min(p['maxBlock'],len(buffer)//2)+1):
            required=length*(p['repeatThreshold']+1)
            if len(buffer)<required:continue
            lo=len(buffer)-required;depth=buffer[lo]['depth']
            if buffer[lo]['_frame']!=buffer[-length]['_frame'] or not same(buffer[-1],buffer[lo+length-1]):continue
            if any(buffer[lo+j]['depth']<depth or j%length==0 and buffer[lo+j]['_frame']!=buffer[lo]['_frame'] or not same(buffer[lo+j],buffer[lo+j%length]) for j in range(length,required)):continue
            output.extend(public(n) for n in buffer[:lo]);pattern=buffer[lo:lo+length];iterations=p['repeatThreshold']+1;lastEnd=buffer[-1]['endSeq'];buffer=[];return
        weight=lambda:sum(len(n['path'])+2*len(n['unknownPositions'])+len(n['functions']) for n in buffer)
        while len(buffer)>(p['repeatThreshold']+1)*p['maxBlock'] or len(buffer)>65536 or weight()>1048576:output.append(public(buffer.pop(0)))
    for n in nodes:add(n)
    finish();return output
