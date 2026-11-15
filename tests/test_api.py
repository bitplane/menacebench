import asyncio
import io
import json
import os
import tarfile
from aiohttp import web, ClientSession, TCPConnector
from contextlib import asynccontextmanager
import pytest

@pytest.fixture(autouse=True)
def inline_workers(monkeypatch):
    if os.environ.get('NETHACK_TEST_LIVE'):
        return
    # This restricted test environment cannot wake asyncio via its socketpair.
    # Exercise HTTP serialization and handlers in memory; live transport is separate.
    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", inline)

from nethack_run.server import Server
from nethack_run.package import verify


def verify_separate_process(package):
    import subprocess
    import sys
    from nethack_run.engine import ROOT
    result = subprocess.run(
        [sys.executable, '-m', 'nethack_run.cli', 'replay', str(package)],
        env=dict(os.environ, PYTHONPATH=str(ROOT/'src')),
        capture_output=True, text=True, check=True, timeout=60)
    return json.loads(result.stdout)


@asynccontextmanager
async def wire_client(server, path):
    runner = web.AppRunner(server.app())
    await runner.setup()
    if os.environ.get('NETHACK_TEST_LIVE'):
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with ClientSession(base_url=f'http://127.0.0.1:{port}') as client:
                yield client
        finally:
            await runner.cleanup()
        return
    from aiohttp.client_proto import ResponseHandler
    transports = []
    class MemoryTransport(asyncio.Transport):
        def __init__(self, protocol, loop):
            super().__init__(); self.protocol=protocol; self.loop=loop; self.closed=False
        def write(self, data):
            if not self.closed: self.loop.call_soon(self.peer.protocol.data_received, bytes(data))
        def is_closing(self): return self.closed
        def get_write_buffer_size(self): return 0
        def pause_reading(self): pass
        def resume_reading(self): pass
        def close(self):
            if self.closed: return
            self.closed=True
            self.loop.call_soon(self.protocol.connection_lost,None)
            self.peer.close()
        def abort(self): self.close()
    class PairConnector(TCPConnector):
        async def _create_connection(self, req, traces, timeout):
            loop=asyncio.get_running_loop()
            server_protocol=runner.server(); client_protocol=ResponseHandler(loop)
            left=MemoryTransport(server_protocol,loop); right=MemoryTransport(client_protocol,loop)
            left.peer=right; right.peer=left
            server_protocol.connection_made(left); client_protocol.connection_made(right)
            transports.extend([left,right])
            return client_protocol
    try:
        async with ClientSession(connector=PairConnector(), base_url='http://localhost') as client:
            yield client
    finally:
        for transport in transports: transport.close()
        await runner.cleanup()


def test_http_lifecycle(tmp_path):
    async def scenario():
        server = Server(tmp_path/'runs')
        async with wire_client(server, tmp_path) as client:
            response = await client.post('/attempts',json={'player_type':'model','model':'test'})
            assert response.status==201
            created = await response.json(); url='/attempts/'+created['id']
            player={'Authorization':'Bearer '+created['player_token']}
            owner={'Authorization':'Bearer '+created['owner_token']}
            assert (await client.get(url+'/screen')).status==401
            assert (await client.get(url+'/package',headers=player)).status==403
            screen = await (await client.get(url+'/screen',headers=player)).text()
            position=json.loads(screen.splitlines()[-1])
            body={k:position[k] for k in ('session','boundary')}|{'keys':'Tester-Bar-Hum-Mal-Cha\ny..'}
            first=await client.post(url+'/keys',headers=player,json=body)
            assert first.status==200
            assert (await client.post(url+'/keys',headers=player,json=body)).status==409
            await client.post(url+'/pause',headers=owner)
            assert (await client.post(url+'/resume',headers=player)).status==200
            async def send(keys):
                text=await (await client.get(url+'/screen',headers=player)).text()
                pos=json.loads(text.splitlines()[-1])
                response=await client.post(url+'/keys',headers=player,json={k:pos[k] for k in ('session','boundary')}|{'keys':keys})
                assert response.status==200
                return json.loads((await response.text()).splitlines()[-1])
            state=await send('#quit\ny')
            for _ in range(20):
                if state['state']=='finished':break
                state=await send(' ')
            assert state['state']=='finished'
            assert (await client.put(url+'/usage',headers=owner,json={'cost':0.01})).status==200
            package=await client.get(url+'/package',headers=owner)
            assert package.status==200
            folder=tmp_path/'package';folder.mkdir()
            with tarfile.open(fileobj=io.BytesIO(await package.read())) as archive:
                archive.extractall(folder,filter='data')
            assert verify_separate_process(folder)['verified']
            import os, shutil
            if os.environ.get('NETHACK_PROTOCOL_DEMO'):
                output=__import__('pathlib').Path(os.environ['NETHACK_PROTOCOL_DEMO'])
                output.mkdir(parents=True,exist_ok=True)
                for name in ('attempt.json','events.jsonl.gz'):shutil.copyfile(folder/name,output/name)
                verify(output,output/'replay.cast')
    asyncio.run(asyncio.wait_for(scenario(), 15))


