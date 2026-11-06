"""Stage only the verified dist ZIP/manifest for the static Pages download page."""
import hashlib,json,shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    source=ROOT/'dist';manifest=json.loads((source/'manifest.json').read_text(encoding='utf8'))
    if manifest['file']!='superlinkPRO-win64.zip':raise ValueError('Unexpected distribution filename')
    archive=source/manifest['file']
    if archive.stat().st_size!=manifest['bytes'] or hashlib.sha256(archive.read_bytes()).hexdigest()!=manifest['sha256']:raise ValueError('Distribution checksum mismatch')
    target=ROOT/'web/downloads';target.mkdir(exist_ok=True)
    shutil.copyfile(archive,target/archive.name);shutil.copyfile(source/'manifest.json',target/'manifest.json')
    print(f"Staged {manifest['version']}: {manifest['bytes']} bytes, SHA-256 {manifest['sha256']}")
if __name__=='__main__':main()
