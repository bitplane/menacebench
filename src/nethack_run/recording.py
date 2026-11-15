"""Verified, cached terminal recordings without freezing a run's usage report."""
import os
from pathlib import Path
import tempfile

from .attempt import Conflict
from .package import build_identity, verify
from .storage import atomic_json, sync_dir


def recording(attempt):
    if attempt.state != 'finished':
        raise Conflict('The recording is available after the attempt finishes')
    target = attempt.path/'replay.cast'
    if target.exists():
        return target
    # Take an immutable package snapshot under the attempt lock. This is not
    # the public package: watching must not finalize usage or expose a seed.
    with tempfile.TemporaryDirectory(prefix='recording-', dir=attempt.path) as tmp:
        folder = attempt.package(Path(tmp)/'package')
        output = Path(tmp)/'replay.cast'
        diagnostic = (attempt.info['image_digest'] != os.environ.get('NETHACK_IMAGE_DIGEST')
                      or attempt.info['build_identity'] != build_identity())
        result = verify(folder, output, diagnostic_cross_build=diagnostic)
        # Cross-build exports still compare every checkpoint and final result;
        # they never claim verification in the originally recorded image.
        atomic_json(attempt.path/'replay.json', result)
        with output.open('rb') as file:
            os.fsync(file.fileno())
        os.replace(output, target)
        sync_dir(attempt.path)
    return target
