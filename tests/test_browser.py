import asyncio
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from nethack_run.server import Server
from test_api import wire_client, inline_workers

CONFIG = {'client_id': 'test-client', 'client_secret': 'test-secret',
          'owner_ids': [273059], 'public_url': 'http://127.0.0.1:8756'}


async def login(client, server, monkeypatch, user_id=273059):
    response = await client.get('/auth/github', allow_redirects=False, headers={'Host': urlsplit(server.browser.origin).netloc})
    assert response.status == 302
    params = parse_qs(urlsplit(response.headers['Location']).query, keep_blank_values=True)
    assert params['redirect_uri'] == [CONFIG['public_url']+'/auth/github/callback']
    assert params['scope'] == ['public_repo']
    state = params['state'][0]
    async def identity(code, verifier):
        assert code == 'test-code'
        assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode() == params['code_challenge'][0]
        return {'id': user_id, 'login': 'bitplane'}, {'token': 'oauth-token-'+str(user_id)}
    monkeypatch.setattr(server.browser, 'identity', identity)
    response = await client.get('/auth/github/callback', params={'code': 'test-code', 'state': state}, allow_redirects=False)
    return response, state


def test_owner_login_browse_controls_and_logout(tmp_path, monkeypatch):
    async def scenario():
        server = Server(tmp_path/'runs', github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            assert 'Sign in with GitHub' in await (await client.get('/')).text()
            assert (await client.get('/dashboard/attempts')).status == 401
            created = await (await client.post('/attempts', json={'model': '<script>bad</script>'})).json()
            response, state = await login(client, server, monkeypatch)
            assert response.status == 302
            assert response.cookies['nethack_owner']['httponly']
            assert response.cookies['nethack_owner']['samesite'] == 'Lax'
            assert (await client.get('/auth/github/callback', params={'code': 'test-code', 'state': state})).status == 400
            me = await (await client.get('/dashboard/me')).json()
            assert me['id'] == 273059
            rows = await (await client.get('/dashboard/attempts')).json()
            assert len(rows) == 1 and rows[0]['id'] == created['id']
            assert 'seed' not in json.dumps(rows) and 'token' not in json.dumps(rows)
            for path in ['/leaderboard', '/games/'+created['id']]:
                page = await client.get(path)
                assert page.status == 200 and 'id="games-nav"' in await page.text()
                assert page.headers['Cache-Control'] == 'no-store'
            base = '/dashboard/attempts/'+created['id']
            assert (await client.get(base)).status == 200
            assert (await client.get('/attempts/'+created['id'])).status == 401
            assert (await client.post(base+'/pause')).status == 403
            headers = {'Origin': CONFIG['public_url'], 'X-CSRF-Token': me['csrf']}
            assert (await client.post(base+'/pause', headers=headers | {'Origin': 'http://evil.test'})).status == 403
            assert (await client.post(base+'/pause', headers=headers)).status == 200
            assert server.attempts[created['id']].state == 'paused'
            assert (await client.post(base+'/resume', headers=headers)).status == 200
            assert (await client.post(base+'/package', headers=headers)).status == 409
            player = {'Authorization': 'Bearer '+created['player_token']}
            assert (await client.get('/dashboard/attempts', headers=player)).status == 403
            assert (await client.get(base, headers=player)).status == 403
            assert (await client.post(base+'/pause', headers=headers)).status == 200
            server.attempts[created['id']].expire()
            package = await client.post(base+'/package', headers=headers)
            assert package.status == 200 and package.content_type == 'application/x-tar'
            assert (await client.post('/auth/logout', headers=headers)).status == 200
            assert (await client.get('/dashboard/attempts')).status == 401
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['bad-nonce', 'expired', 'cancelled'])
def test_login_rejects_invalid_authorization(tmp_path, monkeypatch, mode):
    async def scenario():
        server = Server(tmp_path/'runs', github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            response = await client.get('/auth/github', allow_redirects=False, headers={'Host': urlsplit(server.browser.origin).netloc})
            state = parse_qs(urlsplit(response.headers['Location']).query)['state'][0]
            if mode == 'expired': server.browser.pending[state]['expires'] = 0
            if mode == 'bad-nonce': client.cookie_jar.clear()
            params = {'state': state, 'code': 'test-code'}
            if mode == 'cancelled': params['error'] = 'access_denied'
            assert (await client.get('/auth/github/callback', params=params, allow_redirects=False)).status == 400
            assert not server.browser.sessions
            assert (await client.get('/dashboard/attempts')).status == 401
    asyncio.run(scenario())


def test_browser_disabled_without_config(tmp_path):
    async def scenario():
        server = Server(tmp_path/'runs')
        async with wire_client(server, tmp_path) as client:
            assert (await client.get('/auth/github')).status == 503
            assert (await client.get('/dashboard/attempts')).status == 401
            assert 'not configured' in await (await client.get('/')).text()
    asyncio.run(scenario())


def test_owner_replay_preparation_download_and_player_isolation(tmp_path, monkeypatch):
    async def scenario():
        server = Server(tmp_path/'runs', github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            created = await (await client.post('/agent/start', json={})).json()
            url = '/dashboard/attempts/'+created['id']+'/replay'
            assert (await client.get(url)).status == 401
            response, _ = await login(client, server, monkeypatch)
            assert response.status == 302
            me = await (await client.get('/dashboard/me')).json()
            headers = {'Origin': CONFIG['public_url'], 'X-CSRF-Token': me['csrf']}
            assert (await client.post(url, headers=headers)).status == 409
            assert (await client.get(url)).status == 409
            a = server.attempts[created['id']]
            a.keys('Tester-Bar-Hum-Mal-Cha\ny#quit\ny'+' '*20, 1, 1)
            assert a.state == 'finished'
            assert (await client.post(url)).status == 403
            release = asyncio.Event()
            builds = []
            original = server.browser.build_recording
            async def delayed(attempt):
                builds.append(attempt.id)
                await release.wait()
                await original(attempt)
            monkeypatch.setattr(server.browser, 'build_recording', delayed)
            assert (await client.post(url, headers=headers)).status == 202
            assert (await client.post(url, headers=headers)).status == 202
            assert builds == [a.id]
            release.set()
            await server.browser.replays[a.id]
            assert (await (await client.post(url, headers=headers)).json())['status'] == 'ready'
            response = await client.get(url)
            assert response.status == 200
            assert json.loads((await response.text()).splitlines()[0])['version'] == 3
            assert response.headers['Content-Encoding'] == 'gzip'
            assert response.headers['Content-Type'] == 'application/x-asciicast'
            assert 'Accept-Encoding' in response.headers['Vary']
            download = await client.get(url+'?download=1')
            assert a.id+'.cast' in download.headers['Content-Disposition']
            # Clients without gzip support get the same recording decompressed.
            plain = await client.get(url+'?download=1', headers={'Accept-Encoding': 'identity'})
            assert plain.status == 200 and 'Content-Encoding' not in plain.headers
            assert plain.headers['Content-Type'] == 'application/x-asciicast'
            assert a.id+'.cast' in plain.headers['Content-Disposition']
            assert plain.headers['Content-Security-Policy'].startswith("default-src 'self'")
            assert await plain.text() == await response.text()
            player = {'Authorization':'Bearer '+created['player_token']}
            assert (await client.get(url, headers=player)).status == 403
            assert not (a.path/'package').exists()
            a.report_usage({'cost': 0.1})
    asyncio.run(scenario())


def test_login_normalizes_hostname_before_setting_cookie(tmp_path):
    async def scenario():
        server = Server(tmp_path/'runs', github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            response = await client.get('/auth/github', headers={'Host': 'localhost:8756'}, allow_redirects=False)
            assert response.status == 302
            assert response.headers['Location'] == CONFIG['public_url']+'/auth/github'
            assert not response.cookies and not server.browser.pending
    asyncio.run(scenario())


def test_agent_can_start_from_public_instructions_without_owner_access(tmp_path):
    async def scenario():
        server = Server(tmp_path/'runs', github_auth=CONFIG, concurrency=1)
        async with wire_client(server, tmp_path) as client:
            home = await (await client.get('/')).text()
            assert '/player.md' in home and '/agent/start' in home and 'Sign in with GitHub' in home
            instructions = await (await client.get('/player.md')).text()
            assert '/agent/start' in instructions and 'BASE_URL' in instructions
            response = await client.post('/agent/start', json={'player_type':'model','agent_tool':'Codex','model':'Luna'})
            assert response.status == 201
            created = await response.json()
            assert 'owner_token' not in created and 'seed' not in json.dumps(created)
            base = created['attempt_path']
            player = {'Authorization':'Bearer '+created['player_token']}
            screen = await client.get(base+'/screen', headers=player)
            position = json.loads((await screen.text()).splitlines()[-1])
            response = await client.post(base+'/keys', headers=player, json={
                k: position[k] for k in ('session','boundary')} | {'keys':'Luna\n'})
            assert response.status == 200
            assert (await client.get(base+'/package', headers=player)).status == 403
            assert (await client.get('/dashboard/attempts', headers=player)).status == 403
            assert (await client.post('/agent/start', headers=player, json={})).status == 403
            assert (await client.post('/agent/start', json={})).status == 429
    asyncio.run(scenario())


def test_agent_bootstrap_respects_hosted_contributor_key(tmp_path):
    async def scenario():
        server = Server(tmp_path/'runs', contributors={hashlib.sha256(b'contributor').hexdigest():'tester'})
        async with wire_client(server, tmp_path) as client:
            assert (await client.post('/agent/start', json={})).status == 401
            response = await client.post('/agent/start', json={}, headers={'Authorization':'Bearer contributor'})
            assert response.status == 201 and 'owner_token' not in await response.json()
    asyncio.run(scenario())


def test_session_expiry_and_secure_cookie(tmp_path, monkeypatch):
    async def scenario():
        server = Server(tmp_path/'runs', github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            response, _ = await login(client, server, monkeypatch)
            assert response.status == 302
            for session in server.browser.sessions.values(): session['expires'] = 0
            assert (await client.get('/dashboard/me')).status == 401
            server.browser.origin = 'https://runs.example.test'
            response = await client.get('/auth/github', allow_redirects=False, headers={'Host': urlsplit(server.browser.origin).netloc})
            assert response.cookies['nethack_oauth']['secure']
    asyncio.run(scenario())


@pytest.mark.parametrize('changes', [
    {'owner_ids': []}, {'public_url': 'http://evil.test'},
    {'public_url': 'http://127.0.0.1:8756/path'}, {'client_secret': 'YOUR_SECRET'}])
def test_invalid_owner_configuration_fails_closed(tmp_path, changes):
    with pytest.raises(ValueError): Server(tmp_path, github_auth=CONFIG | changes)


def test_any_github_user_can_view_but_only_owner_can_control(tmp_path, monkeypatch):
    async def scenario():
        server = Server(tmp_path/'runs', github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            created = await (await client.post('/agent/start', json={})).json()
            a = server.attempts[created['id']]
            base = '/dashboard/attempts/'+a.id
            response, _ = await login(client, server, monkeypatch, user_id=999)
            assert response.status == 302
            me = await (await client.get('/dashboard/me')).json()
            assert me['role'] == 'viewer'
            headers = {'Origin': CONFIG['public_url'], 'X-CSRF-Token': me['csrf']}
            assert (await client.get('/')).status == 200
            for path in ['/dashboard/attempts', '/dashboard/leaderboard', base]:
                assert (await client.get(path)).status == 200
            for action in ['pause', 'resume', 'package']:
                assert (await client.post(base+'/'+action, headers=headers)).status == 403
            assert a.state == 'running'
            # A valid viewer cookie does not elevate an agent bearer token.
            player = {'Authorization': 'Bearer '+created['player_token']}
            assert (await client.get(base, headers=player)).status == 403
            a.keys('Tester-Bar-Hum-Mal-Cha\ny#quit\ny'+' '*20, 1, 1)
            assert a.state == 'finished'
            assert (await client.post(base+'/package', headers=headers)).status == 403
            assert not (a.path/'package').exists()
            assert (await client.post(base+'/replay')).status == 403  # CSRF still required
            assert (await client.post(base+'/replay', headers=headers)).status == 202
            # A successful build removes its task, possibly before we look.
            if task := server.browser.replays.get(a.id): await task
            assert (await client.post(base+'/replay', headers=headers)).status == 200
            assert (await client.get(base+'/replay')).status == 200
            assert (await client.get(base+'/replay?download=1')).status == 200
            assert (await client.post('/auth/logout', headers=headers)).status == 200
            assert (await client.get(base)).status == 401
            response, _ = await login(client, server, monkeypatch)
            assert response.status == 302
            me = await (await client.get('/dashboard/me')).json()
            assert me['role'] == 'admin'
            headers['X-CSRF-Token'] = me['csrf']
            assert (await client.post(base+'/package', headers=headers)).status == 200
    asyncio.run(scenario())


@pytest.mark.parametrize('scope,authorized', [('public_repo', True), ('repo', True), ('', False), ('read:user', False)])
def test_oauth_exchange_retains_only_authorized_session_credentials(monkeypatch, scope, authorized):
    from nethack_run.browser import Browser
    calls = []
    class Response:
        def __init__(self, body): self.body = body
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def raise_for_status(self): pass
        async def json(self): return self.body
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def post(self, url, **kwargs):
            calls.append(kwargs)
            return Response({'access_token': 'private-token', 'scope': scope, 'expires_in': 3600})
        def get(self, url, **kwargs):
            assert kwargs['headers']['Authorization'] == 'Bearer private-token'
            return Response({'id': 77, 'login': 'alice'})
    monkeypatch.setattr('nethack_run.browser.ClientSession', lambda **kwargs: Client())
    identity, credentials = asyncio.run(Browser(None, CONFIG).identity('code', 'verifier'))
    assert identity == {'id': 77, 'login': 'alice'}
    assert calls[0]['data']['code_verifier'] == 'verifier'
    if authorized:
        assert credentials['token'] == 'private-token' and credentials['expires'] > 0
    else:
        assert credentials == {}
