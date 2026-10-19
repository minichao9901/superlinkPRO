"""可重建的 SQLite 浏览索引；原始 JSONL/decoded.json 保持不变。

v7 把重复的源码/搜索字段放在模板表，通过兼容视图提供原来的查询列。
事件仍按字节偏移读取原始 JSONL。稀疏导航替代重复状态索引；默认轨迹图
随索引一次构建。可选原生导入器额外存储紧凑统计块，供精确窗口汇总使用。
旧版、无 sidecar 或没有原生工具时保留 Python 构建/JSONL 窗口回退路径。
版本签名不同会原子重建派生索引，既有原始记录不会被改写。
"""
import hashlib
import json
import math
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import closing
from pathlib import Path
from timeline_index import TimelineIndex

VERSION=7
SIDECAR='browse-columns.tsv'
# 与 decode_stream.SIDECAR_HEADER 对应（v2）：
# `#etm-browse-columns 2 events=<20 位字节数> rows=<10 位行数> mtime=<20 位 ns> sha256=<64 hex>`
SIDECAR_HEADER_PREFIX='#etm-browse-columns 2 '
PRAGMA_SQL='''
    PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA page_size=8192;
    PRAGMA cache_size=-2097152; PRAGMA temp_store=MEMORY; PRAGMA locking_mode=EXCLUSIVE;
    PRAGMA threads=8;
'''
SCHEMA_SQL=PRAGMA_SQL+'''
    CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
    CREATE TABLE event_templates(id INTEGER PRIMARY KEY,kind TEXT,fn TEXT,file TEXT,
       line INTEGER,type INTEGER,search TEXT,pc INTEGER,flow TEXT);
    CREATE TABLE event_rows(seq INTEGER PRIMARY KEY,pc INTEGER,flow TEXT,state TEXT,
       template INTEGER,src INTEGER,data TEXT);
    CREATE VIEW events AS SELECT r.seq,t.kind,r.pc,t.fn,t.file,t.line,r.flow,r.state,
       t.type,t.search,r.src,r.data FROM event_rows r JOIN event_templates t ON t.id=r.template;
    CREATE TABLE event_navigation(mode TEXT,seq INTEGER,PRIMARY KEY(mode,seq)) WITHOUT ROWID;
    CREATE TABLE metric_paths(id INTEGER PRIMARY KEY,path TEXT);
    CREATE TABLE metric_blocks(first INTEGER PRIMARY KEY,last INTEGER,data BLOB);
    CREATE TABLE packets(id INTEGER PRIMARY KEY,search TEXT,src INTEGER,data TEXT);
    CREATE TABLE sources(file TEXT PRIMARY KEY,text TEXT);
    CREATE TABLE issues(id INTEGER PRIMARY KEY,startSeq INTEGER,endSeq INTEGER,data TEXT);
'''
INDEX_SQL=('CREATE INDEX events_pc ON event_rows(pc,seq)',)
EVENT_COLUMNS='(seq,kind,pc,fn,file,line,flow,state,type,search,src,data)'
_guard=threading.Lock()
_stores=OrderedDict()
_stores_lock=threading.Lock()


def record_store(folder,loader):
    """Bounded per-record cache survives individual HTTP request objects."""
    key=Path(folder).resolve()
    with _stores_lock:
        store=_stores.pop(key,None)
        if store is None:store=RecordStore(key,loader)
        else:store.loader=loader
        _stores[key]=store
        while len(_stores)>4:_stores.popitem(last=False)
        return store