def test_hosted_auth_limits(tmp_path):
    import hashlib
    async def scenario():
        server=Server(tmp_path,contributors={hashlib.sha256(b'test-key').hexdigest():'tester'},concurrency=1)
        async with wire_client(server, tmp_path) as client:
            assert (await client.post('/attempts',json={})).status==401
            headers={'Authorization':'Bearer test-key'}
            assert (await client.post('/attempts',headers=headers,json={})).status==201
            assert (await client.post('/attempts',headers=headers,json={})).status==429
    asyncio.run(asyncio.wait_for(scenario(), 15))


def test_terminal_control_and_watch(tmp_path):
    async def scenario():
        server=Server(tmp_path/'runs')
        async with wire_client(server,tmp_path) as client:
            created=await (await client.post('/attempts',json={'player_type':'human'})).json()
            base='/attempts/'+created['id']
            headers={'Authorization':'Bearer '+created['owner_token']}
            first=await client.ws_connect(base+'/tty?mode=play',headers=headers)
            assert (await first.receive()).type.name=='BINARY'
            assert (await first.receive_json())['controlling']
            watcher=await client.ws_connect(base+'/tty?mode=play',headers=headers)
            await watcher.receive()
            assert not (await watcher.receive_json())['controlling']
            a=server.attempts[created['id']]
            await watcher.send_bytes(b'ignored')
            await asyncio.sleep(.01)
            assert a.frame.boundary==1
            await first.send_bytes(b'T')
            received=[]
            for _ in range(3):
                message=await first.receive()
                received.append(message)
                if message.type.name=='TEXT':break
            assert a.frame.boundary==2
            assert any(m.type.name=='BINARY' for m in received)
            assert (await watcher.receive()).type.name=='BINARY'
            state = await watcher.receive_json()
            assert not state['controlling'] and state['status']['boundary'] == 2
            await first.close()
            player = {'Authorization': 'Bearer '+created['player_token']}
            response = await client.post(base+'/keys', headers=player, json={
                'session': 1, 'boundary': 2, 'keys': 'ester-Bar-Hum-Mal-Cha\ny#quit\ny'+' '*20})
            assert response.status == 200
            while True:
                message = await watcher.receive()
                if message.type.name == 'TEXT' and json.loads(message.data)['status']['state'] == 'finished':
                    break
            await first.close();await watcher.close()
    asyncio.run(asyncio.wait_for(scenario(),15))


@pytest.mark.parametrize('role',list('abchkmprRstvw'))
def test_character_selection_through_api(tmp_path,role):
    async def scenario():
        server=Server(tmp_path/'runs')
        async with wire_client(server,tmp_path) as client:
            created=await (await client.post('/attempts',json={})).json()
            base='/attempts/'+created['id'];headers={'Authorization':'Bearer '+created['player_token']}
            async def send(keys):
                text=await (await client.get(base+'/screen',headers=headers)).text()
                position=json.loads(text.splitlines()[-1])
                response=await client.post(base+'/keys',headers=headers,json={k:position[k] for k in ('session','boundary')}|{'keys':keys})
                assert response.status==200
                return await response.text()
            text=await send('Tester\nn'+role)
            for _ in range(12):
                if server.attempts[created['id']].character:break
                text=await send('y' if 'Is this ok?' in text else '*')
            a=server.attempts[created['id']]
            assert a.character
            owner={'Authorization':'Bearer '+created['owner_token']}
            await client.post(base+'/pause',headers=owner)
            a.expire()
            assert verify_separate_process(a.package())['verified']
    asyncio.run(asyncio.wait_for(scenario(),15))


def test_save_and_discard_through_api(tmp_path):
    async def scenario():
        server=Server(tmp_path/'runs')
        async with wire_client(server,tmp_path) as client:
            created=await (await client.post('/attempts',json={})).json()
            base='/attempts/'+created['id'];headers={'Authorization':'Bearer '+created['player_token']}
            keys='Tester-Bar-Hum-Mal-Cha\ny..Sy '
            response=await client.post(base+'/keys',headers=headers,json={'session':1,'boundary':1,'keys':keys+'discarded'})
            state=json.loads((await response.text()).splitlines()[-1])
            assert state['state']=='saved' and state['discarded']==9
            response=await client.post(base+'/resume',headers=headers)
            state=json.loads((await response.text()).splitlines()[-1]);assert state['session']==2
            response=await client.post(base+'/keys',headers=headers,json={'session':1,'boundary':1,'keys':'a'})
            assert response.status==409
            response=await client.post(base+'/keys',headers=headers,json={'session':2,'boundary':1,'keys':'a..'})
            assert response.status==200
            a=server.attempts[created['id']];a.pause();a.expire()
            assert verify_separate_process(a.package())['sessions']==2
    asyncio.run(asyncio.wait_for(scenario(),15))


