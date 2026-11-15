import gzip
import json
import os
import pytest

from nethack_run.attempt import Attempt, Conflict
from nethack_run.recording import cached, compress, prune, recording
from nethack_run.package import verify


def finished(root):
    a = Attempt.create(root, {'player_type':'human'}, 'test')[0]
    a.keys('Tester-Bar-Hum-Mal-Cha\ny#quit\ny'+' '*20, 1, 1)
    assert a.state == 'finished'
    return a


def test_recording_is_cached_without_freezing_usage(tmp_path, monkeypatch):
    a = finished(tmp_path)
    path = recording(a)
    assert path.name == 'replay.cast.gz' and not (a.path/'replay.cast').exists()
    rows = [json.loads(line) for line in gzip.decompress(path.read_bytes()).decode().splitlines()]
    assert rows[0]['version'] == 3
    assert 'You quit' in ''.join(row[2] for row in rows[1:])
    assert not (a.path/'package').exists()
    assert not list(a.path.glob('recording-*'))
    a.report_usage({'cost': 0.25})
    assert verify(a.package())['verified']
    def unexpected(*args, **kwargs): raise AssertionError('Cached recording was regenerated')
    monkeypatch.setattr('nethack_run.recording.verify', unexpected)
    assert recording(a) == path


def test_old_image_recording_is_marked_as_diagnostic(tmp_path, monkeypatch):
    monkeypatch.setenv('NETHACK_IMAGE_DIGEST', 'sha256:'+'a'*64)
    a = finished(tmp_path)
    monkeypatch.setenv('NETHACK_IMAGE_DIGEST', 'sha256:'+'b'*64)
    assert recording(a).exists()
    result = json.loads((a.path/'replay.json').read_text())
    assert result['reproduced'] and not result['verified']
    assert result['environment'] == 'cross-build-diagnostic'


def test_divergent_recording_is_not_published(tmp_path, monkeypatch):
    a = finished(tmp_path)
    def divergence(*args, **kwargs): raise ValueError('Divergence')
    monkeypatch.setattr('nethack_run.recording.verify', divergence)
    with pytest.raises(ValueError, match='Divergence'): recording(a)
    assert not (a.path/'replay.cast').exists() and not (a.path/'replay.cast.gz').exists()
    assert not (a.path/'package').exists()
    assert not list(a.path.glob('recording-*'))


def test_running_game_has_no_completed_recording(tmp_path):
    a = Attempt.create(tmp_path, {}, 'test')[0]
    with pytest.raises(Conflict): recording(a)
    a.pause()


def test_legacy_plain_recording_is_compressed_in_place(tmp_path, monkeypatch):
    a = finished(tmp_path)
    (a.path/'replay.cast').write_text('{"version": 3}\n')
    def unexpected(*args, **kwargs): raise AssertionError('Legacy recording was regenerated')
    monkeypatch.setattr('nethack_run.recording.verify', unexpected)
    path = recording(a)
    assert gzip.decompress(path.read_bytes()) == b'{"version": 3}\n'
    assert not (a.path/'replay.cast').exists()


def test_compression_is_deterministic(tmp_path):
    source = tmp_path/'replay.cast'; source.write_bytes(b'{"version": 3}\n'*1000)
    compress(source, tmp_path/'one.gz'); compress(source, tmp_path/'two.gz')
    assert (tmp_path/'one.gz').read_bytes() == (tmp_path/'two.gz').read_bytes()
    assert gzip.decompress((tmp_path/'one.gz').read_bytes()) == source.read_bytes()
    assert not list(tmp_path.glob('*.tmp'))


def fake_cast(root, name, size, age, now):
    folder = root/name; folder.mkdir(parents=True)
    path = folder/'replay.cast.gz'; path.write_bytes(b'x'*size)
    os.utime(path, (now-age, now-age))
    return path


def test_prune_expires_then_evicts_least_recently_used(tmp_path, monkeypatch):
    now = 1_000_000
    old = fake_cast(tmp_path, 'old', 10, 100, now)
    middle = fake_cast(tmp_path, 'middle', 10, 50, now)
    new = fake_cast(tmp_path, 'new', 10, 1, now)
    legacy = tmp_path/'legacy'/'replay.cast'; legacy.parent.mkdir(); legacy.write_bytes(b'y')
    os.utime(legacy, (now-500, now-500))
    plenty = type('Usage', (), {'free': 10**12})
    monkeypatch.setattr('nethack_run.recording.shutil.disk_usage', lambda path: plenty)
    # Expired by age, including a legacy uncompressed cast.
    assert prune(tmp_path, ttl=75, max_bytes=10**9, min_free_bytes=0, now=now) == 2
    assert not old.exists() and not legacy.exists() and middle.exists() and new.exists()
    # Over the size budget: the least recently used goes first.
    assert prune(tmp_path, ttl=10**9, max_bytes=10, min_free_bytes=0, now=now) == 1
    assert not middle.exists() and new.exists()
    # Low disk space evicts even recent recordings.
    monkeypatch.setattr('nethack_run.recording.shutil.disk_usage', lambda path: type('Usage', (), {'free': 0}))
    assert prune(tmp_path, ttl=10**9, max_bytes=10**9, min_free_bytes=5, now=now) == 1
    assert not new.exists()


def test_pruned_recording_is_rebuilt_on_demand(tmp_path):
    a = finished(tmp_path)
    path = recording(a)
    contents = path.read_bytes()
    assert prune(tmp_path, ttl=0, max_bytes=10**12, min_free_bytes=0) == 1
    assert cached(a.path) is None
    assert recording(a).read_bytes() == contents
