"""Rebuildable SQLite index of attempt summaries.

Attempt folders stay authoritative. Every row can be recomputed from an
attempt's private.json and events.jsonl, so deleting the database only costs
one full scan on the next start. The index lets the server keep finished
attempts (and their event histories) out of memory.
"""
import json
from pathlib import Path
import sqlite3
import threading

SCHEMA_VERSION = 1


def fingerprint(path: Path):
    """Detect any durable change to an attempt since its row was written."""
    parts = []
    for name in ('private.json', 'events.jsonl'):
        stat = (path/name).stat()
        parts.append(f'{stat.st_size}:{stat.st_mtime_ns}')
    return '/'.join(parts)


class Index:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.generation = 0
        self.cached = (None, [])
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        if self.db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
            # Rows are derived data; an incompatible index is simply rebuilt.
            self.db.execute('DROP TABLE IF EXISTS attempts')
            self.db.execute('''CREATE TABLE attempts (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
                owner_hash TEXT NOT NULL, player_hash TEXT NOT NULL,
                last_activity REAL NOT NULL, summary TEXT NOT NULL)''')
            self.db.execute('CREATE INDEX attempts_owner_hash ON attempts(owner_hash)')
            self.db.execute('CREATE INDEX attempts_player_hash ON attempts(player_hash)')
            self.db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

    def close(self):
        with self.lock: self.db.close()

    def write(self, identifier, fingerprint, owner_hash, player_hash, summary):
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO attempts VALUES (?,?,?,?,?,?,?)',
                            (identifier, fingerprint, summary['state'], owner_hash, player_hash,
                             summary['last_activity'], json.dumps(summary, separators=(',', ':'))))
            self.generation += 1

    def put(self, attempt):
        # Fingerprint before summarizing, under the attempt lock, so a row can
        # only be older than its fingerprint claims and is then rebuilt.
        with attempt.lock:
            if attempt.deleted: return
            self.write(attempt.id, fingerprint(attempt.path), attempt.info['owner_hash'],
                       attempt.info['player_hash'], attempt.summary())

    def remove(self, identifier):
        with self.lock:
            self.db.execute('DELETE FROM attempts WHERE id=?', (identifier,))
            self.generation += 1

    def row(self, identifier):
        with self.lock:
            return self.db.execute('SELECT state, fingerprint FROM attempts WHERE id=?', (identifier,)).fetchone()

    def ids(self):
        with self.lock:
            return {row[0] for row in self.db.execute('SELECT id FROM attempts')}

    def token(self, digest):
        """Map a bearer-token digest to (attempt id, role) without loading attempts."""
        with self.lock:
            for role in ('owner', 'player'):
                row = self.db.execute(f'SELECT id FROM attempts WHERE {role}_hash=?', (digest,)).fetchone()
                if row: return row[0], role
        return None

    def summaries(self):
        """All rows, newest activity first. Parsed rows are reused until the next write."""
        with self.lock:
            if self.cached[0] != self.generation:
                rows = self.db.execute('SELECT summary FROM attempts ORDER BY last_activity DESC').fetchall()
                self.cached = (self.generation, [json.loads(row[0]) for row in rows])
            return self.cached[1]
