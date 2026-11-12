"""Portable ETM workbench. Runtime and assets are bundled; user data stays beside the EXE."""
import argparse,gzip,hashlib,importlib.util,json,os,secrets,socket,sys,tempfile,threading,time,uuid,webbrowser,zipfile
from pathlib import Path
from urllib.request import urlopen
from flask import send_file,request
from werkzeug.serving import make_server

VERSION='1.0.14-native.15'
HERE=Path(__file__).resolve().parent
for stream in (sys.stdout,sys.stderr):
    if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf8',errors='replace')

def configure_engine(cli_engine,config):
    """便携入口默认启用完整优化链路；命令行优先于包配置。"""
    engine=cli_engine if cli_engine is not None else config.get('engine','native')
    if engine not in ('python','native','auto'):raise ValueError('engine 必须为 python、native 或 auto')
    # auto 与默认值一致；缺失能力时明确报错，不静默切换较慢的引擎。
    if engine=='auto':engine='native'
    # 避免开发机残留的实验环境开关改变双击启动的行为；只影响本进程。
    os.environ.update(ETM_STORE_WRITE='1',ETM_STORE_ENGINE=engine,
                      ETM_DECODE_ENGINE='fast',ETM_NATIVE_FORMAT='binary',
                      ETM_STORE_ENGINE_FALLBACK='0')
    return engine

def load_module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module;spec.loader.exec_module(module);return module

def load_current_web_queue():
    if getattr(sys,'frozen',False):
        # The ETM provenance snapshot may have an older queue module. The
        # build ships this workbench's authoritative queue separately.
        return load_module('web_cli_queue',Path(sys._MEIPASS)/'web_cli_queue.py')
    from etm.tools import web_cli_queue
    return web_cli_queue

_seed_guard=threading.Lock()
def valid_demo(folder):
    try:
        if not all((folder/name).is_file() for name in ('session.json','capture.csv','firmware.elf','sources.json','decoded.json')):return False
        for name in ('session.json','sources.json','decoded.json'):
            if not isinstance(json.loads((folder/name).read_text(encoding='utf8')),dict):return False
        return True
    except (OSError,ValueError):return False

def demo_bundled(backend):
    """构建时是否带了随包示例。

    `records/` 是 gitignored：在只含跟踪文件的构建里，同步脚本会**降级跳过** demo 并打印
    "包仍可用"。既然同步允许缺示例，启动器就不能无条件 seed —— 否则干净目录启动/`--check`
    直接 FileNotFoundError（审查 R10）。
    """
    return (backend/'demo/sample.etmrecord').is_file() and (backend/'demo/decoded.json.gz').is_file()

def seed_demo(backend,records):
    # Publish a complete directory only; retain any interrupted old seed outside
    # the record list instead of deleting potentially useful user evidence.
    folder=records/'demo-spi-70-35-v1'
    if not demo_bundled(backend):
        print('[bridge] 本包未内置示例记录（构建时源仓没有 records/ 演示数据），跳过 seed；'
              '采集、解码、浏览不受影响',flush=True)
        return False
    with _seed_guard:
        if valid_demo(folder):return True
        with tempfile.TemporaryDirectory(prefix='.etm-demo-',dir=records.parent) as temporary:
            staging=Path(temporary)/folder.name;staging.mkdir()
            with zipfile.ZipFile(backend/'demo/sample.etmrecord') as archive:
                for name in ['session.json','capture.csv','capture.kvdat','firmware.elf','sources.json','spi-validation.json']:
                    if name in archive.namelist():(staging/name).write_bytes(archive.read(name))
            (staging/'decoded.json').write_bytes(gzip.decompress((backend/'demo/decoded.json.gz').read_bytes()))
            metadata=json.loads((staging/'session.json').read_text(encoding='utf8'));metadata['name']='Demo · CPU 70 / Trace 35 MHz · 1 ms'
            (staging/'session.json').write_text(json.dumps(metadata,ensure_ascii=False),encoding='utf8')
            if not valid_demo(staging):raise ValueError('随包 Demo 不完整；原有记录未修改，请重新下载完整桥包')
            if valid_demo(folder):return  # Another workbench may share this data directory.
            if folder.exists():folder.rename(records.parent/('demo-recovery-'+uuid.uuid4().hex))
            try:staging.rename(folder)
            except FileExistsError:
                if not valid_demo(folder):raise

