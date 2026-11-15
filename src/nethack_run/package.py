"""Run package validation and deterministic replay."""
from __future__ import annotations
import gzip
import hashlib
import json
import math
import os
import uuid
from pathlib import Path
import tempfile
from .engine import Game, clean_directory, session_seed, ROOT, FIXED_DATE


def result(directory):
    lines = (directory/'xlogfile').read_text().splitlines()
    if not lines:
        return None
    raw = dict(item.split('=', 1) for item in lines[-1].split('\t'))
    death = raw['death']
    return {'score': int(raw['points']), 'turns': int(raw['turns']),
            'maximum_depth': int(raw['maxlvl']), 'ascended': death.startswith('ascended'),
            'cause_of_death': death, 'conducts': raw.get('conductX', '').split(','),
            'character': {k: raw[k] for k in ('name','role','race','gender','align')}}


def exit_result(directory, code):
    if code != 0:
        raise RuntimeError(f'Game exited unsuccessfully: {code}')
    record = result(directory)
    if record:
        outcome = 'ascended' if record['ascended'] else ('quit' if record['cause_of_death'].startswith(('quit','escaped')) else 'death')
        return outcome, record
    if any((directory/'save').iterdir()):
        return 'saved', None
    # Quitting during character selection has no xlog record.
    return 'quit', None


def build_identity():
    path = ROOT/'build/build.json'
    if not path.exists():
        raise RuntimeError('Run make setup before starting the harness')
    data = path.read_bytes()
    return 'native-sha256:' + hashlib.sha256(data).hexdigest()


def load_package(path):
    path = Path(path)
    for name in ('attempt.json','events.jsonl.gz'):
        if (path/name).is_symlink() or not (path/name).is_file(): raise ValueError('Package members must be regular files')
    if (path/'events.jsonl.gz').stat().st_size > 64*1024*1024: raise ValueError('Oversized compressed log')
    if (path/'attempt.json').stat().st_size > 1024*1024:
        raise ValueError('Oversized manifest')
    manifest = json.loads((path/'attempt.json').read_bytes())
    if not isinstance(manifest, dict) or manifest.get('schema_version') != 1:
        raise ValueError('Unsupported package schema')
    uuid.UUID(manifest['attempt_id'])
    if manifest['outcome'] not in {'quit','death','ascended','abandoned'}:
        raise ValueError('Invalid completed attempt outcome')
    if not isinstance(manifest['seed'], str) or len(manifest['seed']) != 64:
        raise ValueError('Invalid attempt seed')
    int(manifest['seed'], 16)
    with gzip.open(path/'events.jsonl.gz', 'rb') as file:
        data = file.read(64*1024*1024 + 1)
    if len(data) > 64*1024*1024:
        raise ValueError('Oversized event log')
    if hashlib.sha256(data).hexdigest() != manifest['event_log_digest']:
        raise ValueError('Event log digest mismatch')
    events = [json.loads(line) for line in data.splitlines()]
    for i, event in enumerate(events, 1):
        if event.get('seq') != i:
            raise ValueError('Nonsequential event log')
    return manifest, events


