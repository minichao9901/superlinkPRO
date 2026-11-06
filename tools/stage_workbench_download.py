"""Stage only the verified dist ZIP/manifest for the static Pages download page."""
import argparse,hashlib,json,shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--release',action='store_true',help='Stage manifest pointing to the verified GitHub Release instead of duplicating its ZIP in Pages');args=parser.parse_args()
    source=ROOT/'dist';manifest=json.loads((source/'manifest.json').read_text(encoding='utf8'))
    if manifest['file']!='superlinkPRO-win64.zip':raise ValueError('Unexpected distribution filename')
    archive=source/manifest['file']
    if archive.stat().st_size!=manifest['bytes'] or hashlib.sha256(archive.read_bytes()).hexdigest()!=manifest['sha256']:raise ValueError('Distribution checksum mismatch')
    target=ROOT/'web/downloads';target.mkdir(exist_ok=True)
    if args.release:
        manifest['downloadUrl']=f"https://github.com/minichao9901/superlinkPRO/releases/download/workbench-v{manifest['version']}/{manifest['file']}"
        (target/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf8',newline='\n')
    else:
        shutil.copyfile(archive,target/archive.name);shutil.copyfile(source/'manifest.json',target/'manifest.json')
    print(f"Staged {manifest['version']}: {manifest['bytes']} bytes, SHA-256 {manifest['sha256']}")
if __name__=='__main__':main()
