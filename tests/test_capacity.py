import asyncio
import json

import pytest
from nethack_run.server import Server
from test_api import wire_client, inline_workers
from test_browser import CONFIG, login


@pytest.mark.parametrize('release', ['pause', 'save'])
def test_slots_and_all_resume_paths(tmp_path, monkeypatch, release):
    async def scenario():
        server = Server(tmp_path/'runs', concurrency=1, github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            first = await (await client.post('/attempts', json={})).json()
            base = '/attempts/'+first['id']
            player = {'Authorization': 'Bearer '+first['player_token']}
            a = server.attempts[first['id']]
            if release == 'pause':
                assert (await client.post(base+'/pause', headers=player)).status == 200
                assert (await client.post(base+'/pause', headers=player)).status == 200
            else:
                response = await client.post(base+'/keys', headers=player, json={
                    'session': 1, 'boundary': 1, 'keys': 'Tester-Bar-Hum-Mal-Cha\ny..Sy '})
                assert json.loads((await response.text()).splitlines()[-1])['state'] == 'saved'
            second = await (await client.post('/attempts', json={})).json()
            assert (await client.post(base+'/resume', headers=player)).status == 429
            assert a.state == ('paused' if release == 'pause' else 'saved')
            assert (await client.post('/attempts/'+second['id']+'/pause', headers=player)).status == 403
            await login(client, server, monkeypatch)
            me = await (await client.get('/dashboard/me')).json()
            headers = {'Origin': CONFIG['public_url'], 'X-CSRF-Token': me['csrf']}
            assert (await client.post('/dashboard/attempts/'+first['id']+'/resume', headers=headers)).status == 429
            owner = {'Authorization': 'Bearer '+first['owner_token']}
            async with client.ws_connect(base+'/tty?mode=play', headers=owner) as ws:
                if a.frame: await ws.receive(timeout=5)  # terminal snapshot
                await ws.receive_json()  # status
                await ws.send_str('x')
                error = await ws.receive_json(timeout=5)
                assert error['code'] == 429
                assert 'Running game limit' in error['error']
            second_player = {'Authorization': 'Bearer '+second['player_token']}
            assert (await client.post('/attempts/'+second['id']+'/pause', headers=second_player)).status == 200
            # Two competing resumes must never exceed the one-process cap.
            responses = await asyncio.gather(
                client.post(base+'/resume', headers=player),
                client.post('/attempts/'+second['id']+'/resume', headers=second_player))
            assert sorted(r.status for r in responses) == [200, 429]
            assert sum(a.state == 'running' for a in server.attempts.values()) == 1
    asyncio.run(scenario())


def test_concurrent_creation_respects_capacity(tmp_path):
    async def scenario():
        server = Server(tmp_path/'runs', concurrency=2)
        async with wire_client(server, tmp_path) as client:
            responses = await asyncio.gather(*(client.post('/agent/start', json={}) for _ in range(5)))
            assert sorted(r.status for r in responses) == [201, 201, 429, 429, 429]
            assert len(server.attempts) == 2
    asyncio.run(scenario())
