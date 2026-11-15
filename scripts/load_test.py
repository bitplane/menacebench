"""Exercise 100 live game processes in disposable storage, never the live volume."""
import asyncio
import os
from pathlib import Path
import tempfile
import time
from aiohttp import web
from nethack_run.server import Server


def rss(pid):
    return int(next(line.split()[1] for line in Path(f'/proc/{pid}/status').read_text().splitlines() if line.startswith('VmRSS:')))


async def main():
    with tempfile.TemporaryDirectory(prefix='nethack-load-') as directory:
        server = Server(Path(directory))
        await server.start(None)
        start = time.monotonic()
        try:
            from nethack_run.attempt import Attempt
            for _ in range(100):
                async with server.admission_lock:
                    server.check_capacity(None)
                    a, _, _ = await asyncio.to_thread(Attempt.create, server.root, {}, 'load-test')
                    server.attempts[a.id] = a
            assert sum(a.state == 'running' for a in server.attempts.values()) == 100
            try:
                server.check_capacity(None)
            except web.HTTPTooManyRequests:
                pass
            else:
                raise AssertionError('101st game admitted')
            print(f'100 running games: {time.monotonic()-start:.2f}s; game RSS sum {sum(rss(a.game.process.pid) for a in server.attempts.values())/1024:.1f} MiB; server RSS {rss(os.getpid())/1024:.1f} MiB', flush=True)
            for a in list(server.attempts.values())[:10]:
                await asyncio.to_thread(a.pause, 'load-test')
            await asyncio.gather(*(server.resume_attempt(a) for a in list(server.attempts.values())[:10]))
            assert sum(a.state == 'running' for a in server.attempts.values()) == 100
            print('Ten paused slots released and resumed successfully.', flush=True)
        finally:
            await server.stop(None)


if __name__ == '__main__':
    asyncio.run(main())
