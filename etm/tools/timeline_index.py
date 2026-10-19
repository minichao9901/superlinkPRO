"""Compute the default whole-record function lanes during index insertion."""


class TimelineIndex:
    def __init__(self, count, bins=1600, maximum_functions=256):
        self.count=count;self.bins=bins;self.maximum_functions=maximum_functions
        self.first=None;self.seen=0;self.valid=bool(count)
        self.values={};self.counts={};self.unknown=[0]*bins;self.skipped=[0]*bins
        self.markers={};self.marker_count=0

    def feed(self,seq,kind,function,state,etype):
        if not self.valid:return
        if self.first is None:self.first=seq
        if seq!=self.first+self.seen:
            self.valid=False;self.values.clear();return
        self.seen+=1
        bucket=min(self.bins-1,(seq-self.first)*self.bins//self.count)
        if kind!='instruction':
            if etype in (1,2,3,6,7,8,9,10):
                marker=self.markers.get(bucket)
                if marker is None:
                    marker=dict(startSeq=seq,endSeq=seq,seq=seq,types={});self.markers[bucket]=marker
                marker['endSeq']=seq;key=str(etype)
                marker['types'][key]=marker['types'].get(key,0)+1;self.marker_count+=1
            return
        if state=='executed':
            name=function or '(未知函数)';values=self.values.get(name)
            if values is None:
                if len(self.values)>=self.maximum_functions:
                    self.valid=False;self.values.clear();return
                values=[0]*self.bins;self.values[name]=values;self.counts[name]=0
            self.counts[name]+=1
        elif state=='not-taken':values=self.skipped
        else:values=self.unknown
        values[bucket]+=1

    def finish(self):
        if not self.valid or self.seen!=self.count:return None
        counts=self.counts;top=sorted(counts,key=lambda name:(-counts[name],name))[:6]
        lanes=[dict(key='fn:'+name,name=name,kind='function',records=counts[name],bins=self.values[name]) for name in top]
        others=[self.values[name] for name in counts if name not in top]
        other=sum(counts[name] for name in counts if name not in top)
        if other:lanes.append(dict(key='other',name='其他函数',kind='other',records=other,bins=[sum(x) for x in zip(*others)]))
        unknown=sum(self.unknown);skipped=sum(self.skipped)
        if unknown:lanes.append(dict(key='unknown',name='执行状态未知',kind='unknown',records=unknown,bins=self.unknown))
        if skipped:lanes.append(dict(key='not-taken',name='未成立路径（未证实执行）',kind='not-taken',records=skipped,bins=self.skipped))
        markers=[]
        for value in self.markers.values():
            value['count']=sum(value['types'].values());markers.append(value)
        last=self.first+self.count-1
        return dict(range=dict(start=self.first,end=last,totalStart=self.first,totalEnd=last),
                    lanes=lanes,binCount=self.bins,markers=markers,markerCount=self.marker_count,
                    summary=dict(executed=sum(counts.values()),unknown=unknown,notTaken=skipped,
                                 filteredRecords=self.count,markers=self.marker_count),
                    timing=dict(axis='execution-order',verifiedTime=False,
                                reason='当前记录没有已验收的逐指令时间戳，横轴只表示执行序号'))