@pytest.mark.parametrize('name', [
    'Tester-Wiz-Elf-Fem-Cha', 'Tester-Wiz-Orc-Mal-Cha',
    'Tester-Arc-Dwa-Mal-Law', 'Tester-Hea-Gno-Fem-Neu',
    'Tester-Kni-Hum-Fem-Law'])
def test_races_genders_alignments_through_api(tmp_path, name):
    async def scenario():
        server = Server(tmp_path/'runs')
        async with wire_client(server, tmp_path) as client:
            created = await (await client.post('/attempts', json={})).json()
            base = '/attempts/'+created['id']
            player = {'Authorization': 'Bearer '+created['player_token']}
            owner = {'Authorization': 'Bearer '+created['owner_token']}
            response = await client.post(base+'/keys', headers=player, json={
                'session': 1, 'boundary': 1, 'keys': name+'\ny'})
            assert response.status == 200
            a = server.attempts[created['id']]
            for field, value in zip(('role', 'race', 'gender', 'align'), name.split('-')[1:]):
                assert a.character[field] == value
            assert (await client.post(base+'/pause', headers=owner)).status == 200
            a.expire()
            assert verify_separate_process(a.package())['verified']
    asyncio.run(asyncio.wait_for(scenario(), 20))


def test_long_run_death_and_prompt_recovery_through_api(tmp_path):
    async def scenario():
        server = Server(tmp_path/'runs')
        async with wire_client(server, tmp_path) as client:
            created = await (await client.post('/attempts', json={})).json()
            base = '/attempts/'+created['id']
            player = {'Authorization': 'Bearer '+created['player_token']}
            owner = {'Authorization': 'Bearer '+created['owner_token']}
            async def screen():
                return await (await client.get(base+'/screen', headers=player)).text()
            async def send(keys):
                position = json.loads((await screen()).splitlines()[-1])
                response = await client.post(base+'/keys', headers=player, json={
                    k: position[k] for k in ('session', 'boundary')} | {'keys': keys})
                assert response.status == 200
                return await response.text()
            async def pause_resume():
                before = await screen()
                assert (await client.post(base+'/pause', headers=owner)).status == 200
                assert (await client.post(base+'/resume', headers=player)).status == 200
                assert await screen() == before
            await send('Tes')
            await pause_resume()  # Mid-name prompt.
            await send('ter-Bar-Hum-Mal-Cha\nyi')
            await pause_resume()  # Inventory menu.
            text = await send('\x1b')
            for _ in range(10000):
                if json.loads(text.splitlines()[-1])['state'] == 'finished':
                    break
                text = await send(' ' if any(p in text for p in ('--More--', '[ynq]', '[ynaq]')) else 'm.')
            a = server.attempts[created['id']]
            assert a.state == 'finished' and a.outcome == 'death'
            result = verify_separate_process(a.package())
            assert result['verified'] and result['outcome'] == 'death'
    asyncio.run(asyncio.wait_for(scenario(), 120))


def test_probing_other_routes_is_audited(tmp_path):
    async def scenario():
        server=Server(tmp_path/'runs')
        async with wire_client(server,tmp_path) as client:
            created=await (await client.post('/attempts',json={})).json()
            headers={'Authorization':'Bearer '+created['player_token']}
            assert (await client.get('/not-an-endpoint',headers=headers)).status==403
            assert (await client.post('/attempts',headers=headers,json={})).status==403
            assert (await client.get('/attempts/wrong-id/screen',headers=headers)).status==403
            records=[e for e in server.attempts[created['id']].journal.events if e['type']=='request']
            assert [e['path'] for e in records]==['/not-an-endpoint','/attempts','/attempts/wrong-id/screen']
            assert all(e['disposition']==403 for e in records)
    asyncio.run(asyncio.wait_for(scenario(),15))


@pytest.mark.parametrize('limit',['idle','duration','events'])
def test_limits_and_expiry(tmp_path,limit):
    async def scenario():
        import time
        limits={'idle':10,'duration':20,'events':10000,'expiry':30}
        server=Server(tmp_path/'runs',limits=limits)
        async with wire_client(server,tmp_path) as client:
            created=await (await client.post('/attempts',json={})).json()
            a=server.attempts[created['id']]
            if limit=='idle':a.last_activity=time.time()-11
            elif limit=='duration':a.started=time.monotonic()-21
            else:server.limits['events']=len(a.journal.events)
            await server.enforce_limits()
            assert a.state=='paused'
            a.last_activity=time.time()-31
            await server.enforce_limits()
            assert a.outcome=='abandoned'
            assert verify(a.package())['verified']
    asyncio.run(asyncio.wait_for(scenario(),15))
