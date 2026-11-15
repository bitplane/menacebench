"""Open results-repository pull requests with the server admin's GitHub token.

Players never grant the server extra GitHub access. A fine-grained token scoped
to the results repository (contents and pull requests: read and write) pushes a
branch ``attempt/<id>`` adding ``attempts/<id>/attempt.json`` and
``attempts/<id>/events.jsonl.gz``, then opens a pull request crediting the
person who submitted it.
"""
import base64
import json
from pathlib import Path
import re

from aiohttp import ClientSession, ClientTimeout

PACKAGE_FILES = ('attempt.json', 'events.jsonl.gz')
REPOSITORY = re.compile(r'[A-Za-z0-9-]+/[A-Za-z0-9._-]+')


class SubmissionError(Exception):
    pass


def noreply(github_id, login):
    return f'{github_id}+{login}@users.noreply.github.com'


def player_label(manifest):
    player = manifest['player_metadata']
    if player.get('player_type') == 'human' and player.get('github_login'):
        return f"{player['github_login']} (human)"
    model = player.get('model') or player.get('player_type') or 'Unknown player'
    effort = player.get('reasoning_effort')
    return f'{model} ({effort})' if effort else model


def describe(manifest, login, github_id):
    """Pull request title, body and commit message for one package."""
    result = manifest['result'] or {}
    score = result.get('score')
    title = f"{player_label(manifest)}: {manifest['outcome']}" + (f', score {score}' if score is not None else '')
    character = manifest.get('character') or {}
    rows = [('Attempt', f"`{manifest['attempt_id']}`"), ('Player', player_label(manifest)),
            ('Character', ' / '.join(str(character[k]) for k in ('role', 'race', 'gender', 'align') if k in character) or 'Not chosen'),
            ('Outcome', manifest['outcome']), ('Score', score if score is not None else '—'),
            ('Turns', result.get('turns', '—')), ('Keys', manifest['key_count']),
            ('Harness version', manifest.get('harness_version') or 'unknown'),
            ('Image', f"`{manifest['image_digest']}`" if manifest['image_digest'] else 'development build (unverifiable)'),
            ('Server instance', manifest['server_instance'])]
    # A link, not an @-mention, so submitting does not notify anyone.
    body = '\n'.join([f'Submitted by [{login}](https://github.com/{login}) from the menacebench server.', '',
                      '| | |', '|---|---|', *(f'| {k} | {v} |' for k, v in rows), '',
                      'CI checks the package and replays it in its recorded image before it is merged.'])
    message = (f"Add {manifest['outcome']} attempt {manifest['attempt_id']}\n\n{title}\n\n"
               f'Co-authored-by: {login} <{noreply(github_id, login)}>\n')
    return title, body, message


class Submitter:
    def __init__(self, repository, token, api='https://api.github.com'):
        if not REPOSITORY.fullmatch(repository or ''):
            raise ValueError('results repository must look like owner/name')
        if not token or not token.strip():
            raise ValueError('results token is empty')
        self.repository = repository
        self.owner = repository.split('/')[0]
        self.token = token.strip()
        self.api = api.rstrip('/')

    async def call(self, method, path, body=None):
        """One GitHub REST call; tests replace this with an in-memory fake."""
        async with ClientSession(timeout=ClientTimeout(total=60)) as client:
            async with client.request(method, f'{self.api}/repos/{self.repository}{path}', json=body, headers={
                    'Authorization': 'Bearer '+self.token, 'Accept': 'application/vnd.github+json',
                    'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'menacebench'}) as response:
                data = await response.json(content_type=None)
                return response.status, data

    async def checked(self, method, path, body=None, ok=(200, 201)):
        status, data = await self.call(method, path, body)
        if status not in ok:
            # Never echo the token; GitHub's message is enough to diagnose.
            message = data.get('message') if isinstance(data, dict) else None
            raise SubmissionError(f'GitHub {method} {path.split("?")[0]} failed ({status}): {message or "no message"}')
        return data

    async def submit(self, folder: Path, login, github_id):
        """Open (or find) the pull request for a finalized package folder. Returns its URL and number."""
        manifest = json.loads((Path(folder)/'attempt.json').read_bytes())
        identifier = manifest['attempt_id']
        branch = f'attempt/{identifier}'
        existing = await self.checked('GET', f'/pulls?state=all&head={self.owner}:{branch}')
        if existing:
            return self.record(existing[0])
        base = (await self.checked('GET', ''))['default_branch']
        head = (await self.checked('GET', f'/git/ref/heads/{base}'))['object']['sha']
        tree = (await self.checked('GET', f'/git/commits/{head}'))['tree']['sha']
        entries = []
        for name in PACKAGE_FILES:
            blob = await self.checked('POST', '/git/blobs', {
                'content': base64.b64encode((Path(folder)/name).read_bytes()).decode(), 'encoding': 'base64'})
            entries.append({'path': f'attempts/{identifier}/{name}', 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
        title, body, message = describe(manifest, login, github_id)
        new_tree = (await self.checked('POST', '/git/trees', {'base_tree': tree, 'tree': entries}))['sha']
        commit = (await self.checked('POST', '/git/commits', {'message': message, 'tree': new_tree, 'parents': [head]}))['sha']
        status, _ = await self.call('POST', '/git/refs', {'ref': f'refs/heads/{branch}', 'sha': commit})
        if status == 422:
            # A branch left by an earlier attempt that failed before opening its PR.
            await self.checked('PATCH', f'/git/refs/heads/{branch}', {'sha': commit, 'force': True})
        elif status != 201:
            raise SubmissionError(f'GitHub could not create branch {branch} ({status})')
        pull = await self.checked('POST', '/pulls', {'title': title, 'head': branch, 'base': base, 'body': body})
        return self.record(pull)

    def record(self, pull):
        # The dashboard renders this as a link, so accept only GitHub pull request URLs.
        url, number = pull.get('html_url'), pull.get('number')
        if (not isinstance(url, str) or type(number) is not int
                or not url.startswith(f'https://github.com/{self.repository}/pull/')):
            raise SubmissionError('GitHub returned an unexpected pull request link')
        return {'url': url, 'number': number}
