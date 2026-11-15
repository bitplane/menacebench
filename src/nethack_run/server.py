"""HTTP and terminal API over serialized attempt controllers."""
from __future__ import annotations
import asyncio
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tarfile
import time
from aiohttp import web, WSMsgType
from .attempt import Attempt, Conflict
from .engine import ROOT


class Server:
    def __init__(self, root, instance='local', contributors=None, concurrency=100, limits=None, github_auth=None):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.instance = instance
        self.contributors = contributors
        self.concurrency = concurrency
        self.limits = limits or {'idle': 1800, 'duration': 86400, 'events': 1000000, 'expiry': 604800}
        self.attempts = {}
        self.token_index = {}
        self.admission_lock = asyncio.Lock()
        self.monitor_task = None
        self.state_lock = None
        from .browser import Browser
        self.browser = Browser(self, github_auth)

    async def start(self, app):
        import fcntl
        self.state_lock = (self.root/".server.lock").open("a")
        fcntl.flock(self.state_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        for path in self.root.iterdir():
            if path.is_dir() and (path/'private.json').exists():
                attempt = Attempt(path)
                await asyncio.to_thread(attempt.recover)
                self.attempts[attempt.id] = attempt
                self.index_tokens(attempt)
        self.monitor_task = asyncio.create_task(self.monitor())

    async def stop(self, app):
        if self.monitor_task:
            self.monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError): await self.monitor_task
        for attempt in self.attempts.values():
            if attempt.state == 'running': await asyncio.to_thread(attempt.pause, 'shutdown')

        if self.state_lock: self.state_lock.close()

    async def monitor(self):
        while True:
            await asyncio.sleep(1)
            await self.enforce_limits()

    async def enforce_limits(self):
        for a in list(self.attempts.values()):
            if a.state == 'running':
                reason = ('idle' if time.time()-a.last_activity >= self.limits['idle'] else
                          'duration_limit' if a.clock() >= self.limits['duration'] else
                          'event_limit' if len(a.journal.events) >= self.limits['events'] else None)
                if reason:
                    with contextlib.suppress(Conflict): await asyncio.to_thread(a.pause, reason)
            if a.state in {'paused','saved'} and time.time()-a.last_activity >= self.limits['expiry']:
                await asyncio.to_thread(a.expire)

    def index_tokens(self, attempt):
        for role in ('owner','player'):
            self.token_index[attempt.info[role+'_hash']] = (attempt,role)

    @web.middleware
    async def audit_requests(self, request, handler):
        token = request.headers.get('Authorization','').removeprefix('Bearer ')
        known = self.token_index.get(hashlib.sha256(token.encode()).hexdigest()) if token else None
        status = 500
        try:
            if known and request.path != '/player.md':
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
        if not isinstance(metadata, dict) or set(metadata)-{'player_type','agent_tool','tool_version','model','reasoning_effort','notes'}:
            raise web.HTTPBadRequest(text='Invalid player metadata')
        if metadata.get('player_type') not in {None,'human','model'}:
            raise web.HTTPBadRequest(text='Invalid player_type')
        if any(value is not None and (not isinstance(value,str) or len(value)>4096) for value in metadata.values()):
            raise web.HTTPBadRequest(text='Metadata must contain strings or null')
        async with self.admission_lock:
            self.check_capacity(contributor)
            a, owner, player = await asyncio.to_thread(Attempt.create, self.root, metadata, self.instance, contributor, self.limits)
            self.attempts[a.id] = a
            self.index_tokens(a)
        if player_only:
            await asyncio.to_thread(a.audit, request.method, request.path, 'agent-start', 201)
            return web.json_response({'id':a.id, 'player_token':player,
                                      'attempt_path':'/attempts/'+a.id,
                                      'instructions_path':'/player.md', 'status':a.status()}, status=201)
        return web.json_response({'id':a.id,'owner_token':owner,'player_token':player,'status':a.status()}, status=201)

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
            await asyncio.to_thread(attempt.resume)

    async def route(self, request):
        a = self.attempts.get(request.match_info['id'])
        if a is None: raise web.HTTPNotFound()
        token = request.headers.get('Authorization','').removeprefix('Bearer ')
        role = a.role(token)
        if role is None: raise web.HTTPUnauthorized()
        action = request.match_info.get('action','')
        allowed = {'owner': {('POST','pause'),('PUT','usage'),('GET',''),('GET','package'),('GET','tty')},
                   'player': {('POST','keys'),('GET','screen'),('POST','resume'),('POST','pause')}}
        disposition = 500
        try:
            if (request.method,action) not in allowed[role]:
                disposition = 403
                raise web.HTTPForbidden()
            if action == 'keys':
                body = await request.json()
                if not isinstance(body,dict) or set(body)!={'keys','session','boundary'}:
                    raise ValueError('Expected keys, session, boundary')
                if type(body['session']) is not int or type(body['boundary']) is not int:
                    raise ValueError('Position must contain integer session and boundary')
                consumed, discarded = await asyncio.to_thread(a.keys, body['keys'], body['session'], body['boundary'])
                response = web.Response(text=a.screen(consumed,discarded))
            elif action == 'screen': response = web.Response(text=a.screen())
            elif action == 'resume':
                await self.resume_attempt(a); response = web.Response(text=a.screen())
            elif action == 'pause':
                await asyncio.to_thread(a.pause, role); response = web.json_response(a.status())
            elif action == 'usage':
                await asyncio.to_thread(a.report_usage, await request.json()); response = web.json_response(a.status())
            elif action == 'package':
                path = await asyncio.to_thread(a.package)
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode='w') as archive:
                    for name in ('attempt.json','events.jsonl.gz'): archive.add(path/name,arcname=name)
                response = web.Response(body=buffer.getvalue(),content_type='application/x-tar',
                                        headers={'Content-Disposition':f'attachment; filename="{a.id}.tar"'})
            elif action == 'tty':
                disposition = 101
                return await self.terminal(request,a,role)
            else: response = web.json_response(a.status())
            disposition = response.status
            return response
        except Conflict:
            disposition = 409
            return web.Response(status=409,text=a.screen())
        except (ValueError,TypeError,KeyError,json.JSONDecodeError) as error:
            disposition = 400
            return web.Response(status=400,text=str(error))

    async def terminal(self, request, a, role):
        ws = web.WebSocketResponse(max_msg_size=4096, heartbeat=30)
        await ws.prepare(request)
        controlling = request.query.get('mode')=='play' and a.controller is None
        if controlling: a.controller = ws
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue(maxsize=128)
        def receive_output(data):
            # Capture the status at the durable publication boundary, before
            # the worker can consume another key. Every watcher needs it too.
            packet = (data, {'controlling': controlling, 'status': a.status()})
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
        await ws.send_json({'controlling':controlling,'status':a.status()})
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
                if not controlling:
                    await asyncio.to_thread(a.audit,'WS','tty',role,403)
                    continue
                keys = message.data.decode('latin1') if isinstance(message.data,bytes) else message.data
                try:
                    if a.state in {'paused','saved'}: await self.resume_attempt(a)
                    if a.frame: await asyncio.to_thread(a.keys,keys,a.session,a.frame.boundary)
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
                queue.put_nowait({'controlling':True,'status':a.status()})
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError,ConnectionError,RuntimeError): await task
            a.listeners.discard(receive_output)
            if a.controller is ws: a.controller=None
        return ws

    async def player_instructions(self, request):
        return web.FileResponse(ROOT/'PLAYER.md')

    def app(self):
        from .browser import browser_headers
        app = web.Application(client_max_size=32768,middlewares=[browser_headers,self.audit_requests])
        app.on_startup.append(self.start); app.on_cleanup.append(self.stop)
        app.router.add_post('/attempts',self.create)
        app.router.add_post('/agent/start',self.agent_start)
        app.router.add_get('/player.md',self.player_instructions)
        app.router.add_route('*','/attempts/{id}',self.route)
        app.router.add_route('*','/attempts/{id}/{action}',self.route)
        self.browser.routes(app)
        return app
