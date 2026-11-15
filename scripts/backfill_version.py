"""Backfill an unversioned store while stopped, preserving replay checksums.

Usage: python scripts/backfill_version.py STATE VERSION BACKUP_DIRECTORY
Backups contain private credentials/seeds and are created with mode 0700.
"""
import fcntl
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sys
from nethack_run.storage import atomic_bytes


def version_log(data, version):
    events = [json.loads(line) for line in data.splitlines()]
    created = [event for event in events if event['type'] == 'created']
    if len(created) != 1:
        raise ValueError('Expected exactly one creation record')
    if created[0].get('harness_version') not in (None, version):
        raise ValueError('Conflicting creation version')
    created[0]['harness_version'] = version
    return b''.join((json.dumps(event, separators=(',', ':'))+'\n').encode() for event in events)


def migrate(root, version, backup):
    root, backup = Path(root), Path(backup)
    with (root/'.server.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        changes = {}
        count = 0
        for private in sorted(root.glob('*/private.json')):
            info = json.loads(private.read_bytes())
            if info.get('harness_version') is not None:
                if info['harness_version'] != version:
                    raise ValueError('Store contains a different version')
                continue
            info['harness_version'] = version
            changes[private] = json.dumps(info).encode()
            log = private.parent/'events.jsonl'
            changes[log] = version_log(log.read_bytes(), version)
            manifest_path = private.parent/'package/attempt.json'
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_bytes())
                compressed = manifest_path.parent/'events.jsonl.gz'
                original = gzip.decompress(compressed.read_bytes())
                if hashlib.sha256(original).hexdigest() != manifest['event_log_digest']:
                    raise ValueError('Existing package digest mismatch')
                data = version_log(original, version)
                manifest['harness_version'] = version
                manifest['event_log_digest'] = hashlib.sha256(data).hexdigest()
                changes[compressed] = gzip.compress(data, mtime=0)
                changes[manifest_path] = json.dumps(manifest).encode()
            count += 1
        if not changes:
            return 0
        backup.mkdir(mode=0o700, parents=False, exist_ok=False)
        # Back up every affected file before changing any of them.
        for path in changes:
            target = backup/path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(path, target)
        for path, data in changes.items():
            atomic_bytes(path, data)
        return count


if __name__ == '__main__':
    print(f'Backfilled {migrate(*sys.argv[1:])} attempts.')
