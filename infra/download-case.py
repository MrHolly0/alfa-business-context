#!/usr/bin/env python3
"""Download organizer originals into an ignored private folder; verify pinned hashes."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import urllib.request

os.umask(0o077)
ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / '.local/data/case'

def download(entry):
    name=entry['filename']
    if Path(name).name != name: raise ValueError('invalid filename')
    path=DEST/name
    if path.exists():
        data=path.read_bytes()
        if len(data)!=entry['size'] or hashlib.sha256(data).hexdigest()!=entry['sha256']:
            raise ValueError('Existing original differs: '+name)
        return name
    request=urllib.request.Request('https://drive.google.com/uc?export=download&id='+entry['drive_id'])
    with urllib.request.urlopen(request, timeout=120) as response:
        data=response.read(entry['size']+1)
    if len(data)!=entry['size'] or hashlib.sha256(data).hexdigest()!=entry['sha256']:
        raise ValueError('Download size/hash mismatch: '+name)
    part=path.with_suffix(path.suffix+'.part')
    with part.open('xb') as stream:stream.write(data)
    os.chmod(part,0o600);part.replace(path)
    return name

if __name__=='__main__':
    DEST.mkdir(parents=True,exist_ok=True)
    entries=json.loads((ROOT/'infra/case-files.json').read_text())
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for name in pool.map(download,entries):print(name,flush=True)
    (DEST/'download-manifest.json').write_text(json.dumps(entries,ensure_ascii=False,indent=2)+'\n')
