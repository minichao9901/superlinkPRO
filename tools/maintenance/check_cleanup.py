"""Read-only reconciliation of an applied cleanup plan and deletion log."""
import argparse,hashlib,json,os,subprocess,time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,default=ROOT/'docs/maintenance/storage-cleanup.plan.json')
    parser.add_argument('--log',type=Path)
    parser.add_argument('--out',type=Path,default=ROOT/'docs/maintenance/cleanup-check.json')
    args=parser.parse_args();root=ROOT.resolve();plan=json.loads(args.plan.read_text(encoding='utf-8-sig'))
    assert hashlib.sha256(args.plan.read_bytes()).hexdigest()==args.plan.with_suffix(args.plan.suffix+'.sha256').read_text().strip()
    log=args.log or max(args.plan.parent.glob('cleanup-applied-*.jsonl'),key=lambda p:p.stat().st_mtime_ns)
    expected={r[0]:r for r in plan['files']};deleted={};duplicates=[]
    with log.open(encoding='utf-8-sig') as stream:
        for line in stream:
            row=json.loads(line);name=row['path']
            if name in deleted:duplicates.append(name)
            assert name in expected and row['bytes']==expected[name][1],name
            deleted[name]=row
    surviving=[];recreated=[]
    for name,size,mtime in expected.values():
        p=root/name
        if p.exists():
            stat=p.stat()
            row=dict(path=name,bytes=stat.st_size,mtimeNs=stat.st_mtime_ns)
            (surviving if stat.st_size==size and stat.st_mtime_ns==mtime else recreated).append(row)
    git_deleted=subprocess.check_output(['git','ls-files','--deleted','-z'],cwd=root).decode('utf8').split('\0')
    git_deleted=[p for p in git_deleted if p]
    protected={name:(root/name).exists() for name in
        plan['summary']['protectedPrefixes']+plan['summary']['protectedSyntheticSegments']}
    manifest=json.loads((root/'web/bridge/etm/downloads/manifest.json').read_text(encoding='utf8'))
    package=root/'web/bridge/etm/downloads'/manifest['file']
    package_ok=package.stat().st_size==manifest['bytes'] and hashlib.sha256(package.read_bytes()).hexdigest()==manifest['sha256']
    key_documents=['docs/trace-cli-ai.md','docs/trace-reading.md','docs/etm-reading-release-2026-10-24.md',
                   'docs/etm-native-engine.md','etm/docs/trace-store-v1.md','etm/firmware/stm32h743_cdc_trace/README.md']
    key_ok={p:(root/p).is_file() for p in key_documents}
    result=dict(passed=not duplicates and not surviving and not recreated and not git_deleted and
        expected.keys()==deleted.keys() and all(protected.values()) and package_ok and all(key_ok.values()),
        checkedAt=time.strftime('%Y-%m-%dT%H:%M:%S'),log=str(log),plannedFiles=len(expected),loggedDeletions=len(deleted),
        loggedApparentBytes=sum(r['bytes'] for r in deleted.values()),unloggedPaths=sorted(expected.keys()-deleted.keys()),
        duplicateLogPaths=duplicates,unchangedSurvivors=surviving,recreatedPaths=recreated,
        trackedFilesMissing=git_deleted,protectedAssetsExist=protected,keyDocumentsExist=key_ok,
        officialPackage=dict(version=manifest['version'],bytes=manifest['bytes'],sha256=manifest['sha256'],verified=package_ok))
    args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
