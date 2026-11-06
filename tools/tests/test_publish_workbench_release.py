import hashlib,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request
from tools import publish_workbench_release as release

class PublicationTests(unittest.TestCase):
    def run_publication(self,remote=b'package',existing=False):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);root=Path(temp.name);(root/'dist').mkdir();raw=b'package';(root/'dist/superlinkPRO-win64.zip').write_bytes(raw)
        m=dict(version='1.1.0-web.3',file='superlinkPRO-win64.zip',bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),sourceDirty=False,sourceRevision='a'*40)
        (root/'dist/manifest.json').write_text(json.dumps(m));calls=[]
        asset=dict(id=4,name=m['file'],size=len(raw),url='https://api.github.com/asset/4',browser_download_url='https://github.com/asset/4')
        draft=dict(id=3,tag_name='workbench-v1.1.0-web.3',target_commitish='a'*40,draft=True,assets=[{**asset,'size':1}] if existing else [],upload_url='https://uploads.github.com/upload{?name}')
        class Client:
            def open(self,req,timeout=None):
                method=req.get_method();calls.append((method,req.full_url,req.data))
                if '/releases/tags/' in req.full_url:
                    if existing:return io.BytesIO(json.dumps(draft).encode())
                    raise HTTPError(req.full_url,404,'not found',{},None)
                if req.full_url.endswith('releases?per_page=100'):body=[]
                elif method=='PATCH':body={**draft,'draft':False}
                elif 'uploads.github.com' in req.full_url:body=asset
                elif '/asset/4' in req.full_url:return io.BytesIO(remote)
                else:body=draft
                return io.BytesIO(json.dumps(body).encode())
        with patch.object(release,'ROOT',root),patch.object(release,'build_opener',return_value=Client()),patch.dict('os.environ',{'GITHUB_TOKEN':'test-token'}),patch('builtins.print'):
            try:release.main()
            except ValueError as error:return calls,error
        return calls,None
    def test_remote_bytes_are_verified_before_draft_is_published(self):
        calls,error=self.run_publication();self.assertIsNone(error);self.assertEqual(calls[-1][0],'PATCH');self.assertIn('/asset/4',calls[-2][1]);created=json.loads(next(data for method,url,data in calls if method=='POST' and url.endswith('/releases')));self.assertTrue(created['draft'])
    def test_bad_remote_content_keeps_release_unpublished(self):
        calls,error=self.run_publication(remote=b'corrupt');self.assertIn('content mismatch',str(error));self.assertFalse(any(method=='PATCH' for method,_,_ in calls))
    def test_existing_wrong_asset_is_never_replaced(self):
        calls,error=self.run_publication(existing=True);self.assertIn('size mismatch',str(error));self.assertTrue(all(method=='GET' for method,_,_ in calls))
    def test_redirect_does_not_forward_the_github_credential(self):
        req=Request('https://api.github.com/x',headers={'Authorization':'Bearer secret'})
        new=release.SafeRedirect().redirect_request(req,None,302,'redirect',{},'https://objects.githubusercontent.com/asset')
        self.assertIsNone(new.get_header('Authorization'))

if __name__=='__main__':unittest.main()
