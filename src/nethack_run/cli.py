"""Command-line server, verifier, exporter and terminal clients."""
import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import sys
import termios
import tty
from aiohttp import web, ClientSession, UnixConnector, WSMsgType
from .server import Server
from .package import verify


async def terminal(args):
    token = os.environ.get('NETHACK_OWNER_TOKEN')
    if not token: raise SystemExit('Set NETHACK_OWNER_TOKEN to the owner bearer token')
    connector = UnixConnector(path=args.unix_socket) if args.unix_socket else None
    url = args.url.rstrip('/')+'/tty?mode='+args.command
    loop = asyncio.get_running_loop()
    pending = asyncio.Queue()
    def stdin_ready():
        data = os.read(sys.stdin.fileno(), 4096)
        pending.put_nowait(data)
    saved = None
    async with ClientSession(connector=connector) as client:
        async with client.ws_connect(url,headers={'Authorization':'Bearer '+token}) as ws:
            async def input_loop():
                while True:
                    data = await pending.get()
                    if not data or b'\x1d' in data: # Ctrl-] disconnects without sending a game key.
                        await ws.close();return
                    await ws.send_bytes(data)
            task = None
            try:
                if args.command=='play':
                    if sys.stdin.isatty():
                        saved=termios.tcgetattr(sys.stdin);tty.setraw(sys.stdin)
                    loop.add_reader(sys.stdin.fileno(), stdin_ready)
                    task=asyncio.create_task(input_loop())
                async for message in ws:
                    if message.type==WSMsgType.BINARY:
                        sys.stdout.buffer.write(message.data);sys.stdout.buffer.flush()
                    elif message.type==WSMsgType.TEXT:
                        state=json.loads(message.data)
                        if args.command=='play' and not state.get('controlling',False):
                            print('\r\nAnother client controls this attempt; viewing only.',file=sys.stderr)
                        if state.get('status',{}).get('state')=='finished': break
            finally:
                if args.command=='play': loop.remove_reader(sys.stdin.fileno())
                if task:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):await task
                if saved:termios.tcsetattr(sys.stdin,termios.TCSADRAIN,saved)
                sys.stdout.write('\033[0m\033[?25h\n');sys.stdout.flush()


def main(argv=None):
    parser=argparse.ArgumentParser(prog='nethack-run')
    sub=parser.add_subparsers(dest='command',required=True)
    serve=sub.add_parser('serve')
    serve.add_argument('--state',type=Path,default=Path('runs'))
    serve.add_argument('--host',default='127.0.0.1');serve.add_argument('--port',type=int,default=8756)
    serve.add_argument('--unix-socket');serve.add_argument('--instance',default='local')
    serve.add_argument('--contributors',type=Path,help='JSON mapping SHA-256 contributor-key hashes to contributor names')
    serve.add_argument('--github-auth',type=Path,help='Private GitHub OAuth configuration JSON for the owner dashboard')
    serve.add_argument('--concurrency',type=int,default=2)
    serve.add_argument('--idle',type=float,default=1800)
    serve.add_argument('--duration',type=float,default=86400)
    serve.add_argument('--events',type=int,default=1000000)
    serve.add_argument('--expiry',type=float,default=604800)
    for name in ('replay','export'):
        p=sub.add_parser(name);p.add_argument('package',type=Path)
        if name=='export':p.add_argument('--output',type=Path,required=True)
        p.add_argument('--diagnostic-cross-build',action='store_true',help='Compare another build without claiming official verification')
    for name in ('play','watch'):
        p=sub.add_parser(name);p.add_argument('url');p.add_argument('--unix-socket')
    args=parser.parse_args(argv)
    if args.command=='serve':
        limits={k:getattr(args,k) for k in ('idle','duration','events','expiry')}
        if any(value<=0 for value in limits.values()) or args.concurrency<1:parser.error('Limits must be positive')
        contributors=json.loads(args.contributors.read_text()) if args.contributors else None
        github_auth=json.loads(args.github_auth.read_text()) if args.github_auth else None
        server=Server(args.state,args.instance,contributors,args.concurrency,limits,github_auth)
        web.run_app(server.app(),host=args.host if not args.unix_socket else None,
                    port=args.port if not args.unix_socket else None,path=args.unix_socket,access_log=None)
    elif args.command in ('replay','export'):
        from .images import dispatch
        if args.diagnostic_cross_build or not dispatch(args.package, getattr(args,'output',None)):
            print(json.dumps(verify(args.package, getattr(args,'output',None),args.diagnostic_cross_build),indent=2))
    else:asyncio.run(terminal(args))

if __name__=='__main__':main()
