"""封存已验收的 70/35 MHz 固件、原始记录、解码和源执行流；拒绝覆盖不同内容。"""
import gzip,hashlib,json,shutil,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import server

def write_once(path,data):
    if path.exists():
        if path.read_bytes()!=data:raise ValueError(f'封存文件已存在且不同：{path}')
        return
    with path.open('xb') as stream:stream.write(data)

def main():
    report=json.loads((ROOT/'docs/validation/2026-10-11-spi-70-35-baseline.json').read_text(encoding='utf8'))
    assert report['restored'] and report['running'] and not report.get('error') and not report.get('restoreError')
    short,long=[c['id'] for c in report['captures']]
    assert [c['seconds'] for c in report['captures']]==[.001,.01]
    bundle=ROOT/'baselines/spi-70-35-v1';bundle.mkdir(parents=True,exist_ok=True)
    firmware=ROOT/'firmware/stm32h7b0_spi_trace/stable-70-35-v1';firmware.mkdir(exist_ok=True)
    for extension in ('elf','bin','hex'):
        write_once(firmware/f'fw.{extension}',(firmware.parent/f'fw-70.{extension}').read_bytes())
    frozen_sha=hashlib.sha256((firmware/'fw.elf').read_bytes()).hexdigest()
    assert frozen_sha==report['elfSha']
    flash=json.loads((ROOT/'docs/validation/2026-10-11-spi-flash-70.json').read_text())
    assert flash['verified'] and hashlib.sha256((firmware/'fw.bin').read_bytes()).hexdigest()==flash['sha256']
    for sid,ms in [(short,1),(long,10)]:
        metadata=server.meta(sid);metadata.update(name=f'稳定基准 70/35 v1 · {ms} ms · 全地址业务窗口',baseline='spi-70-35-v1')
        server.save(sid,metadata)
    r=server.result(long);v=r['spiValidation'];q=r['quality'];clock=r['targetSnapshot']['clocks']
    assert clock['coreHz']==70000000 and clock['physicalTraceHz']==35000000
    assert r['status']['lastErrorCode']==0 and q['processedBytes']==q['accountedBytes']
    assert all(q[k]==0 for k in ('overflowPackets','packetIssues','overlapBytes','unaccountedBytes','unknownSourceBytes','unsynchronizedBytes'))
    assert v['closedSpiCalls']>0 and v['bytesPerClosedTransfer']==100
    assert not r['samplingValidation']['sensitive']
    for fraction in (.1,.2,.25,.3,.4):
        p=next(p for p in r['samplingValidation']['phases'] if p['fraction']==fraction)
        assert p.get('sha')==hashlib.sha256((server.directory(long)/'etm.bin').read_bytes()).hexdigest()
    raw_record=bundle/'capture-10ms.etmrecord'
    if not raw_record.exists():
        with zipfile.ZipFile(raw_record,'x',zipfile.ZIP_DEFLATED) as archive:
            for name in ('session.json','capture.csv','capture.kvdat','firmware.elf','sources.json'):
                archive.write(server.directory(long)/name,name)
    decoded=bundle/'decoded.json.gz'
    if not decoded.exists():
        with decoded.open('xb') as dest,gzip.GzipFile(fileobj=dest,mode='wb',mtime=0) as compressed:
            with (server.directory(long)/'decoded.json').open('rb') as source:shutil.copyfileobj(source,compressed)
    write_once(bundle/'execution.c',server.export_source(r).encode('utf8'))
    write_once(bundle/'execution-simplified.c',server.export_source(r,True).encode('utf8'))
    write_once(bundle/'validation.json',json.dumps(dict(summary=r['summary'],quality=q,spiValidation=v,
               samplingValidation=r['samplingValidation'],targetSnapshot=r['targetSnapshot'],laFilter=r['laFilter'],
               captureScope='采集窗口内全部地址的业务指令 trace；启动过程未包含',
               limits='边界保留；条件状态未知不算已证实执行；未证明任意程序绝对无损'),ensure_ascii=False,indent=2).encode('utf8'))
    artifacts={str(p.relative_to(ROOT)):dict(bytes=p.stat().st_size,sha256=hashlib.sha256(p.read_bytes()).hexdigest())
               for p in [*firmware.glob('fw.*'),raw_record,decoded,bundle/'execution.c',bundle/'execution-simplified.c',bundle/'validation.json']}
    manifest=dict(version=1,id='spi-70-35-v1',profile='spi-70',record=long,previewRecord=short,
                  frequencyOptimization='paused by user; no scheduled follow-up',
                  firmware=str((firmware/'fw.elf').relative_to(ROOT)),parameters=dict(cpuHz=70000000,traceHz=35000000,
                  hseHz=25000000,supply='LDO',rate=500000000,seconds=.01,threshold=1.65,fraction=.25,
                  channels=[0,1,2,3,4],pinSpeed=3,traceid=2,config=1),
                  artifacts=artifacts,summary=r['summary'],quality=q,spiValidation=v)
    write_once(bundle/'manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf8'))
    print(json.dumps(dict(bundle=str(bundle),record=long,instructions=r['summary']['instructions'],files=artifacts),ensure_ascii=False))
if __name__=='__main__':main()