def verify(path, export=None, diagnostic_cross_build=False):
    manifest, events = load_package(path)
    if not diagnostic_cross_build and manifest.get('image_digest') is not None:
        if manifest['image_digest'] != os.environ.get('NETHACK_IMAGE_DIGEST'):
            raise ValueError('Package must be verified in its recorded image')
    if not diagnostic_cross_build and manifest['build_identity'] != build_identity():
        raise ValueError('Package requires a different build; run in its recorded image')
    game = None
    count = 0
    sessions = 0
    final_result = None
    last_exit = None
    character = None
    usage = None
    durations = []
    lifecycle = "new"
    checkpoint_seen = False
    created = None
    cast = []
    elapsed = 0.0
    session_time = 0.0
    with tempfile.TemporaryDirectory(prefix='nethack-replay-') as tmp:
        directory = Path(tmp)/'game'; clean_directory(directory)
        try:
            for event in events:
                kind = event['type']
                if kind == 'created':
                    if created is not None or sessions: raise ValueError('Invalid creation record')
                    created = event
                elif kind == 'session_start':
                    if lifecycle not in {'new','saved'}: raise ValueError('Invalid session transition')
                    lifecycle = 'running'; checkpoint_seen = False
                    if game and not game.exited:
                        raise ValueError('Session started before prior session exited')
                    if game: game.close()
                    sessions += 1
                    if event['session'] != sessions:
                        raise ValueError('Nonsequential session index')
                    elapsed += session_time
                    session_time = 0
                    game = Game(directory, session_seed(manifest['seed'], sessions))
                    if export: cast.append([elapsed, 'o', bytes(game.output).decode('latin1')])
                elif kind == 'key':
                    if not game or game.exited or event['boundary'] != game.boundary or event['session'] != sessions:
                        raise ValueError('Key at an invalid input position')
                    if lifecycle != 'running' or not checkpoint_seen: raise ValueError('Key without running boundary checkpoint')
                    if type(event['key']) is not int or not 1 <= event['key'] <= 255: raise ValueError('Invalid key byte')
                    if not math.isfinite(event['elapsed']) or event['elapsed'] < session_time: raise ValueError('Invalid key time')
                    game.key(event['key']); count += 1
                    checkpoint_seen = False
                    session_time = event['elapsed']
                    if export: cast.append([elapsed+session_time, 'o', bytes(game.output).decode('latin1')])
                elif kind == 'checkpoint':
                    if event['session'] != sessions: raise ValueError('Checkpoint in wrong session')
                    # A pre-exit checkpoint is the sole case where the game
                    # has no current input frame. Never substitute a previously
                    # checked frame for a different, still-running boundary.
                    frame = game.last_frame if game and game.exited else (game.frame if game else None)
                    if not frame or event['boundary'] != frame.boundary:
                        raise ValueError('Checkpoint names an unavailable boundary')
                    if frame.digest != event['hash']:
                        raise ValueError(f"Divergence at session {sessions} boundary {frame.boundary}: expected {event['hash']}\n{frame.screen}")
                    if frame.character != event.get('character'):
                        raise ValueError('Character checkpoint mismatch')
                    character = frame.character or character
                    checkpoint_seen = True
                elif kind == 'session_end':
                    if event['session'] != sessions or lifecycle not in {'running','paused'}: raise ValueError('Invalid session end')
                    if not game or not game.exited:
                        raise ValueError('Expected game exit')
                    actual, final_result = exit_result(directory, game.exit_code)
                    if actual != event['outcome'] or final_result != event['result']:
                        raise ValueError('Session exit/result mismatch')
                    last_exit = actual
                    if not math.isfinite(event['elapsed']) or event['elapsed'] < session_time: raise ValueError('Invalid session duration')
                    durations.append(event['elapsed'])
                    lifecycle = 'saved' if actual == 'saved' else 'finished'
                elif kind == 'pause':
                    if lifecycle not in {'running','paused'} or not game or game.exited: raise ValueError('Invalid pause')
                    if event['session'] != sessions or event['boundary'] != game.boundary: raise ValueError('Invalid pause position')
                    if not math.isfinite(event['elapsed']) or event['elapsed'] < session_time: raise ValueError('Invalid pause time')
                    session_time = event['elapsed']; lifecycle = 'paused'
                elif kind == 'resume':
                    if lifecycle != 'paused' or event['session'] != sessions: raise ValueError('Invalid resume')
                    lifecycle = 'running'
                elif kind == 'usage': usage = event['usage']
                elif kind == 'abandoned':
                    if lifecycle not in {'paused','saved'}: raise ValueError('Invalid abandonment')
                    if lifecycle == 'paused': durations.append(session_time)
                    lifecycle = 'abandoned'
                elif kind == 'request': pass
                else:
                    raise ValueError(f'Unknown event type: {kind}')
            if created is None: raise ValueError('Missing creation record')
            if manifest.get('harness_version') != created.get('harness_version'):
                raise ValueError('Manifest harness_version mismatch')
            for field, event_field in [('attempt_id','attempt_id'),('player_metadata','metadata'),('server_instance','instance'),
                                       ('player_md_digest','player_md_digest'),('build_identity','build_identity'),
                                       ('image_digest','image_digest'),('fixed_game_date','fixed_game_date')]:
                if manifest[field] != created[event_field]: raise ValueError(f'Manifest {field} mismatch')
            if manifest['fixed_game_date'] != FIXED_DATE: raise ValueError('Unsupported fixed game date')
            if manifest['character'] != (final_result['character'] if final_result else character): raise ValueError('Manifest character mismatch')
            if manifest['usage'] != usage: raise ValueError('Manifest usage mismatch')
            if manifest['session_durations'] != durations or manifest['total_duration'] != sum(durations): raise ValueError('Manifest duration mismatch')
            if not checkpoint_seen: raise ValueError('Missing final checkpoint')
            if manifest['key_count'] != count or manifest['result'] != final_result:
                raise ValueError('Manifest result/count mismatch')
            if manifest['outcome'] == 'abandoned':
                if lifecycle != 'abandoned': raise ValueError('Missing abandoned event')
                if not (game and ((not game.exited and game.frame) or last_exit == 'saved')):
                    raise ValueError('Invalid abandoned ending')
            elif manifest['outcome'] != last_exit:
                raise ValueError('Manifest outcome mismatch')
        finally:
            if game: game.close()
    if export:
        # Asciicast v3 uses an initial header and relative event timestamps.
        header = {'version': 3, 'term': {'cols': 80, 'rows': 24, 'type': 'xterm'}}
        previous = 0.0
        with Path(export).open('w') as file:
            file.write(json.dumps(header)+'\n')
            for timestamp, kind, data in cast:
                file.write(json.dumps([round(max(0, timestamp-previous), 6), kind, data])+'\n')
                previous = timestamp
    return {'verified': not diagnostic_cross_build, 'reproduced': True,
            'environment': 'cross-build-diagnostic' if diagnostic_cross_build else ('pinned-image' if manifest['image_digest'] else 'native-development'),
            'sessions': sessions, 'keys': count, 'outcome': manifest['outcome']}
