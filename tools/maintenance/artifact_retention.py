"""Create an explicit, read-only cleanup plan. This program never deletes files.

The PowerShell companion defaults to preview; applying the reviewed plan is a
separate local action. Published payloads, tracked files, Git checkouts, original
etm/records captures and regression fixtures are always excluded.
"""
import argparse,collections,hashlib,json,os,re,subprocess,time
from pathlib import Path,PurePosixPath

ROOT=Path(__file__).resolve().parents[2]
PROTECTED=(
    'docs/validation/20261018-ds-review-bb2f8ee-6ff02745/data/records/capture-be1e78730b7f486a9e27d5911cd4a434',
    'docs/validation/20261018-ds-recheck-0cd4229-a7b4f912/live-data/records/capture-36431f7c7567446e8bcd6229094d1b28',
    'docs/validation/20261020-codex-ds-next-1685c01-6e93c42a/binding-probe/native.bin',
    'tmp/perf-baseline',
    'tmp/r2-bench/runs/1s/ref1/store',
)
SCRIPTS={'.py','.ps1','.js','.mjs','.cjs','.sh','.bat','.cmd','.tcl','.cmake'}
CLONES={'runtime','node_modules','vendor','compiler-runtime','toolbox','backend','opt3-code','opt4-code',
        'snapshot','web-etm','app','baseline','baseline-copy','source-v2','final-source','opt-code',
        '独立 CLI 验收','最终 CLI 验收'}

def below(path,prefix):return path==prefix or path.startswith(prefix+'/')

def in_scope(path):
    p=PurePosixPath(path);parts=p.parts
    if not parts:return False
    if parts[0]=='tmp' or below(path,'web/tmp') or below(path,'web/validation'):return True
    if re.match(r'^\.offline-reading-review-[\w-]+$',parts[0]):return True
    if any(parts[i:i+2]==('docs','validation') for i in range(len(parts)-1)):return True
    return len(parts)>=3 and parts[:2]==('etm','records') and (parts[2].startswith(('_p4_','_r0_','_r1_')) or parts[2]=='.p4_test_cache')

def copied_payload(path):
    parts=PurePosixPath(path).parts
    return any(p in CLONES or p.startswith(('独立 桥包-','etm-package-','portable-native','portable-accepted',
               'portable-ownership','portable-validation')) for p in parts)

def decision(path,size,tracked=False,extra_protected=()):
    if tracked:return 'keep:tracked'
    if not in_scope(path):return 'keep:outside-generated-scope'
    if any(below(path,p) for p in (*PROTECTED,*extra_protected)):return 'keep:regression-or-git-baseline'
    parts=PurePosixPath(path).parts;name=parts[-1];ext=PurePosixPath(name).suffix.lower()
    if any(p in ('.git','.hardware-review.lock') for p in parts):return 'keep:git-or-lease'
    if copied_payload(path):return 'delete:copied-runtime-or-source'
    if parts[:2]==('etm','records'):return 'delete:regenerable-test-cache'
    if ext=='.md':return 'keep:report'
    if ext in SCRIPTS:return 'keep:script'
    if ext in ('.json','.log','.txt','.csv','.tsv') and size<=512*1024:return 'keep:compact-evidence'
    if ext in ('.c','.cpp','.h','.hpp') and size<=512*1024 and not re.search(r'execution|simplified|export|reading|source',name,re.I):
        return 'keep:small-experimental-source'
    if ext in ('.png','.jpg','.jpeg','.svg') and size<=1024*1024:return 'keep:key-screenshot'
    return 'delete:generated-output'

def scan(root,tracked):
    # Find project scopes without traversing dependencies outside a selected tree.
    for current,dirs,files in os.walk(root,followlinks=False):
        base=Path(current);rel=base.relative_to(root).as_posix()
        if (base/'.git').exists() and base!=root:
            dirs[:]=[];continue
        if not in_scope(rel):
            dirs[:]=[d for d in dirs if d not in ('.git','node_modules','vendor','.agent-teams')]
        else:dirs[:]=[d for d in dirs if d!='.git']
        dirs[:]=[d for d in dirs if not (base/d).is_symlink() and not (base/d).is_junction()]
        if not in_scope(rel):continue
        for name in files:
            p=base/name;relative=p.relative_to(root).as_posix()
            if p.is_symlink():continue
            yield relative,p.stat(follow_symlinks=False)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--out',type=Path,default=ROOT/'docs/maintenance/storage-cleanup.plan.json')
    args=parser.parse_args();root=args.root.resolve();began=time.perf_counter()
    tracked=set(subprocess.check_output(['git','ls-files','-z'],cwd=root).decode('utf8').split('\0'))
    # Native frontend regression currently samples first/middle/last physical
    # synthetic segment. Preserve those rather than falsely skipping the test.
    segments=sorted((root/'tmp/p6a-1s/physical/segments').glob('segment-*.kvdat'))
    extra=tuple(p.relative_to(root).as_posix() for p in
                ([segments[0],segments[len(segments)//2],segments[-1]] if segments else []))
    files=[];kept=collections.Counter();reasons=collections.Counter();groups=collections.Counter();inodes={};bytes_seen=0
    for name,stat in scan(root,tracked):
        size=stat.st_size;bytes_seen+=size;why=decision(name,size,name in tracked,extra)
        if why.startswith('keep:'):kept[why]+=size;continue
        files.append([name,size,stat.st_mtime_ns]);reasons[why]+=size
        parts=PurePosixPath(name).parts
        group='/'.join(parts[:3] if parts[:2]==('docs','validation') else parts[:2]);groups[group]+=size
        key=(stat.st_dev,stat.st_ino)
        value=inodes.setdefault(key,[size,stat.st_nlink,0]);value[2]+=1
    total=sum(r[1] for r in files)
    summary=dict(schema='repo-artifact-cleanup/1',root=str(root),created=time.strftime('%Y-%m-%dT%H:%M:%S'),
        revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
        plannedFiles=len(files),plannedApparentBytes=total,scannedApparentBytes=bytes_seen,
        allLinksSelectedApparentBytes=sum(v[0] for v in inodes.values() if v[2]==v[1]),
        bytesByReason=dict(reasons),keptBytesByReason=dict(kept),protectedPrefixes=list(PROTECTED),
        protectedSyntheticSegments=list(extra),
        largestGroups=[dict(path=p,bytes=b) for p,b in groups.most_common(25)],
        note='Read-only plan. Hardlinks/compression mean these figures are not guaranteed physical free space. No files deleted.',
        seconds=time.perf_counter()-began)
    result=dict(summary=summary,files=files)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,ensure_ascii=False,separators=(',',':')),encoding='utf8')
    digest=hashlib.sha256(args.out.read_bytes()).hexdigest()
    args.out.with_suffix(args.out.suffix+'.sha256').write_text(digest+'\n',encoding='ascii')
    args.out.with_name('storage-cleanup-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(dict(files=len(files),apparentGiB=round(total/2**30,3),
        allLinksSelectedGiB=round(summary['allLinksSelectedApparentBytes']/2**30,3),
        plan=str(args.out),sha256=digest),ensure_ascii=False))

if __name__=='__main__':main()
