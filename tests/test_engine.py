from pathlib import Path
import shutil
import pytest
from nethack_run.engine import Game, clean_directory, replay, session_seed

START = b'Tester\nyy'
SAVE = START + b'..Sy '


def record(path, seed, keys):
    frames = {}
    with Game(path, seed) as game:
        frames[game.boundary] = game.frame.digest
        for key in keys:
            frame = game.key(key)
            if frame:
                frames[frame.boundary] = frame.digest
        return frames, game.exited, game.exit_code


def test_repeatability(tmp_path):
    for name in ('one', 'two'):
        clean_directory(tmp_path / name)
    keys = START + b'..ihjkl\x1b..'
    first = record(tmp_path / 'one', 42, keys)
    second = record(tmp_path / 'two', 42, keys)
    assert first == second


@pytest.mark.parametrize('cut', [0, 3, 7, 8, 9, 11, 14])
def test_kill_rebuild(tmp_path, cut):
    original, rebuilt = tmp_path/'original', tmp_path/'rebuilt'
    clean_directory(original)
    clean_directory(rebuilt)
    keys = START + b'..Sy '
    frames, exited, code = record(original, 42, keys[:cut])
    with replay(rebuilt, 42, list(keys[:cut]), frames) as game:
        assert game.exited == exited
        assert game.exit_code == code
        for key in keys[cut:]:
            game.key(key)
        assert game.exited
        assert list((rebuilt/'save').iterdir())


def test_save_restore_session_seed(tmp_path):
    results = []
    for name in ('one','two'):
        path = tmp_path/name
        clean_directory(path)
        first = record(path, session_seed('abc', 1), SAVE)
        assert first[1:] == (True, 0)
        assert len(list((path/'save').iterdir())) == 1
        # Saved-character selection is ordinary game input in the next session.
        second = record(path, session_seed('abc', 2), b'a...i\x1b...')
        results.append((first, second))
    assert results[0] == results[1]


def test_restore_snapshot_rebuild(tmp_path):
    path = tmp_path/'original'
    clean_directory(path)
    record(path, 42, SAVE)
    snapshot = tmp_path/'snapshot'
    shutil.copytree(path, snapshot)
    keys = b'a...i\x1b'
    frames, _, _ = record(path, 43, keys)
    rebuilt = tmp_path/'rebuilt'
    shutil.copytree(snapshot, rebuilt)
    with replay(rebuilt, 43, list(keys), frames) as game:
        assert game.frame.digest == frames[game.boundary]


def test_replay_detects_divergence(tmp_path):
    path = tmp_path/'game'; clean_directory(path)
    with pytest.raises(ValueError, match='Divergence at boundary 1'):
        replay(path, 42, [], {1: 'bad'})


def test_seed_changes_game(tmp_path):
    captures=[]
    for seed in (42,43):
        path=tmp_path/str(seed);clean_directory(path)
        frames,_,_=record(path,seed,START)
        captures.append(list(frames.values())[-1])
    assert captures[0]!=captures[1]


@pytest.mark.parametrize('role', list('abchkmprRstvw'))
def test_character_role_menus(tmp_path, role):
    path=tmp_path/'game';clean_directory(path)
    with Game(path,42) as game:
        for key in ('Tester\nn'+role).encode():game.key(key)
        for _ in range(12):
            if game.character:break
            text=game.frame.screen
            if 'Is this ok?' in text:game.key(ord('y'))
            elif 'Pick a race' in text or 'Pick a gender' in text or 'Pick an alignment' in text:game.key(ord('*'))
            else:raise AssertionError(text)
        assert game.character is not None


@pytest.mark.parametrize('name', ['Tester-Wiz-Elf-Fem-Cha','Tester-Wiz-Orc-Mal-Cha',
                                  'Tester-Arc-Dwa-Mal-Law','Tester-Hea-Gno-Fem-Neu',
                                  'Tester-Kni-Hum-Fem-Law'])
def test_character_races_genders_alignments(tmp_path,name):
    path=tmp_path/'game';clean_directory(path)
    with Game(path,42) as game:
        for key in (name+'\n').encode():game.key(key)
        for _ in range(5):
            if game.character:break
            game.key(ord('y'))
        assert game.character
        assert game.character['race']==name.split('-')[2]
        assert game.character['gender']==name.split('-')[3]
        assert game.character['align']==name.split('-')[4]


def test_long_run_and_death(tmp_path):
    checkpoints={};keys=[]
    original=tmp_path/'original';clean_directory(original)
    with Game(original,42) as game:
        checkpoints[game.boundary]=game.frame.digest
        for key in START:
            keys.append(key);game.key(key);checkpoints[game.boundary]=game.frame.digest
        for _ in range(10000):
            if game.exited:break
            text=game.frame.screen
            batch=b' ' if '--More--' in text or '[ynq]' in text or '[ynaq]' in text else b'm.'
            for key in batch:
                if game.exited: break
                keys.append(key);frame=game.key(key)
                if frame:checkpoints[frame.boundary]=frame.digest
        assert game.exited
        assert 'death=' in (original/'xlogfile').read_text()
    rebuilt=tmp_path/'rebuilt';clean_directory(rebuilt)
    with replay(rebuilt,42,keys,checkpoints) as game:
        assert game.exited
