"""Submit packages as the signed-in GitHub user, through a fork when needed."""
import asyncio
import base64
import json
from pathlib import Path
import re

from aiohttp import ClientSession, ClientTimeout

PACKAGE_FILES = ('attempt.json', 'events.jsonl.gz')
REPOSITORY = re.compile(r'[A-Za-z0-9-]+/[A-Za-z0-9._-]+')


class SubmissionError(Exception):
    pass


class AuthorizationError(SubmissionError):
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

    async def call(self, method, path, body=None, repository=None):
        """One GitHub REST call; tests replace this with an in-memory fake."""
        async with ClientSession(timeout=ClientTimeout(total=60)) as client:
            async with client.request(method, f'{self.api}/repos/{repository or self.repository}{path}', json=body, headers={
                    'Authorization': 'Bearer '+self.token, 'Accept': 'application/vnd.github+json',
                    'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'menacebench'}) as response:
                data = await response.json(content_type=None)
                return response.status, data

    async def checked(self, method, path, body=None, ok=(200, 201), repository=None):
        status, data = await self.call(method, path, body, repository=repository)
        if status == 401:
            raise AuthorizationError('GitHub authorization is no longer valid')
        if status not in ok:
            # Never echo the token; GitHub's message is enough to diagnose.
            message = data.get('message') if isinstance(data, dict) else None
            raise SubmissionError(f'GitHub {method} {path.split("?")[0]} failed ({status}): {(message or "no message").replace(self.token, "[redacted]")}')
        return data

    async def submit(self, folder: Path, login, github_id):
        """Open (or find) the pull request for a finalized package folder. Returns its URL and number."""
        manifest = json.loads((Path(folder)/'attempt.json').read_bytes())
        identifier = manifest['attempt_id']
        branch = f'attempt/{identifier}'
        upstream = await self.checked('GET', '')
        direct = upstream.get('permissions', {}).get('push', False)
        destination = self.repository if direct else f'{login}/{self.repository.split("/")[1]}'
        head_owner = self.owner if direct else login
        existing = await self.checked('GET', f'/pulls?state=all&head={head_owner}:{branch}')
        if existing:
            return self.record(existing[0])
        base = upstream['default_branch']
        head = (await self.checked('GET', f'/git/ref/heads/{base}'))['object']['sha']
        if not direct:
            status, fork = await self.call('GET', '', repository=destination)
            if status == 401: raise AuthorizationError('GitHub authorization is no longer valid')
            if status == 404:
                fork = await self.checked('POST', '/forks', {'default_branch_only': True}, ok=(202,))
            elif status != 200:
                raise SubmissionError(f'GitHub could not access your fork ({status})')
            destination = fork.get('full_name', '')
            if (not REPOSITORY.fullmatch(destination)
                    or fork.get('owner', {}).get('id') != github_id
                    or not fork.get('fork')):
                raise SubmissionError('GitHub did not return a fork owned by your account')
            # Fork metadata and Git objects can become available at different times.
            for attempt in range(6):
                status, fork = await self.call('GET', '', repository=destination)
                if status == 200:
                    parents = {fork.get(key, {}).get('full_name', '').lower() for key in ('parent', 'source')}
                    if self.repository.lower() not in parents:
                        raise SubmissionError('Your repository is not a fork of the results repository')
                    status, _ = await self.call('GET', f'/git/commits/{head}', repository=destination)
                    if status == 200: break
                if status == 401: raise AuthorizationError('GitHub authorization is no longer valid')
                if status not in (404, 409):
                    raise SubmissionError(f'GitHub could not prepare your fork ({status})')
                if attempt == 5:
                    raise SubmissionError('GitHub is still preparing your fork. Try Submit again shortly.')
                await asyncio.sleep(1)
        async def write(method, path, body=None):
            return await self.checked(method, path, body, repository=destination)
        tree = (await write('GET', f'/git/commits/{head}'))['tree']['sha']
        entries = []
        for name in PACKAGE_FILES:
            blob = await write('POST', '/git/blobs', {
                'content': base64.b64encode((Path(folder)/name).read_bytes()).decode(), 'encoding': 'base64'})
            entries.append({'path': f'attempts/{identifier}/{name}', 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
        title, body, message = describe(manifest, login, github_id)
        new_tree = (await write('POST', '/git/trees', {'base_tree': tree, 'tree': entries}))['sha']
        commit = (await write('POST', '/git/commits', {'message': message, 'tree': new_tree, 'parents': [head]}))['sha']
        status, _ = await self.call('POST', '/git/refs', {'ref': f'refs/heads/{branch}', 'sha': commit}, repository=destination)
        if status == 422:
            # A branch left by an earlier attempt that failed before opening its PR.
            current = await write('GET', f'/git/ref/heads/{branch}')
            previous = await write('GET', f"/git/commits/{current['object']['sha']}")
            if previous['tree']['sha'] != new_tree:
                raise SubmissionError('Submission branch already exists with different content; it was not overwritten')
        elif status == 401:
            raise AuthorizationError('GitHub authorization is no longer valid')
        elif status != 201:
            raise SubmissionError(f'GitHub could not create branch {branch} ({status})')
        pull = await self.checked('POST', '/pulls', {'title': title, 'head': branch if direct else f'{login}:{branch}', 'base': base, 'body': body})
        return self.record(pull)

    def record(self, pull):
        # The dashboard renders this as a link, so accept only GitHub pull request URLs.
        url, number = pull.get('html_url'), pull.get('number')
        if (not isinstance(url, str) or type(number) is not int
                or not url.startswith(f'https://github.com/{self.repository}/pull/')):
            raise SubmissionError('GitHub returned an unexpected pull request link')
        return {'url': url, 'number': number}
