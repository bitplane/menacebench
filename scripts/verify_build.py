#!/usr/bin/env python3
import hashlib
import json
import os
from pathlib import Path

root=Path(__file__).resolve().parents[1]
manifest=json.loads((root/'build/build.json').read_text())
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
checks={
    'executable_digest':digest(root/'build/install/games/lib/nethackdir/nethack'),
    'options_digest':digest(root/'config/nethackrc'),
    'sysconf_digest':digest(root/'config/sysconf'),
    'patch_set_digest':hashlib.sha256(b''.join(p.name.encode()+b'\0'+p.read_bytes() for p in sorted((root/'patches').glob('*.patch')))).hexdigest(),
}
for key,value in checks.items():
    if manifest.get(key)!=value:raise SystemExit(f'{key} differs from the last build: run make setup')
image=root/'build/image-id'
manifest['image_digest']=os.environ.get('NETHACK_IMAGE_DIGEST') or (image.read_text().strip() if image.exists() else None)
print(json.dumps(manifest,indent=2))
