"""Publish the checksum-verified dist archive once; refuse mismatched release assets."""
import hashlib,json,os,re
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request,urlopen,build_opener,HTTPRedirectHandler

ROOT=Path(__file__).resolve().parents[1]
API='https://api.github.com/repos/minichao9901/superlinkPRO'
class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        request=super().redirect_request(*args,**kwargs)
        if request:request.remove_header('Authorization')
        return request
def main():
    token=os.environ.get('GITHUB_TOKEN')
    if not token:raise ValueError('GITHUB_TOKEN required; never stored or printed')
    manifest=json.loads((ROOT/'dist/manifest.json').read_text(encoding='utf8'))
    if manifest['file']!='superlinkPRO-win64.zip' or not re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?',manifest['version']):raise ValueError('Invalid package identity')
    archive=ROOT/'dist'/manifest['file'];raw=archive.read_bytes()
    if len(raw)!=manifest['bytes'] or hashlib.sha256(raw).hexdigest()!=manifest['sha256'] or manifest['sourceDirty']:raise ValueError('Unverified archive')
    headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','User-Agent':'superlinkPRO-release'};opener=build_opener(SafeRedirect())
    def api(url,data=None):
        payload=json.dumps(data).encode() if data is not None else None
        request=Request(url,data=payload,headers={**headers,**({'Content-Type':'application/json'} if payload is not None else {})})
        with opener.open(request,timeout=60) as response:return json.load(response)
    tag='workbench-v'+manifest['version']
    try:release=api(API+'/releases/tags/'+tag)
    except HTTPError as error:
        if error.code!=404:raise
        drafts=[r for r in api(API+'/releases?per_page=100') if r['tag_name']==tag]
        if len(drafts)>1:raise ValueError('Ambiguous draft release')
        release=drafts[0] if drafts else api(API+'/releases',{'tag_name':tag,'target_commitish':manifest['sourceRevision'],'name':'superlinkPRO '+manifest['version'],'body':f"Windows x64 WebUSB 工作台。源码 {manifest['sourceRevision']}。\n\nZIP SHA-256: {manifest['sha256']}\n\n解压后运行 superlinkPRO/superlinkPRO.exe；更新时保留 data。",'draft':True,'make_latest':'false'})
    if release['target_commitish']!=manifest['sourceRevision']:raise ValueError('Release source revision mismatch')
    assets=[a for a in release['assets'] if a['name']==manifest['file']]
    if len(assets)>1:raise ValueError('Duplicate release asset')
    if assets:asset=assets[0]
    else:
        url=release['upload_url'].split('{')[0]+'?name='+manifest['file']
        with opener.open(Request(url,data=raw,headers={**headers,'Content-Type':'application/zip'}),timeout=180) as response:asset=json.load(response)
    if asset['size']!=manifest['bytes']:raise ValueError('Existing asset size mismatch; refusing replacement')
    # Verify actual remote content even when the host does not expose digest metadata.
    remote_hash=hashlib.sha256();received=0
    with opener.open(Request(asset['url'],headers={**headers,'Accept':'application/octet-stream'}),timeout=90) as response:
        while block:=response.read(1048576):remote_hash.update(block);received+=len(block)
    if received!=manifest['bytes'] or remote_hash.hexdigest()!=manifest['sha256']:raise ValueError('Remote asset content mismatch; refusing publication')
    if release['draft']:
        request=Request(API+'/releases/'+str(release['id']),data=b'{"draft":false,"make_latest":"true"}',method='PATCH',headers={**headers,'Content-Type':'application/json'})
        with opener.open(request,timeout=60) as response:published=json.load(response)
        if published['draft']:raise ValueError('Release publication not confirmed')
    print(json.dumps({'tag':tag,'assetId':asset['id'],'url':asset['browser_download_url'],'bytes':received,'sha256':remote_hash.hexdigest(),'verified':True}))
if __name__=='__main__':main()
