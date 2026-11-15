"""Serialized attempt lifecycle; the journal is authoritative."""
from __future__ import annotations
import gzip
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import threading
import time
import uuid
from .engine import ROOT, FIXED_DATE, Game, clean_directory, replay, session_seed
from .storage import Journal, atomic_json, atomic_bytes, snapshot, sync_dir
from .package import build_identity, exit_result


class Conflict(Exception):
    pass


class Attempt:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.pause_requested = threading.Event()
        self.pause_reason = "owner"
        self.advancing = False
        self.info = json.loads((path/'private.json').read_bytes())
        self.id = self.info['id']
        self.journal = Journal(path/'events.jsonl')
        if not self.journal.events:
            self.journal.append('created', attempt_id=self.id, metadata=self.info['metadata'], instance=self.info['instance'],
                                player_md_digest=self.info['player_digest'], build_identity=self.info['build_identity'],
                                image_digest=self.info['image_digest'], fixed_game_date=FIXED_DATE)
        self.game = None
        self.frame = None
        self.started = None
        self.session = 0
        self.elapsed = 0.0
        self.state = 'new'
        self.outcome = None
        self.result = None
        self.usage = None
        self.character = None
        self.last_activity = time.time()
        self.terminal = b''
        self.listeners = set()
        self.controller = None
        for e in self.journal.events:
            self.session = max(self.session, e.get('session', 0))
            if e['type'] == 'session_start':
                self.state = 'running'; self.elapsed = 0.0
            elif e['type'] == 'key': self.elapsed = e['elapsed']
            elif e['type'] == 'pause':
                self.state = 'paused'; self.elapsed = e['elapsed']; self.last_activity = e['wall_time']
            elif e['type'] == 'resume': self.state = 'running'
            elif e['type'] == 'session_end':
                self.state = 'saved' if e['outcome'] == 'saved' else 'finished'
                self.outcome = e['outcome']; self.result = e['result']; self.elapsed = e['elapsed']
                self.last_activity = e['wall_time']
            elif e['type'] == 'abandoned':
                self.state = 'finished'; self.outcome = 'abandoned'; self.result = None
            elif e['type'] == 'usage': self.usage = e['usage']
            elif e['type'] == 'checkpoint' and e.get('character'): self.character = e['character']

    @classmethod
    def create(cls, root, metadata, instance, contributor=None, limits=None):
        identifier = str(uuid.uuid4())
        path = root/identifier; path.mkdir(mode=0o700)
        owner, player = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        info = {'id': identifier, 'seed': secrets.token_hex(32), 'metadata': metadata,
                'instance': instance, 'contributor': contributor, 'limits': limits or {},
                'owner_hash': hashlib.sha256(owner.encode()).hexdigest(),
                'player_hash': hashlib.sha256(player.encode()).hexdigest(),
                'build_identity': build_identity(), 'image_digest': os.environ.get('NETHACK_IMAGE_DIGEST'),
                'player_digest': hashlib.sha256((ROOT/'PLAYER.md').read_bytes()).hexdigest()}
        atomic_json(path/'private.json', info); sync_dir(root)
        attempt = cls(path)
        attempt.start_session()
        return attempt, owner, player

    def role(self, token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        for role in ('owner', 'player'):
            if secrets.compare_digest(self.info[role+'_hash'], digest): return role
        return None

    @property
    def work(self): return self.path/'work'

    def clock(self):
        return self.elapsed + (time.monotonic()-self.started if self.started is not None else 0)

    def checkpoint(self, frame=None):
        frame = frame or self.frame
        if frame:
            self.journal.append('checkpoint', session=self.session, boundary=frame.boundary, hash=frame.digest, character=frame.character)
            self.character = frame.character or self.character

    def publish(self):
        if self.game:
            self.terminal = bytes(self.game.output)
        for callback in tuple(self.listeners): callback(self.terminal)

    def start_session(self):
        with self.lock:
            if self.state not in {'new','saved'}: raise Conflict('Not ready for a new session')
            index = self.session + 1
            destination = self.path/f'snapshot-{index}'
            if self.state == 'new':
                if self.work.exists(): shutil.rmtree(self.work)
                clean_directory(self.work)
            if not destination.exists(): snapshot(self.work, destination)
            self.journal.append('session_start', session=index)
            self.session = index; self.elapsed = 0; self.started = time.monotonic()
            self.pause_requested.clear()
            self.game = Game(self.work, session_seed(self.info['seed'], index), on_consumed=self.record_key)
            if self.game.frame: self.checkpoint(self.game.frame)
            self.frame = self.game.frame; self.state = 'running'; self.outcome = None
            if self.game.exited: self.finish_session()
            self.publish()
            self.last_activity = time.time()

    def record_key(self, boundary, key):
        self.journal.append('key', session=self.session, boundary=boundary, key=key, elapsed=self.clock())

    def keys(self, keys, session, boundary):
        with self.lock:
            if self.state != 'running' or not self.frame or session != self.session or boundary != self.frame.boundary:
                raise Conflict('Stale input position or session not accepting input')
            if not isinstance(keys, str) or not keys or len(keys)>4096 or any(not 1<=ord(c)<=255 for c in keys):
                raise ValueError('keys must contain 1–4096 non-NUL byte characters')
            consumed = 0
            for char in keys:
                if self.state != 'running': break
                if self.pause_requested.is_set():
                    self.pause(self.pause_reason); break
                if len(self.journal.events) >= self.info['limits'].get('events', 1000000):
                    self.pause('event_limit'); break
                if self.clock() >= self.info['limits'].get('duration', 86400):
                    self.pause('duration_limit'); break
                before = len(self.game.consumed)
                self.advancing = True
                try:
                    self.game.key(ord(char))
                except (RuntimeError, BrokenPipeError):
                    if not self.pause_requested.is_set(): raise
                consumed += len(self.game.consumed)-before
                if self.pause_requested.is_set():
                    # The kill may land after acknowledgment but before the next
                    # boundary. Reconcile only durable consumed keys before pausing.
                    elapsed = self.clock()
                    self.game.close(); self.game = None
                    self.elapsed = elapsed; self.started = None
                    self.rebuild()
                    self.advancing = False
                    if self.state == 'running': self.pause(self.pause_reason)
                    break
                next_frame = self.game.frame
                if next_frame: self.checkpoint(next_frame)
                else:
                    self.checkpoint()
                self.frame = next_frame
                self.terminal = bytes(self.game.output)
                if self.game.exited: self.finish_session()
                self.advancing = False
                self.publish()
            self.last_activity = time.time()
            return consumed, len(keys)-consumed

    def finish_session(self):
        outcome, result = exit_result(self.work, self.game.exit_code)
        elapsed = self.clock()
        self.journal.append('session_end', session=self.session, outcome=outcome, result=result,
                            elapsed=elapsed, wall_time=time.time())
        self.elapsed = elapsed; self.started = None
        self.outcome = outcome; self.result = result
        self.state = 'saved' if outcome == 'saved' else 'finished'
        self.game.close(); self.game = None; self.frame = None

    def rebuild(self):
        if self.info['build_identity'] != build_identity():
            raise RuntimeError('Cannot recover an attempt under a different build')
        if self.work.exists(): shutil.rmtree(self.work)
        source = self.path/f'snapshot-{self.session}'
        shutil.copytree(source, self.work)
        events = [e for e in self.journal.events if e.get('session') == self.session]
        keys = [e['key'] for e in events if e['type']=='key']
        checkpoints = {e['boundary']: e['hash'] for e in events if e['type']=='checkpoint'}
        self.game = replay(self.work, session_seed(self.info['seed'], self.session), keys, checkpoints)
        self.frame = self.game.frame
        self.game.on_consumed = self.record_key
        if self.game.exited:
            self.frame = self.game.last_frame
            self.checkpoint()
            self.frame = None
            self.finish_session()
            return
        self.state = 'running'

    def pause(self, reason='owner'):
        self.pause_reason = reason
        self.pause_requested.set()
        game = self.game
        if self.advancing and game is not None and game.process.poll() is None:
            try: game.process.kill()
            except ProcessLookupError: pass
        with self.lock:
            if self.state == 'paused': return
            if self.state != 'running': raise Conflict('Session is not running')
            elapsed = self.clock()
            if self.game: self.game.close(); self.game = None
            self.elapsed = elapsed; self.started = None
            self.journal.append('pause', session=self.session, reason=reason,
                                boundary=self.frame.boundary if self.frame else None,
                                elapsed=elapsed, wall_time=time.time())
            self.checkpoint(); self.state = 'paused'; self.last_activity = time.time()

    def resume(self):
        with self.lock:
            if self.state == 'saved':
                self.start_session(); return
            if self.state != 'paused': raise Conflict('Session is not resumable')
            self.pause_requested.clear()
            self.rebuild()
            if self.state == 'running':
                self.journal.append('resume', session=self.session)
                self.started = time.monotonic()
            self.last_activity = time.time()

    def recover(self):
        with self.lock:
            if self.state != 'finished' and self.info['build_identity'] != build_identity():
                raise RuntimeError('Cannot recover an attempt under a different build')
            if self.state == 'new': self.start_session(); self.pause('crash'); return
            if self.state in {'running','paused'}:
                was_running = self.state == 'running'
                self.rebuild()
                if self.state == 'running':
                    if was_running: self.pause('crash')
                    else:
                        self.game.close(); self.game = None; self.state = 'paused'
            # A saved working directory is replaceable; replay it from the immutable snapshot.
            elif self.state == 'saved':
                self._recover_saved()

    def _recover_saved(self):
        if self.work.exists(): shutil.rmtree(self.work)
        shutil.copytree(self.path/f'snapshot-{self.session}', self.work)
        keys = [e['key'] for e in self.journal.events if e.get('session')==self.session and e['type']=='key']
        with replay(self.work, session_seed(self.info['seed'], self.session), keys) as game:
            if not game.exited or exit_result(self.work, game.exit_code)[0] != 'saved':
                raise RuntimeError('Saved session failed recovery')

    def expire(self):
        with self.lock:
            if self.state not in {'paused','saved'}: return
            self.journal.append('abandoned', session=self.session)
            self.state = 'finished'; self.outcome = 'abandoned'; self.result = None

    def report_usage(self, usage):
        with self.lock:
            if (self.path/'package').exists(): raise Conflict('Package already finalized')
            if not isinstance(usage, dict) or set(usage)-{'input_tokens','output_tokens','cost'}:
                raise ValueError('Unknown usage fields')
            for key, value in usage.items():
                if value is not None and (type(value) not in (int,float) or value < 0 or not __import__('math').isfinite(value)):
                    raise ValueError('Usage values must be finite nonnegative numbers or null')
            for key in ('input_tokens','output_tokens'):
                if usage.get(key) is not None and type(usage[key]) is not int:
                    raise ValueError('Token counts must be integers or null')
            self.journal.append('usage', usage=usage); self.usage = usage

    def audit(self, method, path, role, disposition):
        with self.lock:
            fields = dict(method=method, path=path, role=role, disposition=disposition)
            if (self.path/'package').exists():
                Journal(self.path/'audit.jsonl').append('request', **fields)
            else: self.journal.append('request', **fields)

    def status(self):
        return {'id': self.id, 'session': self.session, 'boundary': self.frame.boundary if self.frame else None,
                'state': 'advancing' if self.state == 'running' and self.advancing else self.state,
                'accepting_input': self.state == 'running' and not self.advancing,
                'outcome': self.outcome, 'result': self.result,
                'key_count': sum(e['type']=='key' for e in self.journal.events), 'usage': self.usage}

    def screen(self, consumed=0, discarded=0):
        status = self.status() | {'consumed': consumed, 'discarded': discarded,
                                'cursor': self.frame.cursor if self.frame else None}
        return (self.frame.screen+'\n' if self.frame else '') + json.dumps(status, separators=(',', ':'))+'\n'

    def package(self, destination=None):
        with self.lock:
            if self.state != 'finished': raise Conflict('Attempt is not finished')
            destination = Path(destination) if destination is not None else self.path/'package'
            if destination.exists(): return destination
            data = self.journal.path.read_bytes()
            ends = [e for e in self.journal.events if e['type']=='session_end']
            durations = [e['elapsed'] for e in ends]
            if self.outcome == 'abandoned' and self.state != 'saved' and len(ends)<self.session:
                durations.append(self.elapsed)
            manifest = {'schema_version': 1, 'attempt_id': self.id, 'server_instance': self.info['instance'],
                        'player_metadata': self.info['metadata'], 'player_md_digest': self.info['player_digest'],
                        'image_digest': self.info['image_digest'], 'build_identity': self.info['build_identity'],
                        'seed': self.info['seed'], 'fixed_game_date': FIXED_DATE,
                        'character': self.result['character'] if self.result else self.character,
                        'outcome': self.outcome, 'result': self.result, 'session_durations': durations,
                        'total_duration': sum(durations), 'key_count': self.status()['key_count'],
                        'usage': self.usage, 'event_log_digest': hashlib.sha256(data).hexdigest()}
            temporary = destination.with_name('package-'+uuid.uuid4().hex+'.tmp'); temporary.mkdir()
            atomic_json(temporary/'attempt.json', manifest)
            atomic_bytes(temporary/'events.jsonl.gz', gzip.compress(data, mtime=0))
            os.rename(temporary, destination); sync_dir(destination.parent)
            return destination
