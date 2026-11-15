#!/usr/bin/env python3
"""Exercise the real play/watch CLI clients against a running local server."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import pty
import subprocess
import tempfile

from aiohttp import ClientSession

ROOT = Path(__file__).resolve().parents[1]


async def main(url):
    async with ClientSession(base_url=url) as client:
        response = await client.post('/attempts', json={'player_type': 'human'})
        response.raise_for_status()
        created = await response.json()
        base = '/attempts/' + created['id']
        headers = {'Authorization': 'Bearer ' + created['player_token']}
        env = dict(os.environ, NETHACK_OWNER_TOKEN=created['owner_token'])
        async def screen():
            response = await client.get(base+'/screen', headers=headers)
            response.raise_for_status()
            return json.loads((await response.text()).splitlines()[-1])
        master, slave = pty.openpty()
        processes = []
        try:
            with tempfile.TemporaryFile() as play_output, tempfile.TemporaryFile() as watch_output:
                play = subprocess.Popen([str(ROOT/'nethack-run'), 'play', url+base],
                                        stdin=slave, stdout=play_output, stderr=subprocess.STDOUT, env=env)
                processes.append(play)
                watch = subprocess.Popen([str(ROOT/'nethack-run'), 'watch', url+base],
                                         stdout=watch_output, stderr=subprocess.STDOUT, env=env)
                processes.append(watch)
                # Wait for initial rendering before sending through the PTY.
                for _ in range(100):
                    if os.fstat(play_output.fileno()).st_size and os.fstat(watch_output.fileno()).st_size:
                        break
                    await asyncio.sleep(.05)
                else:
                    raise AssertionError('CLI clients did not render the initial screen')
                os.write(master, b'T')
                for _ in range(100):
                    position = await screen()
                    if position['boundary'] == 2:
                        break
                    await asyncio.sleep(.05)
                else:
                    raise AssertionError('PTY key was not consumed')
                os.write(master, b'\x1d')
                await asyncio.to_thread(play.wait, timeout=10)
                assert play.returncode == 0
                assert watch.poll() is None
                async def send(keys):
                    position = await screen()
                    response = await client.post(base+'/keys', headers=headers,
                        json={k: position[k] for k in ('session', 'boundary')} | {'keys': keys})
                    response.raise_for_status()
                    return json.loads((await response.text()).splitlines()[-1])
                status = await send('ester-Bar-Hum-Mal-Cha\ny#quit\ny')
                for _ in range(30):
                    if status['state'] == 'finished':
                        break
                    status = await send(' ')
                assert status['state'] == 'finished'
                await asyncio.to_thread(watch.wait, timeout=10)
                assert watch.returncode == 0
                watch_output.seek(0)
                assert b'You quit' in watch_output.read()
                print('PASS: PTY play input, Ctrl-] disconnect, concurrent watch, final quit rendering')
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=10)
            os.close(master)
            os.close(slave)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    asyncio.run(main(parser.parse_args().url))
