#!/usr/bin/env python3
"""Download organizer originals into an ignored private folder; verify pinned hashes."""
import concurrent.futures
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.request

os.umask(0o077)
ROOT = Path(__file__).resolve().parents[1]
def download(entry, dest):
    name=entry['filename']
    if Path(name).name != name: raise ValueError('invalid filename')
    path=dest/name
    if path.is_symlink(): raise ValueError('existing original is a symlink: '+name)
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
    fd, part_name = tempfile.mkstemp(prefix=f'.{name}.', suffix='.part', dir=dest)
    part=Path(part_name)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(data)
        os.chmod(part,0o600)
        part.replace(path)
    except BaseException:
        try:part.unlink()
        except OSError:pass
        raise
    return name

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--case-dir',type=Path,default=ROOT/'.local/data/case')
    args=parser.parse_args()
    DEST=args.case_dir.resolve()
    DEST.mkdir(parents=True,exist_ok=True,mode=0o700)
    entries=json.loads((ROOT/'infra/case-files.json').read_text())
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for name in pool.map(lambda entry:download(entry,DEST),entries):print(name,flush=True)
    manifest=DEST/'download-manifest.json'
    fd,temp_name=tempfile.mkstemp(prefix='.download-manifest.',suffix='.tmp',dir=DEST)
    temporary=Path(temp_name)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            stream.write(json.dumps(entries,ensure_ascii=False,indent=2)+'\n')
            stream.flush();os.fsync(stream.fileno())
            os.fchmod(stream.fileno(),0o600)
        temporary.replace(manifest)
        os.chmod(manifest,0o600)
    except BaseException:
        try:temporary.unlink()
        except OSError:pass
        raise
