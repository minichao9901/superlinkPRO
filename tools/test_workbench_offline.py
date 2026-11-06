"""Run Web logic tests without CDP, browser launch, USB or target access."""
import argparse,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--with-c-models',action='store_true');args=p.parse_args()
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    tests=[];separate=[]
    for path in sorted((ROOT/'web/tools/selftest').glob('*.test.mjs')):
        text=path.read_text(encoding='utf-8-sig')
        browser=any(s in text for s in ['cdp-lib.mjs','ensureBrowser(',"from 'playwright'",'new WebSocket(',"fetch(CDP",'9333','9345'])
        cmodel=path.name in ('analog-wire.test.mjs','bus-periodic.test.mjs')
        if browser or cmodel and not args.with_c_models:separate.append(dict(file=path.name,reason='browser/hardware harness' if browser else 'optional C compiler model'))
        else:tests.append(str(path.relative_to(ROOT/'web')))
    env=dict(os.environ,PROBE_FIRMWARE_REPO=str(ROOT),TEMP=str(out),TMP=str(out))
    start=time.monotonic()
    with (out/'tests.log').open('wb') as log:
        r=subprocess.run(['node','--test','--test-concurrency=4','--test-timeout=120000',*tests],cwd=ROOT/'web',env=env,stdout=log,stderr=subprocess.STDOUT,timeout=300)
    summary=dict(exitCode=r.returncode,files=len(tests),seconds=round(time.monotonic()-start,3),separate=separate,hardware='not accessed')
    (out/'result.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps(summary,ensure_ascii=False,indent=2));return r.returncode
if __name__=='__main__':raise SystemExit(main())
