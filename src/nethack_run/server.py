"""HTTP and terminal API over serialized attempt controllers."""
from __future__ import annotations
import asyncio
import contextlib
from collections import defaultdict
import hashlib
import io
import json
from pathlib import Path
import tarfile
import threading
import time
import shutil
import uuid
import weakref
from aiohttp import web, WSMsgType
from .attempt import Attempt, Conflict
from .access import AccessGate, github_id, run_blocking
from .engine import ROOT
from .index import Index, fingerprint
from .recording import DEFAULT_CACHE, prune
from .storage import atomic_json, sync_dir

# Finished attempts leave memory after this long without use. Their summaries
# stay in the SQLite index and the attempt reloads from disk on demand.
EVICT_AFTER = 300


class Server:
    def __init__(self, root, instance='local', contributors=None, concurrency=100, limits=None, github_auth=None,
                 cast_cache=None, submitter=None):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.cast_cache = DEFAULT_CACHE | (cast_cache or {})
        # Opens results pull requests with the admin's token; None disables submission.
        self.submitter = submitter
        self.submitting = set()
        self.instance = instance
        self.contributors = contributors
        self.concurrency = concurrency
        self.limits = limits or {'idle': 1800, 'duration': 86400, 'events': 1000000, 'expiry': 604800}
        # Loaded attempts: every unfinished one, plus recently used finished ones.
        self.attempts = {}
        # Evicted attempts still referenced elsewhere (a request in flight) must
        # not be loaded twice: two objects would interleave journal sequence numbers.
        self.evicted = weakref.WeakValueDictionary()
        self.used = {}
        self.load_lock = threading.Lock()
        self.index = Index(self.root/'.index.sqlite')
        self.token_index = {}
        self.access = defaultdict(AccessGate)
        self.terminals = defaultdict(set)
        self.admission_lock = asyncio.Lock()
        self.monitor_task = None
        self.state_lock = None
        self.deleted_ids = set()
        self.pending_deletions = set()
        self.next_cleanup = 0
        self.next_eviction = 0
        self.next_cast_prune = 0
        self.cleanup_lock = asyncio.Lock()
        from .browser import Browser
        self.browser = Browser(self, github_auth)

    async def start(self, app):
        import fcntl
        self.state_lock = (self.root/".server.lock").open("a")
        fcntl.flock(self.state_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        tombstones = self.root/'.deleted'
        tombstones.mkdir(mode=0o700, exist_ok=True)
        sync_dir(self.root)
        for path in tombstones.glob('*.json'):
            record = json.loads(path.read_bytes())
            if str(uuid.UUID(path.stem)) != path.stem or record.get('attempt_id') != path.stem:
                raise ValueError('Invalid deletion record')
            self.deleted_ids.add(path.stem)
        self.pending_deletions = set(self.deleted_ids)
        await self.cleanup_deleted()
        present = set()
        for path in self.root.iterdir():
            if path.name in self.deleted_ids: continue
            if path.is_dir() and (path/'private.json').exists():
                present.add(path.name)
                # A finished attempt whose files are unchanged since its row was
                # written stays on disk; only its index row is consulted.
                row = self.index.row(path.name)
                if row and row[0] == 'finished' and row[1] == await asyncio.to_thread(fingerprint, path):
                    continue
                attempt = await asyncio.to_thread(Attempt, path)
                await asyncio.to_thread(attempt.recover)
                await asyncio.to_thread(self.index.put, attempt)
                if attempt.state == 'finished': continue
                self.attempts[attempt.id] = attempt
                self.used[attempt.id] = time.monotonic()
                self.index_tokens(attempt)
        for identifier in self.index.ids()-present:
            self.index.remove(identifier)
        self.monitor_task = asyncio.create_task(self.monitor())

    async def stop(self, app):
        if self.monitor_task:
            self.monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError): await self.monitor_task
        for attempt in list(self.attempts.values()):
            if attempt.state == 'running': await asyncio.to_thread(attempt.pause, 'shutdown')
        for attempt in list(self.attempts.values()):
            await asyncio.to_thread(self.index.put, attempt)
        self.index.close()
        if self.state_lock: self.state_lock.close()

    async def monitor(self):
        while True:
            await asyncio.sleep(1)
            await self.enforce_limits()
            if self.pending_deletions and time.monotonic() >= self.next_cleanup:
                await self.cleanup_deleted()
            if time.monotonic() >= self.next_eviction:
                self.next_eviction = time.monotonic()+30
                await self.evict_idle()
            if time.monotonic() >= self.next_cast_prune:
                self.next_cast_prune = time.monotonic()+600
                await self.prune_casts()

    async def prune_casts(self):
        try:
            await asyncio.to_thread(prune, self.root, self.cast_cache['ttl'], self.cast_cache['max_bytes'],
                                    self.cast_cache['min_free_bytes'])
        except OSError:
            pass

    async def evict_idle(self, idle=EVICT_AFTER):
        """Write finished, unused attempts to the index and drop them from memory."""
        now = time.monotonic()
        for a in list(self.attempts.values()):
            replay = self.browser.replays.get(a.id)
            if (a.state != 'finished' or now-self.used.get(a.id, 0) < idle or self.terminals.get(a.id)
                    or (replay is not None and not replay.done())
                    or self.access[a.id].active or self.access[a.id].changing):
                continue
            await asyncio.to_thread(self.index.put, a)
            # Never block the event loop on a disk load in progress; retry later.
            if not self.load_lock.acquire(blocking=False): return
            try:
                if self.attempts.get(a.id) is not a: continue
                del self.attempts[a.id]
                self.evicted[a.id] = a
                self.used.pop(a.id, None)
                self.access.pop(a.id, None)
                self.terminals.pop(a.id, None)
                for digest, (known, _) in list(self.token_index.items()):
                    if known is a: del self.token_index[digest]
            finally:
                self.load_lock.release()

    def load(self, identifier):
        """Return the single live object for an attempt, reading it from disk if evicted."""
        with self.load_lock:
            a = self.attempts.get(identifier) or self.evicted.get(identifier)
            if a is None:
                # Only indexed IDs reach the filesystem, so a path can never escape the root.
                if identifier in self.deleted_ids or not self.index.row(identifier): return None
                path = self.root/identifier
                if not (path/'private.json').exists(): return None
                a = Attempt(path)
            if a.deleted: return None
            self.used[identifier] = time.monotonic()
            if self.attempts.get(identifier) is not a:
                self.attempts[identifier] = a
                self.index_tokens(a)
            return a

    async def get(self, identifier):
        a = self.attempts.get(identifier)
        if a is not None:
            self.used[identifier] = time.monotonic()
            return a
        return await asyncio.to_thread(self.load, identifier)

    async def token_lookup(self, token):
        """Resolve a bearer token to (attempt, role), including evicted finished attempts."""
        digest = hashlib.sha256(token.encode()).hexdigest()
        known = self.token_index.get(digest)
        if known: return known
        found = await asyncio.to_thread(self.index.token, digest)
        if not found: return None
        a = await self.get(found[0])
        # The row may predate a credential rotation; trust only the live hash.
        return (a, found[1]) if a is not None and a.info[found[1]+'_hash'] == digest else None

    def summaries(self):
        """Every attempt's public summary, newest activity first."""
        live = {a.id: a.summary() for a in list(self.attempts.values()) if not a.deleted}
        rows = [row for row in self.index.summaries() if row['id'] not in live]
        return sorted([*rows, *live.values()], key=lambda row: row['last_activity'], reverse=True)

    async def enforce_limits(self):
        for a in list(self.attempts.values()):
            if a.state == 'running':
                reason = ('idle' if time.time()-a.last_activity >= self.limits['idle'] else
                          'duration_limit' if a.clock() >= self.limits['duration'] else
                          'event_limit' if len(a.journal.events) >= self.limits['events'] else None)
                if reason:
                    with contextlib.suppress(Conflict): await run_blocking(a.pause, reason)
            if a.state in {'paused','saved'} and time.time()-a.last_activity >= self.limits['expiry']:
                await asyncio.to_thread(a.expire)

    def index_tokens(self, attempt):
        for digest, (known, _) in list(self.token_index.items()):
            if known is attempt: del self.token_index[digest]
        if attempt.deleted: return
        for role in ('owner','player'):
            self.token_index[attempt.info[role+'_hash']] = (attempt,role)

    def commit_deletion(self, attempt, actor):
        with attempt.lock:
            if attempt.state == 'running': attempt.pause('admin_delete')
            marker = self.root/'.deleted'/(attempt.id+'.json')
            try:
                atomic_json(marker, {'attempt_id': attempt.id, 'actor_github_id': actor,
                                     'action': 'delete_attempt', 'wall_time': time.time()})
            finally:
                # A published tombstone is authoritative even if directory fsync
                # reports an error. Never resurrect it in the live process.
                attempt.deleted = marker.exists()

    async def delete_attempt(self, attempt, actor):
        try:
            await run_blocking(self.commit_deletion, attempt, actor)
        finally:
            if attempt.deleted:
                self.deleted_ids.add(attempt.id)
                self.pending_deletions.add(attempt.id)
                self.attempts.pop(attempt.id, None)
                self.evicted.pop(attempt.id, None)
                self.used.pop(attempt.id, None)
                await asyncio.to_thread(self.index.remove, attempt.id)
                self.index_tokens(attempt)
                for ws in list(self.terminals[attempt.id]):
                    await ws.close(code=1008, message=b'Attempt deleted')
        await self.cleanup_deleted()
        return attempt.id not in self.pending_deletions

    def purge_deleted(self, identifier):
        path = self.root/identifier
        try:
            if path.is_symlink(): path.unlink()
            elif path.exists(): shutil.rmtree(path)
            sync_dir(self.root)
            return True
        except OSError:
            return False

    async def cleanup_deleted(self):
        async with self.cleanup_lock:
            for identifier in list(self.pending_deletions):
                if await run_blocking(self.purge_deleted, identifier):
                    self.pending_deletions.discard(identifier)
            self.next_cleanup = time.monotonic()+30

    @web.middleware
    async def audit_requests(self, request, handler):
        token = request.headers.get('Authorization','').removeprefix('Bearer ')
        known = await self.token_lookup(token) if token else None
        status = 500
        try:
            if known and not (request.method in {'GET', 'HEAD'} and request.path in {'/player.md', '/guidebook.txt'}):
                attempt, role = known
                base = '/attempts/'+attempt.id
                permissions = {
                    'player': {('POST',base+'/keys'),('GET',base+'/screen'),('POST',base+'/resume'),('POST',base+'/pause')},
                    'owner': {('POST',base+'/pause'),('PUT',base+'/usage'),('GET',base),
                              ('GET',base+'/package'),('GET',base+'/tty')},
                }
                if (request.method,request.path) not in permissions[role]:
                    raise web.HTTPForbidden()
            response = await handler(request)
            status = response.status
            return response
        except web.HTTPException as error:
            status = error.status
            raise
        finally:
            if known:
                attempt, role = known
                if not attempt.deleted:
                    await asyncio.to_thread(attempt.audit,request.method,request.path,role,status)

    async def agent_start(self, request):
        return await self.create(request, player_only=True)

    async def create(self, request, player_only=False):
        token = request.headers.get('Authorization','').removeprefix('Bearer ')
        contributor = None
        if self.contributors is not None:
            digest = hashlib.sha256(token.encode()).hexdigest()
            contributor = self.contributors.get(digest)
            if contributor is None: raise web.HTTPUnauthorized()
        try:
            metadata = await request.json()
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text='Expected a JSON metadata object')
        if not isinstance(metadata, dict) or set(metadata)-{'player_type','agent_tool','tool_version','model','reasoning_effort','notes','owner_github_id'}:
            raise web.HTTPBadRequest(text='Invalid player metadata')
        try:
            owner_github_id = github_id(metadata.pop('owner_github_id', None))
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from None
        if metadata.get('player_type') not in {None,'human','model'}:
            raise web.HTTPBadRequest(text='Invalid player_type')
        if any(value is not None and (not isinstance(value,str) or len(value)>4096) for value in metadata.values()):
            raise web.HTTPBadRequest(text='Metadata must contain strings or null')
        async with self.admission_lock:
            self.check_capacity(contributor)
            a, owner, player = await asyncio.to_thread(Attempt.create, self.root, metadata, self.instance, contributor, self.limits, owner_github_id)
            self.attempts[a.id] = a
            self.used[a.id] = time.monotonic()
            self.index_tokens(a)
            await asyncio.to_thread(self.index.put, a)
        if player_only:
            await asyncio.to_thread(a.audit, request.method, request.path, 'agent-start', 201)
            return web.json_response({'id':a.id, 'player_token':player,
                                      'attempt_path':'/attempts/'+a.id,
                                      'instructions_path':'/player.md', 'owner_github_id':owner_github_id, 'status':a.status()}, status=201)
        return web.json_response({'id':a.id,'owner_token':owner,'player_token':player,'owner_github_id':owner_github_id,'status':a.status()}, status=201)

    def human_game(self, github_id):
        """A person's one unfinished dashboard game. Unfinished attempts are always loaded."""
        return next((a for a in self.attempts.values() if not a.deleted and a.state != 'finished'
                     and a.info.get('human_github_id') == github_id), None)

    async def play(self, github_id, login):
        """Return the person's unfinished game, resumed, or start their only one."""
        async with self.admission_lock:
            a = self.human_game(github_id)
            if a is None:
                contributor = 'github:'+str(github_id)
                self.check_capacity(contributor)
                a, _, _ = await asyncio.to_thread(
                    Attempt.create, self.root, {'player_type': 'human', 'github_login': login},
                    self.instance, contributor, self.limits, github_id, github_id)
                self.attempts[a.id] = a
                self.used[a.id] = time.monotonic()
                self.index_tokens(a)
                await asyncio.to_thread(self.index.put, a)
                return a, True
        async with self.access[a.id].operation():
            if a.deleted: raise Conflict('Attempt was deleted')
            if a.state in {'paused', 'saved'}:
                try: await self.resume_attempt(a)
                except Conflict:
                    # Another window resumed it first.
                    if a.state != 'running': raise
        return a, False

    def check_capacity(self, contributor):
        active = sum(a.info['contributor'] == contributor and a.state == 'running'
                     for a in self.attempts.values())
        if active >= self.concurrency:
            raise web.HTTPTooManyRequests(
                text=f'Running game limit reached ({self.concurrency}). Pause a running game or retry when a slot is free. Existing attempts and tokens remain valid.')

    async def resume_attempt(self, attempt):
        # Creation and every resume entry point share admission serialization.
        async with self.admission_lock:
            if attempt.state not in {'paused', 'saved'}:
                raise Conflict('Session is not resumable')
            self.check_capacity(attempt.info['contributor'])
            await run_blocking(attempt.resume)

    async def route(self, request):
        a = await self.get(request.match_info['id'])
        if a is None: raise web.HTTPNotFound()
        token = request.headers.get('Authorization','').removeprefix('Bearer ')
        role = a.role(token)
        if role is None: raise web.HTTPUnauthorized()
        action = request.match_info.get('action','')
        allowed = {'owner': {('POST','pause'),('PUT','usage'),('GET',''),('GET','package'),('GET','tty')},
                   'player': {('POST','keys'),('GET','screen'),('POST','resume'),('POST','pause')}}
        if action == 'tty':
            async with self.access[a.id].operation():
                if request.method != 'GET' or a.role(token) != 'owner': raise web.HTTPForbidden()
            return await self.terminal(request, a, role, lambda: a.role(token) == 'owner')
        disposition = 500
        try:
            async with self.access[a.id].operation():
                if a.deleted: raise web.HTTPNotFound()
                role = a.role(token)
                if role is None: raise web.HTTPUnauthorized()
                if (request.method,action) not in allowed[role]:
                    disposition = 403
                    raise web.HTTPForbidden()
                if action == 'keys':
                    body = await request.json()
                    if not isinstance(body,dict) or set(body)!={'keys','session','boundary'}:
                        raise ValueError('Expected keys, session, boundary')
                    if type(body['session']) is not int or type(body['boundary']) is not int:
                        raise ValueError('Position must contain integer session and boundary')
                    consumed, discarded = await run_blocking(a.keys, body['keys'], body['session'], body['boundary'])
                    response = web.Response(text=a.screen(consumed,discarded))
                elif action == 'screen': response = web.Response(text=a.screen())
                elif action == 'resume':
                    await self.resume_attempt(a); response = web.Response(text=a.screen())
                elif action == 'pause':
                    await run_blocking(a.pause, role); response = web.json_response(a.status())
                elif action == 'usage':
                    await run_blocking(a.report_usage, await request.json()); response = web.json_response(a.status())
                elif action == 'package':
                    path = await run_blocking(a.package)
                    buffer = io.BytesIO()
                    with tarfile.open(fileobj=buffer, mode='w') as archive:
                        for name in ('attempt.json','events.jsonl.gz'): archive.add(path/name,arcname=name)
                    response = web.Response(body=buffer.getvalue(),content_type='application/x-tar',
                                            headers={'Content-Disposition':f'attachment; filename="{a.id}.tar"'})
                else: response = web.json_response(a.status())
                disposition = response.status
                return response
        except Conflict:
            disposition = 409
            return web.Response(status=409,text=a.screen())
        except (ValueError,TypeError,KeyError,json.JSONDecodeError) as error:
            disposition = 400
            return web.Response(status=400,text=str(error))

    async def terminal(self, request, a, role, authorized, takeover=False):
        """Stream an attempt's terminal; at most one connection controls input.

        ``authorized`` is rechecked before every key, so revoked credentials stop
        input immediately. With ``takeover`` a new play connection replaces the
        controller (a refreshed or second browser tab) instead of only watching.
        """
        ws = web.WebSocketResponse(max_msg_size=4096, heartbeat=30)
        await ws.prepare(request)
        self.terminals[a.id].add(ws)
        previous = None
        if request.query.get('mode')=='play' and (a.controller is None or takeover):
            previous, a.controller = a.controller, ws
        if previous is not None:
            with contextlib.suppress(ConnectionError, RuntimeError):
                await previous.send_json({'controlling': False, 'status': a.status(), 'taken_over': True})
                await previous.close(code=4001, message=b'Control moved to another window')
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue(maxsize=128)
        def receive_output(data):
            # Capture the status at the durable publication boundary, before
            # the worker can consume another key. Every watcher needs it too.
            packet = (data, {'controlling': a.controller is ws, 'status': a.status()})
            def put():
                if not queue.full(): queue.put_nowait(packet)
                else: asyncio.create_task(ws.close(code=1013,message=b'Slow terminal client'))
            try:
                same_loop = asyncio.get_running_loop() is loop
            except RuntimeError:
                same_loop = False
            if same_loop: put()
            else: loop.call_soon_threadsafe(put)
        a.listeners.add(receive_output)
        if a.frame:
            await ws.send_bytes(('\033[2J\033[H'+a.frame.screen.replace('\n','\r\n')+
                                 f'\033[{a.frame.cursor[1]+1};{a.frame.cursor[0]+1}H').encode('latin1'))
        await ws.send_json({'controlling':a.controller is ws,'status':a.status()})
        async def output():
            while True:
                data = await queue.get()
                try:
                    if isinstance(data,tuple):
                        await ws.send_bytes(data[0])
                        await ws.send_json(data[1])
                    elif isinstance(data,dict): await ws.send_json(data)
                    else: await ws.send_bytes(data)
                finally: queue.task_done()
        task = asyncio.create_task(output())
        try:
            async for message in ws:
                if message.type not in (WSMsgType.TEXT,WSMsgType.BINARY): continue
                if a.controller is not ws:
                    await asyncio.to_thread(a.audit,'WS','tty',role,403)
                    continue
                keys = message.data.decode('latin1') if isinstance(message.data,bytes) else message.data
                try:
                    async with self.access[a.id].operation():
                        if a.deleted or not authorized():
                            await ws.close(code=1008, message=b'Credential revoked')
                            break
                        if a.state in {'paused','saved'}: await self.resume_attempt(a)
                        if a.frame: await run_blocking(a.keys,keys,a.session,a.frame.boundary)
                        await asyncio.to_thread(a.audit,'WS','tty',role,200)
                except web.HTTPTooManyRequests as error:
                    await asyncio.to_thread(a.audit,'WS','tty',role,429)
                    await ws.send_json({'controlling':True,'status':a.status(),
                                        'error':error.text,'code':429})
                except (Conflict,ValueError):
                    await asyncio.to_thread(a.audit,'WS','tty',role,409)
                if queue.full():
                    await ws.close(code=1013,message=b'Slow terminal client')
                    break
                queue.put_nowait({'controlling':a.controller is ws,'status':a.status()})
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError,ConnectionError,RuntimeError): await task
            self.terminals[a.id].discard(ws)
            a.listeners.discard(receive_output)
            if a.controller is ws: a.controller=None
        return ws

    async def guidebook(self, request):
        return web.FileResponse(ROOT/'build/Guidebook.txt', headers={
            'Content-Type': 'text/plain; charset=utf-8', 'X-Content-Type-Options': 'nosniff'})

    async def player_instructions(self, request):
        return web.FileResponse(ROOT/'PLAYER.md')

    def app(self):
        from .browser import browser_headers
        app = web.Application(client_max_size=32768,middlewares=[browser_headers,self.audit_requests])
        app.on_startup.append(self.start); app.on_cleanup.append(self.stop)
        app.router.add_post('/attempts',self.create)
        app.router.add_post('/agent/start',self.agent_start)
        app.router.add_get('/player.md',self.player_instructions)
        app.router.add_get('/guidebook.txt',self.guidebook)
        app.router.add_route('*','/attempts/{id}',self.route)
        app.router.add_route('*','/attempts/{id}/{action}',self.route)
        self.browser.routes(app)
        return app
