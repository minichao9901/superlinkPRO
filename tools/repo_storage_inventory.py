"""Read-only inventory of project docs/tmp trees; never follows directory links.

python tools/repo_storage_inventory.py --out docs/maintenance/storage-before.json
Apparent sizes count each directory entry; hard-linked bytes are also reported
once for the selected trees. Neither figure promises the same freed disk bytes.
"""
import argparse,collections,json,os,subprocess,time
from pathlib import Path

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();root=args.root.resolve();began=time.perf_counter()
    tracked=set(subprocess.check_output(['git','ls-files','-z'],cwd=root).decode('utf8').split('\0'))
    skip={'.git','node_modules','vendor','.agent-teams'}
    totals=collections.defaultdict(lambda:dict(bytes=0,files=0,trackedBytes=0,trackedFiles=0))
    extensions=collections.defaultdict(lambda:dict(bytes=0,files=0))
    roots=set();largest=[];seen=set();unique=0;errors=[];links=[]
    for current,dirs,files in os.walk(root,followlinks=False):
        base=Path(current);relative=base.relative_to(root);parts=relative.parts
        selected=next((i for i,p in enumerate(parts) if p in ('docs','tmp')),None)
        if selected is None:
            dirs[:]=[d for d in dirs if d not in skip]
        else:dirs[:]=[d for d in dirs if d!='.git']
        for d in dirs[:]:
            p=base/d
            if p.is_symlink() or p.is_junction():dirs.remove(d);links.append(p.relative_to(root).as_posix())
        if selected is None and parts and parts[0].startswith('.offline-reading-review-'):
            selected=0
        if selected is None:continue
        prefix=Path(*parts[:selected+1]).as_posix();roots.add(prefix)
        for name in files:
            path=base/name;rel=path.relative_to(root).as_posix()
            try:stat=path.stat(follow_symlinks=False)
            except OSError as error:errors.append(dict(path=rel,error=str(error)));continue
            size=stat.st_size;key=(stat.st_dev,stat.st_ino)
            if key not in seen:seen.add(key);unique+=size
            for depth in range(selected+1,len(parts)+1):
                d=Path(*parts[:depth]).as_posix();row=totals[d];row['bytes']+=size;row['files']+=1
                if rel in tracked:row['trackedBytes']+=size;row['trackedFiles']+=1
            ext=path.suffix.lower() or '<none>';extensions[ext]['bytes']+=size;extensions[ext]['files']+=1
            largest.append((size,rel,rel in tracked))
    report=dict(root=str(root),seconds=time.perf_counter()-began,
        roots={p:totals[p] for p in sorted(roots)},
        directories=[dict(path=p,**r) for p,r in sorted(totals.items(),key=lambda x:-x[1]['bytes']) if r['bytes']>=10*1024*1024],
        largestFiles=[dict(path=p,bytes=s,tracked=t) for s,p,t in sorted(largest,reverse=True)[:100]],
        extensions=dict(sorted(extensions.items(),key=lambda x:-x[1]['bytes'])),
        uniqueApparentBytes=unique,errors=errors,skippedDirectoryLinks=links)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    for p,r in report['roots'].items():print(f"{r['bytes']/2**30:9.3f} GiB  {r['files']:7} files  {p}")
    print('unique apparent GiB',round(unique/2**30,3),'errors',len(errors),'seconds',round(report['seconds'],2))

if __name__=='__main__':main()
