"""Build the Windows x64 ETM download from this repository's reviewed snapshot.

python tools/dev/build-etm-package.py
Requires build-only dependencies in bridge/etm/requirements-build.txt.
"""
import argparse,hashlib,importlib.metadata,json,os,shutil,subprocess,sys,uuid,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
VERSION='1.0.14-native.15'
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

RETIRED_ETM_ASSETS = {
    'web/app/shared/rtt/dap-webusb.js',
    'web/app/shared/core/bin.js',
    'web/app/shared/core/operation-budget.js',
}

def public_app_files(root):
    return [p.relative_to(root).as_posix() for p in (root/'app').rglob('*')
            if p.is_file() and not p.is_relative_to(root/'app/legacy') and p.suffix != '.map']

def tracked_samples(root):
    # -z preserves UTF-8, spaces and Git's otherwise C-quoted filenames.
    return [p for p in subprocess.check_output(['git','ls-files','-z','--','samples'],cwd=root).decode('utf8').split('\0') if p]
def package_cli(package,toolbox,node,backend):
    """Ship the Web command sources and pinned dependencies, not an SDK."""
    repo=ROOT.parent;cli=package/'cli'
    # The frozen host serves package/toolbox (trace_cli_paths.toolbox_root).
    # Native Node imports still need cli/web/app and index.html, but copying
    # panel images/animations here duplicated ~5 MB of compressed sample data.
    def cli_only_modules(folder,names):
        folder=Path(folder)
        if folder==toolbox:return {'samples'}
        # Native worker imports source modules; the GUI bundle is served once
        # from toolbox/app/site and is never executed by linkedom.
        if folder==toolbox/'app':return {'site'}
        return set()
    shutil.copytree(toolbox,cli/'web',ignore=cli_only_modules)
    shutil.copyfile(ROOT/'package.json',cli/'web/package.json')
    service=cli/'web/tools/dev/serve-etm.py';service.parent.mkdir(parents=True)
    shutil.copyfile(ROOT/'tools/dev/serve-etm.py',service)
    shutil.copytree(backend/'web',cli/'etm/web')
    (cli/'package.json').write_text('{"private":true,"type":"module"}\n',encoding='utf8')
    transport=cli/'etm/tools/node-transport';transport.parent.mkdir(parents=True,exist_ok=True)
    shutil.copytree(repo/'etm/tools/node-transport',transport,ignore=shutil.ignore_patterns('__pycache__','.git'))
    shutil.copyfile(repo/'etm/tools/trace_target_cli.mjs',transport.parent/'trace_target_cli.mjs')
    executable=cli/'node/node.exe';executable.parent.mkdir()
    shutil.copyfile(node,executable)
    version=subprocess.check_output([str(node),'-p','process.version'],text=True).strip()
    license=ROOT/'bridge/etm/licenses'/('Node-'+version+'-LICENSE.txt')
    if not license.is_file():raise ValueError('缺少对应Node版本的第三方许可证：'+str(license))
    shutil.copyfile(license,package/'licenses'/license.name)
    shutil.copyfile(repo/'docs/trace-cli-ai.md',package/'Trace-CLI-中文说明.md')
    shutil.copyfile(repo/'docs/etm-flame-svg.md',package/'etm-flame-svg.md')
    (package/'etm-trace.cmd').write_text('@echo off\n"%~dp0etm-bridge.exe" --cli %*\nexit /b %errorlevel%\n',encoding='ascii')
    return version

