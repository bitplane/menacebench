import gc
import json

from nethack_run.attempt import Attempt
from nethack_run.index import Index
from nethack_run.server import Server
from test_api import wire_client, inline_workers
from test_browser import CONFIG, login

QUIT = 'Tester-Bar-Hum-Mal-Cha\ny#quit\ny'+' '*20


def bearer(token): return {'Authorization': 'Bearer '+token}


async def finish(client, server, metadata):
    created = await (await client.post('/attempts', json=metadata)).json()
    server.attempts[created['id']].keys(QUIT, 1, 1)
    assert server.attempts[created['id']].state == 'finished'
    return created


def test_restart_keeps_finished_attempts_on_disk_until_used(tmp_path, monkeypatch):
    import asyncio
    async def scenario():
        root = tmp_path/'runs'
        server = Server(root, github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            done = await finish(client, server, {'model': 'Indexed', 'reasoning_effort': 'high'})
            live = await (await client.post('/attempts', json={'model': 'Live'})).json()
        restarted = Server(root, github_auth=CONFIG)
        async with wire_client(restarted, tmp_path) as client:
            # Only the unfinished attempt is loaded; the finished one is an index row.
            assert set(restarted.attempts) == {live['id']}
            await login(client, restarted, monkeypatch)
            rows = await (await client.get('/dashboard/attempts')).json()
            assert {row['id'] for row in rows} == {done['id'], live['id']}
            row = next(row for row in rows if row['id'] == done['id'])
            assert row['state'] == 'finished' and row['outcome'] == 'quit' and row['key_count'] > 0
            assert 'seed' not in json.dumps(rows) and 'hash' not in json.dumps(rows)
            board = await (await client.get('/dashboard/leaderboard')).json()
            assert [g['model'] for g in board['groups']] == ['Indexed']
            assert done['id'] not in restarted.attempts
            # A bearer token for an unloaded attempt resolves through the index.
            response = await client.get('/attempts/'+done['id'], headers=bearer(done['owner_token']))
            assert response.status == 200 and (await response.json())['state'] == 'finished'
            assert done['id'] in restarted.attempts
            assert (await client.get('/attempts/'+done['id'], headers=bearer('wrong'))).status == 401
    asyncio.run(scenario())


def test_index_is_rebuilt_when_missing_or_stale(tmp_path):
    import asyncio
    async def scenario():
        root = tmp_path/'runs'
        server = Server(root)
        async with wire_client(server, tmp_path) as client:
            done = await finish(client, server, {'model': 'Rebuilt'})
        indexed = Server(root)
        async with wire_client(indexed, tmp_path):
            expected = indexed.summaries()
            assert not indexed.attempts and expected[0]['id'] == done['id']
        # Deleting the database loses nothing: rows come back from attempt folders.
        for path in root.glob('.index.sqlite*'): path.unlink()
        restarted = Server(root)
        async with wire_client(restarted, tmp_path):
            assert restarted.summaries() == expected
        # A change made while the server was down invalidates the row's fingerprint.
        attempt = Attempt(root/done['id'])
        attempt.assign_owner(4242, None)
        changed = Server(root)
        async with wire_client(changed, tmp_path):
            [row] = changed.summaries()
            assert row['owner_github_id'] == 4242
        # An attempt folder removed behind the server's back loses its row.
        import shutil
        shutil.rmtree(root/done['id'])
        emptied = Server(root)
        async with wire_client(emptied, tmp_path):
            assert emptied.summaries() == [] and emptied.index.ids() == set()
    asyncio.run(scenario())


def test_eviction_and_reload_keep_one_object_per_attempt(tmp_path):
    import asyncio
    async def scenario():
        server = Server(tmp_path/'runs')
        async with wire_client(server, tmp_path) as client:
            done = await finish(client, server, {'model': 'Evicted'})
            running = await (await client.post('/attempts', json={})).json()
            held = server.attempts[done['id']]
            before = server.summaries()
            await server.evict_idle(idle=0)
            # Unfinished attempts are never evicted.
            assert set(server.attempts) == {running['id']}
            assert held.summary() == next(r for r in server.summaries() if r['id'] == held.id)
            assert [r['id'] for r in server.summaries()] == [r['id'] for r in before]
            # While anything still references the evicted object, it is reused,
            # so two objects can never append to the same journal.
            assert await server.get(done['id']) is held
            await server.evict_idle(idle=0)
            identity = id(held)
            del held
            gc.collect()
            reloaded = await server.get(done['id'])
            assert reloaded is not None and reloaded.id == done['id'] and reloaded.state == 'finished'
            assert server.attempts[done['id']] is reloaded
            # A successful recording leaves nothing behind that pins the attempt.
            await server.browser.build_recording(reloaded)
            assert done['id'] not in server.browser.replays
            # Recently used attempts stay loaded.
            await server.evict_idle()
            assert done['id'] in server.attempts
            # IDs outside the index never touch the filesystem.
            for bad in ('..', '../runs', 'missing', '.index.sqlite', ''):
                assert await server.get(bad) is None
            assert (await client.get('/attempts/missing')).status == 404
    asyncio.run(scenario())


def test_deleted_attempt_leaves_the_index(tmp_path, monkeypatch):
    import asyncio
    async def scenario():
        root = tmp_path/'runs'
        server = Server(root, github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            done = await finish(client, server, {'model': 'Deleted'})
            await server.evict_idle(idle=0)
            a = await server.get(done['id'])
            assert await server.delete_attempt(a, 1)
            assert server.summaries() == [] and done['id'] not in server.index.ids()
            assert await server.get(done['id']) is None
            assert (await client.get('/attempts/'+done['id'], headers=bearer(done['owner_token']))).status == 404
    asyncio.run(scenario())


def test_key_count_uses_journal_totals(tmp_path):
    a, _, _ = Attempt.create(tmp_path, {}, 'test')
    a.keys('Tester-Bar', 1, 1)
    assert a.status()['key_count'] == sum(e['type'] == 'key' for e in a.journal.events) == 10
    assert Attempt(a.path).status()['key_count'] == 10
    a.pause()


def test_index_schema_mismatch_is_rebuilt(tmp_path):
    index = Index(tmp_path/'index.sqlite')
    index.write('x', 'f', 'o', 'p', {'state': 'finished', 'last_activity': 1, 'id': 'x'})
    index.db.execute('PRAGMA user_version=999')
    index.close()
    assert Index(tmp_path/'index.sqlite').ids() == set()


def test_maintenance_continues_after_eviction_failure(tmp_path, monkeypatch, caplog):
    import asyncio
    from unittest.mock import AsyncMock

    async def scenario():
        server = Server(tmp_path)
        eviction = AsyncMock(side_effect=[OSError("index unavailable"), None])
        eviction.__name__ = "evict_idle"
        prune = AsyncMock()
        monkeypatch.setattr(server, 'evict_idle', eviction)
        monkeypatch.setattr(server, 'prune_casts', prune)
        await server.maintain()
        prune.assert_awaited_once()
        assert 'index unavailable' in caplog.text
        server.next_eviction = 0
        await server.maintain()
        assert eviction.await_count == 2
        await server.stop(None)

    asyncio.run(scenario())
