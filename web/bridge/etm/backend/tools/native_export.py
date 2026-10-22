"""Prepare static export text once; C++ reads and projects dynamic RUNs directly.

The host adapter holds a verified generation lease for this entire operation.
No query-index/checkpoint state is repurposed as a decoder state. The existing
Python writer remains the explicit reference and the old-format fallback.
"""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

FORMATS=('simplified','source','simplified-html','csv')
PROTOCOL='etm-export/1'


def binary_path():
    import decode_trace
    return decode_trace.ROOT/'build-native/etm_export.exe'


def prefer_prepared_small_range(store,start=None,end=None):
    """A prepared numeric reader beats process/SHA overhead for one small cell.

    Explicit engine settings retain their diagnostic meaning. An old/unprepared
    index must not send a deep selection back through state-machine replay.
    """
    if 'ETM_EXPORT_ENGINE' in os.environ:return False
    index=store.query_index
    if index is None or getattr(index,'_row_state',None) is None:return False
    lo=store.first_seq if start is None else max(store.first_seq,int(start))
    hi=store.last_seq if end is None else min(store.last_seq,int(end))
    return max(0,hi-lo+1)<=4096


def _json(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':')).replace('&','\\u0026').replace('<','\\u003c').replace('>','\\u003e')


def _csv_tail(fields):
    out=io.StringIO(newline='');csv.writer(out).writerow(['',*fields]);return out.getvalue()


def prepare_plan(store,result,kind,path,start=None,end=None):
    """Only static PCs, source lines and sparse strings cross the language boundary."""
    from source_export import base,_html_document
    sources=result.get('sources',{})
    clean=lambda text:str(text).replace('*/','* /')
    source_lines={file:text.splitlines() for file,text in sources.items()}
    if kind not in FORMATS:raise ValueError('未知执行流导出类型')
    if kind!='csv' and not any(text.strip() for text in sources.values()):
        raise ValueError('当前记录尚未载入配套源码文本。请在“4 文件”点击“配套源码设置”选择对应固件的源码目录，再导出；ELF 中的文件名和行号不包含源码正文。只需指令记录可选择“执行流 CSV”。')
    pool=[];ids={}
    def intern(value):
        value=value.encode('utf8')
        if value not in ids:ids[value]=len(pool);pool.append(value)
        return ids[value]
    protocol=result.get('protocol','ETMv4');bounds=result.get('exportRange')
    scope='/* 导出执行序号区间 %s；不是微秒时间区间。 */\n' % bounds if bounds else ''
    unprocessed=result.get('quality',{}).get('unprocessedBytes')
    if kind=='simplified':
        header='/* %s 简化源码执行流：按恢复顺序排列，不是可编译程序。 */\n' % protocol+scope
        if unprocessed:header+='/* ETM 输入仍有未处理字节，缺失路径保持未知。 */\n'
        header+=''.join('/* 配套文件：'+clean(base(file))+' = '+clean(file)+' */\n' for file in sorted(sources))
        footer=''
    elif kind=='source':
        header='/* %s 源码执行流记录：按实际恢复顺序排列，不是可编译程序。 */\n' % protocol+scope
        if unprocessed:header+='/* 另有 %s 字节 ETM 输入未处理；缺失的执行路径保持未知。 */\n' % unprocessed
        footer=''
    elif kind=='simplified-html':
        marker='__ETM_EXPORT_NATIVE_PAYLOAD_4E16__'
        before,separator,after=_html_document(marker,bounds).partition(marker);assert separator
        header=before+'{"rows":[';footer='],"sources":'+_json(sources)+'}'+after
    else:
        header='\ufeffseq,kind,pc,executed,executionState,instruction,function,file,line,event\r\n';footer=''
    header_id,footer_id=intern(header),intern(footer)
    groups={};contexts={};entries=[]
    for entry in store.static:
        key=(entry.function,entry.file,entry.line);context=(entry.function,entry.file)
        group_id=groups.setdefault(key,len(groups));context_id=contexts.setdefault(context,len(contexts))
        lines=source_lines.get(entry.file,[]);valid=bool(entry.line and 0<entry.line<=len(lines))
        code=lines[entry.line-1] if valid else ''
        location=',"pc":'+str(entry.pc)+',"file":'+_json(entry.file)+',"line":'+_json(entry.line)+',"function":'+_json(entry.function)
        full=' PC=0x%08x %s · %s:%s */\n' % (entry.pc,clean(entry.function),clean(entry.file),entry.line)
        unknown='条件执行状态未知，不视为已证实执行';missing='无配套源码'
        values=[code+'\n', '/* '+clean(entry.function)+' · '+clean(base(entry.file))+' */\n' if valid else '',full,
                ' PC=0x%08x %s */\n' % (entry.pc,unknown),' PC=0x%08x %s */\n' % (entry.pc,unknown),
                ' PC=0x%08x %s */\n' % (entry.pc,missing),
                location+',"text":'+_json(code)+',"records":',
                location+',"text":'+_json(unknown)+'}',location+',"text":'+_json(missing)+'}']
        instruction=(entry.mnemonic+' '+entry.operands).strip()
        for executed,state in [(False,'not-taken'),(True,'executed'),(None,'unknown')]:
            values.append(_csv_tail(['instruction',hex(entry.pc),executed,state,instruction,entry.function,entry.file,entry.line,'']))
        entries.append([entry.pc,int(entry.predicated),int(valid),group_id,context_id,*map(intern,values)])
    event_text=[]
    for text in store.strings:
        event_text.append(list(map(intern,['/* '+clean(text)+' */\n',' '+clean(text)+' */\n',
                                          ',"text":'+_json(text)+'}',_csv_tail(['event','','','','','','','',text])])))
    file_info=next(item for item in store.manifest['files'] if item['name']=='store.bin')
    first,last=store.first_seq,store.last_seq
    lo=first if start is None else max(first,int(start));hi=last if end is None else min(last,int(end))
    digest=hashlib.sha256()
    with Path(path).open('wb') as out:
        def put(data):out.write(data);digest.update(data)
        def number(value):put(struct.pack('<Q',value))
        def blob(value):number(len(value));put(value)
        put(b'ETMEXP1\0')
        for value in [1,FORMATS.index(kind),lo,hi,store.container.generation_tag,file_info['bytes'],first,last,store.counts['events'],store.counts['instructions']]:number(value)
        blob(file_info['sha256'].encode('ascii'));number(len(pool))
        for value in pool:blob(value)
        number(header_id);number(footer_id);number(len(entries))
        for values in entries:
            for value in values:number(value)
        number(len(event_text))
        for values in event_text:
            for value in values:number(value)
        out.write(digest.digest())
    return dict(start=lo,end=hi,selectedEvents=max(0,hi-lo+1))


