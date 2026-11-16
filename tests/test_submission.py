import asyncio
import base64
import gzip
import json

import pytest
from nethack_run.package import verify
from nethack_run.server import Server
from nethack_run.submission import Submitter, SubmissionError, describe
from test_api import wire_client, inline_workers
from test_browser import CONFIG, login

QUIT = 'Tester-Bar-Hum-Mal-Cha\ny#quit\ny'+' '*20


class FakeGitHub(Submitter):
    """Just enough of GitHub's Git Data and Pulls APIs, in memory."""
    def __init__(self, fail=None):
        super().__init__('bitplane/menacebench-results', 'secret-token')
        self.files = {'README.md': b'readme'}
        self.refs = {'heads/master': 'base'}
        self.commits = {'base': {'tree': 'base-tree', 'parents': []}}
        self.trees = {'base-tree': dict(self.files)}
        self.blobs = {}
        self.pulls = []
        self.calls = []
        self.fail = fail

    async def call(self, method, path, body=None, repository=None):
        self.calls.append((method, path))
        if self.fail and self.fail in path: return 500, {'message': 'boom'}
        if method == 'GET' and path == '': return 200, {'default_branch': 'master', 'permissions': {'push': True}}
        if method == 'GET' and path.startswith('/pulls'):
            head = path.split('head=')[1].split(':')[1]
            return 200, [p for p in self.pulls if p['head'] == head]
        if method == 'GET' and path.startswith('/git/ref/'):
            return 200, {'object': {'sha': self.refs[path.removeprefix('/git/ref/')]}}
        if method == 'GET' and path.startswith('/git/commits/'):
            return 200, {'tree': {'sha': self.commits[path.rsplit('/', 1)[1]]['tree']}}
        if method == 'POST' and path == '/git/blobs':
            sha = f'blob{len(self.blobs)}'; self.blobs[sha] = base64.b64decode(body['content']); return 201, {'sha': sha}
        if method == 'POST' and path == '/git/trees':
            files = dict(self.trees[body['base_tree']]) | {e['path']: self.blobs[e['sha']] for e in body['tree']}
            sha = next((sha for sha, value in self.trees.items() if value == files), f'tree{len(self.trees)}')
            self.trees[sha] = files; return 201, {'sha': sha}
        if method == 'POST' and path == '/git/commits':
            sha = f'commit{len(self.commits)}'; self.commits[sha] = body; return 201, {'sha': sha}
        if method == 'POST' and path == '/git/refs':
            name = body['ref'].removeprefix('refs/')
            if name in self.refs: return 422, {'message': 'Reference already exists'}
            self.refs[name] = body['sha']; return 201, {}
        if method == 'PATCH' and path.startswith('/git/refs/'):
            self.refs[path.removeprefix('/git/refs/')] = body['sha']; return 200, {}
        if method == 'POST' and path == '/pulls':
            pull = {'number': len(self.pulls)+1, 'html_url': f'https://github.com/bitplane/menacebench-results/pull/{len(self.pulls)+1}',
                    'head': body['head'], 'base': body['base'], 'title': body['title'], 'body': body['body']}
            self.pulls.append(pull); return 201, pull
        raise AssertionError((method, path))

    def branch_files(self, branch):
        return self.trees[self.commits[self.refs['heads/'+branch]]['tree']]


def bind(github, token):
    github.token = token
    return github


async def sign_in(client, server, monkeypatch, user_id):
    await login(client, server, monkeypatch, user_id)
    me = await (await client.get('/dashboard/me')).json()
    return {'Origin': CONFIG['public_url'], 'X-CSRF-Token': me['csrf']}


