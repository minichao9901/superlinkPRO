"""Refresh the reviewed ETM runtime snapshot; not needed by end users or ordinary builds."""
import argparse,gzip,hashlib,json,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
FILES=['server.py','requirements.txt','tools/decode_trace.py','tools/decode_stream.py','tools/stream_store.py','tools/slice_merge.py','tools/trace_port.py','tools/tpiu_demux.py','tools/flow_analysis.py','tools/record_store.py','tools/source_export.py','tools/breakpoint_resolver.py','tools/la/kingst_la.py','tools/kingst_record.py','tools/timeline_index.py','tools/native_index.py','tools/fast_window.py']
def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--etm-root',required=True,type=Path);args=parser.parse_args()
    source=args.etm_root.resolve();dest=ROOT/'bridge/etm/backend';origins={}
    paths=FILES+[p.relative_to(source).as_posix() for p in (source/'web').rglob('*') if p.is_file()]
    # ⚠️ 不要在这里复制 `build-native/etm_decode.exe`：它是构建产物（gitignored），
    #    裸克隆里没有；旧顺序会先因为缺 exe 抛异常，后面的 cmake 自动构建永远轮不到（审查 R09）。
    #    exe 由下面最后一步 g++ 直接链接进快照。
    paths+=['native/decoder.cpp','native/indexer.cpp','native/SQLITE-LICENSE.txt','vendor/OpenCSD-1.8.3/LICENSE']
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
    for name in ['gcc-libs','headers','libwinpthread','winpthreads']:
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
    text=text.replace('static std::vector<uint8_t> read(',helper+'static std::vector<uint8_t> read(',1)
    text=text.replace('std::ifstream f(path.c_str(),std::ios::binary);','std::ifstream f(file_path(path).c_str(),std::ios::binary);')
    text=text.replace('int main(int argc,char **argv)', 'static int decode_main(int argc,char **argv)',1)
    text=text.replace('std::ifstream config(argv[2]);','std::ifstream config(file_path(argv[2]).c_str());',1)
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
    subprocess.run(['C:/msys64/mingw64/bin/g++.exe','-std=c++11','-O2','-municode','-static','-static-libgcc','-static-libstdc++','-I'+str(source/'vendor/OpenCSD-1.8.3/decoder/include'),str(native),str(libs[0]),str(libs[1]),'-o',str(dest/'build-native/etm_decode.exe')],check=True)
    # Rebuild the optional native indexer against the current source as well.
    # SQLite3 dev headers are not required for the Python fallback; a source
    # tree without them must still be able to publish a working snapshot.
    try:
        subprocess.run(['cmake','--build',str(source/'build-native'),'--target','etm_index','--config','Release'],check=True)
        shutil.copyfile(source/'build-native/etm_index.exe',dest/'build-native/etm_index.exe')
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        print(f'[sync] 跳过原生索引器（没有 SQLite3 开发库或 CMake 目标）：{error}')
    provenance=dict(sourceRepository='etm',sourceRevision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip(),sourceHashes=origins,localChanges=['server.py: WEB_ETM_RECORDS redirects mutable records to portable data directory','native/decoder.cpp: UTF-16 Windows argv/file APIs for UTF-8 manifests and Unicode paths; recompiled static OpenCSD wrapper','native/indexer.cpp: static SQLite bulk index and compact browsing metrics'],packagedHashes={p.relative_to(dest).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in dest.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'})
    (ROOT/'bridge/etm/provenance.json').write_text(json.dumps(provenance,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(f'Runtime snapshot: {len(provenance["packagedHashes"])} files; source {provenance["sourceRevision"]}')
if __name__=='__main__':main()
