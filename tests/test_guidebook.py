import asyncio
from nethack_run.engine import ROOT
from nethack_run.server import Server
from test_api import wire_client, inline_workers


def test_public_and_player_guidebook(tmp_path):
    async def scenario():
        server = Server(tmp_path/'runs')
        async with wire_client(server, tmp_path) as client:
            expected = (ROOT/'build/Guidebook.txt').read_bytes()
            assert b'Guidebook for NetHack' in expected
            response = await client.get('/guidebook.txt')
            assert response.status == 200 and response.content_type == 'text/plain'
            assert await response.read() == expected
            created = await (await client.post('/agent/start', json={})).json()
            headers = {'Authorization': 'Bearer '+created['player_token']}
            assert (await client.get('/guidebook.txt', headers=headers)).status == 200
            assert (await client.head('/guidebook.txt', headers=headers)).status == 200
            assert (await client.post('/guidebook.txt', headers=headers)).status == 403
            assert (await client.get('/dashboard/leaderboard', headers=headers)).status == 403
            assert '/guidebook.txt' in await (await client.get('/player.md')).text()
    asyncio.run(scenario())
