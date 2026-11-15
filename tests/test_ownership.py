import asyncio
import hashlib
import json
import threading

import pytest
from nethack_run.attempt import Attempt
from nethack_run.server import Server
from nethack_run.package import verify
from test_api import wire_client, inline_workers
from test_browser import CONFIG, login


async def sign_in(client, server, monkeypatch, user=999):
    response, _ = await login(client, server, monkeypatch, user_id=user)
    assert response.status == 302
    me = await (await client.get('/dashboard/me')).json()
    return {'Origin': CONFIG['public_url'], 'X-CSRF-Token': me['csrf']}


def bearer(token):
    return {'Authorization': 'Bearer '+token}


@pytest.mark.parametrize('endpoint', ['/agent/start', '/attempts'])
@pytest.mark.parametrize('owner', [True, 0, -1, 1.5, '999', [], {}, 9007199254740992])
def test_invalid_nominated_owner(tmp_path, endpoint, owner):
    async def scenario():
        server = Server(tmp_path/'runs')
        async with wire_client(server, tmp_path) as client:
            assert (await client.post(endpoint, json={'owner_github_id': owner})).status == 400
            assert not server.attempts
    asyncio.run(scenario())


def test_owned_recovery_persists_without_resuming_or_changing_game(tmp_path, monkeypatch):
    async def scenario():
        root = tmp_path/'runs'
        server = Server(root, github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            created = await (await client.post('/agent/start', json={
                'owner_github_id': 999, 'model': 'test', 'reasoning_effort': 'high'})).json()
            a = server.attempts[created['id']]
            base = '/attempts/'+a.id
            dash = '/dashboard/attempts/'+a.id
            original = created['player_token']
            assert created['owner_github_id'] == 999
            assert 'owner_github_id' not in a.info['metadata']
            assert a.info['contributor'] is None  # nominated ownership grants no quota identity
            assert (await client.post(base+'/pause', headers=bearer(original))).status == 200
            before = (a.info['seed'], a.session, a.frame.digest, a.info['harness_version'])
            headers = await sign_in(client, server, monkeypatch)
            mine = await (await client.get('/dashboard/attempts?scope=mine')).json()
            assert [r['id'] for r in mine] == [a.id]
            assert mine[0]['permissions'] == {'manage': True, 'recover': True, 'play': False, 'submit': False, 'assign_owner': False, 'delete': False}
            assert (await client.post(dash+'/owner', headers=headers, json={'owner_github_id': 1000})).status == 403
            assert (await client.post(dash+'/recover-player')).status == 403
            assert (await client.post(dash+'/recover-player', headers=bearer(original))).status == 403
            response = await client.post(dash+'/recover-player', headers=headers)
            assert response.status == 200 and response.headers['Cache-Control'] == 'no-store'
            recovered = await response.json()
            token = recovered['player_token']
            assert recovered['id'] == a.id and recovered['status']['state'] == 'paused'
            assert (a.info['seed'], a.session, a.frame.digest, a.info['harness_version']) == before
            assert hashlib.sha256(original.encode()).hexdigest() not in server.token_index
            for action, method in [('screen', 'get'), ('resume', 'post'), ('pause', 'post')]:
                assert (await getattr(client, method)(base+'/'+action, headers=bearer(original))).status == 401
            assert (await client.get(base+'/screen', headers=bearer(token))).status == 200
            assert a.info['access_audit'][-1]['actor_github_id'] == 999
            assert a.info['access_audit'][-1]['action'] == 'rotate_player_token'
            assert token not in (a.path/'private.json').read_text()
            assert token not in (a.path/'events.jsonl').read_text()
            viewer = await sign_in(client, server, monkeypatch, 1000)
            assert (await (await client.get('/dashboard/attempts?scope=mine')).json()) == []
            assert (await client.post(dash+'/recover-player', headers=viewer)).status == 403
        restarted = Server(root, github_auth=CONFIG)
        async with wire_client(restarted, tmp_path) as client:
            assert (await client.get(base+'/screen', headers=bearer(original))).status == 401
            assert (await client.get(base+'/screen', headers=bearer(token))).status == 200
            assert (await client.post(base+'/resume', headers=bearer(token))).status == 200
            a = restarted.attempts[created['id']]
            assert a.frame.digest == before[2]
            a.pause(); a.expire()
            folder = a.package()
            assert verify(folder)['verified']
            assert json.loads((folder/'attempt.json').read_bytes())['initial_owner_github_id'] == 999
            headers = await sign_in(client, restarted, monkeypatch)
            assert (await client.post(dash+'/recover-player', headers=headers)).status == 409
    asyncio.run(scenario())


def test_admin_assignment_revokes_previous_capabilities_and_preserves_package(tmp_path, monkeypatch):
    async def scenario():
        server = Server(tmp_path/'runs', github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            created = await (await client.post('/attempts', json={'owner_github_id': 999})).json()
            a = server.attempts[created['id']]
            base = '/attempts/'+a.id; dash = '/dashboard/attempts/'+a.id
            headers = await sign_in(client, server, monkeypatch, 273059)
            async with client.ws_connect(base+'/tty?mode=play', headers=bearer(created['owner_token'])) as ws:
                await ws.receive(); await ws.receive_json()
                response = await client.post(dash+'/owner', json={'owner_github_id': 1000}, headers=headers)
                assert response.status == 200
                assert (await response.json())['owner_github_id'] == 1000
                await asyncio.wait_for(ws.receive(), 5)
            assert (await client.get(base, headers=bearer(created['owner_token']))).status == 401
            assert (await client.get(base+'/screen', headers=bearer(created['player_token']))).status == 401
            previous = await sign_in(client, server, monkeypatch, 999)
            assert (await client.post(dash+'/recover-player', headers=previous)).status == 403
            current = await sign_in(client, server, monkeypatch, 1000)
            assert (await client.post(dash+'/recover-player', headers=current)).status == 200
            assert (await client.post(dash+'/pause', headers=current)).status == 200
            a.expire()
            folder = a.package()
            original = (folder/'attempt.json').read_bytes(), (folder/'events.jsonl.gz').read_bytes()
            admin = await sign_in(client, server, monkeypatch, 273059)
            assert (await client.post(dash+'/owner', json={'owner_github_id': None}, headers=admin)).status == 200
            assert a.info['owner_github_id'] is None
            assert (folder/'attempt.json').read_bytes() == original[0]
            assert (folder/'events.jsonl.gz').read_bytes() == original[1]
            assert verify(folder)['verified']
            assert json.loads(original[0])['initial_owner_github_id'] == 999
    asyncio.run(scenario())


def test_rotation_waits_for_accepted_keys_and_rechecks_queued_old_token(tmp_path, monkeypatch):
    async def scenario():
        server = Server(tmp_path/'runs', github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            created = await (await client.post('/agent/start', json={'owner_github_id': 999})).json()
            a = server.attempts[created['id']]
            headers = await sign_in(client, server, monkeypatch)
            base = '/attempts/'+a.id; dash = '/dashboard/attempts/'+a.id
            started = asyncio.Event(); release = asyncio.Event()
            original_thread = asyncio.to_thread
            async def delayed(function, *args, **kwargs):
                if function == a.keys:
                    started.set()
                    await release.wait()
                return await original_thread(function, *args, **kwargs)
            monkeypatch.setattr(asyncio, 'to_thread', delayed)
            keys = asyncio.create_task(client.post(base+'/keys', headers=bearer(created['player_token']),
                                                  json={'session': 1, 'boundary': 1, 'keys': 'T'}))
            await asyncio.wait_for(started.wait(), 5)
            rotation = asyncio.create_task(client.post(dash+'/recover-player', headers=headers))
            async def changing():
                while not server.access[a.id].changing: await asyncio.sleep(.001)
            await asyncio.wait_for(changing(), 5)
            stale = asyncio.create_task(client.get(base+'/screen', headers=bearer(created['player_token'])))
            await asyncio.sleep(.01)
            assert not rotation.done() and not stale.done()
            release.set()
            assert (await keys).status == 200
            response = await rotation
            assert response.status == 200
            assert (await stale).status == 401
            token = (await response.json())['player_token']
            assert (await client.get(base+'/screen', headers=bearer(token))).status == 200
            assert a.status()['key_count'] == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('after', [False, True])
def test_failed_rotation_never_resurrects_persistently_revoked_token(tmp_path, monkeypatch, after):
    async def scenario():
        import nethack_run.attempt as module
        server = Server(tmp_path/'runs', github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            created = await (await client.post('/agent/start', json={'owner_github_id': 999})).json()
            a = server.attempts[created['id']]
            headers = await sign_in(client, server, monkeypatch)
            base = '/attempts/'+a.id; dash = '/dashboard/attempts/'+a.id
            atomic = module.atomic_json
            def fail(path, data):
                if after: atomic(path, data)
                raise OSError('simulated access persistence failure')
            with monkeypatch.context() as patch:
                patch.setattr(module, 'atomic_json', fail)
                assert (await client.post(dash+'/recover-player', headers=headers)).status == 500
            assert (await client.get(base+'/screen', headers=bearer(created['player_token']))).status == (401 if after else 200)
            assert Attempt(a.path).role(created['player_token']) == (None if after else 'player')
            response = await client.post(dash+'/recover-player', headers=headers)
            assert response.status == 200
            token = (await response.json())['player_token']
            assert (await client.get(base+'/screen', headers=bearer(token))).status == 200
    asyncio.run(scenario())


def test_concurrent_recoveries_leave_only_last_token_valid(tmp_path, monkeypatch):
    async def scenario():
        server = Server(tmp_path/'runs', github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            created = await (await client.post('/agent/start', json={'owner_github_id': 999})).json()
            a = server.attempts[created['id']]
            headers = await sign_in(client, server, monkeypatch)
            responses = await asyncio.gather(*(client.post('/dashboard/attempts/'+a.id+'/recover-player', headers=headers) for _ in range(2)))
            assert all(r.status == 200 for r in responses)
            tokens = [(await r.json())['player_token'] for r in responses]
            statuses = [(await client.get('/attempts/'+a.id+'/screen', headers=bearer(t))).status for t in tokens]
            assert sorted(statuses) == [200, 401]
            assert a.info['player_token_generation'] == 2
    asyncio.run(scenario())


def test_legacy_unowned_attempt_can_be_assigned_without_guessing(tmp_path, monkeypatch):
    async def scenario():
        root = tmp_path/'runs'; root.mkdir()
        a, _, player = Attempt.create(root, {}, 'legacy')
        a.pause()
        # Old private files have no ownership or administrative-audit fields.
        info = json.loads((a.path/'private.json').read_bytes())
        for key in ('owner_github_id', 'initial_owner_github_id', 'access_audit', 'player_token_generation'):
            info.pop(key)
        (a.path/'private.json').write_text(json.dumps(info))
        server = Server(root, github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            headers = await sign_in(client, server, monkeypatch, 273059)
            body = await (await client.get('/dashboard/attempts/'+a.id)).json()
            assert body['owner_github_id'] is None
            response = await client.post('/dashboard/attempts/'+a.id+'/owner', headers=headers, json={'owner_github_id': 999})
            assert response.status == 200
            assert (await client.get('/attempts/'+a.id+'/screen', headers=bearer(player))).status == 401
            headers = await sign_in(client, server, monkeypatch, 999)
            assert (await client.post('/dashboard/attempts/'+a.id+'/recover-player', headers=headers)).status == 200
    asyncio.run(scenario())


def test_cancelled_worker_keeps_operation_lease_until_completion(monkeypatch):
    from nethack_run.access import AccessGate, run_blocking
    async def scenario():
        gate = AccessGate(); started = asyncio.Event(); release = asyncio.Event()
        async def fake_thread(function, *args):
            started.set(); await release.wait(); return function(*args)
        monkeypatch.setattr(asyncio, 'to_thread', fake_thread)
        async def operation():
            async with gate.operation(): await run_blocking(lambda: None)
        work = asyncio.create_task(operation())
        await started.wait(); work.cancel(); await asyncio.sleep(0)
        assert gate.active == 1
        changed = asyncio.Event()
        async def change():
            async with gate.change(): changed.set()
        rotation = asyncio.create_task(change())
        await asyncio.sleep(0); assert not changed.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError): await work
        await rotation
        assert changed.is_set() and gate.active == 0 and not gate.changing
    asyncio.run(scenario())
