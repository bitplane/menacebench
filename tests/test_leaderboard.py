import asyncio

from nethack_run.leaderboard import leaderboard
from nethack_run.server import Server
from test_api import wire_client, inline_workers
from test_browser import CONFIG, login


def run(model, effort, score, version='0.0.4', state='finished', kind='model', outcome='death'):
    # The same summary-row shape as Attempt.summary() and the SQLite index.
    return {'harness_version': version, 'player': {'model': model, 'reasoning_effort': effort, 'player_type': kind},
            'state': state, 'result': {'score': score} if score is not None else None, 'outcome': outcome}


def test_averages_groups_versions_and_eligibility():
    runs = [run('A', 'high', 100), run('A', 'high', 0, outcome='quit'),
            run('A', 'low', 80), run('B', 'high', 60, outcome='ascended'),
            run('A', 'high', 1000, version='0.0.1'), run('A', 'high', None),
            run('A', 'high', 9999, state='running'), run('Human', None, 9999, kind='human'),
            run(None, None, 20, version=None)]
    result = leaderboard(runs, '0.0.4', humans=False)
    assert result['scored_attempts'] == 4 and result['excluded_attempts'] == 1
    assert [(r['model'], r['reasoning_effort'], r['average_score']) for r in result['groups']] == [
        ('A', 'low', 80), ('B', 'high', 60), ('A', 'high', 50)]
    assert result['groups'][2]['attempt_count'] == 2
    assert result['groups'][2]['best_score'] == 100
    assert result['groups'][1]['ascensions'] == 1
    assert leaderboard(runs, humans=False)['groups'][0]['average_score'] == 1100/3
    # Humans share the table by default.
    assert leaderboard(runs, '0.0.4')['groups'][0]['player_type'] == 'human'
    unknown = leaderboard(runs, 'unknown')['groups'][0]
    assert unknown['model'] is None and unknown['reasoning_effort'] is None
    assert leaderboard(runs, 'missing')['groups'] == []


def test_ties_and_unknown_are_not_literal_model_names():
    rows = leaderboard([run(None, None, 40), run('Unknown model', None, 40), run('C', '', 0)])['groups']
    assert [r['rank'] for r in rows] == [1, 1, 3]
    assert len(rows) == 3


def test_leaderboard_requires_owner_and_filters_version(tmp_path, monkeypatch):
    async def scenario():
        server = Server(tmp_path/'runs', github_auth=CONFIG)
        async with wire_client(server, tmp_path) as client:
            assert (await client.get('/dashboard/leaderboard')).status == 401
            created = await (await client.post('/agent/start', json={})).json()
            token = {'Authorization': 'Bearer '+created['player_token']}
            assert (await client.get('/dashboard/leaderboard', headers=token)).status == 403
            await login(client, server, monkeypatch)
            # Fixture results are deliberately separate from any real game or journal.
            row = run('<script>model</script>', 'xhigh', 25) | {'id': 'fixture', 'last_activity': 0, 'owner_github_id': None}
            server.index.write('fixture', '', 'owner', 'player', row)
            try:
                response = await client.get('/dashboard/leaderboard?version=0.0.4')
                body = await response.json()
                assert response.status == 200 and body['groups'][0]['average_score'] == 25
                assert 'seed' not in str(body) and 'token' not in str(body)
                assert (await (await client.get('/dashboard/leaderboard?version=0.0.1')).json())['groups'] == []
            finally:
                server.index.remove('fixture')
    asyncio.run(scenario())


def test_leaderboard_reconstructed_from_durable_attempt(tmp_path):
    from nethack_run.attempt import Attempt
    a, _, _ = Attempt.create(tmp_path, {'player_type': 'model', 'model': 'Persisted', 'reasoning_effort': 'high'}, 'test')
    a.keys('Tester-Bar-Hum-Mal-Cha\ny#quit\ny'+' '*20, 1, 1)
    assert a.state == 'finished' and a.result is not None
    expected = leaderboard([a.summary()])
    assert expected['scored_attempts'] == 1
    assert leaderboard([Attempt(a.path).summary()]) == expected


def test_model_formatting_groups_with_stable_readable_display():
    variants = [run('GPT-6 Luna', 'high', 10), run('gpt 6 luna', 'high', 30),
                run('GPT.6_LUNA', 'low', 60), run('GPT-6 Luna', 'high', 1000, version='0.0.1')]
    for index, a in enumerate(variants):
        a['id'] = str(index)
        a['last_activity'] = index
    result = leaderboard(reversed(variants), '0.0.4')
    assert len(result['groups']) == 2
    assert {r['model_key'] for r in result['groups']} == {'gpt6luna'}
    assert {r['model'] for r in result['groups']} == {'GPT-6 Luna'}
    high = next(r for r in result['groups'] if r['reasoning_effort'] == 'high')
    assert high['average_score'] == 20 and high['attempt_count'] == 2
    assert variants[1]['player']['model'] == 'gpt 6 luna'
    assert leaderboard(variants, '0.0.4') == result


def human(login, account, score, outcome='death', state='finished', at=0):
    return {'harness_version': '0.0.4', 'state': state, 'outcome': outcome, 'last_activity': at, 'id': str(at),
            'result': {'score': score} if score is not None else None, 'human_github_id': account,
            'player': {'player_type': 'human', 'github_login': login}}


def test_humans_share_the_leaderboard_grouped_by_github_account():
    runs = [run('A', 'high', 50), human('alice', 1, 100, at=1), human('Alice-Renamed', 1, 20, 'quit', at=2),
            human('bob', 2, 50, 'ascended'), human('carol', 3, 999, state='running'), human('dave', 4, None),
            human(None, None, 10)]
    result = leaderboard(runs)
    rows = [(r['player_type'], r['player'], r['average_score'], r['rank']) for r in result['groups']]
    assert rows == [('human', 'Alice-Renamed', 60, 1), ('model', 'A', 50, 2), ('human', 'bob', 50, 2),
                    ('human', None, 10, 4)]
    alice = result['groups'][0]
    assert alice['github_id'] == 1 and alice['attempt_count'] == 2 and alice['best_score'] == 100
    assert alice['model'] is None and alice['reasoning_effort'] is None
    assert result['groups'][2]['ascensions'] == 1
    assert result['scored_attempts'] == 5 and result['excluded_attempts'] == 1
    without = leaderboard(runs, humans=False)
    assert [r['player'] for r in without['groups']] == ['A'] and without['excluded_attempts'] == 0
