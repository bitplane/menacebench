import gzip
import hashlib
import json
import runpy

import pytest
from nethack_run.attempt import Attempt
from nethack_run.engine import ROOT
from nethack_run.package import verify
from nethack_run.version import VERSION


def test_release_version_and_legacy_backfill(tmp_path):
    root = tmp_path/'runs'
    root.mkdir()
    a, owner, player = Attempt.create(root, {}, 'test')
    a.pause()
    a.expire()
    package = a.package()
    assert a.info['harness_version'] == VERSION
    assert a.journal.events[0]['harness_version'] == VERSION
    assert json.loads((package/'attempt.json').read_bytes())['harness_version'] == VERSION
    verify(package)
    # Model the old unversioned data, including an already finalized package.
    private = a.path/'private.json'
    info = json.loads(private.read_bytes())
    del info['harness_version']
    private.write_text(json.dumps(info))
    log = a.path/'events.jsonl'
    events = [json.loads(line) for line in log.read_bytes().splitlines()]
    del events[0]['harness_version']
    data = b''.join((json.dumps(e)+'\n').encode() for e in events)
    log.write_bytes(data)
    (package/'events.jsonl.gz').write_bytes(gzip.compress(data))
    manifest_path = package/'attempt.json'
    manifest = json.loads(manifest_path.read_bytes())
    del manifest['harness_version']
    manifest['event_log_digest'] = hashlib.sha256(data).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    verify(package)  # Legacy packages remain supported.
    original_private = private.read_bytes()
    migrate = runpy.run_path(str(ROOT/'scripts/backfill_version.py'))['migrate']
    backup = tmp_path/'backup'
    assert migrate(root, '0.0.1', backup) == 1
    assert (backup/a.id/'private.json').read_bytes() == original_private
    assert (backup.stat().st_mode & 0o777) == 0o700
    assert migrate(root, '0.0.1', tmp_path/'unused') == 0
    recovered = Attempt(a.path)
    assert recovered.info['harness_version'] == '0.0.1'
    assert recovered.role(owner) == 'owner' and recovered.role(player) == 'player'
    verify(package)
    manifest = json.loads(manifest_path.read_bytes())
    manifest['harness_version'] = 'wrong'
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='harness_version mismatch'):
        verify(package)