def dumps(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'))

def _packet_fields(line):
    """packet 行（compact 或 json.dumps 默认分隔符皆可）→ (offset, size, search 文本, text)。"""
    head, separator, tail = line.partition(',"text":')
    if not separator:
        head, separator, tail = line.partition(', "text": ')
    if not separator:
        raise ValueError('包记录缺少 text 字段')
    fields = {}
    for part in head.lstrip('{').split(','):
        key, _, value = part.partition(':')
        fields[key.strip().strip('"')] = value.strip()
    offset = int(fields['offset'])
    size = int(fields['size'])
    hexed = fields.get('hex', '""').strip('"')
    text = json.loads(tail.rstrip()[:-1])
    return offset, size, (text + ' ' + str(offset) + ' ' + hexed).lower(), text

def _base_search(event):
    search=' '.join(str(event.get(k,'') or '') for k in ('function','file','mnemonic','operands','text'))
    if 'pc' in event:
        search+=f" 0x{event['pc']:08x} {event['pc']}"
    return search.lower()


class RecordStore:
    def __init__(self,folder,loader):
        self.folder=Path(folder);self.path=self.folder/'browse-v3.sqlite';self.loader=loader
        # 区间统计缓存：全记录第一次 window() 要扫几百万事件（实测 3.86 M 事件 119.5 s），
        # 前端固定超时等不起；同一区间第二次必须立即返回。索引重建后 signature 变化 → 清空。
        self._windows={}
        self._windows_lock=threading.Lock()
        self._window_compute_lock=threading.Lock()
    def signature(self):
        p=self.folder/'decoded.json';s=p.stat()
        parts=[f'{VERSION}:{s.st_size}:{s.st_mtime_ns}']
        for name in ('spi-validation.json','phase-validation.json','firmware.elf','capture.csv','capture.bin','capture.kvdat','etm.bin','session.json','slices.json','events.jsonl','packets.jsonl','elements.jsonl','sources.json'):
            side=self.folder/name
            if side.exists():t=side.stat();parts.append(f'{name}:{t.st_size}:{t.st_mtime_ns}')
        return '|'.join(parts)
    def ready(self):
        if not self.path.exists():return False
        try:
            with closing(sqlite3.connect(self.path)) as db:
                return db.execute("SELECT value FROM meta WHERE key='signature'").fetchone()==(self.signature(),)
        except sqlite3.DatabaseError:return False
    # ---- 建索引 -------------------------------------------------------------------------------
    def ensure(self):
        if self.ready():return
        with _guard:
            if self.ready():return
            r=self.loader();temporary=self.path.with_suffix('.building')
            self._templates={}
            if temporary.exists():temporary.unlink()
            events=r['events'];packets=r['packets']
            self._timeline=TimelineIndex(len(events))
            accelerated=False;native_first=None
            if not isinstance(events,list) and self._sidecar_fresh(self.folder/SIDECAR,self.folder/'events.jsonl',events):
                from native_index import executable, build
                if executable() is not None:
                    with closing(sqlite3.connect(temporary)) as schema:schema.executescript(SCHEMA_SQL)
                    native_first=build(temporary,self.folder/'events.jsonl',self.folder/SIDECAR,len(events))
                    accelerated=True
            with closing(sqlite3.connect(temporary)) as db:
                db.executescript(PRAGMA_SQL if accelerated else SCHEMA_SQL)
                summary={k:v for k,v in r.items() if k not in ('events','elements','packets','sources')}
                summary['eventCount']=len(events);summary['packetCount']=len(packets)
                summary['sourceFiles']=list(r.get('sources',{}))
                issues=[]
                if accelerated:
                    summary['firstSeq']=native_first
                    self._native_issues(db,issues)
                elif isinstance(events,list):
                    summary['firstSeq']=self._insert_event_dicts(db,events,issues)
                else:
                    summary['firstSeq']=self._insert_events_streamed(db,events,issues)
                if isinstance(packets,list):
                    db.executemany('INSERT INTO packets VALUES (?,?,?,?)',
                                   ((i,(p.get('text','')+' '+str(p.get('offset'))+' '+p.get('hex','')).lower(),None,dumps(p))
                                    for i,p in enumerate(packets)))
                else:
                    self._insert_packets_streamed(db,packets)
                db.executemany('INSERT INTO sources VALUES (?,?)',r.get('sources',{}).items())
                self._append_packet_issues(r,packets,issues)
                db.executemany('INSERT INTO issues VALUES (?,?,?,?)',((i,p['startSeq'],p['endSeq'],dumps(p)) for i,p in enumerate(issues)))
                db.executemany('INSERT INTO meta VALUES (?,?)',[('signature',self.signature()),('overview',dumps(summary))])
                timeline=self._timeline.finish()
                if timeline is not None:db.execute('INSERT INTO meta VALUES (?,?)',('function-timeline',dumps(timeline)))
                for statement in INDEX_SQL:db.execute(statement)
                db.commit()
            deadline=time.monotonic()+10
            while True:
                try:temporary.replace(self.path);break
                except PermissionError:
                    # A previous read request may still hold the old SQLite
                    # handle on Windows. Wait boundedly; keep the old index intact.
                    if time.monotonic()>=deadline:raise
                    time.sleep(.02)
            with self._windows_lock:self._windows.clear()
    def _native_issues(self,db,issues):
        run=None
        rows=db.execute("SELECT e.data,e.src FROM events e JOIN event_navigation n ON n.seq=e.seq WHERE n.mode='issue' ORDER BY e.seq")
        for event in self._payload_stream('events',rows):
            run=self._note_issue(issues,run,event,event['seq'])
        if run:issues.append(run)
    @staticmethod
    def _note_issue(issues,run,event,seq):
        """谓词未知区间与边界事件（与 v3 逐条同口径）。返回新的 run。"""
        if event.get('executionState')=='unknown':
            if run and seq==run['endSeq']+1:run['endSeq']=seq;run['records']+=1
            else:
                if run:issues.append(run)
                run=dict(category='predicate',startSeq=seq,endSeq=seq,records=1,
                         offset=event.get('offset'),
                         reason=event.get('executionReason') or '条件执行结果未被当前 trace 证实')
            return run
        if run:issues.append(run)
        if event['kind']=='event' and event['type'] in (1,2,6,7,8):
            issues.append(dict(category='boundary',startSeq=seq,endSeq=seq,offset=event.get('offset'),
                               reason=event.get('text','边界事件'),records=1))
        return None
    def _insert_event_batch(self,db,rows):
        """Intern repeated source/search strings once per instruction template.

        The public SQL view retains the previous query columns. Per-event PC,
        execution state, flow and byte offset remain directly indexed.
        """
        navigation=[]
        def compact():
            for seq,kind,pc,fn,file,line,flow,state,etype,search,src,data in rows:
                event_type=int(etype) if etype is not None else None
                self._timeline.feed(seq,kind,fn,state,event_type)
                if state=='executed' and flow in ('call','return'):navigation.append((flow,seq))
                if event_type in (9,10):navigation.append(('exception',seq))
                if state=='unknown' or event_type in (1,2,6,7,8):navigation.append(('issue',seq))
                if kind!='instruction':navigation.append(('event',seq))
                key=(kind,fn,file,line,etype,search)
                identifier=self._templates.get(key)
                if identifier is None:
                    identifier=len(self._templates)
                    self._templates[key]=identifier
                    db.execute('INSERT INTO event_templates VALUES (?,?,?,?,?,?,?,?,?)',(identifier,*key,pc,flow))
                yield seq,pc,flow,state,identifier,src,data
        db.executemany('INSERT INTO event_rows VALUES (?,?,?,?,?,?,?)',compact())
        if navigation:db.executemany('INSERT INTO event_navigation VALUES (?,?)',navigation)
    def _insert_event_dicts(self,db,events,issues):
        run=None;first=None;rows=[]
        for event in events:
            if first is None and event['kind']=='instruction':first=event['seq']
            run=self._note_issue(issues,run,event,event['seq'])
            rows.append((event['seq'],event['kind'],event.get('pc'),event.get('function'),event.get('file'),
                         event.get('line'),event.get('flow'),event.get('executionState'),event.get('type'),
                         _base_search(event),None,dumps(event)))
            if len(rows)>=8192:
                self._insert_event_batch(db,rows);rows.clear()
        if rows:self._insert_event_batch(db,rows)
        if run:issues.append(run)
        return first
    def _sidecar_fresh(self,sidecar,source,events):
        """sidecar 头部指纹是否对得上 events.jsonl：size + 行数 + mtime_ns + sha256。

        v1（只有 size/行数）一律视为过期：events.jsonl 被**等长改写**时 size/行数不变，
        但 sidecar 里的列来自旧事件，会造成"查 alpha 返回 function=bravo"这种自相矛盾
        （审查 F03）。sha256 在展开成功后按文件计算，索引端再算一遍校验（约 1 s）。
        """
        if not sidecar.exists():return False
        try:
            with sidecar.open(encoding='utf-8') as handle:
                header=handle.readline()
            if not header.startswith(SIDECAR_HEADER_PREFIX):return False
            fields=dict(token.split('=',1) for token in header.split()[2:] if '=' in token)
            stat=source.stat()
            if int(fields.get('events','-1'))!=stat.st_size:return False
            try:declared=len(events)
            except TypeError:declared=None
            if declared is not None and int(fields.get('rows','-1'))!=declared:return False
            if int(fields.get('mtime','-1'))!=stat.st_mtime_ns:return False
            expected=fields.get('sha256','')
            if len(expected)!=64:return False
            digest=hashlib.sha256()
            with source.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b''):
                    digest.update(chunk)
            return digest.hexdigest()==expected
        except (OSError,ValueError):return False
    def _insert_events_streamed(self,db,events,issues):
        """流式记录：列来自展开阶段的 sidecar，`data` 留空、只存 src（该行字节偏移）。"""
        source=self.folder/'events.jsonl';sidecar=self.folder/SIDECAR
        run=None;first=None;rows=[];counted=0
        if not source.exists():
            raise ValueError('流式记录缺少 events.jsonl，无法建立浏览索引')
        with source.open('rb') as raw:
            if self._sidecar_fresh(sidecar,source,events):
                with sidecar.open(encoding='utf-8') as side:
                    side.readline()          # 跳过头部指纹
                    offset=0
                    for line in raw:
                        fields=next(side,None)
                        if fields is None:
                            raise ValueError(f'索引列记录数不一致：{SIDECAR} 少于 events.jsonl；请重新解码')
                        fields=fields.rstrip('\n').split('\t')
                        seq=int(fields[0])
                        counted+=1
                        if first is None and fields[1]=='instruction':first=seq
                        if fields[7]=='unknown' or (fields[1]=='event' and fields[8] in ('1','2','6','7','8')):
                            run=self._note_issue(issues,run,json.loads(line),seq)
                        elif run:
                            issues.append(run);run=None
                        rows.append((seq,fields[1],fields[2] or None,fields[3] or None,fields[4] or None,
                                     fields[5] or None,fields[6] or None,fields[7] or None,
                                     fields[8] or None,fields[9],offset,None))
                        offset+=len(line)
                        if len(rows)>=8192:
                            self._insert_event_batch(db,rows);rows.clear()
            else:
                offset=0
                for line in raw:
                    event=json.loads(line)
                    counted+=1
                    if first is None and event['kind']=='instruction':first=event['seq']
                    run=self._note_issue(issues,run,event,event['seq'])
                    rows.append((event['seq'],event['kind'],event.get('pc'),event.get('function'),
                                 event.get('file'),event.get('line'),event.get('flow'),
                                 event.get('executionState'),event.get('type'),_base_search(event),
                                 offset,None))
                    offset+=len(line)
                    if len(rows)>=8192:
                        self._insert_event_batch(db,rows);rows.clear()
        if rows:self._insert_event_batch(db,rows)
        if run:issues.append(run)
        # 与 JsonLines 同口径：JSONL 实际行数必须等于记录声明的条数，否则拒绝建立索引
        expected=len(events)
        if counted!=expected:
            raise ValueError(f'JSONL记录数不一致：events.jsonl，预期{expected}，实际{counted}；请重新解码')
        return first
    def _insert_packets_streamed(self,db,packets):
        source=self.folder/'packets.jsonl'
        if not source.exists():return
        statement='INSERT INTO packets VALUES (?,?,?,?)'
        rows=[];identifier=0;offset=0
        with source.open('rb') as raw:
            for line in raw:
                _,_,search,_=_packet_fields(line.decode('utf-8'))
                rows.append((identifier,search,offset,None))
                identifier+=1;offset+=len(line)
                if len(rows)>=8192:
                    db.executemany(statement,rows);rows.clear()
        if rows:db.executemany(statement,rows)
        expected=len(packets)
        if identifier!=expected:
            raise ValueError(f'JSONL记录数不一致：packets.jsonl，预期{expected}，实际{identifier}；请重新解码')
    def _append_packet_issues(self,r,packets,issues):
        q=r.get('quality',{})
        packet_end=0
        if isinstance(packets,list):
            iterator=((p['offset'],p['size'],p.get('text','')) for p in packets)
        else:
            source=self.folder/'packets.jsonl'
            def stream():
                if not source.exists():return
                with source.open(encoding='utf-8') as handle:
                    for line in handle:
                        offset,size,_,text=_packet_fields(line.rstrip('\n'));yield offset,size,text
            iterator=stream()
        for offset,size,text in iterator:
            start,stop=offset,offset+size
            if start>packet_end:issues.append(dict(category='gap',startSeq=None,endSeq=None,records=0,offset=packet_end,bytes=start-packet_end,reason='ETM 包字节覆盖缺口；不能据此补造指令'))
            if start<packet_end:issues.append(dict(category='overlap',startSeq=None,endSeq=None,records=0,offset=start,bytes=max(0,min(stop,packet_end)-start),reason='ETM 包字节覆盖重叠'))
            packet_end=max(packet_end,stop)
            if any(t in text.upper() for t in ('OVERFLOW','BAD_','RESERVED','INCOMPLETE_EOT')):
                issues.append(dict(category='packet',startSeq=None,endSeq=None,records=0,offset=start,bytes=size,reason='包问题：'+text))
        input_size=q.get('inputBytes',q.get('processedBytes',0))
        # 长窗（streamed.schema=3）记录只合并了事件流，packets 留在各片里：
        # 拿"整段输入字节"去比"根目录 packet_end"会造出一条几十 MB 的假"尾部覆盖缺口"。
        if r.get('streamed',{}).get('schema')!=3 and input_size>packet_end:
            issues.append(dict(category='gap',startSeq=None,endSeq=None,records=0,offset=packet_end,bytes=input_size-packet_end,reason='ETM 输入尾部未被包记录覆盖'))
        if r.get('status',{}).get('lastErrorCode'):issues.append(dict(category='decoder',startSeq=None,endSeq=None,records=0,offset=r['status'].get('processed'),reason='解码器错误：'+r['status'].get('lastError','未知错误')))
        for key,label in [('initialPhysicalBytes','TPIU 同步前物理字节'),('trailingFrameBytes','不完整的 TPIU 尾帧'),('unknownSourceBytes','未知源 ID 字节')]:
            if q.get(key):issues.append(dict(category='physical',startSeq=None,endSeq=None,records=0,bytes=q[key],reason=label))
    # ---- 读取 ---------------------------------------------------------------------------------
    def _payloads(self,table,rows):
        """(data, src) 行 → 字典列表；流式记录按 src 从原始 JSONL 读回该行（等价于 v3 的 data 列）。"""
        payloads=[data for data,_ in rows]
        if any(data is None for data in payloads):
            path=self.folder/('events.jsonl' if table=='events' else 'packets.jsonl')
            with path.open('rb') as stream:
                for index,(data,src) in enumerate(rows):
                    if data is None:
                        stream.seek(src)
                        payloads[index]=stream.readline().decode('utf-8')
        return [json.loads(text) for text in payloads]
    def _payload_stream(self,table,rows):
        """惰性版本：窗口可能覆盖上百万条事件，逐条读回再解析，不整体驻留。"""
        path=self.folder/('events.jsonl' if table=='events' else 'packets.jsonl')
        stream=None;next_offset=None
        try:
            for data,src in rows:
                if data is None:
                    if stream is None:stream=path.open('rb')
                    if src!=next_offset:stream.seek(src)
                    raw=stream.readline();next_offset=src+len(raw)
                    data=raw.decode('utf-8')
                yield json.loads(data)
        finally:
            if stream is not None:stream.close()
    def connect(self):
        self.ensure();return sqlite3.connect(f'{self.path.as_uri()}?mode=ro',uri=True)
    def overview(self):
        with closing(self.connect()) as db:return json.loads(db.execute("SELECT value FROM meta WHERE key='overview'").fetchone()[0])
    def page(self,view='events',offset=0,limit=150,search='',hide_skipped=False,start=None,end=None):
        offset=max(0,int(offset));limit=max(1,min(500,int(limit)))
        table='packets' if view=='packets' else 'events';order='id' if table=='packets' else 'seq'
        where=[];params=[]
        if table=='events':
            if start is not None:where.append('seq>=?');params.append(int(start))
            if end is not None:where.append('seq<=?');params.append(int(end))
        if search:
            literal=search.lower().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
            where.append("search LIKE ? ESCAPE '\\'");params.append('%'+literal+'%')
        if hide_skipped and table=='events':where.append("(state IS NULL OR state!='not-taken')")
        clause=' WHERE '+' AND '.join(where) if where else ''
        with closing(self.connect()) as db:
            count_table='event_rows' if table=='events' and not clause else table
            total=db.execute(f'SELECT count(*) FROM {count_table}'+clause,params).fetchone()[0]
            offset=min(offset,max(0,((total-1)//limit)*limit))
            rows=db.execute(f'SELECT data,src FROM {table}'+clause+f' ORDER BY {order} LIMIT ? OFFSET ?',params+[limit,offset]).fetchall()
        return dict(rows=self._payloads(table,rows),total=total,offset=offset,limit=limit)
    def function_timeline(self,start=None,end=None,search='',hide_skipped=False,bins=1600,max_functions=6):
        """Compact function-lane activity indexed by execution sequence, never by inferred time."""
        bins=max(240,min(2400,int(bins)));max_functions=max(1,min(10,int(max_functions)))
        with closing(self.connect()) as db:
            if start is None and end is None and not search and not hide_skipped and bins==1600 and max_functions==6:
                cached=db.execute("SELECT value FROM meta WHERE key='function-timeline'").fetchone()
                if cached is not None:return json.loads(cached[0])
            bounds=self._bounds(db)
            total_start,total_end=bounds if bounds[0] is not None else (0,0)
            lo=total_start if start is None else max(total_start,min(total_end,int(start)))
            hi=total_end if end is None else max(total_start,min(total_end,int(end)))
            if hi<lo:raise ValueError('区间结束序号不能小于起点')
            query_where=[];query_params=[]
            if search:
                literal=search.lower().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
                query_where.append("search LIKE ? ESCAPE '\\'");query_params.append('%'+literal+'%')
            if hide_skipped:query_where.append("(state IS NULL OR state!='not-taken')")
            query_where.append('seq>=?');query_params.append(lo)
            query_where.append('seq<=?');query_params.append(hi)
            clause=' AND '.join(query_where)
            counts={name:count for name,count in db.execute(
                'SELECT coalesce(fn,\'(未知函数)\'),count(*) FROM events WHERE '+clause+
                " AND kind='instruction' AND state='executed' GROUP BY coalesce(fn,'(未知函数)')",query_params)}
            state_counts=dict(db.execute('SELECT state,count(*) FROM events WHERE '+clause+
                " AND kind='instruction' GROUP BY state",query_params))
            unknown=sum(count for state,count in state_counts.items() if state not in ('executed','not-taken'))
            not_taken=state_counts.get('not-taken',0)
            filtered_records=db.execute('SELECT count(*) FROM events WHERE '+clause,query_params).fetchone()[0]
            top=[name for name,_ in sorted(counts.items(),key=lambda item:(-item[1],item[0]))[:max_functions]]
            other=sum(value for name,value in counts.items() if name not in top)
            labels=[dict(key='fn:'+name,name=name,kind='function',records=counts[name]) for name in top]
            if other:labels.append(dict(key='other',name='其他函数',kind='other',records=other))
            if unknown:labels.append(dict(key='unknown',name='执行状态未知',kind='unknown',records=unknown))
            if not hide_skipped and not_taken:labels.append(dict(key='not-taken',name='未成立路径（未证实执行）',kind='not-taken',records=not_taken))
            span=max(1,hi-lo+1);values={lane['key']:[0]*bins for lane in labels};marker_bins={};marker_count=0
            for seq,kind,fn,state,event_type in db.execute(
                    'SELECT seq,kind,fn,state,type FROM events WHERE '+clause+' ORDER BY seq',query_params):
                if kind!='instruction':
                    if event_type in (1,2,3,6,7,8,9,10):
                        marker_count+=1
                        index=min(bins-1,max(0,(seq-lo)*bins//span))
                        bucket=marker_bins.setdefault(index,dict(startSeq=seq,endSeq=seq,seq=seq,types={}))
                        bucket['endSeq']=seq
                        bucket['types'][str(event_type)]=bucket['types'].get(str(event_type),0)+1
                    continue
                if state=='executed':
                    name=fn or '(未知函数)';key='fn:'+name if ('fn:'+name) in values else 'other'
                elif state=='not-taken':key='not-taken'
                else:key='unknown'
                if key not in values:continue
                index=min(bins-1,max(0,(seq-lo)*bins//span));values[key][index]+=1
            for lane in labels:lane['bins']=values[lane['key']]
            markers=[]
            for bucket in marker_bins.values():
                bucket['count']=sum(bucket['types'].values())
                markers.append(bucket)
        return dict(range=dict(start=lo,end=hi,totalStart=total_start,totalEnd=total_end),
                    lanes=labels,binCount=bins,markers=markers,markerCount=marker_count,
                    summary=dict(executed=sum(counts.values()),unknown=unknown,notTaken=not_taken,
                                 filteredRecords=filtered_records,markers=marker_count),
                    timing=dict(axis='execution-order',verifiedTime=False,
                                reason='当前记录没有已验收的逐指令时间戳，横轴只表示执行序号'))
    @staticmethod
    def _bounds(db):
        return db.execute('SELECT (SELECT seq FROM event_rows ORDER BY seq LIMIT 1),'
                          '(SELECT seq FROM event_rows ORDER BY seq DESC LIMIT 1)').fetchone()

    def window(self,start=None,end=None,bins=1200):
        with self._window_compute_lock:
            return self._window(start,end,bins)

    def _window(self,start=None,end=None,bins=1200):
        bins=max(100,min(2000,int(bins)))
        with closing(self.connect()) as db:
            bounds=self._bounds(db)
            total_start,total_end=bounds if bounds[0] is not None else (0,0)
            lo=total_start if start is None else max(total_start,min(total_end,int(start)))
            hi=total_end if end is None else max(total_start,min(total_end,int(end)))
            if hi<lo:raise ValueError('区间结束序号不能小于起点')
            step=max(1,math.ceil((hi-lo+1)/bins))
            cache_key=(lo,hi,step)
            with self._windows_lock:
                cached=self._windows.get(cache_key)
            if cached is not None:return cached
            from fast_window import window as fast_window
            def event_loader():
                rows=db.execute("SELECT e.data,e.src FROM event_navigation n JOIN event_rows e ON e.seq=n.seq WHERE n.mode='event' AND n.seq BETWEEN ? AND ? ORDER BY n.seq",(lo,hi))
                yield from self._payload_stream('events',rows)
            accelerated=fast_window(db,lo,hi,total_start,total_end,bins,event_loader)
            if accelerated is not None:
                with self._windows_lock:
                    if len(self._windows)>=8:self._windows.pop(next(iter(self._windows)))
                    self._windows[cache_key]=accelerated
                return accelerated
            root=dict(name='所选执行区间',value=0,self=0,selfCycles=0,valueCycles=0,children={})
            buckets={};functions={};hotspots={};inclusive={};previous=None;observed=0;first=None;pending_irq=False
            tree_path=None;tree_nodes=[]
            cursor=db.execute('SELECT data,src FROM events WHERE seq BETWEEN ? AND ? ORDER BY seq',(lo,hi))
            for e in self._payload_stream('events',cursor):
                key=(e['seq']-lo)//step
                bucket=buckets.get(key)
                if bucket is None:
                    bucket=dict(startSeq=e['seq'],endSeq=e['seq'],firstSeq=e['seq'],path=None,records=0,unknown=0,notTaken=0,mixed=False,events=[])
                    buckets[key]=bucket
                bucket['endSeq']=e['seq']
                if e['kind']!='instruction':
                    bucket['events'].append(dict(seq=e['seq'],type=e['type'],text=e.get('text','')))
                    pending_irq=e['type']==9
                    previous=None
                    continue
                if first is None:first=e['seq']
                fn=e.get('function') or '(未知函数)'
                path=e.get('callPath') or ['调用栈未知',fn]
                if bucket['path'] is None:bucket['path']=list(path)
                elif bucket['path']!=path:
                    common=[]
                    for a,b in zip(bucket['path'],path):
                        if a!=b:break
                        common.append(a)
                    bucket['path']=common;bucket['mixed']=True
                bucket['records']+=1;bucket['unknown']+=e.get('executed') is None;bucket['notTaken']+=e.get('executed') is False
                cc=int(e.get('cycleCount') or 0)
                node=root;node['value']+=1;node['valueCycles']+=cc
                if path!=tree_path:
                    tree_nodes=[]
                    for name in path:
                        child=node['children'].get(name)
                        if child is None:
                            child=dict(name=name,value=0,self=0,selfCycles=0,valueCycles=0,children={});node['children'][name]=child
                        node=child;tree_nodes.append(node)
                    tree_path=path
                for node in tree_nodes:node['value']+=1;node['valueCycles']+=cc
                node=tree_nodes[-1] if tree_nodes else root
                node['self']+=1;node['selfCycles']+=cc
                stat=functions.get(fn)
                if stat is None:
                    stat=dict(function=fn,instructions=0,knownExecuted=0,unknownExecution=0,notTaken=0,calls=0,interruptEntries=0,selfCycles=0);functions[fn]=stat
                stat['instructions']+=1;stat['knownExecuted']+=e.get('executed') is True;stat['unknownExecution']+=e.get('executed') is None;stat['notTaken']+=e.get('executed') is False
                if cc:
                    stat['selfCycles']+=cc
                    # 包含周期：沿调用路径给每个祖先函数累加（与 flow_analysis 的 totalCycles 同口径）
                    for name in path:inclusive[name]=inclusive.get(name,0)+cc
                if pending_irq:stat['interruptEntries']+=1;pending_irq=False
                if previous and previous.get('flow')=='call' and previous.get('executed') is True:stat['calls']+=1;observed+=1
                hotkey=(fn,e.get('file',''),e.get('line'))
                hot=hotspots.get(hotkey)
                if hot is None:
                    hot=dict(function=fn,file=e.get('file',''),line=e.get('line'),records=0,unknown=0,cycles=0,pc=e['pc']);hotspots[hotkey]=hot
                hot['records']+=1;hot['unknown']+=e.get('executed') is None;hot['cycles']+=cc;previous=e
            def serialize(node):return {**node,'children':[serialize(c) for c in node['children'].values()]}
            for b in buckets.values():
                b['path']=b['path'] or []
                if b['mixed']:b['path'].append('多段调用（放大查看）')
            # 窗口内的周期聚合（供"按周期加权"的火焰图与函数表使用；没有周期数据时明确降级）
            total_cycles=sum(s['selfCycles'] for s in functions.values())
            executed=sum(s['knownExecuted'] for s in functions.values())
            for s in functions.values():
                s['totalCycles']=inclusive.get(s['function'],0)
                s['cyclePercent']=100*s['selfCycles']/total_cycles if total_cycles else 0
                s['cpi']=(s['selfCycles']/s['instructions']) if (s['instructions'] and s['selfCycles']) else None
            cycle_functions=[dict(function=s['function'],selfCycles=s['selfCycles'],cyclePercent=s['cyclePercent'],
                                   instructions=s['instructions'],cpi=s['cpi'],calls=s['calls'])
                              for s in sorted((x for x in functions.values() if x['selfCycles']),key=lambda x:x['selfCycles'],reverse=True)]
            result=dict(range=dict(start=lo,end=hi,totalStart=total_start,totalEnd=total_end),firstSeq=first,
                timing=dict(axis='execution-order',verifiedTime=False,reason='当前记录没有已验收的逐指令时间戳，执行序号不能换算为微秒'),
                timeline=list(buckets.values()),resolution=step,
                analysis=dict(flame=serialize(root),functions=sorted(functions.values(),key=lambda x:x['instructions'],reverse=True),
                    hotspots=sorted(hotspots.values(),key=lambda x:(x['cycles'],x['records']),reverse=True),observedCalls=observed,
                    timing=dict(totalCycles=total_cycles,cycleRanges=sum(1 for s in cycle_functions),
                                executedInstructions=executed,instructions=sum(s['instructions'] for s in functions.values()),
                                cpi=(total_cycles/executed) if (executed and total_cycles) else None,
                                functions=cycle_functions,timeFlameAvailable=bool(total_cycles),
                                basis=('所选区间的周期计数（区间末指令记录，可求和）' if total_cycles
                                       else '所选区间没有周期计数数据（目标未启用或 ETMv3 不支持）')),
                    weight=('所选区间的路径记录数；周期列才是耗时' if total_cycles
                            else '所选区间的路径记录数（含未成立/未知状态），不是耗时')))
            with self._windows_lock:
                if len(self._windows)>=8:self._windows.pop(next(iter(self._windows)))
                self._windows[cache_key]=result
            return result
    def event(self,seq):
        with closing(self.connect()) as db:row=db.execute('SELECT data,src FROM events WHERE seq=?',(int(seq),)).fetchone()
        if not row:raise ValueError('记录序号不存在')
        e=self._payloads('events',[row])[0]
        return dict(event=e,callPath=e.get('callPath',[]),stackBasis='历史 trace 重建，包含未证实祖先；不是当前寄存器快照')
    def source(self,file):
        with closing(self.connect()) as db:row=db.execute('SELECT text FROM sources WHERE file=?',(file,)).fetchone()
        return dict(file=file,text=row[0] if row else '')
    def navigate(self,seq=0,direction=1,mode='instruction',pc=None,start=None,end=None):
        seq=int(seq);direction=1 if int(direction)>0 else -1;op='>' if direction>0 else '<';order='ASC' if direction>0 else 'DESC'
        modes={'instruction':"kind='instruction'",'call':"flow='call' AND state='executed'",'return':"flow='return' AND state='executed'",'exception':'type IN (9,10)','issue':"state='unknown' OR type IN (1,2,6,7,8)",'source':"kind='instruction' AND line IS NOT NULL"}
        if mode not in modes:raise ValueError('未知导航类型')
        params=[seq];clause=modes[mode]
        if pc is not None:clause="kind='instruction' AND pc=?";params.append(int(pc))
        if mode=='source' and pc is None:
            current=self.event(seq)['event'];clause+=' AND (coalesce(file,\'\')!=? OR coalesce(line,0)!=?)';params.extend([current.get('file',''),current.get('line') or 0])
        scope='';scope_params=[]
        if start is not None:scope+=' AND seq>=?';scope_params.append(int(start))
        if end is not None:scope+=' AND seq<=?';scope_params.append(int(end))
        with closing(self.connect()) as db:
            table='events'
            if pc is None and mode in ('call','return','exception','issue'):
                table='event_navigation';clause='mode=?';params=[seq,mode]
            row=db.execute(f'SELECT seq FROM {table} WHERE seq {op} ? AND ({clause})'+scope+f' ORDER BY seq {order} LIMIT 1',params+scope_params).fetchone()
            wrapped=False
            if not row and pc is not None:
                row=db.execute('SELECT seq FROM events WHERE pc=?'+scope+' ORDER BY seq LIMIT 1',[int(pc),*scope_params]).fetchone();wrapped=bool(row)
        return dict(seq=row[0] if row else None,wrapped=wrapped)
    def issues(self,offset=0,limit=100):
        with closing(self.connect()) as db:
            count=db.execute('SELECT count(*) FROM issues').fetchone()[0]
            rows=[json.loads(r[0]) for r in db.execute('SELECT data FROM issues ORDER BY id LIMIT ? OFFSET ?',(min(500,max(1,int(limit))),max(0,int(offset))))]
        return dict(rows=rows,total=count)