def main(argv=None):
    default_output=ROOT/'tmp/packages'/VERSION if '-rc.' in VERSION else ROOT/'bridge/etm/downloads'
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=default_output)
    parser.add_argument('--app-name',default='etm-bridge',choices=('etm-bridge','superlinkPRO'))
    parser.add_argument('--version',default=VERSION)
    args=parser.parse_args(argv)
    if os.name!='nt' or sys.maxsize<=2**32:raise ValueError('请在 Windows 64 位 Python 下构建')
    node=shutil.which('node')
    if not node:raise ValueError('构建CLI包需要Node.js')
    if subprocess.check_output([node,'-p','process.arch'],text=True).strip()!='x64':raise ValueError('CLI包需要Windows x64 Node')
    subprocess.run([node,'tools/dev/build-web.mjs','--check'],cwd=ROOT,check=True)
    snapshot=ROOT/'bridge/etm/backend'
    provenance=json.loads((ROOT/'bridge/etm/provenance.json').read_text(encoding='utf8'))
    for name,expected in provenance['packagedHashes'].items():
        if sha(snapshot/name)!=expected:raise ValueError('ETM 快照与来源清单不一致：'+name)
    work=ROOT/'tmp'/('etm-package-'+uuid.uuid4().hex[:8]);work.mkdir(parents=True)
    payload=work/'backend'
    packaged_hashes={name:digest for name,digest in provenance['packagedHashes'].items()
                     if name not in RETIRED_ETM_ASSETS}
    for name in packaged_hashes:
        target=payload/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(snapshot/name,target)
    command=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir','--name',args.app_name,'--contents-directory','runtime','--distpath',str(work/'dist'),'--workpath',str(work/'build'),'--specpath',str(work),'--add-data',str(payload)+';backend','--add-data',str(ROOT/'tools/dev/serve-etm.py')+';.','--add-data',str(ROOT.parent/'etm/tools/web_cli_queue.py')+';.','--add-data',str(ROOT.parent/'docs/toolbox-cli-reference.json')+';docs','--paths',str(ROOT.parent),'--paths',str(snapshot),'--paths',str(snapshot/'tools'),'--paths',str(snapshot/'tools/la'),'--collect-all','capstone','--collect-all','elftools',
               # numpy 2.x 的 `numpy._core._exceptions` 等子模块不会被默认 hook 完整收进 PYZ，
               # 冻结后首次 import 就 ModuleNotFoundError → 前端静默回退纯 Python（审查 F02）。
               # 用 collect-submodules/binaries（不用 collect-all）：后者会把 numpy 测试数据也带进包。
               '--collect-binaries','numpy','--collect-binaries','pandas']
    from PyInstaller.utils.hooks import collect_submodules
    for package_name in ('numpy','pandas'):
        # Keep runtime modules and the official hooks' DLL/NumPy-2 handling.
        # Third-party tests are neither the acceleration runtime nor our tests.
        modules=collect_submodules(package_name,filter=lambda name:'tests' not in name.split('.'))
        print(f'{package_name}: {len(modules)} runtime submodules (tests excluded)',flush=True)
        for module in modules:command+=['--hidden-import',module]
    for name in ['server','werkzeug.middleware.dispatcher','html.parser','decode_trace','decode_stream','stream_store','trace_port','tpiu_demux','flow_analysis','record_store','source_export','breakpoint_resolver','kingst_la','kingst_record','timeline_index','native_index','fast_window',
                 'trace_store','trace_store_write','trace_store_adapter','trace_store_index','trace_store_nav',
                 'native_binary','native_facts_index','native_pipeline','static_dict',
                 'native_frontend','native_storebuild','native_export','native_selection','trace_frontend','concurrent.futures.thread',
                 'trace_cli','trace_cli_hardware','trace_cli_runtime','trace_cli_paths','trace_cli_legacy','web_cli_queue','serial','serial.tools.list_ports',
                 'reading_projection','reading_view','source_reading','flame_svg']:
        command+=['--hidden-import',name]
    # Keep numpy/pandas acceleration; their optional image/XML/crypto adapters
    # are unused here and bloat the portable Node/WebUSB CLI package.
    for name in ['tkinter','matplotlib','scipy','IPython','pytest','PyQt6','PIL','lxml','cryptography']:
        command+=['--exclude-module',name]
    command.append(str(ROOT/'bridge/etm/etm_bridge.py'))
    subprocess.run(command,cwd=ROOT,check=True)
    package=work/'dist'/args.app_name;toolbox=package/'toolbox';toolbox.mkdir()
    shutil.copyfile(ROOT/'index.html',toolbox/'index.html')
    public_files=public_app_files(ROOT)
    # A pre-commit build must also ship newly introduced bundle source modules.
    bundle_inputs=json.loads((ROOT/'app/site/manifest.json').read_text(encoding='utf8'))['inputs']
    if any(name.startswith('app/legacy/') for name in bundle_inputs):
        raise ValueError('生产 bundle 引用了旧版诊断模块')
    public_files+=list(bundle_inputs)
    public_files+=['docs/etm-integration.md','docs/etm-online-bridge.md']
    public_files+=tracked_samples(ROOT)
    for name in sorted(set(public_files)):
        original=ROOT/name
        if original.is_file() and original.suffix!='.map':
            destination=toolbox/name;destination.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(original,destination)
    # Force the validated bundle in portable mode; original module URLs still resolve for workers/assets.
    index=toolbox/'index.html';text=index.read_text(encoding='utf8').replace("window.__useWebBundle = mode === 'bundle' || (mode !== 'source' && !local);","window.__useWebBundle = mode !== 'source';")
    index.write_text(text,encoding='utf8')
    (package/'etm.config.json').write_text(json.dumps({'port':8878,'engine':'native'},indent=2)+'\n',encoding='utf8')
    (package/'start-etm-bridge.bat').write_text('@echo off\n"%~dp0'+args.app_name+'.exe" %*\nexit /b %errorlevel%\n',encoding='ascii')
    (package/'README.txt').write_text('superlinkPRO Windows x64 工作台\n完整解压后双击 '+args.app_name+'.exe；保留 runtime、toolbox、cli 目录。\nETM 沿用 KingstVIS Socket，更新前保留 data。\n',encoding='utf8')
    licenses=package/'licenses';licenses.mkdir();shutil.copyfile(snapshot/'vendor/OpenCSD-1.8.3/LICENSE',licenses/'OpenCSD-BSD-3-Clause.txt')
    shutil.copyfile(snapshot/'vendor/OpenCSD-1.8.3/NOTICES.txt',licenses/'OpenCSD-NOTICES.txt')
    shutil.copyfile(snapshot/'native/SQLITE-LICENSE.txt',licenses/'SQLite-PUBLIC-DOMAIN.txt')
    shutil.copytree(snapshot/'vendor/native-runtime-licenses',licenses/'Native-MinGW-runtime')
    python_license=Path(sys.base_prefix)/'LICENSE.txt'
    if python_license.exists():shutil.copyfile(python_license,licenses/'Python-LICENSE.txt')
    distributions={}
    for name in ['PyInstaller','Flask','Werkzeug','pyelftools','capstone','Jinja2','MarkupSafe','itsdangerous','click','blinker']:
        try:dist=importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            if name=='blinker':continue # Optional with the pinned Flask 2.2 release.
            raise
        distributions[name]=dist.version
        for member in dist.files or []:
            if any(key in member.name.lower() for key in ('license','copying','notice','copyright')):
                original=Path(dist.locate_file(member))
                if original.is_file():
                    destination=licenses/name/member.name;destination.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(original,destination)
    distributions['Node.js']=package_cli(package,toolbox,node,payload)
    # Keep the established offline Trace CLI regardless of the desktop EXE name.
    (package/'etm-trace.cmd').write_text('@echo off\n"%~dp0'+args.app_name+'.exe" --cli %*\nexit /b %errorlevel%\n',encoding='ascii')
    (package/'workbench-cli.cmd').write_text('@echo off\n"%~dp0'+args.app_name+'.exe" --workbench-cli %*\nexit /b %errorlevel%\n',encoding='ascii')
    shutil.copyfile(ROOT.parent/'docs/toolbox-cli-ai.md',package/'Workbench-CLI-中文说明.md')
    (package/'source-provenance.json').write_text(
        json.dumps({**provenance,'packagedHashes':packaged_hashes},ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    files=[dict(name=p.relative_to(package).as_posix(),bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(package.rglob('*')) if p.is_file()]
    identity=hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()
    revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    workspace_dirty=bool(subprocess.check_output(['git','status','--porcelain','--untracked-files=normal'],cwd=ROOT,text=True).strip())
    source_scope=['web/app','web/index.html','web/samples','web/tools/dev','web/bridge/etm/etm_bridge.py','web/bridge/etm/backend','web/bridge/etm/provenance.json','etm','toolbox.py','docs/toolbox-cli-ai.md','docs/toolbox-cli-reference.json','tools/build_workbench.py','tools/verify_workbench_package.py']
    dirty=bool(subprocess.check_output(['git','status','--porcelain','--untracked-files=normal','--',*source_scope],cwd=ROOT.parent,text=True).strip())
    inputs={name:sha(ROOT/name) for name in sorted(set(public_files+['index.html','tools/dev/serve-etm.py','tools/dev/build-etm-package.py','bridge/etm/etm_bridge.py','../toolbox.py','../docs/toolbox-cli-ai.md','../docs/toolbox-cli-reference.json','../etm/tools/web_cli_queue.py','../tools/build_workbench.py','../tools/verify_workbench_package.py'])) if (ROOT/name).is_file()}
    manifest=dict(version=args.version,platform='windows-x64',launcher=args.app_name+'.exe',archiveRoot='superlinkPRO' if args.app_name=='superlinkPRO' else 'etm-bridge-kit',packageId=identity,sourceRevision=revision,sourceDirty=dirty,workspaceDirty=workspace_dirty,sourceScope=source_scope,runtimeSourceRevision=provenance['sourceRevision'],sourceInputs=inputs,python=sys.version,dependencies=distributions,files=files)
    (package/'package-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    args.output.mkdir(parents=True,exist_ok=True);archive=args.output/(args.app_name+'-win64.zip')
    if archive.exists():raise FileExistsError('候选输出已存在：'+str(archive))
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as zip:
        for file in sorted(package.rglob('*')):
            if file.is_file():zip.write(file,manifest['archiveRoot']+'/'+file.relative_to(package).as_posix())
    if args.app_name=='etm-bridge' and archive.stat().st_size>=100*1024*1024:
        raise ValueError('完整包超过GitHub单文件100MiB限制；不能作为Pages下载提交')
    download=dict(version=args.version,platform='windows-x64',file=archive.name,bytes=archive.stat().st_size,sha256=sha(archive),packageId=identity,sourceRevision=revision,sourceDirty=dirty,runtimeSourceRevision=provenance['sourceRevision'])
    (args.output/'manifest.json').write_text(json.dumps(download,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps(dict(package=str(package),archive=str(archive),**download),ensure_ascii=False))
    return dict(package=package,archive=archive,manifest=args.output/'manifest.json')
if __name__=='__main__':main()