def package_archive(home,data):
    # Re-download the runtime without copying user captures or records.
    manifest=json.loads((home/'package-manifest.json').read_text(encoding='utf8'))
    app=Path(manifest.get('launcher','etm-bridge.exe')).stem
    archive_root=manifest.get('archiveRoot','etm-bridge-kit')
    destination=data/'exports'/(app+'-win64.zip');destination.parent.mkdir(parents=True,exist_ok=True)
    signature=manifest['packageId']+hashlib.sha256((home/'etm.config.json').read_bytes()).hexdigest()
    stamp=destination.with_suffix('.stamp')
    if destination.exists() and stamp.exists() and stamp.read_text()==signature:return destination
    staging=destination.with_suffix('.building')
    with zipfile.ZipFile(staging,'w',zipfile.ZIP_DEFLATED) as archive:
        for item in manifest['files']:
            source=(home/item['name']).resolve()
            if not source.is_relative_to(home.resolve()) or item['name'].startswith('data/'):raise ValueError('Invalid package manifest')
            archive.write(source,archive_root+'/'+item['name'])
            if item['name']=='etm.config.json':item.update(bytes=source.stat().st_size,sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        archive.writestr(archive_root+'/package-manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
    staging.replace(destination);stamp.write_text(signature);return destination

def cli_runtime(data,port):
    return data/'workbench-cli'/str(port)

def cli_browser_url(folder,entry):
    state=json.loads((folder/'toolbox-runtime.json').read_text(encoding='utf8'))
    token=(folder/'cli-token').read_text(encoding='ascii')
    return state['url']+'/'+(entry or '#flash')+'?trace-cli='+token

def main():
    home=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else HERE
    runtime=Path(sys._MEIPASS) if getattr(sys,'frozen',False) else HERE
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int);parser.add_argument('--config',type=Path,default=home/'etm.config.json')
    parser.add_argument('--data-dir',type=Path,default=home/'data');parser.add_argument('--check',action='store_true');parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--no-workbench-cli',action='store_true',help='关闭本机工作台 CLI 队列')
    parser.add_argument('--engine',choices=['python','native','auto'],help='解码引擎：默认 native（完整优化链路），python 用于对照排查；auto 同 native')
    args=parser.parse_args();config=json.loads(args.config.read_text(encoding='utf-8-sig')) if args.config.exists() else {}
    engine=configure_engine(args.engine,config)
    port=args.port if args.port is not None else int(config.get('port',8878))
    if not 1024<=port<=65535:raise ValueError('端口必须在 1024～65535 之间')
    data=args.data_dir.resolve();backend=runtime/'backend';toolbox=home/'toolbox'
    for path in [backend/'server.py',backend/'build-native/etm_decode.exe',backend/'build-native/etm_export.exe',toolbox/'index.html']:
        if not path.is_file():raise ValueError('完整包缺少文件：'+str(path)+'；请重新解压完整 ZIP')
    data.mkdir(parents=True,exist_ok=True);records=data/'records';records.mkdir(exist_ok=True)
    # Native helpers can inherit Low MIC from the package directory and fail
    # to write the desktop user's system TEMP. Scope a writable runtime temp
    # directory to this workbench process and its children (ISSUE-029).
    temporary=data/'runtime-tmp';temporary.mkdir(exist_ok=True)
    os.environ.update(TEMP=str(temporary),TMP=str(temporary));tempfile.tempdir=str(temporary)
    os.environ['WEB_ETM_RECORDS']=str(records)
    server=load_module('portable_same_origin',runtime/'serve-etm.py')
    application=server.create_application(backend,toolbox)
    # Hidden Python modules have __file__ under the bundled runtime, so select the snapshot root explicitly.
    sys.modules['decode_trace'].ROOT=backend
    package_manifest=json.loads((home/'package-manifest.json').read_text(encoding='utf8'))
    package_id=package_manifest['packageId'];version=package_manifest.get('version',VERSION)
    workbench=package_manifest.get('launcher')=='superlinkPRO.exe'
    entry='' if workbench else '#etm'
    cli_enabled=not args.no_workbench_cli
    folder=cli_runtime(data,port)
    app=application.app
    app.view_functions['launcher_info']=lambda:dict(protocol=1,portable=True,version=version,packageId=package_id,port=port,dataDirectory=str(data),engine=engine,workbenchCli=cli_enabled)
    archive_guard=threading.Lock()
    def download_package():
        with archive_guard:return send_file(package_archive(home,data),as_attachment=True)
    app.add_url_rule('/bridge/etm/downloads/etm-bridge-win64.zip','download_portable',download_package)
    def download_manifest():
        with archive_guard:
            archive=package_archive(home,data)
            return dict(version=version,platform='windows-x64',file=archive.name,bytes=archive.stat().st_size,sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),packageId=package_id)
    app.add_url_rule('/bridge/etm/downloads/manifest.json','portable_download_manifest',download_manifest)
    if workbench:
        app.view_functions['workbench_download']=lambda:download_manifest() if request.path.endswith('/manifest.json') else download_package()
    print(f'superlinkPRO {version}；解码引擎：{engine}；数据目录：{data}',flush=True)
    if args.check:
        seed_demo(backend,records);print('环境预检通过；运行环境、网页与解码器完整。',flush=True);return
    url=f'http://127.0.0.1:{port}/'
    try:
        with urlopen(url+'etm-launcher.json',timeout=1) as response:existing=json.load(response)
    except Exception:existing={}
    if existing.get('portable') and existing.get('packageId')==package_id and existing.get('dataDirectory')==str(data):
        if existing.get('engine')!=engine:raise ValueError('已有工作台使用不同解码引擎，请先退出已有工作台再重新启动；不会停止已有服务')
        if existing.get('workbenchCli',False)!=cli_enabled:raise ValueError('已有工作台的 CLI 开关不同，请先退出后再重新启动')
        print('同版本工作台已运行：'+url+entry,flush=True)
        if not args.no_browser:webbrowser.open(cli_browser_url(folder,entry) if cli_enabled else url+entry)
        return
    try:http=make_server('127.0.0.1',port,application,threaded=True)
    except (OSError,SystemExit):raise ValueError(f'端口 {port} 已被占用，请修改 etm.config.json 的 port；不会停止已有服务')
    try:
        if cli_enabled:
            folder.mkdir(parents=True,exist_ok=True)
            # A new process must not accept commands from tabs authorized by an old process.
            staging=folder/('cli-token-'+uuid.uuid4().hex)
            staging.write_text(secrets.token_hex(32),encoding='ascii')
            staging.replace(folder/'cli-token')
            load_current_web_queue().install_web_cli(app,folder/'cli-token')
            if workbench:
                from tools.command_manual import install as install_manual
                install_manual(app, folder/'cli-token', runtime/'docs/toolbox-cli-first-run.md',
                               [sys.executable, '--workbench-cli'], home)
            (folder/'toolbox-runtime.json').write_text(json.dumps(dict(protocol='workbench/1',url=url.rstrip('/'),pid=os.getpid())),encoding='utf8')
        seed_demo(backend,records)
        print('KingstVIS 需开启 Socket 并使能五路通道。',flush=True)
        print('工作台：'+url+entry+'（退出按 Ctrl+C）',flush=True)
        if cli_enabled:print('工作台 CLI 运行时：'+str(folder),flush=True)
        if not args.no_browser:webbrowser.open(cli_browser_url(folder,entry) if cli_enabled else url+entry)
        http.serve_forever()
    except KeyboardInterrupt:pass
    finally:http.server_close()

