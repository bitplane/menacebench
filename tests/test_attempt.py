import json
from nethack_run.attempt import Attempt, Conflict
from nethack_run.package import verify
import pytest


def create(tmp_path):
    return Attempt.create(tmp_path, {'player_type':'human'}, 'test')[0]


def send(attempt, keys):
    return attempt.keys(keys, attempt.session, attempt.frame.boundary)


def quit_game(attempt):
    send(attempt, 'Tester-Bar-Hum-Mal-Cha\ny#quit\ny')
    for _ in range(20):
        if attempt.state == 'finished': return
        send(attempt, ' ')
    raise AssertionError('Quit did not finish')


def test_package_quit(tmp_path):
    a = create(tmp_path); quit_game(a)
    a.report_usage({'input_tokens':100})
    p = a.package()
    assert verify(p)['outcome'] == 'quit'
    assert json.loads((p/'attempt.json').read_text())['usage']['input_tokens']==100
    with pytest.raises(Conflict): a.report_usage({'cost':1})


def test_retry_and_rebuild(tmp_path):
    a = create(tmp_path)
    position = a.frame.boundary
    send(a,'Tes')
    with pytest.raises(Conflict): a.keys('Tes',1,position)
    before = a.frame.digest
    a.pause(); a.resume()
    assert a.frame.digest == before
    send(a,'ter-Bar-Hum-Mal-Cha\ny..')
    a.pause()
    recovered = Attempt(a.path); recovered.recover(); recovered.resume()
    assert recovered.frame.digest == a.frame.digest
    recovered.pause(); recovered.expire()
    assert verify(recovered.package())['outcome'] == 'abandoned'


def test_save_resume(tmp_path):
    a = create(tmp_path)
    send(a,'Tester-Bar-Hum-Mal-Cha\ny..Sy ')
    assert a.state == 'saved'
    recovered = Attempt(a.path); recovered.recover(); recovered.resume()
    assert recovered.session == 2
    send(recovered,'a..')
    recovered.pause(); recovered.expire()
    assert verify(recovered.package())['sessions']==2


def test_crash_after_final_key_before_end(tmp_path, monkeypatch):
    a = create(tmp_path)
    send(a,'Tester-Bar-Hum-Mal-Cha\ny..Sy')
    def crash(): raise OSError('injected crash')
    monkeypatch.setattr(a,'finish_session',crash)
    with pytest.raises(OSError): send(a,' ')
    if a.game: a.game.close()
    recovered = Attempt(a.path); recovered.recover()
    assert recovered.state=='saved'
    assert len([e for e in recovered.journal.events if e['type']=='session_end'])==1
    recovered.expire()
    assert verify(recovered.package())['outcome']=='abandoned'


@pytest.mark.parametrize('field,value',[('key_count',999),('total_duration',999),('character',{'name':'fake'}),('usage',{'cost':999})])
def test_manifest_tampering(tmp_path,field,value):
    a=create(tmp_path);a.pause();a.expire();p=a.package()
    manifest=json.loads((p/'attempt.json').read_text());manifest[field]=value
    (p/'attempt.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError):verify(p)


def test_export(tmp_path):
    a=create(tmp_path);quit_game(a)
    cast=tmp_path/'replay.cast'
    assert verify(a.package(),cast)['verified']
    lines=[json.loads(line) for line in cast.read_text().splitlines()]
    assert lines[0]['version']==3
    assert lines[0]['term']['cols']==80
    assert len(lines)>10
    assert all(line[0]>=0 and line[1]=='o' for line in lines[1:])
    assert 'You quit' in ''.join(line[2] for line in lines[1:])
    assert '\x1b]7777' not in cast.read_text()


def test_pause_interrupts_batch_after_durable_consumption(tmp_path):
    import threading
    a=create(tmp_path)
    reached=threading.Event()
    original=a.game.on_consumed
    def consumed(boundary,key):
        original(boundary,key)
        reached.set()
        assert a.pause_requested.wait(2)
    a.game.on_consumed=consumed
    result=[];errors=[]
    def send_batch():
        try:result.append(a.keys('Tester',1,1))
        except BaseException as error:errors.append(error)
    sender=threading.Thread(target=send_batch);sender.start()
    assert reached.wait(2)
    a.pause()
    sender.join(2)
    assert not sender.is_alive()
    assert not errors
    assert result==[(1,5)]
    assert a.state=='paused' and a.frame.boundary==2
    a.resume()
    assert a.frame.boundary==2
    a.pause();a.expire()
    assert verify(a.package())['keys']==1


def test_screen_during_advancement_is_marked_busy(tmp_path):
    a=create(tmp_path);original=a.game.on_consumed
    observed=[]
    def consumed(boundary,key):
        original(boundary,key)
        observed.append(a.status())
    a.game.on_consumed=consumed
    send(a,'T')
    assert observed[0]['state']=='advancing'
    assert observed[0]['boundary']==1
    assert not observed[0]['accepting_input']
    assert a.status()['accepting_input']
    a.pause()
