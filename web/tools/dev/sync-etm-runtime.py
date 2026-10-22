"""Refresh the reviewed ETM runtime snapshot; not needed by end users or ordinary builds."""
import argparse,gzip,hashlib,json,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
FILES=['server.py','requirements.txt','tools/decode_trace.py','tools/decode_stream.py','tools/stream_store.py','tools/slice_merge.py','tools/trace_port.py','tools/tpiu_demux.py','tools/flow_analysis.py','tools/record_store.py','tools/source_export.py','tools/breakpoint_resolver.py','tools/la/kingst_la.py','tools/kingst_record.py','tools/timeline_index.py','tools/native_index.py','tools/fast_window.py','tools/trace_store.py','tools/trace_store_write.py','tools/trace_store_adapter.py','tools/native_binary.py']
FILES += ['tools/trace_store_index.py','tools/trace_store_nav.py','tools/native_facts_index.py',
          'tools/native_pipeline.py','tools/static_dict.py','tools/native_frontend.py',
          'tools/native_storebuild.py','tools/trace_frontend.py','tools/native_export.py','tools/native_selection.py']
# 可选原生索引器：只有配置时找到 SQLite3，CMakeLists 才会生成 etm_index 目标（否则 Python 回退）。
# FindSQLite3 模块在缓存里写的是单数 INCLUDE_DIR/LIBRARY；config 模式才可能有 SQLite3_DIR。
SQLITE_CACHE_KEYS=('SQLite3_INCLUDE_DIR','SQLite3_LIBRARY')
INDEXER_TARGET='etm_index'
TARGET_MISSING_MARKERS=('unknown target','no rule to make target','msb4057',
                        "target 'etm_index' does not exist",'target "etm_index" does not exist',
                        'does not exist in the project')


def native_indexer_cache_state(build_dir):
    """读取 CMakeCache，判断配置阶段是否找到 SQLite3；返回 (available, reason)。

    返回 None 表示配置本身缺失（不是可降级的可选依赖问题），调用方必须失败。
    """
    cache=Path(build_dir)/'CMakeCache.txt'
    if not cache.is_file():
        return None,f'尚未配置原生构建目录（缺 {cache}）'
    values={}
    for line in cache.read_text(encoding='utf8',errors='replace').splitlines():
        if line.startswith('//') or '=' not in line:continue
        key,_,value=line.partition('=')
        base=key.split(':',1)[0]
        if base in SQLITE_CACHE_KEYS:values[base]=value.strip()
    missing=[key for key in SQLITE_CACHE_KEYS if not values.get(key) or values[key].endswith('-NOTFOUND')]
    if values.get('SQLite3_DIR','').endswith('-NOTFOUND'):
        missing.append('SQLite3_DIR')
    if missing:
        return False,'未找到 SQLite3 开发库（'+', '.join(missing)+' 缺失或 NOTFOUND）'
    return True,''


def native_indexer_target_missing(output):
    """只有“报错里点名 etm_index 目标不存在”才算可降级的可选依赖缺失。

    防止把依赖/对象文件缺失的 “No rule to make target 'sqlite3.lib'” 误判成目标不存在。
    """
    text=(output or '').lower()
    return INDEXER_TARGET in text and any(marker in text for marker in TARGET_MISSING_MARKERS)


def _last_line(text):
    lines=[line.strip() for line in (text or '').splitlines() if line.strip()]
    return lines[-1][:240] if lines else ''


def drop_stale_indexer(target):
    """降级/失败时移除快照里的旧派生产物，避免 provenance 继续宣称拥有它。"""
    target=Path(target)
    if target.exists():
        target.unlink()
        print(f'[sync] 已移除快照旧派生产物：{target}')


