"""Verified, cached terminal recordings without freezing a run's usage report.

Recordings are a disposable cache: each one is regenerated from the attempt's
event log on demand. They are stored gzip-compressed (served as-is with
``Content-Encoding: gzip``) and pruned by age, total size and free disk space.
"""
import gzip
import os
from pathlib import Path
import shutil
import tempfile
import time

from .attempt import Conflict
from .package import build_identity, verify
from .storage import atomic_json, sync_dir

CAST = 'replay.cast'
CACHED = CAST+'.gz'
DEFAULT_CACHE = {'ttl': 7*86400, 'max_bytes': 2*1024**3, 'min_free_bytes': 2*1024**3}


def compress(source: Path, target: Path):
    """Atomically publish a gzip copy of ``source`` at ``target``."""
    temporary = target.with_name(target.name+'.'+os.urandom(8).hex()+'.tmp')
    try:
        with source.open('rb') as raw, temporary.open('xb') as file:
            with gzip.GzipFile(filename='', fileobj=file, mode='wb', mtime=0) as packed:
                shutil.copyfileobj(raw, packed, 1024*1024)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, target)
        sync_dir(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def touch(path: Path):
    # The cache evicts least recently used files; mtime records the last use.
    try: os.utime(path)
    except FileNotFoundError: pass


def cached(directory: Path):
    """Return the compressed recording if one exists, upgrading a legacy plain cast."""
    target = directory/CACHED
    legacy = directory/CAST
    if not target.exists() and legacy.exists():
        compress(legacy, target)
    legacy.unlink(missing_ok=True)
    return target if target.exists() else None


def recording(attempt):
    if attempt.state != 'finished':
        raise Conflict('The recording is available after the attempt finishes')
    target = cached(attempt.path)
    if target is not None:
        touch(target)
        return target
    target = attempt.path/CACHED
    # Take an immutable package snapshot under the attempt lock. This is not
    # the public package: watching must not finalize usage or expose a seed.
    with tempfile.TemporaryDirectory(prefix='recording-', dir=attempt.path) as tmp:
        folder = attempt.package(Path(tmp)/'package')
        output = Path(tmp)/CAST
        diagnostic = (attempt.info['image_digest'] != os.environ.get('NETHACK_IMAGE_DIGEST')
                      or attempt.info['build_identity'] != build_identity())
        result = verify(folder, output, diagnostic_cross_build=diagnostic)
        # Cross-build exports still compare every checkpoint and final result;
        # they never claim verification in the originally recorded image.
        atomic_json(attempt.path/'replay.json', result)
        compress(output, target)
    return target


def prune(root: Path, ttl, max_bytes, min_free_bytes, now=None):
    """Delete cached recordings: expired ones, then least recently used while over budget.

    Returns the number of files removed. A removed recording is rebuilt from
    the event log the next time someone watches it.
    """
    now = time.time() if now is None else now
    entries = []
    for pattern in ('*/'+CACHED, '*/'+CAST):
        for path in root.glob(pattern):
            try: stat = path.stat()
            except FileNotFoundError: continue
            entries.append((stat.st_mtime, stat.st_size, path))
    entries.sort()
    removed = 0
    total = sum(size for _, size, _ in entries)
    free = shutil.disk_usage(root).free
    for mtime, size, path in entries:
        if now-mtime < ttl and total <= max_bytes and free >= min_free_bytes:
            break
        path.unlink(missing_ok=True)
        removed += 1
        total -= size
        free += size
    return removed
