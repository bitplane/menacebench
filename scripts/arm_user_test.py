#!/usr/bin/env python3
"""Build and compare ARM replay using workspace-only PRoot/QEMU on Debian/Ubuntu.

This diagnostic does not install host packages or register binfmt handlers.
The normal cross-platform target remains preferable on an ARM-capable runtime.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT/'build/arm-tools'


def run(*command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def main(package, replay_only):
    package = package.resolve()
    if not (package/'attempt.json').is_file():
        raise SystemExit('PACKAGE must contain attempt.json and events.jsonl.gz')
    WORK.mkdir(parents=True, exist_ok=True)
    tools = WORK/'tools'
    proot = tools/'usr/bin/proot'
    qemu = tools/'usr/bin/qemu-aarch64'
    if not proot.exists() or not qemu.exists():
        run('apt-get', 'download', 'proot', 'qemu-user', 'libtalloc2', cwd=WORK)
        for archive in sorted(WORK.glob('*.deb')):
            run('dpkg-deb', '-x', str(archive), str(tools))
    dockerfile = (ROOT/'Dockerfile').read_text()
    base = re.search(r'^FROM (\S+)', dockerfile, re.M)[1]
    rootfs = WORK/'rootfs'
    marker = WORK/'base-image.txt'
    engine = os.environ.get('NETHACK_CONTAINER_ENGINE', 'podman')
    if not rootfs.exists():
        name = 'nethack-arm-test-'+uuid.uuid4().hex
        run(engine, 'create', '--platform', 'linux/arm64', '--name', name, base)
        try:
            run(engine, 'export', '--output', str(WORK/'rootfs.tar'), name)
        finally:
            run(engine, 'rm', name)
        rootfs.mkdir()
        run('tar', '--no-same-owner', '-xf', str(WORK/'rootfs.tar'), '-C', str(rootfs))
        marker.write_text(base+'\n')
    elif not marker.exists() or marker.read_text().strip() != base:
        raise SystemExit('Existing ARM rootfs has no matching base-image marker; use a fresh build/arm-tools directory')
    destination = rootfs/'opt/nethack-run'
    destination.mkdir(parents=True, exist_ok=True)
    sources = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    for name in filter(None, sources):
        source = ROOT/name
        target = destination/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    # Reuse the pinned apt setup from Dockerfile rather than maintaining a
    # second set of repository dates and compiler dependencies.
    apt_setup = re.search(r'RUN (printf .*?)(?=\nRUN )', dockerfile, re.S)[1]
    steps = [
        'set -eu',
        'export PATH=/usr/local/bin:/usr/bin:/bin',
        'export DEBIAN_FRONTEND=noninteractive',
        'cd /opt/nethack-run',
        'python3 -c "import platform; assert platform.machine() == \'aarch64\'; print(\'ARM runtime confirmed: aarch64\')"',
    ]
    if not replay_only:
        steps += [apt_setup, 'python3 scripts/setup.py',
                  'python3 -m venv .venv',
                  '.venv/bin/pip install --require-hashes -r requirements.lock']
    steps += ['python3 scripts/verify_build.py',
              './nethack-run replay /package --diagnostic-cross-build']
    script = WORK/'user-test.sh'
    script.write_text('\n'.join(steps)+'\n')
    env = dict(os.environ, PROOT_NO_SECCOMP='1',
               LD_LIBRARY_PATH=str(tools/'usr/lib/x86_64-linux-gnu'))
    run(str(proot), '-0', '-r', str(rootfs), '-q', str(qemu),
        '-b', '/dev', '-b', '/proc', '-b', '/etc/resolv.conf',
        '-b', str(script)+':/user-test.sh', '-b', str(package)+':/package',
        '-w', '/', '/bin/sh', '/user-test.sh', env=env)
    manifest = json.loads((package/'attempt.json').read_text())
    arm_build = json.loads((destination/'build/build.json').read_text())
    evidence = {'base_image': base, 'architecture': 'aarch64',
                'source_commit': arm_build['source_commit'],
                'arm_executable_digest': arm_build['executable_digest'],
                'event_log_digest': manifest['event_log_digest'],
                'qemu_sha256': hashlib.sha256(qemu.read_bytes()).hexdigest(),
                'proot_sha256': hashlib.sha256(proot.read_bytes()).hexdigest(),
                'package': str(package), 'reproduced': True,
                'official_verification': False}
    (WORK/('comparison-'+str(uuid.UUID(manifest['attempt_id']))+'.json')).write_text(json.dumps(evidence, indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', type=Path)
    parser.add_argument('--replay-only', action='store_true', help='Reuse an existing ARM build after validating its inputs')
    args = parser.parse_args()
    main(args.package, args.replay_only)
