import asyncio
import json

from aiohttp import WSMsgType
from nethack_run.package import verify
from nethack_run.server import Server
from test_api import wire_client, inline_workers
from test_browser import CONFIG, login

ORIGIN = {'Origin': CONFIG['public_url']}
QUIT = 'Tester-Bar-Hum-Mal-Cha\ny#quit\ny'+' '*20


async def sign_in(client, server, monkeypatch, user_id=273059):
    await login(client, server, monkeypatch, user_id)
    me = await (await client.get('/dashboard/me')).json()
    return ORIGIN | {'X-CSRF-Token': me['csrf']}


async def status(ws):
    """Skip terminal bytes until the next status message."""
    while True:
        message = await ws.receive(timeout=5)
        if message.type == WSMsgType.TEXT: return json.loads(message.data)
        assert message.type == WSMsgType.BINARY, message


async def type_keys(ws, a, keys):
    for key in keys:
        boundary = a.frame.boundary if a.frame else None
        await ws.send_str(key)
        # Wait until the key is consumed so the next one is not queued behind it.
        while (await status(ws))['status']['boundary'] == boundary and a.state == 'running':
            pass


def run(scenario):
    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_play_starts_one_game_and_resumes_it(tmp_path, monkeypatch):
    async def scenario():
        server = Server(tmp_path/'runs', github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            assert (await client.post('/dashboard/play', headers=ORIGIN)).status == 401
            headers = await sign_in(client, server, monkeypatch, 5)
            assert (await client.post('/dashboard/play')).status == 403  # CSRF required
            assert (await client.post('/dashboard/play', headers=headers | {'Origin': 'http://evil.test'})).status == 403
            response = await client.post('/dashboard/play', headers=headers)
            assert response.status == 201
            game = await response.json()
            a = server.attempts[game['id']]
            assert a.info['human_github_id'] == a.info['owner_github_id'] == 5
            assert a.info['metadata'] == {'player_type': 'human', 'github_login': 'bitplane'}
            assert game['permissions']['play'] and game['permissions']['manage']
            assert not game['permissions']['delete']  # play rights are not admin rights
            # No bot token can be issued for a person's game.
            assert not game['permissions']['recover']
            response = await client.post('/dashboard/attempts/'+a.id+'/recover-player', headers=headers)
            assert response.status == 403 and a.info['player_token_generation'] == 0
            # Playing again returns the same unfinished game, even when paused.
            assert (await client.post('/dashboard/attempts/'+a.id+'/pause', headers=headers)).status == 200
            assert a.state == 'paused'
            again = await client.post('/dashboard/play', headers=headers)
            assert again.status == 200 and (await again.json())['id'] == a.id
            assert a.state == 'running' and len(server.attempts) == 1
            # Another account gets its own game, and cannot play this one.
            other = await sign_in(client, server, monkeypatch, 6)
            theirs = await (await client.post('/dashboard/play', headers=other)).json()
            assert theirs['id'] != a.id
            rows = {r['id']: r for r in await (await client.get('/dashboard/attempts')).json()}
            assert not rows[a.id]['permissions']['play'] and rows[theirs['id']]['permissions']['play']
            # Quitting finishes the game, so the next Play starts a new one.
            headers = await sign_in(client, server, monkeypatch, 5)
            await asyncio.to_thread(a.keys, QUIT, a.session, a.frame.boundary)
            assert a.state == 'finished' and a.outcome == 'quit'
            fresh = await client.post('/dashboard/play', headers=headers)
            assert fresh.status == 201 and (await fresh.json())['id'] not in {a.id, theirs['id']}
            # Bot attempts the person owns never count as their game.
            bot = await (await client.post('/attempts', json={'owner_github_id': 6, 'player_type': 'human'})).json()
            other = await sign_in(client, server, monkeypatch, 6)
            assert (await (await client.post('/dashboard/play', headers=other)).json())['id'] == theirs['id']
            assert server.attempts[bot['id']].info['human_github_id'] is None
    run(scenario)


def test_browser_terminal_requires_same_origin_and_the_player(tmp_path, monkeypatch):
    async def scenario():
        server = Server(tmp_path/'runs', github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            headers = await sign_in(client, server, monkeypatch, 5)
            game = await (await client.post('/dashboard/play', headers=headers)).json()
            url = '/dashboard/attempts/'+game['id']+'/tty?mode=play'
            # A cross-site page can open a WebSocket with the victim's cookie;
            # the Origin check refuses it before any upgrade.
            for bad in ({}, {'Origin': 'http://evil.test'}, {'Origin': 'null'}):
                response = await client.get(url, headers=bad)
                assert response.status == 403
            bot = await (await client.post('/attempts', json={})).json()
            response = await client.get(url, headers=ORIGIN | {'Authorization': 'Bearer '+bot['owner_token']})
            assert response.status == 403  # bearer tokens never act as a browser session
            # Someone else may watch but not type.
            await sign_in(client, server, monkeypatch, 6)
            assert (await client.get(url, headers=ORIGIN)).status == 403
            a = server.attempts[game['id']]
            async with client.ws_connect(url.replace('play', 'watch'), headers=ORIGIN) as watcher:
                assert not (await status(watcher))['controlling']
                await watcher.send_str('T')
                await asyncio.sleep(.05)
                assert a.journal.counts['key'] == 0
            await client.post('/auth/logout', headers=await sign_in(client, server, monkeypatch, 6))
            assert (await client.get(url, headers=ORIGIN)).status == 401
    run(scenario)


def test_new_window_takes_control_and_logout_stops_input(tmp_path, monkeypatch):
    async def scenario():
        server = Server(tmp_path/'runs', github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            headers = await sign_in(client, server, monkeypatch, 5)
            game = await (await client.post('/dashboard/play', headers=headers)).json()
            a = server.attempts[game['id']]
            url = '/dashboard/attempts/'+a.id+'/tty?mode=play'
            first = await client.ws_connect(url, headers=ORIGIN)
            assert (await status(first))['controlling']
            await type_keys(first, a, 'Te')
            # A refresh opens a new connection before the old one has gone away.
            second = await client.ws_connect(url, headers=ORIGIN)
            assert (await status(second))['controlling']
            handover = await status(first)
            while 'taken_over' not in handover: handover = await status(first)
            assert handover['taken_over'] and not handover['controlling']
            closed = await first.receive(timeout=5)
            assert closed.type == WSMsgType.CLOSE and closed.data == 4001
            await type_keys(second, a, 'ster')
            assert bytes(e['key'] for e in a.journal.events if e['type'] == 'key') == b'Tester'
            # Signing out revokes typing on an already open terminal.
            await client.post('/auth/logout', headers=headers)
            await second.send_str('-')
            message = await second.receive(timeout=5)
            while message.type in {WSMsgType.TEXT, WSMsgType.BINARY}:
                message = await second.receive(timeout=5)
            assert message.type == WSMsgType.CLOSE and message.data == 1008
            assert a.journal.counts['key'] == 6
            await second.close()
    run(scenario)


def test_human_game_is_a_verifiable_package_and_on_the_leaderboard(tmp_path, monkeypatch):
    async def scenario():
        root = tmp_path/'runs'
        server = Server(root, github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            headers = await sign_in(client, server, monkeypatch, 5)
            game = await (await client.post('/dashboard/play', headers=headers)).json()
            a = server.attempts[game['id']]
            async with client.ws_connect('/dashboard/attempts/'+a.id+'/tty?mode=play', headers=ORIGIN) as ws:
                await status(ws)
                await type_keys(ws, a, QUIT)
            assert a.state == 'finished' and a.outcome == 'quit'
            response = await client.post('/dashboard/attempts/'+a.id+'/package', headers=headers)
            assert response.status == 200
            folder = a.package()
            manifest = json.loads((folder/'attempt.json').read_bytes())
            assert manifest['player_metadata'] == {'player_type': 'human', 'github_login': 'bitplane'}
            assert verify(folder)['verified']
            board = await (await client.get('/dashboard/leaderboard')).json()
            assert [(g['player_type'], g['player'], g['github_id']) for g in board['groups']] == [('human', 'bitplane', 5)]
            assert (await (await client.get('/dashboard/leaderboard?humans=0')).json())['groups'] == []
            assert (await client.get('/dashboard/leaderboard?humans=yes')).status == 400
    run(scenario)


def test_unfinished_game_survives_restart(tmp_path, monkeypatch):
    async def scenario():
        root = tmp_path/'runs'
        server = Server(root, github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            headers = await sign_in(client, server, monkeypatch, 5)
            game = await (await client.post('/dashboard/play', headers=headers)).json()
            await asyncio.to_thread(server.attempts[game['id']].keys, 'Test', 1, 1)
        restarted = Server(root, github_auth=CONFIG)
        async with wire_client(restarted, tmp_path) as client:
            headers = await sign_in(client, restarted, monkeypatch, 5)
            response = await client.post('/dashboard/play', headers=headers)
            assert response.status == 200 and (await response.json())['id'] == game['id']
            a = restarted.attempts[game['id']]
            assert a.state == 'running' and a.journal.counts['key'] == 4
    run(scenario)
