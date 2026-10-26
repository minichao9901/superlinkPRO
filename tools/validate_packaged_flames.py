"""Validate frozen CLI/HTTP flame exports with only Windows on PATH; no hardware.

Uses temporary junctions to existing read-only records, not duplicate captures.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1]
def run(a):
    package=a.package.resolve();run=a.output.resolve();run.mkdir(parents=True,exist_ok=True)
    exe=package/'etm-bridge.exe';manifest=json.loads((package/'package-manifest.json').read_text(encoding='utf8'))
    for entry in manifest['files']:
        p=package/entry['name']
        assert p.stat().st_size==entry['bytes']
        assert hashlib.sha256(p.read_bytes()).hexdigest()==entry['sha256'],p
    data=run/'data';records=data/'records';records.mkdir(parents=True,exist_ok=True)
    env=dict(os.environ,PATH=os.environ['SystemRoot']+'/System32;'+os.environ['SystemRoot'],PYTHONNOUSERSITE='1',TEMP=str(run),TMP=str(run))
    for key in ('PYTHONPATH','PYTHONHOME','WEB_ETM_RECORDS'):env.pop(key,None)
    expected=json.loads((ROOT/'docs/validation/flame-svg-20261027.json').read_text(encoding='utf8'))
    links=[];process=None
    report=dict(version=manifest['version'],packageId=manifest['packageId'],sourceRevision=manifest['sourceRevision'],
                sourceDirty=manifest['sourceDirty'],manifestFilesChecked=len(manifest['files']),PATH=env['PATH'],hardwareAccessed=False,cases=[])
    try:
        for case in expected['cases']:
            name=case['record'];link=records/name
            assert not link.exists(),link
            # PowerShell cmdlet creates the named junction; values are positional
            # literals encoded as a script argument, never shell substitutions.
            def literal(p):return "'"+str(p).replace("'","''")+"'"
            subprocess.run(['powershell.exe','-NoProfile','-Command',
                'New-Item -ItemType Junction -Path '+literal(link)+' -Target '+literal(a.records.resolve()/name)+' | Out-Null'],check=True,capture_output=True)
            links.append(link)
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        base=f'http://127.0.0.1:{port}/'
        with (run/'server.log').open('wb') as log:
            process=subprocess.Popen([str(exe),'--no-browser','--port',str(port),'--data-dir',str(data)],env=env,cwd=run,
                stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            for attempt in range(100):
                if process.poll() is not None:raise RuntimeError('Frozen server exited; see server.log')
                try:
                    with urlopen(base+'etm-launcher.json',timeout=1) as r:info=json.load(r)
                    break
                except OSError:time.sleep(.2)
            else:raise RuntimeError('Frozen server startup timeout')
            assert info['version']==manifest['version'] and info['engine']=='native'
            with urlopen(base+'etm/api/capabilities') as r:cap=json.load(r)
            assert cap['flameSvg']['fullDepth'] and cap['flameSvg']['exactCallPaths']
            for case in expected['cases']:
                target=run/(case['record']+'.svg')
                args=['--cli','export','--record',str(a.records.resolve()/case['record']),'--format','flame-svg',
                      '--from-seq',str(case['fromSeq']),'--to-seq',str(case['toSeq']),'--out',str(target)]
                completed=subprocess.run([str(exe),*args],cwd=run,env=env,capture_output=True,timeout=120)
                assert completed.returncode==0,completed.stdout+completed.stderr
                outcome=json.loads(completed.stdout)
                assert outcome['ok'],outcome
                query='?fromSeq='+str(case['fromSeq'])+'&toSeq='+str(case['toSeq'])
                with urlopen(base+'etm/api/download/'+case['record']+'/flame-svg'+query,timeout=120) as r:
                    assert r.headers.get_content_type()=='image/svg+xml';web=r.read()
                actual=hashlib.sha256(web).hexdigest()
                assert actual==case['sha256'] and web==target.read_bytes(),case['record']
                report['cases'].append(dict(record=case['record'],depth=case['depth'],sha256=actual,sourceCliAndFrozenCliAndHTTPBytesEqual=True))
                print(case['record']+': frozen CLI / HTTP / source bytes identical',flush=True)
                target.unlink() # Only the four final examples remain in the project.
            report['server']=info;report['passed']=True
    finally:
        if process:
            process.terminate();process.wait(timeout=15)
        for link in links:
            # os.rmdir removes this junction itself, never traverses its target.
            os.rmdir(link)
    (run/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps(report,ensure_ascii=False),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--package',type=Path,required=True);p.add_argument('--records',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);run(p.parse_args())
