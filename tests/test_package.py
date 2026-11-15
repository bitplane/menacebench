import gzip
import hashlib
import json
import pytest
from nethack_run.attempt import Attempt
from nethack_run.package import verify


def rewrite_log(package, events):
    data=b''.join(json.dumps(event,separators=(',',':')).encode()+b'\n' for event in events)
    (package/'events.jsonl.gz').write_bytes(gzip.compress(data,mtime=0))
    manifest=json.loads((package/'attempt.json').read_text())
    manifest['event_log_digest']=hashlib.sha256(data).hexdigest()
    (package/'attempt.json').write_text(json.dumps(manifest))


def test_checkpoint_cannot_name_previous_running_boundary(tmp_path):
    attempt=Attempt.create(tmp_path,{},'test')[0]
    attempt.keys('T',1,1)
    attempt.pause();attempt.expire()
    package=attempt.package()
    events=[json.loads(line) for line in gzip.decompress((package/'events.jsonl.gz').read_bytes()).splitlines()]
    checkpoints=[e for e in events if e['type']=='checkpoint']
    checkpoints[1].update(boundary=checkpoints[0]['boundary'],hash=checkpoints[0]['hash'])
    rewrite_log(package,events)
    with pytest.raises(ValueError,match='unavailable boundary'):
        verify(package)


def test_pinned_image_package_requires_matching_runtime(tmp_path,monkeypatch):
    attempt=Attempt.create(tmp_path,{},'test')[0]
    attempt.pause();attempt.expire()
    package=attempt.package()
    image='example.invalid/nethack@sha256:'+'a'*64
    events=[json.loads(line) for line in gzip.decompress((package/'events.jsonl.gz').read_bytes()).splitlines()]
    events[0]['image_digest']=image
    rewrite_log(package,events)
    manifest=json.loads((package/'attempt.json').read_text())
    manifest['image_digest']=image
    (package/'attempt.json').write_text(json.dumps(manifest))
    monkeypatch.delenv('NETHACK_IMAGE_DIGEST',raising=False)
    with pytest.raises(ValueError,match='recorded image'):
        verify(package)
    diagnostic=verify(package,diagnostic_cross_build=True)
    assert diagnostic['reproduced'] and not diagnostic['verified']
