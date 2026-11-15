import json
import os
from pathlib import Path
import pytest
from nethack_run.storage import Journal, atomic_bytes, snapshot
from nethack_run.attempt import Attempt
from nethack_run.package import verify


def test_truncated_last_record(tmp_path):
    path=tmp_path/'events.jsonl';journal=Journal(path)
    journal.append('first',value=1)
    durable=path.read_bytes()
    with path.open('ab') as file:file.write(b'{"seq":2,"type":')
    recovered=Journal(path)
    assert path.read_bytes()==durable
    recovered.append('second',value=2)
    assert [e['seq'] for e in Journal(path).events]==[1,2]


def test_complete_corrupt_record_is_not_silently_discarded(tmp_path):
    path=tmp_path/'events.jsonl';path.write_bytes(b'invalid\n')
    with pytest.raises(ValueError):Journal(path)


def test_power_loss_discards_unsynchronized_key(tmp_path,monkeypatch):
    a=Attempt.create(tmp_path,{},'test')[0]
    initial=a.frame.digest
    durable=a.journal.path.read_bytes()
    real_fsync=os.fsync
    def fail_log_sync(fd):
        if os.readlink(f'/proc/self/fd/{fd}')==str(a.journal.path):
            raise OSError('power loss before synchronization')
        real_fsync(fd)
    with monkeypatch.context() as m:
        m.setattr(os,'fsync',fail_log_sync)
        with pytest.raises(OSError):a.keys('T',1,1)
    a.game.close()
    # Simulated storage retains only the last synchronized file contents.
    a.journal.path.write_bytes(durable)
    recovered=Attempt(a.path);recovered.recover()
    assert recovered.frame.digest==initial
    assert recovered.frame.boundary==1
    recovered.expire()
    assert verify(recovered.package())['keys']==0


def test_publication_follows_durable_key(tmp_path):
    a=Attempt.create(tmp_path,{},'test')[0]
    publications=[]
    def published(data):
        disk=Journal(a.journal.path).events
        keys=[e for e in disk if e['type']=='key']
        assert keys[-1]['boundary']==a.frame.boundary-1
        assert any(e['type']=='checkpoint' and e['boundary']==a.frame.boundary for e in disk)
        publications.append(data)
    a.listeners.add(published)
    a.keys('Test',1,1)
    assert len(publications)==4
    a.pause()


@pytest.mark.parametrize('after',[False,True])
def test_snapshot_publication_failure(tmp_path,monkeypatch,after):
    a=Attempt.create(tmp_path,{},'test')[0]
    a.keys('Tester-Bar-Hum-Mal-Cha\ny..Sy ',1,1)
    assert a.state=='saved'
    real_rename=os.rename
    def fail_snapshot(source,destination):
        if str(destination).endswith('snapshot-2'):
            if after:real_rename(source,destination)
            raise OSError('crash at snapshot publication')
        return real_rename(source,destination)
    with monkeypatch.context() as m:
        m.setattr(os,'rename',fail_snapshot)
        with pytest.raises(OSError):a.resume()
    recovered=Attempt(a.path);recovered.recover();recovered.resume()
    assert recovered.session==2
    recovered.pause();recovered.expire()
    assert verify(recovered.package())['sessions']==2


@pytest.mark.parametrize('after',[False,True])
def test_atomic_package_failure(tmp_path,monkeypatch,after):
    a=Attempt.create(tmp_path,{},'test')[0];a.pause();a.expire()
    real_rename=os.rename
    def fail_package(source,destination):
        if Path(destination).name=='package':
            if after:real_rename(source,destination)
            raise OSError('crash at package publication')
        return real_rename(source,destination)
    with monkeypatch.context() as m:
        m.setattr(os,'rename',fail_package)
        with pytest.raises(OSError):a.package()
    recovered=Attempt(a.path);recovered.recover()
    assert verify(recovered.package())['verified']


def test_crash_final_key_before_checkpoint(tmp_path,monkeypatch):
    a=Attempt.create(tmp_path,{},'test')[0]
    a.keys('Tester-Bar-Hum-Mal-Cha\ny..Sy',1,1)
    def crash():raise OSError('crash before checkpoint')
    monkeypatch.setattr(a,'checkpoint',crash)
    with pytest.raises(OSError):a.keys(' ',a.session,a.frame.boundary)
    a.game.close()
    recovered=Attempt(a.path);recovered.recover()
    assert recovered.state=='saved'
    recovered.expire()
    assert verify(recovered.package())['verified']


def test_recover_creation_before_first_log_event(tmp_path):
    a=Attempt.create(tmp_path,{},'test')[0]
    a.game.close()
    a.journal.path.write_bytes(b'')
    recovered=Attempt(a.path);recovered.recover()
    assert recovered.state=='paused'
    recovered.expire()
    assert verify(recovered.package())['verified']