def test_owner_submits_a_finished_run_as_a_pull_request(tmp_path, monkeypatch):
    async def scenario():
        github = FakeGitHub()
        monkeypatch.setattr('nethack_run.browser.Submitter', lambda repo, token: bind(github, token))
        server = Server(tmp_path/'runs', github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            created = await (await client.post('/attempts', json={
                'owner_github_id': 77, 'model': 'Model-X', 'reasoning_effort': 'high'})).json()
            a = server.attempts[created['id']]
            url = '/dashboard/attempts/'+a.id+'/submit'
            headers = await sign_in(client, server, monkeypatch, 77)
            assert (await client.post(url, headers=headers)).status == 409  # still running
            await asyncio.to_thread(a.keys, QUIT, 1, 1)
            row = await (await client.get('/dashboard/attempts/'+a.id)).json()
            assert row['permissions']['submit'] and row['submission'] is None
            # Other signed-in users cannot submit someone else's run.
            stranger = await sign_in(client, server, monkeypatch, 78)
            assert (await client.post(url, headers=stranger)).status == 403
            headers = await sign_in(client, server, monkeypatch, 77)
            assert (await client.post(url)).status == 403  # CSRF still required
            response = await client.post(url, headers=headers)
            assert response.status == 200
            row = await response.json()
            assert github.token == 'oauth-token-77'
            assert row['submission']['url'].endswith('/pull/1') and not row['permissions']['submit']
            [pull] = github.pulls
            assert pull['head'] == 'attempt/'+a.id and pull['base'] == 'master'
            assert pull['title'] == 'Model-X (high): quit, score 0'
            assert '[bitplane](https://github.com/bitplane)' in pull['body'] and '@' not in pull['body']
            # Only the two package files are added, exactly as finalized.
            files = github.branch_files('attempt/'+a.id)
            assert set(files) == {'README.md', f'attempts/{a.id}/attempt.json', f'attempts/{a.id}/events.jsonl.gz'}
            assert files[f'attempts/{a.id}/attempt.json'] == (a.path/'package'/'attempt.json').read_bytes()
            assert gzip.decompress(files[f'attempts/{a.id}/events.jsonl.gz']) == (a.path/'events.jsonl').read_bytes()
            assert verify(a.path/'package')['verified']
            commit = github.commits[github.refs['heads/attempt/'+a.id]]
            assert 'Co-authored-by: bitplane <77+bitplane@users.noreply.github.com>' in commit['message']
            assert commit['parents'] == ['base']
            # Submitting again is idempotent and the record survives a restart.
            calls = len(github.calls)
            assert (await (await client.post(url, headers=headers)).json())['submission']['number'] == 1
            assert len(github.calls) == calls and len(github.pulls) == 1
            assert 'secret-token' not in (a.path/'submission.json').read_text()
        restarted = Server(tmp_path/'runs', github_auth=CONFIG)
        async with wire_client(restarted, tmp_path) as client:
            await sign_in(client, restarted, monkeypatch, 77)
            [row] = await (await client.get('/dashboard/attempts')).json()
            assert row['submission']['number'] == 1 and row['submission']['actor_github_id'] == 77
    asyncio.run(scenario())


def test_human_player_and_admin_can_submit_and_failures_are_retryable(tmp_path, monkeypatch):
    async def scenario():
        github = FakeGitHub(fail='/pulls')
        monkeypatch.setattr('nethack_run.browser.Submitter', lambda repo, token: bind(github, token))
        server = Server(tmp_path/'runs', github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            headers = await sign_in(client, server, monkeypatch, 5)
            game = await (await client.post('/dashboard/play', headers=headers)).json()
            a = server.attempts[game['id']]
            await asyncio.to_thread(a.keys, QUIT, 1, 1)
            url = '/dashboard/attempts/'+a.id+'/submit'
            response = await client.post(url, headers=headers)
            assert response.status == 502 and 'GitHub' in await response.text()
            assert 'secret-token' not in await response.text()
            assert a.submission is None and a.id not in server.submitting
            # A retry after the failure reuses the branch name and opens the PR.
            github.fail = None
            # Retry as an admin uses that admin's own OAuth token.
            admin = await sign_in(client, server, monkeypatch, CONFIG['owner_ids'][0])
            row = await (await client.post(url, headers=admin)).json()
            assert row['submission']['number'] == 1
            assert github.pulls[0]['title'] == 'bitplane (human): quit, score 0'
            assert github.token == 'oauth-token-'+str(CONFIG['owner_ids'][0])
    asyncio.run(scenario())


@pytest.mark.parametrize('credentials', [{}, {'token': 'expired', 'expires': 0}])
def test_submission_requires_reauthorization_without_session_token(tmp_path, monkeypatch, credentials):
    async def scenario():
        server = Server(tmp_path/'runs', github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            created = await (await client.post('/attempts', json={'owner_github_id': 77})).json()
            a = server.attempts[created['id']]
            await asyncio.to_thread(a.keys, QUIT, 1, 1)
            headers = await sign_in(client, server, monkeypatch, 77)
            row = await (await client.get('/dashboard/attempts/'+a.id)).json()
            assert row['permissions']['submit']
            for session in server.browser.sessions.values(): session['github'] = credentials
            assert (await client.post('/dashboard/attempts/'+a.id+'/submit', headers=headers)).status == 401
            assert not (a.path/'package').exists()
    asyncio.run(scenario())


def test_submitter_rejects_bad_configuration():
    for repository in ('', 'no-slash', 'a/b/c', '../x'):
        with pytest.raises(ValueError): Submitter(repository, 'token')
    with pytest.raises(ValueError): Submitter('a/b', '  \n')


def test_description_marks_development_builds():
    manifest = {'attempt_id': 'x', 'result': None, 'outcome': 'abandoned', 'character': None, 'key_count': 3,
                'image_digest': None, 'server_instance': 'local', 'harness_version': None,
                'player_metadata': {'player_type': 'model', 'model': None}}
    title, body, message = describe(manifest, 'someone', 9)
    assert title == 'model: abandoned' and 'development build' in body
    assert message.endswith('Co-authored-by: someone <9+someone@users.noreply.github.com>\n')


class ForkGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.fork_created = False
        self.ready = False
        self.destinations = []
        self.fail_pull = False

    async def call(self, method, path, body=None, repository=None):
        self.destinations.append((method, repository or self.repository, path))
        if method == 'GET' and path == '':
            if repository:
                if not self.fork_created: return 404, {}
                return 200, {'full_name': 'alice/menacebench-results', 'fork': True,
                             'owner': {'id': 77}, 'parent': {'full_name': self.repository}}
            return 200, {'default_branch': 'master', 'permissions': {'push': False}}
        if method == 'POST' and path == '/forks':
            self.fork_created = True
            return 202, {'full_name': 'alice/menacebench-results', 'fork': True, 'owner': {'id': 77}}
        if repository and path == '/git/commits/base' and not self.ready:
            self.ready = True
            return 404, {}
        if method == 'POST' and path == '/pulls' and self.fail_pull:
            return 500, {'message': 'retry later'}
        # The fake stores full cross-repository heads, unlike its direct-write base.
        if method == 'GET' and path.startswith('/pulls'):
            head = path.split('head=')[1]
            return 200, [p for p in self.pulls if p['head'] == head]
        return await super().call(method, path, body, repository)


def test_fork_submission_waits_retries_and_does_not_duplicate(tmp_path, monkeypatch):
    from test_results import package
    folder = package(tmp_path/'runs', tmp_path/'results', {'model': 'Fork tester'})
    async def no_wait(seconds): pass
    monkeypatch.setattr('nethack_run.submission.asyncio.sleep', no_wait)
    async def scenario():
        github = ForkGitHub()
        github.fail_pull = True
        with pytest.raises(SubmissionError): await github.submit(folder, 'alice', 77)
        assert github.fork_created and github.ready
        branch = 'heads/attempt/'+folder.name
        first_commit = github.refs[branch]
        github.fail_pull = False
        result = await github.submit(folder, 'alice', 77)
        assert result['number'] == 1 and github.refs[branch] == first_commit
        assert github.pulls[0]['head'] == 'alice:attempt/'+folder.name
        assert await github.submit(folder, 'alice', 77) == result
        assert len(github.pulls) == 1
        writes = [(repo, path) for method, repo, path in github.destinations
                  if method == 'POST' and path.startswith('/git/')]
        assert writes and all(repo == 'alice/menacebench-results' for repo, _ in writes)
        assert not any(method == 'PATCH' for method, _, _ in github.destinations)
    asyncio.run(scenario())


def test_revoked_authorization_clears_session_credentials(tmp_path, monkeypatch):
    async def scenario():
        github = FakeGitHub()
        async def revoked(*args, **kwargs): return 401, {'message': 'Bad credentials'}
        monkeypatch.setattr(github, 'call', revoked)
        monkeypatch.setattr('nethack_run.browser.Submitter', lambda repo, token: bind(github, token))
        server = Server(tmp_path/'runs', github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            created = await (await client.post('/attempts', json={'owner_github_id': 77})).json()
            a = server.attempts[created['id']]
            await asyncio.to_thread(a.keys, QUIT, 1, 1)
            headers = await sign_in(client, server, monkeypatch, 77)
            response = await client.post('/dashboard/attempts/'+a.id+'/submit', headers=headers)
            assert response.status == 401 and 'Sign in again' in await response.text()
            assert all('github' not in s for s in server.browser.sessions.values())
            assert a.submission is None and not server.submitting
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['wrong_owner', 'unrelated', 'not_ready', 'branch_conflict'])
def test_fork_failures_do_not_overwrite_repositories(tmp_path, monkeypatch, fault):
    from test_results import package
    folder = package(tmp_path/'runs', tmp_path/'results', {'model': 'Fork tester'})
    async def no_wait(seconds): pass
    monkeypatch.setattr('nethack_run.submission.asyncio.sleep', no_wait)
    async def scenario():
        github = ForkGitHub()
        github.fork_created = True
        original = github.call
        async def call(method, path, body=None, repository=None):
            status, data = await original(method, path, body, repository)
            if repository and path == '':
                if fault == 'wrong_owner': data['owner']['id'] = 99
                if fault == 'unrelated': data['parent']['full_name'] = 'alice/other'
            if repository and path == '/git/commits/base' and fault == 'not_ready': return 404, {}
            return status, data
        monkeypatch.setattr(github, 'call', call)
        if fault == 'branch_conflict': github.refs['heads/attempt/'+folder.name] = 'base'
        with pytest.raises(SubmissionError): await github.submit(folder, 'alice', 77)
        assert not github.pulls
        assert not any(method == 'PATCH' for method, _, _ in github.destinations)
        if fault != 'branch_conflict':
            assert not any(method == 'POST' and path.startswith('/git/') for method, _, path in github.destinations)
        else:
            assert github.refs['heads/attempt/'+folder.name] == 'base'
    asyncio.run(scenario())
