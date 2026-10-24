"""Offline portable acceptance only; does not start a service or enumerate USB."""
from pathlib import Path
import hashlib,json,os,subprocess,sys,zipfile

run=Path(__file__).resolve().parent
data=Path('C:/Users/Administrator/.codex/validation')/run.name
release=data/'package-final'
download=json.loads((release/'manifest.json').read_text(encoding='utf8'))
archive=release/download['file']
assert archive.stat().st_size==download['bytes']
assert hashlib.sha256(archive.read_bytes()).hexdigest()==download['sha256']
destination=data/'portable-中文 路径';destination.mkdir()
with zipfile.ZipFile(archive) as z:z.extractall(destination)
home=destination/'etm-bridge-kit'
manifest=json.loads((home/'package-manifest.json').read_text(encoding='utf8'))
for item in manifest['files']:
    p=home/item['name']
    assert p.stat().st_size==item['bytes'],item['name']
    assert hashlib.sha256(p.read_bytes()).hexdigest()==item['sha256'],item['name']
assert manifest['sourceRevision']==download['sourceRevision']
assert manifest['sourceDirty'] is False
assert json.loads((home/'source-provenance.json').read_text(encoding='utf8'))['sourceRevision']==manifest['sourceRevision']
env=dict(os.environ,PATH=str(Path(os.environ['SystemRoot'])/'System32')+';'+os.environ['SystemRoot'],PYTHONNOUSERSITE='1')
for key in ('PYTHONPATH','PYTHONHOME','WEB_ETM_RECORDS'):env.pop(key,None)
checks=[]
def call(name,command,cwd=home):
    p=subprocess.run(command,cwd=cwd,env=env,capture_output=True,encoding='utf8',errors='replace',timeout=45)
    (run/(name+'.stdout.log')).write_text(p.stdout,encoding='utf8')
    (run/(name+'.stderr.log')).write_text(p.stderr,encoding='utf8')
    checks.append(dict(name=name,command=command,exitCode=p.returncode))
    assert p.returncode==0,(name,p.stderr,p.stdout)
    return p.stdout
exe=str(home/'etm-bridge.exe')
doctor=json.loads(call('portable-doctor',[exe,'--cli','doctor']))
assert doctor['ok'] and doctor['result']['hardware']=='not-probed'
assert Path(doctor['result']['node']).is_relative_to(home)
call('portable-cli-help',[exe,'--cli','--help'])
call('portable-bridge-check',[exe,'--check','--data-dir',str(data/'bridge-check-data')])
cmd=json.loads(call('portable-cmd',[str(home/'etm-trace.cmd'),'doctor']))
assert cmd['ok']
modules=['cli/web/app/dbg/cmd.js','cli/web/app/flash/view.js','cli/etm/web/app/target.js']
code="await import('usb'); await import('linkedom');\n"+'\n'.join('await import('+json.dumps((home/p).as_uri())+');' for p in modules)+"\nconsole.log('Imported Web debug/Flash/ETM and USB modules without opening hardware');"
call('portable-node-modules',[str(home/'cli/node/node.exe'),'--input-type=module','-e',code],home/'cli/etm/tools/node-transport')
env['TRACE_TEST_HOME']=str(home)
p=subprocess.run([sys.executable,'-X','utf8',str(run/'accept-portable-offline.py')],env=env,timeout=600)
assert p.returncode==0
result=dict(passed=True,home=str(home),download=download,verifiedFiles=len(manifest['files']),PATH=env['PATH'],checks=checks,hardware='not-run')
(run/'portable-validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
print(json.dumps(result,ensure_ascii=False,indent=2))
