"""GitHub owner login and a browser view, separate from player-token routes."""
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

ASSETS = Path(__file__).with_name('web')
COOKIE = 'nethack_owner'
NONCE = 'nethack_oauth'


class Browser:
    def __init__(self, server, config=None):
        self.server = server
        self.config = config
        self.pending = {}
        self.sessions = {}
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
        if name not in {'app.js', 'style.css'}: raise web.HTTPNotFound()
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
        if identity.get('id') not in self.config['owner_ids']:
            raise web.HTTPForbidden(text='This GitHub account is not an owner of this server')
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
        return web.json_response({k: s[k] for k in ('login', 'id', 'csrf')})

    def summary(self, a):
        return a.status() | {'character': a.character, 'player': a.info['metadata'],
                             'last_activity': a.last_activity, 'session_seconds': a.clock()}

    async def attempts(self, request):
        self.session(request)
        attempts = sorted(self.server.attempts.values(), key=lambda a: a.last_activity, reverse=True)
        return web.json_response([self.summary(a) for a in attempts])

    async def attempt(self, request):
        owner = self.session(request)
        a = self.server.attempts.get(request.match_info['id'])
        if a is None: raise web.HTTPNotFound()
        if request.method == 'GET':
            return web.json_response(self.summary(a) | {'screen': a.frame.screen if a.frame else None})
        action = request.match_info['action']
        disposition = 200
        try:
            if action in {'pause', 'resume'}:
                await asyncio.to_thread(getattr(a, action))
                return web.json_response(self.summary(a))
            if action == 'package':
                folder = await asyncio.to_thread(a.package)
                data = io.BytesIO()
                with tarfile.open(fileobj=data, mode='w') as archive:
                    for name in ('attempt.json', 'events.jsonl.gz'): archive.add(folder/name, arcname=name)
                return web.Response(body=data.getvalue(), content_type='application/x-tar',
                                    headers={'Content-Disposition': f'attachment; filename="{a.id}.tar"'})
            disposition = 404
            raise web.HTTPNotFound()
        except Conflict as error:
            disposition = 409
            raise web.HTTPConflict(text=str(error)) from None
        finally:
            await asyncio.to_thread(a.audit, request.method, request.path, 'github:'+str(owner['id']), disposition)

    def routes(self, app):
        app.router.add_get('/', self.home)
        app.router.add_get('/assets/{name}', self.asset)
        app.router.add_get('/auth/github', self.login)
        app.router.add_get('/auth/github/callback', self.authorized)
        app.router.add_post('/auth/logout', self.logout)
        app.router.add_get('/dashboard/me', self.me)
        app.router.add_get('/dashboard/attempts', self.attempts)
        app.router.add_get('/dashboard/attempts/{id}', self.attempt)
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
                                 'Content-Security-Policy': "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
    return response
