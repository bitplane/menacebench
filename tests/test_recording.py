import json
import pytest

from nethack_run.attempt import Attempt, Conflict
from nethack_run.recording import recording
from nethack_run.package import verify


def finished(root):
    a = Attempt.create(root, {'player_type':'human'}, 'test')[0]
    a.keys('Tester-Bar-Hum-Mal-Cha\ny#quit\ny'+' '*20, 1, 1)
    assert a.state == 'finished'
    return a


def test_recording_is_cached_without_freezing_usage(tmp_path, monkeypatch):
    a = finished(tmp_path)
    path = recording(a)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
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
    assert not (a.path/'replay.cast').exists()
    assert not (a.path/'package').exists()
    assert not list(a.path.glob('recording-*'))


def test_running_game_has_no_completed_recording(tmp_path):
    a = Attempt.create(tmp_path, {}, 'test')[0]
    with pytest.raises(Conflict): recording(a)
    a.pause()
