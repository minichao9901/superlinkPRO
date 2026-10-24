"""Independently read the public release ZIP and verify every downloaded byte."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
import hashlib,json,re,time
import requests

run=Path(__file__).resolve().parent
data=Path('C:/Users/Administrator/.codex/validation')/run.name
receipt=json.loads((run/'release-receipt.json').read_text(encoding='utf8'))
expected=json.loads((data/'package-final/manifest.json').read_text(encoding='utf8'))
assets={item['name']:item['browser_download_url'] for item in receipt['assets']}
r=requests.get(assets['manifest.json'],timeout=(15,60));r.raise_for_status();assert r.json()==expected
(run/'public-manifest.json').write_text(json.dumps(r.json(),indent=2),encoding='utf8')
parts=data/'public-download-parts';parts.mkdir()
size=expected['bytes'];chunk=4*1024*1024;url=assets[expected['file']]
def get(start):
    end=min(size-1,start+chunk-1);began=time.perf_counter()
    response=requests.get(url,headers={'Range':f'bytes={start}-{end}'},timeout=(15,90));response.raise_for_status()
    payload=response.content;cr=response.headers.get('Content-Range')
    if cr:
        assert response.status_code==206
        assert cr==f'bytes {start}-{end}/{size}',cr
        assert len(payload)==end-start+1
    else:
        assert start==0 and len(payload)==size,'Server ignored Range without a complete ZIP'
    p=parts/('%09d.part'%start);p.write_bytes(payload)
    value=dict(start=start,end=start+len(payload)-1,bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest(),seconds=time.perf_counter()-began,status=response.status_code,contentRange=cr)
    print('Verified public part',start,'bytes',len(payload),flush=True)
    return value
rows=[get(0)]
if rows[0]['bytes']!=size:
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures=[pool.submit(get,start) for start in range(chunk,size,chunk)]
        for future in as_completed(futures):rows.append(future.result())
rows.sort(key=lambda x:x['start']);cursor=0;digest=hashlib.sha256();output=data/'public-etm-bridge-win64.zip'
with output.open('xb') as stream:
    for row in rows:
        assert row['start']==cursor
        payload=(parts/('%09d.part'%cursor)).read_bytes();stream.write(payload);digest.update(payload);cursor+=len(payload)
assert cursor==size and digest.hexdigest()==expected['sha256']
result=dict(passed=True,url=url,bytes=cursor,sha256=digest.hexdigest(),download=str(output),parts=rows)
(run/'public-download-verification.json').write_text(json.dumps(result,indent=2),encoding='utf8')
print('PUBLIC ZIP SHA256 VERIFIED',digest.hexdigest(),flush=True)