def ensure_native_indexer(source,dest,runner=subprocess.run):
    """构建并发布可选原生索引器；返回 {'available','reason','path'}。

    只在“明确缺 SQLite3 开发库（CMake 不生成目标）”或“CMake 目标不存在”时降级：
    删除目标快照旧的 etm_index.exe 并打印 Python 回退提示。真实 CMake/编译错误、
    构建成功却没有产物，一律 SystemExit（不降级、不写 provenance）。
    """
    source,dest=Path(source),Path(dest)
    build_dir=source/'build-native';source_exe=build_dir/'etm_index.exe'
    target=dest/'build-native/etm_index.exe'
    available,reason=native_indexer_cache_state(build_dir)
    if available is None:
        # 配置缺失属于树/环境损坏，不是“可选依赖缺失”，不能静默降级。
        raise SystemExit('[sync] 无法构建原生索引器：'+reason)
    if available:
        try:
            runner(['cmake','--build',str(build_dir),'--target',INDEXER_TARGET,'--config','Release'],
                   check=True,capture_output=True,text=True)
        except subprocess.CalledProcessError as error:
            output='\n'.join(part for part in (getattr(error,'stdout',None),getattr(error,'stderr',None),str(error)) if part)
            if native_indexer_target_missing(output):
                available,reason=False,f'CMake 目标 {INDEXER_TARGET} 不存在（未启用 SQLite3）：{_last_line(output)}'
            else:
                drop_stale_indexer(target)
                raise SystemExit('[sync] 原生索引器构建失败（真实 CMake/编译错误，不降级、不发布 provenance）：\n'+output[-4000:])
        else:
            if not source_exe.is_file():
                drop_stale_indexer(target)
                raise SystemExit(f'[sync] 原生索引器构建返回成功但缺少产物：{source_exe}')
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source_exe,target)
            return dict(available=True,reason='',path='build-native/etm_index.exe')
    drop_stale_indexer(target)
    print(f'[sync] 本次快照无原生索引器，走 Python 回退：{reason}')
    return dict(available=False,reason=reason,path=None)


def packaged_hashes(dest):
    dest=Path(dest)
    return {p.relative_to(dest).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in dest.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}


def build_provenance(source,dest,origins,indexer,revision=None):
    source,dest=Path(source),Path(dest)
    revision=revision or subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip()
    local_changes=['server.py: WEB_ETM_RECORDS redirects mutable records to portable data directory',
                   'native/decoder.cpp: UTF-16 Windows argv/file APIs for UTF-8 manifests and Unicode paths; recompiled static OpenCSD wrapper']
    if indexer.get('available'):
        local_changes.append('native/indexer.cpp: static SQLite bulk index and compact browsing metrics')
    else:
        local_changes.append('native/indexer.cpp: 本次快照未构建原生索引器（'+str(indexer.get('reason') or '缺 SQLite3 开发库')+'）；使用 Python 回退，provenance 不含 build-native/etm_index.exe')
    return dict(sourceRepository='etm',sourceRevision=revision,sourceHashes=origins,
                localChanges=local_changes,
                nativeIndexer=dict(available=bool(indexer.get('available')),reason=indexer.get('reason') or None),
                packagedHashes=packaged_hashes(dest))


def write_provenance(root,provenance):
    path=Path(root)/'bridge/etm/provenance.json';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(provenance,ensure_ascii=False,indent=2)+'\n',encoding='utf8')


