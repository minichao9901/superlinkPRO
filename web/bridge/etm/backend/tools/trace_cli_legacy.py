"""Expose the Web's existing legacy RecordStore contract to local CLI readers."""
import hashlib
import json
from pathlib import Path
from record_store import RecordStore
from stream_store import attach_streams


class LegacyView:
    def __init__(self,folder):
        self.folder=Path(folder)
        self.store=RecordStore(self.folder,self.result)

    def result(self):
        path=self.folder/'decoded.json'
        if path.stat().st_size>384<<20:raise ValueError('旧整段JSON超过384MiB，请先decode到新目录')
        value=json.loads(path.read_text(encoding='utf-8-sig'))
        meta=json.loads((self.folder/'session.json').read_text(encoding='utf8'))
        if value.get('config') and meta.get('config') and value['config']!=meta['config']:
            raise ValueError('旧记录解码配置与原始配置不一致')
        if value.get('streamed'):value=attach_streams(value,self.folder)
        if value.get('version',1)<3:
            from flow_analysis import enrich
            from decode_trace import Image
            enrich(value,Image(self.folder/'firmware.elf'))
        for key,name in [('capture','capture.kvdat'),('elf','firmware.elf')]:
            if key=='capture':
                name=meta.get('input','capture.csv')
                if name not in ('capture.csv','capture.bin','capture.kvdat'):raise ValueError('旧记录输入文件名无效')
            expected=(value.get(key) or {}).get('sha256')
            if expected:
                with (self.folder/name).open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
                if actual!=expected:raise ValueError('旧记录输入与解码结果不一致：'+name)
        value.setdefault('sources',self.sources())
        return value

    def __getattr__(self,name):return getattr(self.store,name)
    def ensure(self):self.store.ensure()
    def close(self):pass  # RecordStore opens/closes each SQLite connection itself.
    def sources(self):
        path=self.folder/'sources.json'
        if path.is_file():return json.loads(path.read_text(encoding='utf-8-sig'))
        value=json.loads((self.folder/'decoded.json').read_text(encoding='utf-8-sig'))
        return value.get('sources') or {}
    def iter_events(self,start=0,end=None):
        offset=0
        while True:
            value=self.store.page('events',offset,500,'',False,start,end)
            if not value['rows']:break
            yield from value['rows'];offset+=len(value['rows'])
            if offset>=value['total']:break
    def input_verification(self):
        return dict(verified=False,reason='旧记录无TraceStore输入绑定；按其声明校验原始输入与ELF，完整结果请重新decode')
    def export_execution(self,*_):return False
    def navigate(self,seq,direction,mode,pc,start,end):
        return self.store.navigate(seq,direction,'instruction' if mode=='pc' else mode,pc,start,end)
