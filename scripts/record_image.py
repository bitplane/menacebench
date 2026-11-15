#!/usr/bin/env python3
"""Record the built image ID consistently across Podman and Docker."""
import json
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


def repository_of(reference):
    """ghcr.io/o/n@sha256:... or ghcr.io/o/n:tag -> ghcr.io/o/n (a registry port is kept)."""
    name = reference.split('@')[0]
    last = name.rsplit('/', 1)
    if ':' in last[-1]: name = name[:name.rindex(':')]
    return name


def record_registry(reference):
    """Record a pulled image by its registry digest, e.g. ghcr.io/owner/name@sha256:..."""
    engine = os.environ.get('NETHACK_CONTAINER_ENGINE', 'podman')
    repository = repository_of(reference)
    digests = subprocess.check_output([engine, 'image', 'inspect', reference, '--format', '{{json .RepoDigests}}'], text=True)
    matches = [d for d in json.loads(digests) if d.split('@')[0] == repository]
    if not matches or not re.fullmatch(r'[a-z0-9.:/_-]+@sha256:[0-9a-f]{64}', matches[0]):
        raise ValueError(f'No registry digest for {repository}; pull it from the registry first')
    root = Path(__file__).resolve().parents[1]
    if matches[0] not in json.loads((root/'config/official-images.json').read_text()):
        print(f'warning: {matches[0]} is not in config/official-images.json; its runs will be unverified', file=sys.stderr)
    target = root/'build/image-id'
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(matches[0] + '\n')
    temporary.replace(target)
    print(matches[0])


if __name__ == '__main__':
    if sys.argv[1] == '--registry': record_registry(sys.argv[2])
    else: record(sys.argv[1])
