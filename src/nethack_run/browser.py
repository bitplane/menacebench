"""GitHub viewer login with owner controls, separate from player-token routes."""
import asyncio
import base64
import hashlib
import io
import secrets
import tarfile
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from aiohttp import ClientSession, ClientTimeout, ClientError, web
from .attempt import Conflict
from .access import github_id, run_blocking
from .recording import recording
from .leaderboard import leaderboard

ASSETS = Path(__file__).with_name('web')
COOKIE = 'nethack_owner'
NONCE = 'nethack_oauth'


class Browser:
    def __init__(self, server, config=None):
        self.server = server
        self.config = config
        self.pending = {}
        self.sessions = {}
        self.replays = {}
        self.replay_slots = asyncio.Semaphore(1)
        self.replay_running = set()
        if config is not None:
            if set(config) != {'client_id', 'client_secret', 'owner_ids', 'public_url'}:
                raise ValueError('GitHub configuration needs client_id, client_secret, owner_ids, public_url')
            if any(not isinstance(config[k], str) or not config[k] or config[k].startswith('YOUR_')
                   for k in ('client_id', 'client_secret', 'public_url')):
                raise ValueError('Fill in the GitHub OAuth credentials and public_url')
            if not isinstance(config['owner_ids'], list) or not config['owner_ids'] or any(
                    type(n) is not int or n <= 0 for n in config['owner_ids']):
                raise ValueError('owner_ids must contain GitHub numeric account IDs')
            url = urlsplit(config['public_url'])
            if (url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password
                    or url.path or url.query or url.fragment
                    or (url.scheme == 'http' and url.hostname not in {'localhost', '127.0.0.1', '::1'})):
                raise ValueError('public_url must be an HTTPS origin or HTTP loopback origin, without a trailing slash')
            self.origin = config['public_url']
            self.callback = self.origin+'/auth/github/callback'

    def prune(self):
        now = time.time()
        for entries in (self.pending, self.sessions):
            for key in list(entries):
                if entries[key]['expires'] <= now: del entries[key]

    def session(self, request):
        self.prune()
        # A bot bearer token never grants browser owner privileges.
        if request.headers.get('Authorization'): raise web.HTTPForbidden()
        session = self.sessions.get(request.cookies.get(COOKIE))
        if not session: raise web.HTTPUnauthorized(text='Sign in with GitHub')
        if request.method != 'GET':
            if (request.headers.get('Origin') != self.origin or not secrets.compare_digest(
                    request.headers.get('X-CSRF-Token', ''), session['csrf'])):
                raise web.HTTPForbidden(text='Invalid browser request')
        return session

    def role(self, session):
        return 'admin' if self.config and session['id'] in self.config['owner_ids'] else 'viewer'

    def can_manage(self, session, attempt):
        return self.role(session) == 'admin' or attempt.info.get('owner_github_id') == session['id']

    def require_owner(self, session, attempt):
        if not self.can_manage(session, attempt):
            raise web.HTTPForbidden(text='This action requires the run owner or a server admin')

    def require_admin(self, session):
        if self.role(session) != 'admin':
            raise web.HTTPForbidden(text='This action requires server admin access')

    def cookie(self, response, name, value, age):
        response.set_cookie(name, value, max_age=age, httponly=True,
                            secure=self.origin.startswith('https:'), samesite='Lax', path='/')

    async def home(self, request):
        try: self.session(request)
        except web.HTTPUnauthorized:
            html = (ASSETS/'login.html').read_text()
            if self.config is None:
                html = html.replace('<a class="button" href="/auth/github">Sign in with GitHub →</a>',
                                    '<p>GitHub login is not configured. See README.md: Browser login.</p>')
            return web.Response(text=html, content_type='text/html')
        return web.FileResponse(ASSETS/'index.html')

    async def asset(self, request):
        name = request.match_info['name']
        if name not in {'app.js', 'style.css', 'asciinema-player.min.js', 'asciinema-player.css'}: raise web.HTTPNotFound()
        return web.FileResponse(ASSETS/name)

    async def login(self, request):
        if self.config is None: raise web.HTTPServiceUnavailable(text='GitHub login is not configured')
        # Cookies are host-scoped. In particular localhost and 127.0.0.1
        # cannot share the nonce used by GitHub's registered callback.
        if request.host != urlsplit(self.origin).netloc:
            raise web.HTTPFound(self.origin+'/auth/github')
        self.prune()
        if len(self.pending) >= 256: raise web.HTTPTooManyRequests()
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        self.pending[state] = {'nonce': nonce, 'verifier': verifier, 'expires': time.time()+600}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        response = web.HTTPFound('https://github.com/login/oauth/authorize?'+urlencode({
            'client_id': self.config['client_id'], 'redirect_uri': self.callback, 'scope': '',
            'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256',
            'allow_signup': 'false'}))
        self.cookie(response, NONCE, nonce, 600)
        raise response

    async def identity(self, code, verifier):
        async with ClientSession(timeout=ClientTimeout(total=15)) as client:
            async with client.post('https://github.com/login/oauth/access_token',
                    headers={'Accept': 'application/json'}, data={
                        'client_id': self.config['client_id'], 'client_secret': self.config['client_secret'],
                        'code': code, 'redirect_uri': self.callback, 'code_verifier': verifier}) as response:
                response.raise_for_status()
                token = (await response.json()).get('access_token')
                if not token: raise ValueError('GitHub rejected authorization')
            async with client.get('https://api.github.com/user', headers={
                    'Authorization': 'Bearer '+token, 'Accept': 'application/vnd.github+json',
                    'User-Agent': 'nethack-run'}) as response:
                response.raise_for_status()
                return await response.json()
        # Provider tokens are never stored in the run journal or browser session.

    async def authorized(self, request):
        if self.config is None: raise web.HTTPServiceUnavailable()
        self.prune()
        state = request.query.get('state', '')
        pending = self.pending.get(state)
        if not pending or not secrets.compare_digest(pending['nonce'], request.cookies.get(NONCE, '')):
            raise web.HTTPBadRequest(text='Login expired or state did not match. Start sign-in again.')
        del self.pending[state]
        code = request.query.get('code')
        if not code or request.query.get('error'): raise web.HTTPBadRequest(text='GitHub login was cancelled')
        try: identity = await self.identity(code, pending['verifier'])
        except (ClientError, asyncio.TimeoutError, ValueError, KeyError):
            raise web.HTTPBadGateway(text='GitHub login failed. Please try again.') from None
        if (type(identity.get('id')) is not int or identity['id'] <= 0
                or not isinstance(identity.get('login'), str) or not identity['login']):
            raise web.HTTPBadGateway(text='GitHub returned an invalid identity')
        self.sessions.pop(request.cookies.get(COOKIE), None)
        if len(self.sessions) >= 128: raise web.HTTPTooManyRequests()
        key = secrets.token_urlsafe(32)
        self.sessions[key] = {'expires': time.time()+7*86400, 'csrf': secrets.token_urlsafe(32),
                              'login': identity['login'], 'id': identity['id']}
        response = web.HTTPFound('/')
        self.cookie(response, COOKIE, key, 7*86400)
        response.del_cookie(NONCE, path='/')
        raise response

    async def logout(self, request):
        self.session(request)
        self.sessions.pop(request.cookies.get(COOKIE), None)
        response = web.json_response({'ok': True})
        response.del_cookie(COOKIE, path='/')
        return response

    async def me(self, request):
        s = self.session(request)
        return web.json_response({k: s[k] for k in ('login', 'id', 'csrf')} | {'role': self.role(s)})

    def summary(self, a, session):
        return a.status() | {'harness_version': a.info.get('harness_version'), 'character': a.character, 'player': a.info['metadata'],
                             'last_activity': a.last_activity, 'session_seconds': a.clock(),
                             'owner_github_id': a.info.get('owner_github_id'),
                             'is_mine': a.info.get('owner_github_id') == session['id'],
                             'permissions': {'manage': self.can_manage(session, a),
                                             'recover': self.can_manage(session, a) and a.state != 'finished',
                                             'assign_owner': self.role(session) == 'admin'}}

    async def leaderboard(self, request):
        self.session(request)
        return web.json_response(leaderboard(self.server.attempts.values(), request.query.get('version')))

    async def attempts(self, request):
        session = self.session(request)
        scope = request.query.get('scope', 'all')
        if scope not in {'all', 'mine'}: raise web.HTTPBadRequest(text='Invalid attempt scope')
        attempts = sorted(self.server.attempts.values(), key=lambda a: a.last_activity, reverse=True)
        return web.json_response([self.summary(a, session) for a in attempts
                                  if scope == 'all' or a.info.get('owner_github_id') == session['id']])

    async def attempt(self, request):
        owner = self.session(request)
        a = self.server.attempts.get(request.match_info['id'])
        if a is None: raise web.HTTPNotFound()
        if request.method == 'GET':
            return web.json_response(self.summary(a, owner) | {'screen': a.frame.screen if a.frame else None})
        action = request.match_info['action']
        disposition = 200
        try:
            if action == 'replay':
                if a.state != 'finished': raise Conflict('The attempt is still in progress')
                if (a.path/'replay.cast').exists():
                    return web.json_response({'status':'ready', 'url':request.path})
                task = self.replays.get(a.id)
                if task is not None and task.done():
                    del self.replays[a.id]
                    disposition = 422
                    raise web.HTTPUnprocessableEntity(text='Replay could not be reproduced with this build. Export it in its recorded image.')
                if task is None:
                    self.replays[a.id] = asyncio.create_task(self.build_recording(a))
                disposition = 202
                return web.json_response({'status':'preparing'}, status=202)
            if action in {'recover-player', 'owner'}:
                body = None
                if action == 'owner':
                    self.require_admin(owner)
                    try:
                        body = await request.json()
                        if not isinstance(body, dict) or set(body) != {'owner_github_id'}:
                            raise ValueError('Expected owner_github_id')
                        github_id(body['owner_github_id'])
                    except (ValueError, TypeError) as error:
                        raise web.HTTPBadRequest(text=str(error)) from None
                async with self.server.access[a.id].change():
                    if action == 'owner': self.require_admin(owner)
                    else: self.require_owner(owner, a)
                    try:
                        if action == 'recover-player':
                            token = await run_blocking(a.rotate_player_token, owner['id'])
                            return web.json_response({'id': a.id, 'player_token': token,
                                                      'attempt_path': '/attempts/'+a.id,
                                                      'status': a.status()},
                                                     headers={'Cache-Control': 'no-store'})
                        changed = await run_blocking(a.assign_owner, body['owner_github_id'], owner['id'])
                    finally:
                        self.server.index_tokens(a)
                # Assignment revoked legacy bearer access, including open terminals.
                if action == 'owner' and changed:
                    for ws in list(self.server.terminals[a.id]):
                        await ws.close(code=1008, message=b'Ownership changed; credential revoked')
                return web.json_response(self.summary(a, owner))
            async with self.server.access[a.id].operation():
                self.require_owner(owner, a)
                if action in {'pause', 'resume'}:
                    if action == 'resume': await self.server.resume_attempt(a)
                    else: await run_blocking(a.pause)
                    return web.json_response(self.summary(a, owner))
                if action == 'package':
                    folder = await run_blocking(a.package)
                    data = io.BytesIO()
                    with tarfile.open(fileobj=data, mode='w') as archive:
                        for name in ('attempt.json', 'events.jsonl.gz'): archive.add(folder/name, arcname=name)
                    return web.Response(body=data.getvalue(), content_type='application/x-tar',
                                        headers={'Content-Disposition': f'attachment; filename="{a.id}.tar"'})
                raise web.HTTPNotFound()
        except Conflict as error:
            disposition = 409
            raise web.HTTPConflict(text=str(error)) from None
        except web.HTTPException as error:
            disposition = error.status
            raise
        finally:
            await asyncio.to_thread(a.audit, request.method, request.path, 'github:'+str(owner['id']), disposition)

    async def build_recording(self, attempt):
        async with self.replay_slots:
            self.replay_running.add(attempt.id)
            try:
                await asyncio.to_thread(recording, attempt)
            except (OSError, ValueError, RuntimeError, TimeoutError):
                # Do not publish a partial or divergent recording. The owner
                # receives a retryable error without private verifier details.
                return
            finally:
                self.replay_running.discard(attempt.id)

    async def replay_file(self, request):
        self.session(request)
        a = self.server.attempts.get(request.match_info['id'])
        if a is None: raise web.HTTPNotFound()
        if a.state != 'finished': raise web.HTTPConflict(text='The attempt is still in progress')
        path = a.path/'replay.cast'
        if not path.exists(): raise web.HTTPNotFound(text='Recording is not prepared yet')
        response = web.FileResponse(path)
        response.content_type = 'application/x-asciicast'
        if 'download' in request.query:
            response.headers['Content-Disposition'] = f'attachment; filename="{a.id}.cast"'
        return response

    async def cleanup(self, app):
        # Drop queued work, but let the one active exporter close its game and
        # publish atomically before the event loop shuts down.
        for ident, task in self.replays.items():
            if ident not in self.replay_running: task.cancel()
        await asyncio.gather(*self.replays.values(), return_exceptions=True)

    def routes(self, app):
        app.on_cleanup.append(self.cleanup)
        app.router.add_get('/', self.home)
        app.router.add_get('/assets/{name}', self.asset)
        app.router.add_get('/auth/github', self.login)
        app.router.add_get('/auth/github/callback', self.authorized)
        app.router.add_post('/auth/logout', self.logout)
        app.router.add_get('/dashboard/me', self.me)
        app.router.add_get('/dashboard/attempts', self.attempts)
        app.router.add_get('/dashboard/leaderboard', self.leaderboard)
        app.router.add_get('/dashboard/attempts/{id}', self.attempt)
        app.router.add_get('/dashboard/attempts/{id}/replay', self.replay_file)
        app.router.add_post('/dashboard/attempts/{id}/{action}', self.attempt)


@web.middleware
async def browser_headers(request, handler):
    try: response = await handler(request)
    except web.HTTPException as response:
        response.headers['Cache-Control'] = 'no-store'
        raise
    if request.path == '/' or request.path.startswith(('/auth/', '/dashboard/', '/assets/', '/agent/')):
        response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                                 'X-Content-Type-Options': 'nosniff',
                                 'Content-Security-Policy': "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
    return response
