import asyncio
import json
import pytest

from nethack_run.server import Server
from test_api import wire_client, inline_workers, verify_separate_process


@pytest.mark.parametrize('endpoint', ['/attempts', '/agent/start'])
@pytest.mark.parametrize('effort', ['xhigh', 'provider-specific-level', None, 'omitted'])
def test_reasoning_effort_round_trip_and_legacy_compatibility(tmp_path, endpoint, effort):
    async def scenario():
        server = Server(tmp_path/'runs')
        metadata = {'player_type': 'model', 'model': 'test-model'}
        if effort != 'omitted': metadata['reasoning_effort'] = effort
        async with wire_client(server, tmp_path) as client:
            response = await client.post(endpoint, json=metadata)
            assert response.status == 201
            attempt = server.attempts[(await response.json())['id']]
            assert attempt.info['metadata'] == metadata
            assert server.browser.summary(attempt, {'id': 999})['player'] == metadata
            assert attempt.journal.events[0]['metadata'] == metadata
            attempt.pause(); attempt.expire()
            package = attempt.package()
            manifest = json.loads((package/'attempt.json').read_text())
            assert manifest['player_metadata'] == metadata
            assert verify_separate_process(package)['verified']
    asyncio.run(scenario())


@pytest.mark.parametrize('effort', [4, True, ['high'], {'level': 'high'}])
def test_reasoning_effort_requires_string_or_null(tmp_path, effort):
    async def scenario():
        server = Server(tmp_path/'runs')
        async with wire_client(server, tmp_path) as client:
            response = await client.post('/agent/start', json={'reasoning_effort': effort})
            assert response.status == 400
            assert not server.attempts
    asyncio.run(scenario())
