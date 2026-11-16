import gzip
import json
import shutil
import subprocess

import pytest
from nethack_run import results
from nethack_run.attempt import Attempt
from nethack_run.cli import main
from nethack_run.package import verify
from nethack_run.results import build_site, changed_attempts, check, check_changes

QUIT = 'Tester-Bar-Hum-Mal-Cha\ny#quit\ny'+' '*20
IMAGE = 'ghcr.io/bitplane/menacebench@sha256:'+'a'*64


def package(root, results_root, metadata, owner=None, image=None, monkeypatch=None):
    """A finished run's package, placed at results_root/attempts/<id>/ as a PR would add it."""
    root.mkdir(parents=True, exist_ok=True)
    if image: monkeypatch.setenv('NETHACK_IMAGE_DIGEST', image)
    a = Attempt.create(root, metadata, 'test', owner_github_id=owner)[0]
    a.keys(QUIT, 1, 1)
    if monkeypatch: monkeypatch.delenv('NETHACK_IMAGE_DIGEST', raising=False)
    target = results_root/'attempts'/a.id
    shutil.copytree(a.package(), target)
    return target


@pytest.fixture
def official(monkeypatch):
    monkeypatch.setattr(results, 'official_images', lambda: [IMAGE])
    replays = []
    def dispatch(folder, output=None):
        # Stand-in for the container replay: verify natively under the image's identity.
        replays.append(folder.name)
        monkeypatch.setenv('NETHACK_IMAGE_DIGEST', IMAGE)
        try: verify(folder)
        finally: monkeypatch.delenv('NETHACK_IMAGE_DIGEST')
        return True
    monkeypatch.setattr('nethack_run.images.dispatch', dispatch)
    return replays


def test_check_classifies_packages(tmp_path, monkeypatch, official):
    repo = tmp_path/'results'
    dev = package(tmp_path/'runs', repo, {'model': 'Dev'})
    pinned = package(tmp_path/'runs', repo, {'model': 'Pinned'}, image=IMAGE, monkeypatch=monkeypatch)
    unpublished = package(tmp_path/'runs', repo, {'model': 'Old'}, image='sha256:'+'b'*64, monkeypatch=monkeypatch)
    assert check(dev)['status'] == 'unverified' and 'development' in check(dev)['reason']
    assert check(unpublished)['status'] == 'unverified' and 'not published' in check(unpublished)['reason']
    assert check(pinned) | {'reason': None} == {'attempt_id': pinned.name, 'status': 'verified', 'reason': None, 'image_digest': IMAGE}
    assert official == [pinned.name]  # only official images are replayed
    # A tampered log fails before any replay; so does a misnamed folder.
    data = gzip.decompress((dev/'events.jsonl.gz').read_bytes()).replace(b'"key":', b'"key" :', 1)
    (dev/'events.jsonl.gz').write_bytes(gzip.compress(data))
    assert check(dev)['status'] == 'failed' and 'digest' in check(dev)['reason']
    moved = pinned.rename(pinned.with_name('00000000-0000-4000-8000-000000000000'))
    assert check(moved)['status'] == 'failed'
    # A replay that diverges in its image fails.
    def diverge(folder, output=None): raise subprocess.CalledProcessError(1, 'podman')
    monkeypatch.setattr('nethack_run.images.dispatch', diverge)
    moved.rename(pinned)
    assert check(pinned)['status'] == 'failed' and 'Replay failed' in check(pinned)['reason']


def test_check_replays_natively_inside_recorded_image(tmp_path, monkeypatch):
    folder = package(tmp_path/'runs', tmp_path/'results', {'model': 'Native'},
                     image=IMAGE, monkeypatch=monkeypatch)
    monkeypatch.setattr(results, 'official_images', lambda: [IMAGE])
    monkeypatch.setenv('NETHACK_IMAGE_DIGEST', IMAGE)
    # Real dispatch returns False inside the recorded image; check must replay locally.
    assert check(folder)['status'] == 'verified'
    manifest_path = folder/'attempt.json'
    manifest = json.loads(manifest_path.read_bytes())
    manifest['total_duration'] += 1
    manifest_path.write_text(json.dumps(manifest))
    outcome = check(folder)
    assert outcome['status'] == 'failed' and 'duration mismatch' in outcome['reason']


