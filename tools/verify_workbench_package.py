"""Verify frozen workbench HTTP, native re-decode, CLI and download without hardware."""
import argparse,hashlib,json,os,socket,subprocess,sys,time,zipfile
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.parse import quote
ROOT=Path(__file__).resolve().parents[1]
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--package',type=Path,default=ROOT/'dist/superlinkPRO')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--port',type=int,default=0)
    p.add_argument('--keep-running',action='store_true')
    args=p.parse_args();home=args.package.resolve();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    manifest=json.loads((home/'package-manifest.json').read_text(encoding='utf8'))
    result={k:manifest[k] for k in ['version','sourceRevision','sourceDirty','runtimeSourceRevision','packageId']}
    result.update(files=len(manifest['files']),hardware='not accessed')
    for item in manifest['files']:
        file=(home/item['name']).resolve()
        assert file.is_relative_to(home) and file.stat().st_size==item['bytes'] and sha(file)==item['sha256'],item['name']
    result['allPackageFilesMatch']=True
    for name,expected in manifest['sourceInputs'].items():assert sha(ROOT/'web'/name)==expected,name
    result['sourceInputsMatch']=True
    assert (home/'toolbox/samples').is_dir() and not (home/'cli/web/samples').exists()
    result['publicSamplesSingleCopy']=True
    assert not (home/'cli/web/app/site').exists();result['guiBundleSingleCopy']=True
    samples=[p[4:] for p in subprocess.check_output(['git','ls-files','-z','--','web/samples'],cwd=ROOT).decode('utf8').split('\0') if p and Path(p).suffix!='.map']
    for name in samples:assert (home/'toolbox'/name).read_bytes()==(ROOT/'web'/name).read_bytes(),name
    result['publicSampleFilesMatchSource']=len(samples)
    distribution=home.parent/'manifest.json';archive=home.parent/'superlinkPRO-win64.zip'
    if distribution.exists() and archive.exists():
        info=json.loads(distribution.read_text(encoding='utf8'));assert sha(archive)==info['sha256'] and archive.stat().st_size==info['bytes']
        with zipfile.ZipFile(archive) as z:
            names=z.namelist();assert not any('/data/' in n or '/app/gen/' in n or n.endswith('/rtt-bridge.mjs') for n in names)
            assert len(names)==len(manifest['files'])+1
        result['distribution']=info;result['zipExcludesRetiredCodeAndUserData']=True
    port=args.port
    if not port:
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    base=f'http://127.0.0.1:{port}/';env=dict(os.environ,PATH=os.environ['SystemRoot']+';'+os.environ['SystemRoot']+'/System32',PYTHONNOUSERSITE='1')
    for name in ['PYTHONHOME','PYTHONPATH','WEB_ETM_RECORDS']:env.pop(name,None)
    # The HTTP workbench itself chooses data/runtime-tmp. The standalone CLI
    # processes in this verification get an explicitly scoped child TEMP.
    cli_temp=out/'cli-tmp';cli_temp.mkdir();cli_env=dict(env,TEMP=str(cli_temp),TMP=str(cli_temp))
    exe=home/'superlinkPRO.exe';log=(out/'server.log').open('w',encoding='utf8');process=None
    def api(path,data=None):
        req=Request(base+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
        with urlopen(req,timeout=30) as response:return json.load(response)
    try:
        process=subprocess.Popen([str(exe),'--no-browser','--port',str(port),'--data-dir',str(out/'data')],cwd=out,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        for _ in range(100):
            if process.poll() is not None:raise RuntimeError('冻结服务提前退出，见server.log')
            try:result['launcher']=api('etm-launcher.json');break
            except OSError:time.sleep(.2)
        else:raise TimeoutError('冻结服务启动超时')
        records=api('etm/api/records');assert records;sid=records[0]['id']
        job=api('etm/api/decode/'+sid,{})['job']
        for _ in range(300):
            state=api('etm/api/jobs/'+job)
            if state['state'] in ('done','error'):break
            time.sleep(.2)
        assert state['state']=='done',state
        result['frozenDemoRedecode']=True;result['demoSummary']=api('etm/api/browse/'+sid+'/overview')['summary']
        record=out/'data/records'/sid
        for kind,arguments in [('query',['query','--record',str(record),'--view','summary','--compact']),('export',['export','--record',str(record),'--format','flame-svg','--from-seq','1','--to-seq','500','--out',str(out/'flame.svg')])]:
            r=subprocess.run([str(exe),'--cli',*arguments],cwd=out,env=cli_env,capture_output=True,text=True,encoding='utf8',errors='replace',timeout=120)
            (out/(kind+'.log')).write_text(r.stdout+r.stderr,encoding='utf8');assert r.returncode==0,r.stdout+r.stderr;result[kind]=True
        info=api('downloads/manifest.json');digest=hashlib.sha256();received=0
        with urlopen(base+'downloads/superlinkPRO-win64.zip',timeout=60) as response:
            while block:=response.read(1048576):digest.update(block);received+=len(block)
        assert info['bytes']==received and info['sha256']==digest.hexdigest();result['httpDownload']=info
        # Exercise the frozen CLI host's shared sample root without connecting
        # a probe, granting an image directory, or running hardware commands.
        with socket.socket() as s:s.bind(('127.0.0.1',0));cli_port=s.getsockname()[1]
        cli_base=f'http://127.0.0.1:{cli_port}/';cli_runtime=out/'native-readiness';cli_started=False
        try:
            started=subprocess.run([str(exe),'--cli','--web-url',cli_base,'web','start','--runtime-dir',str(cli_runtime)],cwd=out,env=cli_env,capture_output=True,text=True,encoding='utf8',errors='replace',timeout=60)
            (out/'cli-host-start-private.log').write_text(started.stdout+started.stderr,encoding='utf8')
            if started.returncode:raise RuntimeError('Frozen CLI readiness failed; see private log')
            value=json.loads(started.stdout);assert value['ok'] and value['result']['status']=='ready';cli_started=True
            checked=[]
            for sample in ['samples/test_images/gradient.bmp','samples/anim/花园.mp4','app/site/main.js']:
                expected=(home/'toolbox'/sample).read_bytes()
                with urlopen(cli_base+quote(sample),timeout=15) as response:actual=response.read()
                assert actual==expected;checked.append({'sample':sample,'bytes':len(actual),'sha256':hashlib.sha256(actual).hexdigest()})
            result['frozenCliSharedSamples']={'ready':True,'samples':checked,'matchesPublicCopy':True,'hardware':'not accessed'}
        finally:
            if cli_started:
                original_error=sys.exc_info()[1]
                try:
                    stopped=subprocess.run([str(exe),'--cli','web','stop','--runtime-dir',str(cli_runtime)],cwd=out,env=cli_env,capture_output=True,text=True,encoding='utf8',errors='replace',timeout=45)
                    (out/'cli-host-stop-private.log').write_text(stopped.stdout+stopped.stderr,encoding='utf8')
                    if stopped.returncode:raise RuntimeError('Frozen CLI stop failed; retain runtime and inspect private log')
                    assert json.loads(stopped.stdout)['result']['status']=='stopped'
                    result.setdefault('frozenCliSharedSamples',{})['stopped']=True
                except Exception as error:
                    result['frozenCliCleanupError']=str(error)
                    if original_error is None:raise
        result['passed']=True
        if args.keep_running:result.update(pid=process.pid,url=base)
    finally:
        if process and (not args.keep_running or not result.get('passed')):
            process.terminate();process.wait(timeout=15)
        log.close();(out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0
if __name__=='__main__':raise SystemExit(main())
