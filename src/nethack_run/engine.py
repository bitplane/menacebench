"""Input-boundary transport shared by live play, rebuilding and replay."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import time
from dataclasses import dataclass

ROOT = Path(__file__).resolve().parents[2]
GAME = ROOT / 'build/install/games/lib/nethackdir'
MARKER = b'\x1b]7777;'
SGR = re.compile(rb'\x1b\[[0-9;]*m')
FIXED_DATE = '20260506120000'


def session_seed(seed: str, index: int) -> int:
    return int.from_bytes(hashlib.sha256(f'nethack-run-v1:{seed}:{index}'.encode()).digest()[:4], 'big')


def clean_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=False)
    for source in GAME.iterdir():
        if source.is_file() and source.name not in {'record', 'logfile', 'xlogfile', 'perm', 'paniclog'}:
            shutil.copy2(source, path / source.name)
    for name in ('record', 'logfile', 'xlogfile', 'perm'):
        (path / name).touch()
    (path / 'save').mkdir(exist_ok=True)
    shutil.copyfile(ROOT / 'config/nethackrc', path / '.nethackrc')


@dataclass(frozen=True)
class Frame:
    boundary: int
    screen: str
    cursor: tuple[int, int]
    terminal: bytes
    character: dict | None = None

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps([self.screen, self.cursor], separators=(',', ':')).encode()).hexdigest()


class Game:
    def __init__(self, directory: Path, seed: int, timeout: float = 30, on_consumed=None):
        self.directory = directory.resolve()
        self.timeout = timeout
        self.on_consumed = on_consumed
        self.pending = bytearray()
        self.output = bytearray()
        self.consumed: list[tuple[int, int]] = []
        self.character = None
        self.boundary = 0
        self.exited = False
        self.exit_code = None
        # Deliberately do not inherit caller-supplied NetHack options or RNG settings.
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(self.directory),
               'USER': 'player', 'LOGNAME': 'player', 'TERM': 'xterm',
               'LANG': 'C', 'LC_ALL': 'C', 'TZ': 'UTC',
               'LD_LIBRARY_PATH': str(ROOT / 'lib'),
               'HACKDIR': str(self.directory), 'NETHACKOPTIONS': str(self.directory / '.nethackrc'),
               'NETHACK_SEED': str(seed), 'NETHACK_FIXED_DATETIME': FIXED_DATE,
               'NOMUX_MARKERS': '1'}
        self.process = subprocess.Popen([str(self.directory / 'nethack')], cwd=self.directory,
                                        env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, start_new_session=True)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.frame = self.advance()
        self.last_frame = self.frame

    def _message(self):
        deadline = time.monotonic() + self.timeout
        while True:
            start = self.pending.find(MARKER)
            if start >= 0:
                end = self.pending.find(b'\x07', start)
                if end >= 0:
                    fields = dict(x.split('=', 1) for x in self.pending[start+len(MARKER):end].decode('ascii').split(';'))
                    size = int(fields['LEN'])
                    if size > 100000:
                        raise RuntimeError('Oversized game protocol message')
                    if len(self.pending) >= end + 1 + size:
                        self.output.extend(self.pending[:start])
                        payload = bytes(self.pending[end+1:end+1+size])
                        del self.pending[:end+1+size]
                        return fields, payload
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise TimeoutError('Game did not reach an input boundary')
            data = os.read(self.process.stdout.fileno(), 65536)
            if not data:
                self.output.extend(self.pending)
                self.pending.clear()
                self.exited = True
                self.exit_code = self.process.wait(timeout=5)
                return None, b''
            self.pending.extend(data)
            if len(self.pending)+len(self.output)>16*1024*1024:
                raise RuntimeError("Game output exceeds the per-boundary limit")

    def advance(self) -> Frame | None:
        while True:
            fields, payload = self._message()
            if fields is None:
                return None
            kind = fields['KIND']
            if kind == 'consumed':
                consumed = (int(fields['SEQ']), int(fields['KEY']))
                self.consumed.append(consumed)
                if self.on_consumed: self.on_consumed(*consumed)
            elif kind == 'character':
                self.character = {'name': bytes.fromhex(fields['NAME']).decode('latin1'),
                                  **{k: fields[k.upper()] for k in ('role','race','gender','align')}}
            elif kind == 'input':
                n = int(fields['SEQ'])
                if n != self.boundary + 1:
                    raise RuntimeError(f'Nonsequential boundary {n}, expected {self.boundary+1}')
                self.boundary = n
                plain = SGR.sub(b'', payload).replace(b'\x0e', b'').replace(b'\x0f', b'')
                lines = plain.decode('latin1').split('\n')
                if len(lines) != 24 or any(len(line) > 80 for line in lines):
                    raise RuntimeError('Invalid screen dimensions')
                return Frame(n, '\n'.join(line.ljust(80) for line in lines),
                             (int(fields['CX']), int(fields['CY'])), bytes(self.output), self.character)
            elif kind == 'exit':
                # The final frame is terminal output, not a key-read boundary.
                continue
            elif kind != 'anim':
                raise RuntimeError(f'Unknown game message {kind}')

    def key(self, key: int) -> Frame | None:
        if self.exited or not 1 <= key <= 255:
            raise ValueError('Key must be a non-NUL byte in a running game')
        n = self.boundary
        self.last_frame = self.frame
        self.output.clear()
        self.process.stdin.write(bytes([key]))
        self.process.stdin.flush()
        self.frame = self.advance()
        if not self.consumed or self.consumed[-1] != (n, key):
            raise RuntimeError(f'Missing consumed-key acknowledgment at {n}')
        return self.frame

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        self.selector.close()
        self.process.stdin.close()
        self.process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def replay(directory: Path, seed: int, keys: list[int], checkpoints: dict[int, str] | None = None) -> Game:
    """Return the rebuilt live process; caller owns it and must close it."""
    game = Game(directory, seed)
    try:
        def check():
            if game.frame and checkpoints and game.boundary in checkpoints:
                expected = checkpoints[game.boundary]
                if game.frame.digest != expected:
                    raise ValueError(f'Divergence at boundary {game.boundary}: expected {expected}\n{game.frame.screen}')
        check()
        for key in keys:
            if game.exited:
                raise ValueError('Recorded keys continue after game exit')
            game.key(key)
            check()
        return game
    except BaseException:
        game.close()
        raise
