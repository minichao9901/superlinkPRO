"""Compare stock/pipelined OpenOCD on F103 SRAM without changing target clocks."""
import argparse,hashlib,json,random,re,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--exe',type=Path,required=True);ap.add_argument('--scripts',type=Path,required=True);ap.add_argument('--out',type=Path,required=True);ap.add_argument('--speeds',default='1000,10000,20000,30000,40000,45000,50000,60000');ap.add_argument('--rounds',type=int,default=3);ap.add_argument('--loops',type=int,default=20);a=ap.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False);size=20480;pattern=out/'pattern.bin';pattern.write_bytes(random.Random(20261107).randbytes(size));backup=out/'backup-private.bin';restored=out/'restored-private.bin'
    # verify_image may execute a checksum helper in the target work area and
    # overwrite the very SRAM being measured. Verify saved bytes on the host.
    lines=['stm32f1x.cpu configure -work-area-size 0','init','halt',f'dump_image {backup.as_posix()} 0x20000000 {size}','mdw 0x40021000 2','set bench_rc [catch {']
    for speed in map(int,a.speeds.split(',')):
        loops=min(a.loops,3) if speed<=1000 else a.loops
        lines += [f'adapter speed {speed}']
        for n in range(a.rounds):
            read=out/f'read-{speed}-{n}.bin';written=out/f'written-{speed}-{n}.bin'
            lines += [f'load_image {pattern.as_posix()} 0x20000000 bin','set t0 [clock milliseconds]',f'for {{set i 0}} {{$i < {loops}}} {{incr i}} {{dump_image {read.as_posix()} 0x20000000 {size}}}','set t1 [clock milliseconds]',f'for {{set i 0}} {{$i < {loops}}} {{incr i}} {{load_image {pattern.as_posix()} 0x20000000 bin}}','set t2 [clock milliseconds]',f'dump_image {written.as_posix()} 0x20000000 {size}',f'echo "BENCH speed={speed} round={n} loops={loops} read_ms=[expr {{$t1-$t0}}] write_ms=[expr {{$t2-$t1}}]"']
    lines += ['} bench_message]','adapter speed 1000','set restore_rc [catch {',f'load_image {backup.as_posix()} 0x20000000 bin',f'dump_image {restored.as_posix()} 0x20000000 {size}','} restore_message]','if {$restore_rc} {echo "RESTORE_FAILED $restore_message"} else {echo RESTORE_OK; resume}','if {$bench_rc} {echo "BENCH_FAILED $bench_message"}','shutdown']
    script=out/'bench.tcl';script.write_text('\n'.join(lines)+'\n',encoding='utf8')
    command=[str(a.exe.resolve()),'-s',str(a.scripts.resolve()),'-f',str(ROOT/'script_test/openocd_stm32f1_swd.cfg'),'-c','gdb port disabled','-c','tcl port disabled','-c','telnet port disabled','-f',str(script)]
    started=time.perf_counter()
    with (out/'openocd.log').open('wb') as log:p=subprocess.run(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,timeout=240,creationflags=subprocess.CREATE_NO_WINDOW)
    text=(out/'openocd.log').read_text(encoding='utf8',errors='replace');rows=[]
    for speed,n,loops,r,w in re.findall(r'BENCH speed=(\d+) round=(\d+) loops=(\d+) read_ms=(\d+) write_ms=(\d+)',text):
        speed,n,loops,r,w=map(int,(speed,n,loops,r,w));assert (out/f'read-{speed}-{n}.bin').read_bytes()==pattern.read_bytes() and (out/f'written-{speed}-{n}.bin').read_bytes()==pattern.read_bytes()
        rows.append(dict(requestedKHz=speed,round=n,loops=loops,bytesPerTransfer=size,readMs=r,writeMs=w,readMBps=size*loops/r/1000,writeMBps=size*loops/w/1000,byteExact=True))
    restored_ok=backup.exists() and restored.exists() and backup.read_bytes()==restored.read_bytes() and 'RESTORE_OK' in text
    passed=p.returncode==0 and 'BENCH_FAILED' not in text and restored_ok and len(rows)==len(a.speeds.split(','))*a.rounds
    result=dict(passed=passed,restored=restored_ok,rows=rows,exeSha256=hashlib.sha256(a.exe.read_bytes()).hexdigest(),seconds=time.perf_counter()-started,boundary='OpenOCD dump_image/load_image wall time including local file IO; target clock unchanged',failure=text[-3000:] if not passed else None)
    (out/'result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8');print(json.dumps(result,indent=2));return 0 if passed else 1
if __name__=='__main__':raise SystemExit(main())
