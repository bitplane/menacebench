#!/usr/bin/env python3
"""Exercise the running public API, download, replay and export one run."""
import argparse
import asyncio
import io
import json
from pathlib import Path
import subprocess
import tarfile
from aiohttp import ClientSession, UnixConnector

ROOT=Path(__file__).resolve().parents[1]

async def main(args):
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    connector=UnixConnector(path=args.unix_socket) if args.unix_socket else None
    async with ClientSession(connector=connector,base_url=args.url) as client:
        async with client.post('/attempts',json={'player_type':'model','agent_tool':'harness-demo','notes':'Implementation smoke test'}) as response:
            response.raise_for_status();created=await response.json()
        base='/attempts/'+created['id']
        player={'Authorization':'Bearer '+created['player_token']}
        owner={'Authorization':'Bearer '+created['owner_token']}
        async def send(keys):
            async with client.get(base+'/screen',headers=player) as response:
                response.raise_for_status();position=json.loads((await response.text()).splitlines()[-1])
            async with client.post(base+'/keys',headers=player,json={k:position[k] for k in ('session','boundary')}|{'keys':keys}) as response:
                response.raise_for_status();return json.loads((await response.text()).splitlines()[-1])
        # Ordinary creation inputs select a fixed role/race/gender/alignment.
        status=await send('Demo-Bar-Hum-Mal-Cha\ny')
        status=await send('..')
        if args.save_resume:
            status=await send('Sy ')
            if status['state']!='saved':raise RuntimeError('Demo did not save')
            async with client.post(base+'/resume',headers=player) as response:response.raise_for_status()
            status=await send('a..')
        async with client.post(base+'/pause',headers=owner) as response:response.raise_for_status()
        async with client.post(base+'/resume',headers=player) as response:response.raise_for_status()
        status=await send('#quit\ny')
        for _ in range(30):
            if status['state']=='finished':break
            status=await send(' ')
        if status['state']!='finished':raise RuntimeError('Demo did not reach final exit')
        async with client.put(base+'/usage',headers=owner,json={'input_tokens':None,'output_tokens':None,'cost':None}) as response:response.raise_for_status()
        async with client.get(base+'/package',headers=owner) as response:
            response.raise_for_status();data=await response.read()
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            if set(archive.getnames())!={'attempt.json','events.jsonl.gz'}:raise ValueError('Unexpected package members')
            for name in archive.getnames():
                with archive.extractfile(name) as file:(output/name).write_bytes(file.read())
    subprocess.run([str(ROOT/'nethack-run'),'replay',str(output)],check=True)
    subprocess.run([str(ROOT/'nethack-run'),'export',str(output),'--output',str(output/'replay.cast')],check=True)
    print('Verified package and replay:',output)

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',default='http://127.0.0.1:8000')
    parser.add_argument('--unix-socket')
    parser.add_argument('--save-resume',action='store_true',help='Include a save and restore into a second session')
    parser.add_argument('--output',type=Path,default=ROOT/'build/demo')
    asyncio.run(main(parser.parse_args()))
