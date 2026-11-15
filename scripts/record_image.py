#!/usr/bin/env python3
"""Record the built image ID consistently across Podman and Docker."""
import os
from pathlib import Path
import re
import subprocess
import sys


def record(tag):
    engine = os.environ.get('NETHACK_CONTAINER_ENGINE', 'podman')
    raw = subprocess.check_output([engine, 'image', 'inspect', tag, '--format', '{{.Id}}'], text=True).strip()
    digest = raw.removeprefix('sha256:')
    if not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('Container engine returned an invalid image ID')
    root = Path(__file__).resolve().parents[1]
    target = root/'build/image-id'
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    temporary.write_text('sha256:' + digest + '\n')
    temporary.replace(target)


if __name__ == '__main__':
    record(sys.argv[1])
