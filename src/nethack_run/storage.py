"""Durable records and atomic publication on a local filesystem."""
from collections import Counter
import json
import os
from pathlib import Path
import shutil
import uuid


def sync_dir(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_bytes(path: Path, data: bytes):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value):
    atomic_bytes(path, json.dumps(value, sort_keys=True, separators=(',', ':')).encode() + b'\n')


def snapshot(source: Path, destination: Path):
    temporary = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.tmp')
    shutil.copytree(source, temporary)
    try:
        for path in temporary.rglob('*'):
            if path.is_file():
                with path.open('rb') as file:
                    os.fsync(file.fileno())
        for path in sorted(temporary.rglob('*'), reverse=True):
            if path.is_dir():
                sync_dir(path)
        sync_dir(temporary)
        os.rename(temporary, destination)
        sync_dir(destination.parent)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


class Journal:
    def __init__(self, path: Path):
        self.path = path
        self.events = []
        # Per-type totals keep status polling O(1) on long histories.
        self.counts = Counter()
        if not path.exists():
            atomic_bytes(path, b'')
        with path.open('rb+') as file:
            offset = 0
            for line in file:
                if not line.endswith(b'\n'):
                    file.truncate(offset)
                    file.flush()
                    os.fsync(file.fileno())
                    break
                event = json.loads(line)
                if event.get('seq') != len(self.events) + 1:
                    raise ValueError('Nonsequential journal')
                self.events.append(event)
                self.counts[event.get('type')] += 1
                offset += len(line)

    def append(self, kind, **fields):
        event = dict(fields, seq=len(self.events)+1, type=kind)
        data = json.dumps(event, sort_keys=True, separators=(',', ':')).encode() + b'\n'
        with self.path.open('ab') as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        self.events.append(event)
        self.counts[kind] += 1
        return event
