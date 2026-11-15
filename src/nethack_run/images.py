"""Replay only approved pinned images, with the package mounted as data."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from .engine import ROOT
from .package import load_package


def dispatch(package, output=None):
    manifest, _ = load_package(package)
    image = manifest.get('image_digest')
    if image is None:
        return False  # Explicitly a development package, not an official image run.
    if not re.fullmatch(r'(?:[a-zA-Z0-9./:_-]+@)?sha256:[0-9a-f]{64}', image):
        raise ValueError('Invalid pinned image reference')
    if image == os.environ.get('NETHACK_IMAGE_DIGEST'):
        return False
    allowed = json.loads((ROOT/'config/official-images.json').read_text())
    local = ROOT/'build/image-id'
    if local.exists(): allowed.append(local.read_text().strip())
    if image not in allowed:
        raise ValueError('Package image is not in config/official-images.json')
    with tempfile.TemporaryDirectory(prefix='nethack-export-') as temp:
        os.chmod(temp, 0o777)
        command = [os.environ.get('NETHACK_CONTAINER_ENGINE', 'podman'),'run','--rm','--network','none','--read-only',
                   '--tmpfs','/tmp:rw,nosuid,size=256m','--memory','512m','--cpus','1',
                   '--pids-limit','64','-e','NETHACK_IMAGE_DIGEST='+image,
                   '--mount',f'type=bind,src={Path(package).resolve()},dst=/package,readonly']
        if output:command += ['--mount',f'type=bind,src={temp},dst=/output']
        command += [image, 'export' if output else 'replay', '/package']
        if output: command += ['--output','/output/replay.cast']
        subprocess.run(command,check=True,timeout=600)
        if output: Path(output).write_bytes((Path(temp)/'replay.cast').read_bytes())
    return True