def write(store,result,path,kind,start=None,end=None):
    """Return False only before starting for explicit reference/unsupported stores."""
    engine=os.environ.get('ETM_EXPORT_ENGINE','native').strip().lower()
    if engine not in ('python','native'):raise ValueError('ETM_EXPORT_ENGINE 必须为 native 或 python')
    if engine=='python':return False
    if not (store.meta.get('seq') or {}).get('contiguous',True):return False
    binary=binary_path()
    if not binary.is_file():
        if getattr(sys,'frozen',False):raise ValueError('完整包缺少原生导出程序，请重新下载并完整解压')
        return False # Source/older installs keep their Python exporter.
    completed=subprocess.run([str(binary),'--version'],capture_output=True,timeout=15)
    try:info=json.loads(completed.stdout)
    except (ValueError,UnicodeDecodeError) as error:raise ValueError('原生导出程序握手无效，请更新完整包') from error
    if (completed.returncode or info.get('protocol')!=PROTOCOL or info.get('abi')!=1
            or info.get('storeSchema')!=1 or kind not in info.get('formats',[])):
        raise ValueError('原生导出程序版本不匹配，请更新完整包')
    with tempfile.TemporaryDirectory(prefix='.etm-export-',dir=Path(path).parent) as temporary:
        plan=Path(temporary)/'plan.bin';output=Path(temporary)/'export.out'
        selection=prepare_plan(store,result,kind,plan,start,end)
        completed=subprocess.run([str(binary),str(plan),str(store.container.path),str(output)],capture_output=True,timeout=900)
        if completed.returncode:
            raise ValueError('原生导出失败：'+completed.stderr.decode('utf8',errors='replace')[-2000:])
        try:summary=json.loads(completed.stdout)
        except (ValueError,UnicodeDecodeError) as error:raise ValueError('原生导出程序未返回有效结果') from error
        if summary.get('protocol')!=PROTOCOL or summary.get('matchedEvents')!=selection['selectedEvents']:
            raise ValueError('原生导出返回的记录范围不符')
        if not output.is_file() or output.stat().st_size!=summary.get('bytes'):
            raise ValueError('原生导出文件长度不符')
        digest=hashlib.sha256()
        with output.open('rb') as out:
            while chunk:=out.read(1<<20):digest.update(chunk)
        if digest.hexdigest()!=summary.get('sha256'):raise ValueError('原生导出文件指纹不符')
        os.replace(output,path)
    return True
