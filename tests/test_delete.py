import asyncio
import json

from nethack_run.server import Server
from test_api import wire_client, inline_workers
from test_browser import CONFIG
from test_ownership import sign_in, bearer


def test_only_admin_can_delete_and_other_runs_survive(tmp_path, monkeypatch):
    async def scenario():
        root = tmp_path/'runs'; server = Server(root, github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            created = await (await client.post('/attempts', json={'owner_github_id': 999})).json()
            keep = await (await client.post('/agent/start', json={})).json()
            a = server.attempts[created['id']]; process = a.game.process
            endpoint = '/dashboard/attempts/'+a.id+'/delete'
            assert (await client.post(endpoint)).status == 401
            owner = await sign_in(client, server, monkeypatch)
            assert (await client.post(endpoint, headers=owner)).status == 403
            assert (await client.post(endpoint, headers=bearer(created['owner_token']))).status == 403
            admin = await sign_in(client, server, monkeypatch, 273059)
            assert (await client.post(endpoint)).status == 403
            response = await client.post(endpoint, headers=admin)
            assert response.status == 200
            assert (await response.json()) == {'id': a.id, 'deleted': True, 'files_removed': True}
            assert process.poll() is not None
            assert a.id not in server.attempts and not a.path.exists()
            assert all(known is not a for known, role in server.token_index.values())
            assert (await client.get('/attempts/'+a.id+'/screen', headers=bearer(created['player_token']))).status == 404
            assert (await client.post(endpoint, headers=admin)).status == 404
            assert (await client.get('/attempts/'+keep['id']+'/screen', headers=bearer(keep['player_token']))).status == 200
            record = json.loads((root/'.deleted'/(a.id+'.json')).read_bytes())
            assert record['actor_github_id'] == 273059 and record['action'] == 'delete_attempt'
            assert 'token' not in str(record) and 'seed' not in str(record)
            a.audit('GET', 'stale-request', 'player', 401); a.expire()
            assert not a.path.exists()
        restarted = Server(root, github_auth=CONFIG)
        async with wire_client(restarted, tmp_path):
            assert a.id not in restarted.attempts
            assert keep['id'] in restarted.attempts
    asyncio.run(scenario())


def test_tombstone_survives_cleanup_failure_and_retries_on_restart(tmp_path, monkeypatch):
    async def scenario():
        root = tmp_path/'runs'; server = Server(root, github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            created = await (await client.post('/agent/start', json={})).json()
            a = server.attempts[created['id']]
            admin = await sign_in(client, server, monkeypatch, 273059)
            with monkeypatch.context() as patch:
                patch.setattr(server, 'purge_deleted', lambda identifier: False)
                response = await client.post('/dashboard/attempts/'+a.id+'/delete', headers=admin)
                assert response.status == 200
                assert (await response.json())['files_removed'] is False
                assert a.path.exists() and a.id not in server.attempts
        restarted = Server(root, github_auth=CONFIG)
        async with wire_client(restarted, tmp_path):
            assert a.id not in restarted.attempts and not a.path.exists()
            assert not restarted.pending_deletions
    asyncio.run(scenario())


def test_deletion_waits_for_replay_before_removing_files(tmp_path, monkeypatch):
    async def scenario():
        server = Server(tmp_path/'runs', github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            created = await (await client.post('/agent/start', json={})).json()
            a = server.attempts[created['id']]
            a.pause(); a.expire()
            headers = await sign_in(client, server, monkeypatch, 273059)
            release = asyncio.Event()
            async def exporter():
                await release.wait()
                assert a.path.exists()
                (a.path/'replay.cast').write_text('fixture')
            server.browser.replays[a.id] = asyncio.create_task(exporter())
            deletion = asyncio.create_task(client.post('/dashboard/attempts/'+a.id+'/delete', headers=headers))
            async def changing():
                while not server.access[a.id].changing: await asyncio.sleep(.001)
            await asyncio.wait_for(changing(), 5)
            assert not deletion.done() and a.path.exists()
            assert (await client.post('/dashboard/attempts/'+a.id+'/replay', headers=headers)).status == 409
            release.set()
            assert (await deletion).status == 200
            assert not a.path.exists() and a.id not in server.browser.replays
    asyncio.run(scenario())