def test_pull_request_path_rules():
    ident = '6f1c0f7e-2b1d-4c55-9a0e-2a9f0d7e8b11'
    good = [f'A\tattempts/{ident}/attempt.json', f'A\tattempts/{ident}/events.jsonl.gz', f'M\tattempts/{ident}/disqualified.json']
    assert check_changes(good) == [] and changed_attempts(good) == [ident]
    bad = {'A\t.github/workflows/check.yml': 'only attempts', 'M\tREADME.md': 'only attempts',
           f'A\tattempts/{ident}/replay.cast': 'only attempts', f'A\tattempts/{ident.upper()}/attempt.json': 'only attempts',
           'A\tattempts/../x/attempt.json': 'only attempts', f'M\tattempts/{ident}/events.jsonl.gz': 'immutable',
           f'D\tattempts/{ident}/attempt.json': 'immutable'}
    for line, problem in bad.items():
        [message] = check_changes([line])
        assert problem in message


def test_site_ranks_only_verified_and_escapes_everything(tmp_path, monkeypatch, official):
    repo = tmp_path/'results'
    human = package(tmp_path/'runs', repo, {'player_type': 'human', 'github_login': 'alice'}, owner=5, image=IMAGE, monkeypatch=monkeypatch)
    model = package(tmp_path/'runs', repo, {'model': '<script>alert(1)</script>', 'reasoning_effort': 'high'}, image=IMAGE, monkeypatch=monkeypatch)
    dev = package(tmp_path/'runs', repo, {'model': 'DevOnly'})
    cheat = package(tmp_path/'runs', repo, {'model': 'Cheater'}, image=IMAGE, monkeypatch=monkeypatch)
    (cheat/'disqualified.json').write_text(json.dumps({'reason': 'Read the <source>'}))
    broken = repo/'attempts'/'11111111-1111-4111-8111-111111111111'; broken.mkdir()
    (broken/'attempt.json').write_text('{}')
    statuses = {p.name: check(p) for p in (repo/'attempts').iterdir()}
    assert statuses[broken.name]['status'] == 'failed'
    out = tmp_path/'site'
    data = build_site(repo, out, statuses, 'https://github.com/bitplane/menacebench-results/tree/master', 'menace.bitplane.net')
    board = data['leaderboard']['with_humans']['groups']
    assert sorted((g['player_type'], g['player']) for g in board) == [('human', 'alice'), ('model', '<script>alert(1)</script>')]
    assert [g for g in board if g['player'] == 'alice'][0]['github_id'] == 5
    assert [g['player'] for g in data['leaderboard']['models_only']['groups']] == ['<script>alert(1)</script>']
    assert {a['manifest']['attempt_id'] for a in data['attempts']} == {human.name, model.name, dev.name, cheat.name}
    index = (out/'index.html').read_text()
    assert '<script>alert' not in index and '&lt;script&gt;alert(1)&lt;/script&gt;' in index
    assert 'DevOnly' in index and 'unverified' in index and 'Cheater' not in index
    assert f'tree/master/attempts/{human.name}' in index
    disqualified = (out/'disqualified.html').read_text()
    assert 'Cheater' in disqualified and 'Read the &lt;source&gt;' in disqualified
    assert (out/'CNAME').read_text() == 'menace.bitplane.net\n' and (out/'.nojekyll').exists()


def git(repo, *args):
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t', *args], check=True, capture_output=True)


def test_cli_checks_only_a_pull_requests_changes(tmp_path, monkeypatch, official, capsys):
    repo = tmp_path/'results'; repo.mkdir()
    git(repo, 'init', '-q', '-b', 'master'); (repo/'README.md').write_text('results')
    old = package(tmp_path/'runs', repo, {'model': 'Old'})
    git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'base')
    new = package(tmp_path/'runs', repo, {'model': 'New'}, image=IMAGE, monkeypatch=monkeypatch)
    git(repo, 'checkout', '-qb', 'pr'); git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'add')
    out = tmp_path/'status.json'
    main(['check', '--results', str(repo), '--changed-from', 'master', '--output', str(out)])
    assert set(json.loads(out.read_text())) == {new.name} and official == [new.name]
    # Every package, for the site build.
    main(['check', '--results', str(repo), '--output', str(out)])
    assert set(json.loads(out.read_text())) == {old.name, new.name}
    main(['build-site', str(repo), '--status', str(out), '--output', str(tmp_path/'site')])
    assert 'New' in (tmp_path/'site'/'index.html').read_text()
    # A pull request touching anything else is rejected outright.
    (repo/'README.md').write_text('changed'); git(repo, 'commit', '-qam', 'sneaky')
    with pytest.raises(SystemExit) as exit:
        main(['check', '--results', str(repo), '--changed-from', 'master'])
    assert exit.value.code == 1 and 'README.md' in capsys.readouterr().err
