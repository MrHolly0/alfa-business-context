"""Create one persistent internal service credential; never print its value."""
import os
from pathlib import Path
import secrets

folder = Path('/run/intelligence-auth')
folder.mkdir(parents=True, exist_ok=True)
path = folder / 'service-token'
seed = os.environ.get('SERVICE_TOKEN_SEED', '').strip()
if path.exists():
    saved = path.read_text().strip()
    if len(saved) < 24 or (seed and not secrets.compare_digest(saved, seed)):
        raise SystemExit('Internal credential mismatch; inspect the operator configuration')
else:
    value = seed or secrets.token_urlsafe(48)
    if len(value) < 24:
        raise SystemExit('Internal service credential must contain at least 24 characters')
    with path.open('x') as stream:
        stream.write(value + '\n')
    path.chmod(0o444)
print('Internal service credential ready')
