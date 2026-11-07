"""Build, verify and install the portable superlinkPRO workbench in dist/."""
import argparse, hashlib, importlib.util, json, os, shutil, subprocess, sys, uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
VERSION='1.1.0-web.4'
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def prepare_desktop_launcher(path):
    # A Low MIC label inherited from a build directory also lowers browser
    # children. Apply the normal desktop label only to our delivered EXE.
    if os.name=='nt':
        r=subprocess.run([str(Path(os.environ['SystemRoot'])/'System32/icacls.exe'),str(path),'/setintegritylevel','M'],capture_output=True)
        if r.returncode:raise ValueError('无法设置工作台启动器的标准桌面完整性标签：'+r.stderr.decode('utf8','replace'))

def verify(package):
    manifest=json.loads((package/'package-manifest.json').read_text(encoding='utf8'))
    for item in manifest['files']:
        path=(package/item['name']).resolve()
        if not path.is_relative_to(package.resolve()) or item['name'].split('/')[0]=='data':raise ValueError('包清单包含越界路径或用户数据')
        if not path.is_file() or path.stat().st_size!=item['bytes'] or sha(path)!=item['sha256']:raise ValueError('包文件校验失败：'+item['name'])
    return manifest

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,default=ROOT/'dist/superlinkPRO',help='安装目录；已有程序先归档，data保持原样')
    p.add_argument('--candidate-only',action='store_true',help='只构建验收候选，不替换dist')
    args=p.parse_args(argv)
    if os.name!='nt':raise ValueError('请使用 Windows x64 Python')
    work=ROOT/'tmp'/('workbench-package-'+uuid.uuid4().hex[:8]);work.mkdir(parents=True)
    # Scope TEMP/TMP to this build and its children; never modify system settings.
    temporary=work/'temp';temporary.mkdir();os.environ.update(TEMP=str(temporary),TMP=str(temporary))
    subprocess.run(['node','web/tools/dev/build-web.mjs'],cwd=ROOT,check=True)
    spec=importlib.util.spec_from_file_location('build_etm_package',ROOT/'web/tools/dev/build-etm-package.py')
    build=importlib.util.module_from_spec(spec);spec.loader.exec_module(build)
    result=build.main(['--output',str(work/'download'),'--app-name','superlinkPRO','--version',VERSION])
    package=result['package'];manifest=verify(package)
    prepare_desktop_launcher(package/'superlinkPRO.exe')
    checkdata=work/'check-data'
    env=dict(os.environ,PATH=os.environ['SystemRoot']+';'+os.environ['SystemRoot']+'/System32',PYTHONNOUSERSITE='1')
    for name in ['PYTHONHOME','PYTHONPATH','WEB_ETM_RECORDS']:env.pop(name,None)
    checked=subprocess.run([str(package/'superlinkPRO.exe'),'--check','--no-browser','--data-dir',str(checkdata)],cwd=work,env=env,capture_output=True,text=True,encoding='utf8',errors='replace',timeout=120)
    (work/'frozen-check.log').write_text(checked.stdout+checked.stderr,encoding='utf8')
    if checked.returncode:raise RuntimeError('冻结EXE预检失败：'+str(work/'frozen-check.log'))
    subprocess.run([sys.executable,'-X','utf8',str(ROOT/'tools/verify_workbench_package.py'),
                    '--package',str(package),'--output',str(work/'frozen-verification')],cwd=ROOT,check=True,timeout=240)
    evidence=dict(version=VERSION,sourceRevision=manifest['sourceRevision'],sourceDirty=manifest['sourceDirty'],files=len(manifest['files']),candidate=str(package),frozenCheck=True,hardwareTest='not-run')
    if not args.candidate_only:
        out=args.out.resolve();out.parent.mkdir(parents=True,exist_ok=True)
        if out==ROOT.resolve() or out.is_relative_to(ROOT/'web') or ROOT.resolve().is_relative_to(out):raise ValueError('安装目录不能覆盖源码或仓库祖先')
        previous=work/'previous';had_old=out.exists();data_moved=False;installed=False
        if had_old:
            if not (out/'package-manifest.json').is_file():raise ValueError('已有目录不是已知工作台，拒绝替换：'+str(out))
            out.rename(previous)
        try:
            shutil.copytree(package,out);installed=True;verify(out)
            prepare_desktop_launcher(out/'superlinkPRO.exe')
            if had_old and (previous/'data').exists():
                (previous/'data').rename(out/'data');data_moved=True
            for source,name in [(result['archive'],'superlinkPRO-win64.zip'),(result['manifest'],'manifest.json')]:
                destination=out.parent/name
                if destination.exists():shutil.copyfile(destination,work/('previous-'+name))
                staging=out.parent/(name+'.'+uuid.uuid4().hex+'.partial');shutil.copyfile(source,staging);staging.replace(destination)
        except BaseException:
            if data_moved:(out/'data').rename(previous/'data')
            if installed:out.rename(work/'failed-install')
            if had_old:previous.rename(out)
            for name in ['superlinkPRO-win64.zip','manifest.json']:
                backup=work/('previous-'+name)
                if backup.exists():shutil.copyfile(backup,out.parent/name)
            raise
        evidence.update(installed=str(out),exeSha256=sha(out/'superlinkPRO.exe'),archive=str(out.parent/'superlinkPRO-win64.zip'),dataPreserved=data_moved,previous=str(previous) if had_old else None)
    (work/'result.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps(evidence,ensure_ascii=False,indent=2));return 0

if __name__=='__main__':raise SystemExit(main())