def publish_native_assets(source,dest,root,origins,runner=subprocess.run,revision=None):
    """索引器构建 + provenance 发布；任一真实错误都在写 provenance 之前中止。"""
    indexer=ensure_native_indexer(source,dest,runner=runner)
    provenance=build_provenance(source,dest,origins,indexer,revision=revision)
    write_provenance(root,provenance)
    return indexer,provenance

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--etm-root',required=True,type=Path)
    parser.add_argument('--compiler-prefix',type=Path,help='可选的本地 GCC 内部程序目录（-B）')
    args=parser.parse_args()
    source=args.etm_root.resolve();dest=ROOT/'bridge/etm/backend';origins={}
    paths=FILES+[p.relative_to(source).as_posix() for p in (source/'web').rglob('*') if p.is_file()]
    # ⚠️ 不要在这里复制 `build-native/etm_decode.exe`：它是构建产物（gitignored），
    #    裸克隆里没有；旧顺序会先因为缺 exe 抛异常，后面的 cmake 自动构建永远轮不到（审查 R09）。
    #    exe 由下面最后一步 g++ 直接链接进快照。
    paths+=['native/decoder.cpp','native/indexer.cpp','native/frontend.cpp','native/storebuild.cpp','native/export.cpp',
            'native/SQLITE-LICENSE.txt','vendor/OpenCSD-1.8.3/LICENSE']
    for name in paths:
        target=dest/name;target.parent.mkdir(parents=True,exist_ok=True)
        data=(source/name).read_bytes()
        # 源树在 Windows 上按 autocrlf 检出成 CRLF；快照目录里历史文件的行尾是**混合**的
        # （git 对 text=unset 的路径不做归一化）。copyfile 会把 60 个文件整体改行尾，diff 全是噪声。
        # 规则：内容只差行尾 → 保留快照里的字节；快照原本是 LF 而内容真变了 → 仍写 LF。
        if target.exists():
            old=target.read_bytes()
            if old.replace(b'\r\n',b'\n')==data.replace(b'\r\n',b'\n'):
                origins[name]=hashlib.sha256(old).hexdigest()
                continue
            if b'\r\n' not in old:
                data=data.replace(b'\r\n',b'\n')
        target.write_bytes(data)
        origins[name]=hashlib.sha256(target.read_bytes()).hexdigest()
    notices=set()
    for file in (source/'vendor/OpenCSD-1.8.3/decoder').rglob('*'):
        if file.is_file() and file.suffix in ('.cpp','.c','.h'):
            for line in file.read_text(encoding='utf8',errors='replace').splitlines():
                if 'copyright' in line.lower() and ('20' in line or '19' in line):notices.add(line.strip().lstrip('/* ').rstrip('*/ '))
    (dest/'vendor/OpenCSD-1.8.3/NOTICES.txt').write_text('\n'.join(sorted(notices))+'\n',encoding='utf8')
    for name in ['gcc-libs','headers','libwinpthread','winpthreads','openssl','zlib']:
        directory=Path('C:/msys64/mingw64/share/licenses')/name
        for file in directory.iterdir():
            if file.is_file():
                target=dest/'vendor/native-runtime-licenses'/name/file.name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(file,target)
    # Keep mutable records beside the portable executable, outside packaged read-only resources.
    server=dest/'server.py';text=server.read_text(encoding='utf8')
    # etm/server.py 本身已经 import os（CORS 白名单要用环境变量）→ 只在缺的时候补，避免重复 import。
    if 'import os\n' not in text: text=text.replace('import base64\n','import base64\nimport os\n',1)
    text=text.replace("RECORDS = ROOT / 'records'","RECORDS = Path(os.environ.get('WEB_ETM_RECORDS', str(ROOT / 'records')))")
    server.write_text(text,encoding='utf8')
    # Windows command-line/file APIs use UTF-16; the memory manifest uses UTF-8.
    native=dest/'native/decoder.cpp';text=native.read_text(encoding='utf8')
    text=text.replace('#include <fstream>','#include <fstream>\n#ifdef _WIN32\n#include <windows.h>\n#endif',1)
    helper='''#ifdef _WIN32
static std::wstring file_path(const std::string &path) {
    const int length=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,path.data(),(int)path.size(),0,0);
    if(!length) throw std::runtime_error("Invalid UTF-8 path");
    std::wstring wide(length,0); MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,path.data(),(int)path.size(),&wide[0],length); return wide;
}
#else
static const std::string &file_path(const std::string &path) { return path; }
#endif
'''
    text=text.replace('namespace {',helper+'namespace {',1)
    text=text.replace('std::ifstream f(path.c_str(),std::ios::binary);','std::ifstream f(file_path(path).c_str(),std::ios::binary);')
    text=text.replace('int main(int argc,char **argv)', 'static int decode_main(int argc,char **argv)',1)
    text=text.replace('std::ifstream config(argv[2]);','std::ifstream config(file_path(argv[2]).c_str());',1)
    # P2 二进制模式之后 config/输出路径改为运行时变量；快照里同样要走 UTF-8→UTF-16 转换。
    text=text.replace('std::ifstream config(config_path.c_str());','std::ifstream config(file_path(config_path).c_str());',1)
    text=text.replace('out_.open(path.c_str(), std::ios::binary | std::ios::trunc);','out_.open(file_path(path).c_str(), std::ios::binary | std::ios::trunc);',1)
    text+='''
#ifdef _WIN32
int wmain(int argc,wchar_t **wide) {
    std::vector<std::string> utf8; std::vector<char *> argv;
    for(int i=0;i<argc;i++) {
        int size=WideCharToMultiByte(CP_UTF8,0,wide[i],-1,0,0,0,0); std::string value(size,0);
        WideCharToMultiByte(CP_UTF8,0,wide[i],-1,&value[0],size,0,0); value.resize(size-1); utf8.push_back(value);
    }
    for(auto &value:utf8) argv.push_back(&value[0]);
    return decode_main(argc,argv.data());
}
#else
int main(int argc,char **argv) { return decode_main(argc,argv); }
#endif
'''
    native.write_text(text,encoding='utf8')
    for name in ['firmware/stm32h7b0_spi_trace/stable-70-35-v1/fw.elf','firmware/stm32f103_cdc_trace/fw-ze.elf','firmware/stm32h743_cdc_trace/fw.elf']:
        target=dest/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source/name,target)
        origins[name]=hashlib.sha256(target.read_bytes()).hexdigest()
    # Carry one known demo only. No user recordings, SDKs or toolchain are copied.
    # ⚠️ demo 记录来自 `etm/records/<previewRecord>`，而 `records/` 是 **gitignored** 的
    #    ⇒ 裸克隆（只含 git 跟踪内容）里没有它。这里降级跳过而不是崩：
    #    "运行/采集/解码"不依赖它，只有"包内自带一条示例记录"用得到。
    manifest=json.loads((source/'baselines/spi-70-35-v1/manifest.json').read_text(encoding='utf8'))
    folder=source/'records'/manifest['previewRecord'];demo=dest/'demo';demo.mkdir(exist_ok=True)
    if folder.is_dir():
        with zipfile.ZipFile(demo/'sample.etmrecord','w',zipfile.ZIP_DEFLATED) as archive:
            for name in ['session.json','capture.csv','capture.kvdat','firmware.elf','sources.json','spi-validation.json']:
                if (folder/name).is_file():archive.write(folder/name,name)
        (demo/'decoded.json.gz').write_bytes(gzip.compress((folder/'decoded.json').read_bytes(),mtime=0))
    else:
        print(f'[sync] ⚠ 缺少 demo 记录 {folder}（records/ 是 gitignored，裸克隆里没有）—— 跳过内置示例，包仍可用')
    baseline=dest/'baselines/spi-70-35-v1';baseline.mkdir(parents=True,exist_ok=True)
    (baseline/'manifest.json').write_text(json.dumps(dict(version=1,id='spi-70-35-v1',profile='spi-70',record='demo-spi-70-35-v1',previewRecord='demo-spi-70-35-v1',sourcePreview=manifest['previewRecord'])),encoding='utf8')
    # Rebuild the portable wrapper against the source project's static OpenCSD libraries.
    # ⚠️ `build-native/` 是构建产物（gitignored），裸克隆里没有 ⇒ 这里**自动用 cmake 建一次**：
    #    OpenCSD 源码（etm/vendor/OpenCSD-1.8.3）是 git 跟踪内容，所以这一步是自足的。
    libs=[source/'build-native/vendor/OpenCSD-1.8.3/libopencsd_c_api.a',source/'build-native/vendor/OpenCSD-1.8.3/libopencsd.a']
    if not all(p.is_file() for p in libs):
        print('[sync] build-native 缺 OpenCSD 静态库 → 自动构建（cmake + ninja + msys64 gcc/g++）')
        subprocess.run(['cmake','-S',str(source),'-B',str(source/'build-native'),'-G','Ninja','-DCMAKE_BUILD_TYPE=Release',
                        '-DCMAKE_C_COMPILER=C:/msys64/mingw64/bin/gcc.exe','-DCMAKE_CXX_COMPILER=C:/msys64/mingw64/bin/g++.exe'],check=True)
        subprocess.run(['cmake','--build',str(source/'build-native'),'--config','Release'],check=True)
    for p in libs:
        if not p.is_file():raise SystemExit(f'[sync] 自动构建后仍缺静态库：{p}')
    compiler_options=['-B'+args.compiler_prefix.resolve().as_posix()+'/'] if args.compiler_prefix else []
    subprocess.run(['C:/msys64/mingw64/bin/g++.exe',*compiler_options,'-std=c++11','-O2','-municode','-static','-static-libgcc','-static-libstdc++','-I'+str(source/'vendor/OpenCSD-1.8.3/decoder/include'),str(native),str(libs[0]),str(libs[1]),'-o',str(dest/'build-native/etm_decode.exe')],check=True)
    # These helpers are required by the explicit native engine, so build errors
    # must stop publication instead of silently shipping an older executable.
    subprocess.run(['cmake','--build',str(source/'build-native'),'--target',
                    'etm_frontend','etm_storebuild','etm_export','--config','Release'],check=True)
    for name in ('etm_frontend.exe','etm_storebuild.exe','etm_export.exe'):
        shutil.copyfile(source/'build-native'/name,dest/'build-native'/name)
    # Rebuild the optional native indexer against the current source as well.
    # SQLite3 dev headers are not required for the Python fallback; a source
    # tree without them must still be able to publish a working snapshot.
    # 审查发现 1：只有明确缺 SQLite3/目标不存在才降级，且降级时删除旧 EXE；
    # 真实 CMake/编译错误必须失败，不能留下旧派生产物再生成新 provenance。
    indexer,provenance=publish_native_assets(source,dest,ROOT,origins)
    suffix='' if indexer['available'] else '；无原生索引器（Python 回退）'
    print(f'Runtime snapshot: {len(provenance["packagedHashes"])} files; source {provenance["sourceRevision"]}{suffix}')
if __name__=='__main__':main()
