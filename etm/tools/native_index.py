"""Optional native SQLite bulk insertion; Python owns validation and publication."""
import json
import os
import subprocess
import decode_trace


def executable():
    if os.environ.get('ETM_PYTHON_INDEX')=='1':return None
    path=decode_trace.ROOT/'build-native'/('etm_index.exe' if os.name=='nt' else 'etm_index')
    return path if path.is_file() else None


def build(path,source,sidecar,count):
    helper=executable()
    if helper is None:raise ValueError('缺少原生索引器')
    result=subprocess.run([str(helper),str(path),str(source),str(sidecar),str(count)],
                          stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=300)
    if result.returncode:
        raise ValueError('原生索引构建失败：'+result.stderr.decode('utf8','replace')[-2000:])
    value=json.loads(result.stdout)
    if value.get('indexFormat')!=7:
        raise ValueError('原生索引器版本不符；请重建工具，或设置 ETM_PYTHON_INDEX=1 使用兼容实现。')
    if value.get('rows')!=count:raise ValueError('原生索引记录数不一致')
    return value.get('firstSeq')