if __name__=='__main__':
    try:
        if len(sys.argv)>1 and sys.argv[1]=='--workbench-cli':
            import toolbox
            home=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else HERE
            config=home/'etm.config.json'
            port=int(json.loads(config.read_text(encoding='utf-8-sig')).get('port',8878)) if config.exists() else 8878
            options=sys.argv[2:]
            default=cli_runtime(home/'data',port)
            if options and options[0]=='open-browser':
                parser=argparse.ArgumentParser(description='为已启动的本机 CLI 队列打开执行页面')
                parser.add_argument('--runtime',type=Path,default=default)
                args=parser.parse_args(options[1:])
                toolbox.Client(args.runtime).http('health')
                webbrowser.open(cli_browser_url(args.runtime,'#flash'))
            else:
                sys.exit(toolbox.main(options,portable_runtime=default))
        elif len(sys.argv)>1 and sys.argv[1] in ('--cli','--cli-host'):
            runtime=Path(sys._MEIPASS) if getattr(sys,'frozen',False) else HERE
            for path in (runtime/'backend/tools',runtime/'backend/tools/la'):
                sys.path.insert(0,str(path))
            if sys.argv[1]=='--cli-host':
                if getattr(sys,'frozen',False):load_current_web_queue()
                from trace_cli_runtime import host
                host(sys.argv[2])
            else:
                import decode_trace
                decode_trace.ROOT=runtime/'backend'
                from trace_cli import main as cli_main
                sys.exit(cli_main(sys.argv[2:]))
        else:main()
    except Exception as error:print('[错误] '+str(error),file=sys.stderr,flush=True);sys.exit(1)
